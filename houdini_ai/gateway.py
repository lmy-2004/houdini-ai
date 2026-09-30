# -*- coding: utf-8 -*-
"""Anthropic Messages API 客户端。

只用标准库 urllib，不引第三方依赖——Houdini 自带的 Python 没装 requests，
装依赖又要动 Houdini 的环境，不值得。HTTP 调用发生在工作线程里，
urllib 在等网络时会释放 GIL，不会卡住 Houdini 的界面。

实测结论（本机 api.deepseek.com/anthropic）：
  * 工具调用返回标准 tool_use 块 + stop_reason="tool_use"
  * 回灌 tool_result 时，assistant 的历史里带不带 thinking 块都能跑通
    -> 默认丢掉 thinking，省 token 也不用管那个假 signature
"""

import json
import urllib.error
import urllib.request

ANTHROPIC_VERSION = "2023-06-01"

# 网关明确说"我不认识这个请求"时返回的状态码。撞上这些就说明它没实现流式
# （而不是网络抖了一下），记下来别再试——否则**每一轮都要白跑一次请求**。
STREAM_REJECT_CODES = (400, 404, 405, 415, 422)

# --------------------------------------------------------------------------
# max_tokens
#
# ★★ 2026-09-29 用户定：「直接不要 max tokens」。
# 理由是真的：**思考和答案共享这一个上限**，而「思考深度」这套设计要控制的
# 恰恰是思考预算。留着它等于给思考加了一道天花板——实测「深入」档承诺
# budget_tokens=16000 > 旧上限 8000，**该档的深度物理上够不着**。
# 另一次实测：把上限压到 8000 时，一轮 11 步里有 3 步撞顶，那 3 步吃掉了
# 65% 的时间和 67% 的输出 token；而**撞顶时工具参数会变成空字典**，
# 于是模型白干一步。
#
# 实测本机网关（api.deepseek.com/anthropic）**完全不传这个字段也接受**，
# 它自己的天花板是 65536。所以默认策略是：**一个字段都不发**。
#
# 但**官方 Anthropic 的 Messages API 里 max_tokens 是必填**。所以下面做两道
# 兜底，为的是「哪天换回官方端点」不会变成静默失效：
#   * 域名是官方 → 直接补一个大兜底（这不是"上限"，对本工作负载等于没有）；
#   * 撞到「缺 max_tokens」的 400 → 补上重试一次，并记住（别每轮白撞）。
# --------------------------------------------------------------------------

# 官方端点。命中就主动补 max_tokens。
OFFICIAL_HOSTS = ("api.anthropic.com",)

# 兜底值。要**足够大到不再是约束**（旧值 8000 的 8 倍），
# 又要远小于官方各模型的输出上限，免得被当成非法值拒掉。
FALLBACK_MAX_TOKENS = 64000


class GatewayError(Exception):
    """网关层面的失败：连不上、鉴权不过、返回体看不懂。"""

    def __init__(self, message, status=None, body=None):
        super(GatewayError, self).__init__(message)
        self.status = status
        self.body = body


class Gateway(object):
    def __init__(self, cfg):
        self.cfg = cfg
        self.base_url = (cfg.get("base_url") or "").rstrip("/")
        self.api_key = cfg.get("api_key") or ""
        self.model = cfg.get("model")
        self.timeout = cfg.get("timeout", 300)
        # 留空（None）= **不传这个字段**。见文件顶部那段说明。
        self.max_tokens = cfg.get("max_tokens")
        self.fallback_max_tokens = int(cfg.get("max_tokens_fallback")
                                       or FALLBACK_MAX_TOKENS)
        # 这个网关收不收「没有 max_tokens」的请求。官方域名一开始就置 True；
        # 别的网关撞到 400 之后也会置 True 并记住。
        self.max_tokens_required = _is_official(self.base_url)
        # 网关认不认流式。撞上 STREAM_REJECT_CODES 就置 False，之后这一轮里
        # 直接走一次性请求——不用每轮都去撞一次（那是白花的往返）。
        self.stream_ok = True

    def _max_tokens_for(self, explicit=None):
        """这次请求带不带 max_tokens、带多少。**返回 None = 一个字段都不发**。"""
        if explicit:
            return int(explicit)
        if self.max_tokens:
            return int(self.max_tokens)
        if self.max_tokens_required:
            return self.fallback_max_tokens
        return None

    def endpoint(self):
        # 用户可能填 https://host 或 https://host/anthropic，两种都补成 /v1/messages
        if self.base_url.endswith("/v1"):
            return self.base_url + "/messages"
        return self.base_url + "/v1/messages"

    def ready(self):
        """能不能发请求。缺密钥时给一句人话，别等到发出去才 401。"""
        if not self.base_url:
            return False, "没配置网关地址（base_url）"
        if not self.api_key:
            return False, "没配置密钥（api_key）"
        if not self.model:
            return False, "没配置模型名（model）"
        return True, ""

    def messages(self, system, messages, tools=None, max_tokens=None,
                 thinking=None, on_delta=None):
        """发一轮请求，返回原始响应 dict。

        `thinking` 控制模型的思考预算，比如
        `{"type": "enabled", "budget_tokens": 4096}` 或 `{"type": "disabled"}`。
        实测这个网关认这个参数：disabled 时完全不产生 thinking 块，
        enabled 时按预算产出。传 None 就用网关的默认行为。

        **`on_delta(kind, piece)` 给了就走流式（SSE）**：每收到一小段就回调一次，
        kind 是 `"thinking"` 或 `"text"`。用途是让界面能实时显示"它正在想什么"——
        用户原话是「思考中我完全看不出来他在干什么……我都不知道他有没有卡住」。

        ★ **流式返回的 dict 形状和非流式完全一样**（`content` 是一串块，
        tool_use 的 `input` 已经拼好并解析过）。这一点是刻意的：上面
        `strip_thinking` / 取 tool_use 那套逻辑对两种模式都得成立，
        调用方不该分情况。

        失败一律抛 GatewayError，调用方负责把它变成界面上的一句话。
        """
        ok, why = self.ready()
        if not ok:
            raise GatewayError(why)

        # 这次带不带 max_tokens。None = 一个字段都不发（默认就是这个）。
        sending = self._max_tokens_for(max_tokens)

        while True:
            payload = {"model": self.model, "messages": messages}
            if sending is not None:
                payload["max_tokens"] = sending
            if system:
                payload["system"] = system
            if tools:
                payload["tools"] = tools
            if thinking:
                payload["thinking"] = thinking
            # 已经知道这个网关不认流式了，就别再问一次。
            streaming = on_delta is not None and self.stream_ok
            if streaming:
                payload["stream"] = True

            try:
                return self._send(payload, streaming, on_delta)
            except GatewayError as exc:
                # 网关说"你这个请求缺 max_tokens"——补上再试**一次**，并且记住，
                # 之后每轮都带上。只认这一种 400：别的 400 是请求本身有问题，
                # 重试只会白花两倍的钱。
                if (sending is None and exc.status == 400
                        and "max_tokens" in (exc.body or "")):
                    self.max_tokens_required = True
                    sending = self.fallback_max_tokens
                    continue
                if streaming and exc.status in STREAM_REJECT_CODES:
                    self.stream_ok = False
                raise

    def _send(self, payload, streaming, on_delta):
        """把 payload 发出去。流式走 `_stream`，否则一次性读回整个 JSON。

        ★ 单独拆出来是为了 `max_tokens` 的兜底重试：重试要**换一份 payload**
        重发，所以"拼 payload"和"发出去"必须分开。
        """
        data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        req = urllib.request.Request(
            self.endpoint(),
            data=data,
            headers={
                "content-type": "application/json",
                "x-api-key": self.api_key,
                "anthropic-version": ANTHROPIC_VERSION,
                # 有些网关只认 Authorization，两个都带上，互不冲突。
                "authorization": "Bearer %s" % self.api_key,
            },
            method="POST",
        )
        # 只写 `if streaming`：不流式的话**默默走一次性的那条路**，不报错。
        # 调用方给了 on_delta 却拿不到回调，是"这个网关不支持"，不是失败
        # ——结果照样是对的，只是没有实时的过程可看。
        if streaming:
            req.add_header("accept", "text/event-stream")
            return self._stream(req, on_delta)

        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as resp:
                body = resp.read().decode("utf-8", "replace")
        except urllib.error.HTTPError as exc:
            raw = ""
            try:
                raw = exc.read().decode("utf-8", "replace")
            except Exception:
                pass
            raise GatewayError(
                "网关返回 HTTP %s：%s" % (exc.code, _short(raw) or exc.reason),
                status=exc.code,
                body=raw,
            )
        except urllib.error.URLError as exc:
            raise GatewayError("连不上网关：%s" % exc.reason)
        except Exception as exc:  # 超时、SSL 之类
            raise GatewayError("请求失败：%s" % exc)

        try:
            # `strict=False` 同理：模型写的工具参数里可能夹着裸控制字符
            # （长 VEX snippet 的换行），那是合法 JSON 的本意。
            return json.loads(body, strict=False)
        except ValueError:
            raise GatewayError("网关返回的不是 JSON：%s" % _short(body))

    def _stream(self, req, on_delta):
        """读 SSE，把分片拼回一个跟非流式一样的响应。

        要拼的东西有四类：
          * `text_delta` / `thinking_delta` —— 逐字追加到对应块上；
          * `input_json_delta` —— 工具参数**也是分片送来的**，得先攒着，
            到 `content_block_stop` 才 `json.loads`（这是最容易漏的一处：
            不拼的话 tool_use 的 input 永远是空的，模型看起来像"没给参数"）；
          * `signature_delta` —— 思考块的签名（我们本来就丢掉 thinking，不影响）。
        """
        blocks = {}
        order = []
        stop_reason = None
        usage = {}
        # 拼不出合法 JSON 的工具块（见下面 content_block_stop 那段）。
        bad = []

        try:
            resp = urllib.request.urlopen(req, timeout=self.timeout)
        except urllib.error.HTTPError as exc:
            raw = ""
            try:
                raw = exc.read().decode("utf-8", "replace")
            except Exception:
                pass
            raise GatewayError(
                "网关返回 HTTP %s：%s" % (exc.code, _short(raw) or exc.reason),
                status=exc.code,
                body=raw,
            )
        except urllib.error.URLError as exc:
            raise GatewayError("连不上网关：%s" % exc.reason)
        except Exception as exc:
            raise GatewayError("请求失败：%s" % exc)

        try:
            for raw in resp:
                line = raw.decode("utf-8", "replace").strip()
                if not line or line.startswith(":") or not line.startswith("data:"):
                    continue
                chunk = line[5:].strip()
                if chunk == "[DONE]":
                    break
                try:
                    event = json.loads(chunk)
                except ValueError:
                    continue

                kind = event.get("type")
                index = event.get("index")

                if kind == "content_block_start":
                    block = dict(event.get("content_block") or {})
                    block.setdefault("type", "text")
                    if block["type"] == "tool_use":
                        block["input"] = {}
                        block["_partial"] = ""      # 参数的碎片
                    elif block["type"] == "text":
                        block["text"] = block.get("text") or ""
                    elif block["type"] == "thinking":
                        block["thinking"] = block.get("thinking") or ""
                    if index not in blocks:
                        order.append(index)
                    blocks[index] = block

                elif kind == "content_block_delta":
                    block = blocks.get(index)
                    if block is None:
                        continue
                    delta = event.get("delta") or {}
                    dtype = delta.get("type")
                    if dtype == "text_delta":
                        piece = delta.get("text") or ""
                        block["text"] = (block.get("text") or "") + piece
                        _emit(on_delta, "text", piece)
                    elif dtype == "thinking_delta":
                        piece = delta.get("thinking") or ""
                        block["thinking"] = (block.get("thinking") or "") + piece
                        _emit(on_delta, "thinking", piece)
                    elif dtype == "input_json_delta":
                        block["_partial"] = (block.get("_partial") or "") + \
                            (delta.get("partial_json") or "")
                    elif dtype == "signature_delta":
                        block["signature"] = delta.get("signature")

                elif kind == "content_block_stop":
                    block = blocks.get(index)
                    if block is not None and "_partial" in block:
                        raw_json = block.pop("_partial") or ""
                        if raw_json.strip():
                            try:
                                # `strict=False` 是**便宜的保险**：容忍字符串里的
                                # 裸控制字符（比如裸 tab，SSE 的 data 行里是允许的）。
                                # 注意：裸**换行**在 SSE 里不可能出现（换行会终止
                                # data 字段），所以那**不是**主因——主因见下面那条。
                                block["input"] = json.loads(raw_json, strict=False)
                            except ValueError as exc:
                                # ★★ **绝不在这里抛异常。** 参数拼不出来是「模型
                                # 这一步的输入坏了」，回灌给它重发就行（runner 的
                                # `bad_tool_json` 分支），**不该让整轮崩掉**。
                                # 以前这里抛 GatewayError，而它的 status 是 None，
                                # 不在"流式被拒"那几个码里 → runner 当成网关故障
                                # 直接结束整轮。用户 2026-09-29 问的「失败了为什么
                                # 不继续做下去」就是这个。
                                #
                                # ⚠️ 而且**抛在这里会掩盖真因**：最常见的是输出被
                                # 长度上限截断（参数写到一半停了），那时 `stop_reason`
                                # 会告诉你是 `max_tokens`——可一抛异常就再也走不到
                                # `message_delta` 那一步，永远只看到一句"JSON 不合法"。
                                #
                                # 原始文本要留下来：光看前 400 字查不出真因，
                                # **断点通常在尾巴上**（截断？少个括号？多个逗号？）。
                                block["input"] = {}
                                bad.append({
                                    "id": block.get("id"),
                                    "name": block.get("name"),
                                    "why": str(exc),
                                    "chars": len(raw_json),
                                    "head": raw_json[:200],
                                    "tail": raw_json[-200:],
                                })
                        else:
                            block["input"] = {}

                elif kind == "message_delta":
                    delta = event.get("delta") or {}
                    if delta.get("stop_reason"):
                        stop_reason = delta["stop_reason"]
                    if isinstance(event.get("usage"), dict):
                        usage.update(event["usage"])

                elif kind == "error":
                    err = event.get("error") or {}
                    raise GatewayError("网关在流里报错：%s"
                                       % (err.get("message") or _short(chunk)))

                elif kind == "message_stop":
                    break
        except GatewayError:
            raise
        except Exception as exc:      # 断流、超时
            raise GatewayError("读流失败（%s）：%s" % (type(exc).__name__, exc))
        finally:
            try:
                resp.close()
            except Exception:
                pass

        content = [blocks[i] for i in order if i in blocks]
        if not content:
            raise GatewayError("网关的流里一个内容块都没有")
        # ★ 形状和非流式保持一致（调用方不该分情况），多的那个键只在**真出事**
        # 时才非空。块的 `input` 已经统一成 `{}`，所以回灌给网关的历史是干净的。
        return {"content": content, "stop_reason": stop_reason, "usage": usage,
                "bad_tool_json": bad}


def _emit(callback, kind, piece):
    """把一小段内容交给回调。

    ★ **回调抛的异常一律吞掉**：它只是个"显示进度"的旁路，界面出点小毛病
    绝不能让整轮请求失败——那会把一次已经花钱、已经跑了半天的调用白白扔掉。
    """
    if not piece:
        return
    try:
        callback(kind, piece)
    except Exception:
        pass


def _is_official(base_url):
    """这个地址是不是官方 Anthropic 端点。

    只有那边 max_tokens 是必填（本机这个兼容网关不填也认）。判定放在
    域名上而不是"试一次看看"，是为了**第一发就带对**，不用白撞一次 400。
    """
    try:
        from urllib.parse import urlparse

        host = (urlparse(base_url or "").hostname or "").lower()
    except Exception:
        return False
    return host in OFFICIAL_HOSTS


def _short(text, limit=400):
    if not text:
        return ""
    text = text.strip().replace("\n", " ")
    return text if len(text) <= limit else text[:limit] + "…"


def blocks_of_type(response, block_type):
    return [b for b in (response.get("content") or []) if b.get("type") == block_type]


def text_of(response):
    """把响应里的全部 text 块拼起来。"""
    return "\n".join(b.get("text", "") for b in blocks_of_type(response, "text")).strip()


def strip_thinking(content):
    """去掉 thinking 块再回灌。

    实测带不带都能跑通，但 thinking 块又长、signature 又是网关自己编的，
    留着只是白烧 token。要留着观察思维链时把 config 的开关打开即可。
    """
    return [b for b in content if b.get("type") != "thinking"]
