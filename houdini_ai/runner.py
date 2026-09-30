# -*- coding: utf-8 -*-
"""后台干活：工作线程跑对话循环，HOM 操作回主线程执行。

**为什么不直接把整个循环丢进线程里跑完？**
Houdini 的 HOM（hou 模块）不是线程安全的，改场景必须回主线程。Houdini 给的
官方法是 `hdefereval.executeInMainThreadWithResult`。

**为什么必须区分主线程？**（这是本模块最要命的一条）
读了 `hdefereval.py` 的实现：它是往队列里塞任务，然后**阻塞等主线程的事件
循环回调来处理**。如果从主线程自己调它，主线程就一边等着自己处理、一边
阻塞着不处理——**直接死锁**。所以 `in_main` 必须识别「我已经在主线程了」，
这时直接执行。

分工：
  * HTTP 请求 → 工作线程（urllib 等网络时释放 GIL，不卡界面）
  * 读写场景、读参数真相、设状态 → 主线程
"""

import base64
import json
import threading
import time

from . import config, gateway, node as node_mod, schema, session, prompt

# --------------------------------------------------------------------------
# 输出被截断时的处置
#
# ★★ 为什么现在才接这个：以前 `max_tokens` 是 8000，一轮 11 步里有 3 步撞顶，
# 而**撞顶时工具调用的参数会变成空字典 `{}`** —— 模型白干一步，界面上还看不
# 出来。2026-09-29 起 `max_tokens` 干脆不发了（见 gateway.py 顶部），这个上限
# 从"每个回合都撞"变成"病态情况"，但它**仍然可达**（本机网关自己的天花板是
# 65536）。所以 `stop_reason` 必须被读——它是唯一能发现"有东西在砍输出"的信号。
#
# MAX_TRUNCATIONS 是**错误路径的熔断，不是步数上限**：它数的是"这一步的输出
# 不可用"（被截断 + 参数拼不出 JSON）的次数，**不是步数**。⚠️ 别把它改成步数
# 上限——用户明确禁止按步数中途干涉。正常路径下这两个计数器永远是 0。
# --------------------------------------------------------------------------

MAX_TRUNCATIONS = 3

TRUNCATED_NOTE = (
    "上一条回复被长度上限截断了，里面的工具调用**没有被执行**"
    "（它的参数很可能是空的）。请把要做的事拆小一点重新发——"
    "比如把一次 build 拆成两次，或者先把话说完、再单独调工具。"
)

BAD_JSON_NOTE = (
    "上一条回复里的工具调用**参数没拼成合法 JSON**，所以**没有被执行**。"
    "常见原因：字符串里（比如 VEX snippet）夹了没转义的换行或引号。"
    "请把这一步重发一次：参数里所有换行写成 \\n、引号写成 \\\"，"
    "或者把这段内容拆小一点。"
)


def _add_usage(record, usage):
    """把这次请求的 token 用量累加进这一轮。

    ★ **只记、只展示，绝不据此踩刹车。** 用户要的是"看得见花了多少"，
    不是"花多了就掐它"。
    """
    if not isinstance(usage, dict):
        return
    bucket = record.setdefault("usage", {})
    for key in ("input_tokens", "output_tokens",
                "cache_read_input_tokens", "cache_creation_input_tokens"):
        value = usage.get(key)
        if isinstance(value, int):
            bucket[key] = bucket.get(key, 0) + value

# --------------------------------------------------------------------------
# 什么时候可以不问了
#
# ★★ 用户 2026-09-29：「不要限制 AI，不要限制 AI！等 AI 自己结束，再去判定它
# 有没有构建完成」；同一天又推到底：「**不要再记录步数了，不要有任何的熔断
# 措施，除非它已经运行了 60 分钟以上**」。
#
# 所以一轮的结束条件只剩下这些：
#   * 它自己不再调工具了（＝它说「我干完了」）
#   * 它调了 finish
#   * 你按了停止
#   * 网关出错
#   * ★ 墙上的钟走满 `runaway_minutes`（默认 60 分钟）——**唯一的熔断**
#
# **没有步数上限，也没有任何"原地打转／重复动作"的检测。** 曾经有过一道
# REPEAT_STOP（连着 6 次一模一样的动作+结果就停），用户否掉了：它想反复看
# 同一个东西、想跑一百步，那是它的事。数步数、判打转，都是我在替它做决定。
#
# 判断做没做完是**事后**看容器（cook 过不过、有没有产出），不是事前拿数字卡。
# --------------------------------------------------------------------------

# --------------------------------------------------------------------------
# 主线程调度
# --------------------------------------------------------------------------

_MAIN_THREAD_ID = None
_MAIN_LOCK = threading.Lock()


def note_main_thread():
    """在 UI 入口（参数回调、面板按钮）里调一次，记下主线程是谁。"""
    global _MAIN_THREAD_ID
    _MAIN_THREAD_ID = threading.current_thread().ident


def on_main_thread():
    return (_MAIN_THREAD_ID is not None
            and threading.current_thread().ident == _MAIN_THREAD_ID)


def in_main(fn, *args, **kwargs):
    """在主线程上执行 fn。已经主线程就直接跑——再走一遍事件循环会死锁。"""
    if on_main_thread():
        return fn(*args, **kwargs)
    try:
        import hdefereval
    except ImportError:
        # hython / 测试环境没有 GUI 事件循环，直接执行。
        return fn(*args, **kwargs)
    return hdefereval.executeInMainThreadWithResult(fn, *args, **kwargs)


# --------------------------------------------------------------------------
# 任务管理
# --------------------------------------------------------------------------

_STATE_LOCK = threading.Lock()
_running = {}       # node_path -> {"cancel": Event, "thread": Thread}
_cancel_all = threading.Event()
_listeners = []


def add_listener(fn):
    """面板可以注册一个刷新回调。"""
    if fn not in _listeners:
        _listeners.append(fn)


def _notify():
    for fn in list(_listeners):
        try:
            fn()
        except Exception:
            pass


def busy(node_path):
    with _STATE_LOCK:
        return node_path in _running


def request_build(node):
    """「开始构建」按钮。在主线程被调用，必须立刻返回。"""
    note_main_thread()
    description = node_mod.prompt_text(node).strip()
    if not description:
        node_mod.set_status(node, "failed", "描述是空的，先写点东西",
                            error="描述为空")
        return
    submit(node, "build", description)


def request_followup(node, feedback):
    """面板里的后续轮次。"""
    note_main_thread()
    feedback = (feedback or "").strip()
    if not feedback:
        return
    submit(node, "followup", feedback)


def request_stop(node):
    note_main_thread()
    path = node.path()
    with _STATE_LOCK:
        entry = _running.get(path)
    if entry is None:
        node_mod.set_status(node, "stopped", "没有在跑的任务")
        return
    entry["cancel"].set()
    node_mod.set_status(node, "stopped", "正在停止…")


def submit(node, kind, text):
    """起一个工作线程跑这一轮。同一个节点同时只允许一轮。"""
    path = node.path()
    with _STATE_LOCK:
        if path in _running:
            node_mod.set_status(node, "working", "已经有一轮在跑了，等它结束")
            return False
        entry = {"cancel": threading.Event(), "thread": None}
        _running[path] = entry

    # 记下 sessionId。AI 可能给容器改名（路径会变），sessionId 不会变，
    # 所以出错时靠它还能找回这个节点。
    try:
        session_id = node.sessionId()
    except Exception:
        session_id = None

    thread = threading.Thread(
        target=_guarded_round,
        args=(path, session_id, kind, text, entry["cancel"]),
        name="houdini-ai-%s" % path.replace("/", "_"),
        daemon=True,
    )
    entry["thread"] = thread
    thread.start()
    return True


def _guarded_round(node_path, session_id, kind, text, cancel):
    try:
        _run_round(node_path, kind, text, cancel, session_id)
    except Exception as exc:
        import traceback

        detail = traceback.format_exc()
        try:
            in_main(_fail, session_id if session_id is not None else node_path,
                    "%s: %s" % (type(exc).__name__, exc), detail)
        except Exception:
            pass
    finally:
        with _STATE_LOCK:
            _running.pop(node_path, None)
        _notify()


def _fail(target, message, detail=""):
    node = _resolve(target)
    if node is not None:
        node_mod.set_status(node, "failed", message, error=detail[-1500:])
        _tell_user("AI 节点 %s 出错了：%s"
                   % (node.name(), _one_line(message, 80)), error=True)


def _lookup(node_path):
    import hou

    return hou.node(node_path)


def _resolve(target):
    """按 sessionId（首选）或路径找节点。改名之后路径会失效，sessionId 不会。"""
    import hou

    if isinstance(target, int):
        try:
            return hou.nodeBySessionId(target)
        except Exception:
            return None
    return hou.node(target)


# --------------------------------------------------------------------------
# 一轮的完整流程
# --------------------------------------------------------------------------

def _run_round(node_path, kind, text, cancel, session_id=None):
    cfg = config.load()
    gw = gateway.Gateway(cfg)

    ok, why = gw.ready()
    if not ok:
        in_main(_fail, session_id if session_id is not None else node_path,
                "网关没配好：%s" % why)
        return

    node = in_main(_lookup, node_path)
    if node is None and session_id is not None:
        node = in_main(_resolve, session_id)
    if node is None:
        return

    # ★ 思考深度由**用户在节点上选的**。它现在**只管模型的思考预算**，
    # 一步都不管——用户 2026-09-29 的原话：「不要限制 AI，不要限制 AI！
    # 等 AI 自己结束，再去判定它有没有构建完成」。
    # 原来深度里带着 `max_tool_rounds`（快 12 步）是错的：实测它第 12 步还在
    # 正常干活，被我截停之后界面报「没收尾」，看着像它失败——**那是我掐的，
    # 不是它没做完**。一轮什么时候结束，现在由模型自己决定。
    depth_name = in_main(node_mod.depth_of, node)
    preset = config.depth_preset(depth_name)
    thinking = preset.get("thinking")
    depth_label = preset["label"]
    # 唯一的熔断：时间。不数步数、不判打转。填 0 连时间都不管。
    minutes = float(cfg.get("runaway_minutes") or 0)
    deadline = (time.time() + minutes * 60.0) if minutes > 0 else None

    data = in_main(session.load, node)
    # 思考深度**不进提示词**——它是 API 参数（上面 preset 里的 thinking），
    # 由 gw.messages(thinking=...) 直接交给网关。提示词只拼容器处境。
    system = in_main(prompt.system_prompt, node)

    # ★ 这两块都是**变了才注入**，而且都拼在下面第一条 user 消息里，
    # **不进系统提示词**——系统提示词是缓存前缀的头部，放会变的东西会让
    # 整条前缀失效。见 `_memory_note` / `_promoted_note` 的说明。
    promoted_text = in_main(_promoted_note, node, data)
    memory_text = in_main(_memory_note, data)

    # 动手前存一份快照，方便回退到这一轮之前
    in_main(_snapshot, node, data)

    record = session.add_round(data, kind, text)
    messages = session.api_messages(data)
    head = (prompt.first_user_message(text) if kind == "build"
            else prompt.followup_message(text))
    # 顺序：**旋钮清单在前，共享经验在后**。（两条的标题不一样，互不覆盖。）
    parts = [t for t in (promoted_text, memory_text) if t]
    if parts:
        head = "\n\n".join(parts) + "\n\n" + head
    messages.append({"role": "user", "content": head})

    finished = None
    repair_left = cfg.get("max_repair_rounds", 3)
    built_anything = False

    # ★★ 不中途干涉。
    # 用户 2026-09-28 明确要求：「你只需要调整他的思考模式就可以了，不需要中途
    # 去干涉他的任何的行为。然后他最后结束对话的时候，如果说他并没有摆放任何的
    # 节点，并且做出任何的修改的话，那你再提醒他」。
    # 所以：**绝不按步数催它**（那是对它正当思考的打断），也绝不中途插话。
    # 唯一会提醒的时机是——它打算结束对话、容器里还是空的、而且它一次 build
    # 都没提交过。那种情况下它多半是「用文字描述了一遍自己打算怎么建」就收工了。
    final_reminders = 2

    # 新的一轮 = 新的历史。上一轮的动作不该留在注释里。
    in_main(node_mod.clear_activity, node)
    in_main(node_mod.set_status, node, "working", "开始（%s模式）…" % depth_label)

    turn = 0
    # 被时间熔断截停时记一句，收尾时**如实**写出来（绝不冒充「做完了」）。
    stop_note = ""
    # 输出被长度上限截断的次数。见文件顶部那段说明。
    truncations = 0
    # 工具参数拼不出合法 JSON 的次数（另一种"这一步的输出不可用"）。
    bad_json = 0

    while True:
        if cancel.is_set():
            in_main(node_mod.set_status, node, "stopped", "被你停了")
            record["ok"] = False
            in_main(session.save, node, data)
            return

        # ★ 唯一的熔断：墙上的钟。不数步数、不看重复——它想跑多少步都行。
        # 检查点在两轮之间（单次请求自己有 timeout，不会挂死在这条上）。
        if deadline is not None and time.time() > deadline:
            stop_note = "跑满 %g 分钟了" % minutes
            break

        turn += 1
        in_main(node_mod.set_status, node, "working",
                "思考中…（第 %d 步）" % turn)

        live = LiveStatus(node, in_main)
        live.start()
        try:
            response = gw.messages(system, messages, tools=schema.TOOL_DEFS,
                                   thinking=thinking, on_delta=live.on_delta)
        except gateway.GatewayError as exc:
            live.stop()
            # 网关不认流式（有些兼容网关没实现 SSE）就退回一次性请求。
            # 只在这种"请求本身被拒"的情况下重试——超时/断流重试会重复花钱。
            if exc.status not in (400, 404, 405, 415, 422):
                in_main(node_mod.set_status, node, "failed", str(exc)[:200],
                        error=str(exc))
                record["ok"] = False
                record["error"] = str(exc)
                in_main(session.save, node, data)
                return
            in_main(node_mod.set_status, node, "working",
                    "网关不认流式，这一轮改一次性等结果…")
            try:
                response = gw.messages(system, messages,
                                       tools=schema.TOOL_DEFS,
                                       thinking=thinking)
            except gateway.GatewayError as exc2:
                in_main(node_mod.set_status, node, "failed", str(exc2)[:200],
                        error=str(exc2))
                record["ok"] = False
                record["error"] = str(exc2)
                in_main(session.save, node, data)
                return
        finally:
            live.stop()

        content = gateway.strip_thinking(response.get("content") or [])
        narration = gateway.text_of(response)
        # ★ 响应里有三个字段以前一个都没读：`stop_reason`（有没有被砍）、
        # `usage`（花了多少）。现在都接上——不发 max_tokens 之后，
        # `stop_reason` 是唯一还能发现"有东西在砍输出"的信号。
        stop_reason = response.get("stop_reason")
        _add_usage(record, response.get("usage"))
        if not content:
            # ★ 模型可能只回了 thinking 块（没有 text 也没有 tool_use），
            # 去完 thinking 就是空的。把空 content 的消息塞进历史，
            # **下一次请求会被网关直接拒掉**：
            #   HTTP 400: messages.N: all messages must have non-empty content
            # 实测踩过，只有真联网才碰得到。补一句占位的非空文本。
            content = [{"type": "text", "text": "(这一步没有输出内容)"}]
        messages.append({"role": "assistant", "content": content})
        record["messages"] = messages
        record["turns"] = turn

        tool_uses = [b for b in content if b.get("type") == "tool_use"]

        # ---- 工具参数拼不出合法 JSON（流式那条路才会出现）----
        # ★★ 这**不是网关故障**，是模型这一步的输入坏了。回灌给它重发就行——
        # 绝不该让整轮结束。用户 2026-09-29 报的「失败了为什么不继续做下去」
        # 就是这个：以前 gateway._stream 在这里抛 GatewayError，而它的 status
        # 是 None、不在"流式被拒"那几个码里 → 下面那个 except 把它当成网关故障
        # 直接 return，整轮死掉。
        bad = response.get("bad_tool_json") or []
        if bad:
            bad_json += 1
            record["bad_json"] = bad_json
            # ★ 截断和"真拼错"在**这里**才分得清：`stop_reason` 要等流走到
            # `message_delta` 才拿得到，而以前 gateway 在解析就抛了、根本走不到。
            # 记下来，下次真出问题一眼能看出是哪种。
            for item in bad:
                item["stop_reason"] = stop_reason
            for item in bad:
                # 把**诊断信息**留在这一轮的 record 里。只报"失败了"是没用的，
                # 断点通常在尾巴上——长度、开头、结尾都得留下，下次才好查。
                in_main(node_mod.push_activity, node,
                        "参数拼不出 JSON（%s），这一步没执行"
                        % (item.get("name") or "?"))
                try:
                    record.setdefault("bad_json_detail", []).append(item)
                except Exception:
                    pass
            if truncations + bad_json >= MAX_TRUNCATIONS:
                note = ("连续 %d 步的模型输出不可用（截断 %d 次 + 参数坏 %d 次），"
                        "停在这里。" % (truncations + bad_json, truncations, bad_json))
                record["ok"] = False
                record["error"] = note
                in_main(node_mod.set_status, node, "failed", note, error=note)
                in_main(session.save, node, data)
                _notify()
                return
            if any(not call.get("id") for call in tool_uses):
                note = ("工具参数拼不出 JSON，而且连 id 都没有，没法继续这一轮。"
                        "请重发。")
                record["ok"] = False
                record["error"] = note
                in_main(node_mod.set_status, node, "failed", note, error=note)
                in_main(session.save, node, data)
                _notify()
                return
            # 被截断和真拼错，给模型的建议是不一样的（前者要说"拆小"，
            # 后者要说"把换行引号转义"）——虽然两条都让它重发。
            note = (TRUNCATED_NOTE if stop_reason == "max_tokens"
                    else BAD_JSON_NOTE)
            if tool_uses:
                messages.append({"role": "user", "content": [
                    {"type": "tool_result", "tool_use_id": call.get("id"),
                     "is_error": True, "content": note}
                    for call in tool_uses
                ]})
            else:
                messages.append({"role": "user", "content": note})
            record["messages"] = messages
            in_main(session.save, node, data)
            continue

        # ---- 输出被长度上限砍了 ----
        # ★ 这一段**必须先于"它打算收工了"那条判断**：被砍掉尾巴的回复
        # 是没有 tool_use 的，落到下面就会被当成"它自己结束了"→ 判成功。
        # 那正是这套东西最忌讳的假成功。
        if stop_reason == "max_tokens":
            truncations += 1
            # ★ 就地记进 record。**不能等到收尾再写**——下面几条路都会提前
            # `return`（连续截断收手、缺 id、被提醒到最后判失败），等收尾
            # 写的话那些路上这个痕就丢了。补测试时当场抓出来的。
            record["truncated"] = truncations
            in_main(node_mod.push_activity, node,
                    "输出被截断（第 %d 次），这一步没执行" % truncations)
            if truncations >= MAX_TRUNCATIONS:
                note = ("连续 %d 次输出被长度上限截断，停在这里。"
                        "多半是这个网关自己有个输出天花板。" % truncations)
                record["ok"] = False
                record["error"] = note
                in_main(node_mod.set_status, node, "failed", note,
                        error=note)
                in_main(session.save, node, data)
                _notify()
                return

            # 缺 id 的 tool_use 没法配对 tool_result，再往下走网关会 400。
            # 截得极早才会这样，如实报失败，别硬撑。
            if any(not call.get("id") for call in tool_uses):
                note = ("输出被截断得太早，工具调用连 id 都没有，"
                        "没法继续这一轮。请重发。")
                record["ok"] = False
                record["error"] = note
                in_main(node_mod.set_status, node, "failed", note, error=note)
                in_main(session.save, node, data)
                _notify()
                return

            if tool_uses:
                # 逐个配对回一条 is_error 的 tool_result——**不执行**它们。
                # 参数多半是空的，拿它去执行轻则被拒、重则干出莫名其妙的事。
                messages.append({"role": "user", "content": [
                    {"type": "tool_result", "tool_use_id": call.get("id"),
                     "is_error": True, "content": TRUNCATED_NOTE}
                    for call in tool_uses
                ]})
            else:
                messages.append({"role": "user", "content": TRUNCATED_NOTE})
            record["messages"] = messages
            in_main(session.save, node, data)
            continue

        # 模型明确拒绝了（本项目没有会触发它的东西，但真出现就别静默吞掉）。
        if stop_reason == "refusal":
            note = "模型拒绝了这次请求：%s" % (narration or "（没给理由）")
            record["ok"] = False
            record["error"] = note
            in_main(node_mod.set_status, node, "failed", _one_line(note),
                    error=narration[:1500], full=narration[:1500])
            in_main(session.save, node, data)
            _notify()
            return

        if not tool_uses:
            # 它打算结束对话了（这一轮只有文字、没有工具调用）。
            #
            # ★★ 判定顺序（用户 2026-09-29 定）：「等 AI 自己结束，再去判定
            # 它有没有构建完成」。所以**它自己收工就是一种正常收尾**——不能拿
            # 「没走我那道 finish 手续」当罪状。上一版就是那么报的：「这一轮
            # 没收尾：跑了 12 步也没调用 finish」，可网络明明建出来了，界面一
            # 片红——那是我在骗人。该看的是**容器**：真建出东西了吗？cook 过不过？
            if finished is not None:
                break

            still_empty = in_main(_container_is_empty, node)
            if final_reminders > 0 and not built_anything and still_empty:
                # 唯一会插话的时机：它只说不做、容器还是空的。
                # 实测模型会查完参数就用文字描述它「打算」怎么建，从头到尾
                # 不调 build——那时候把「不调工具」当完成就是假成功（绿色的
                # 「完成」配 0 点产出），比报错还坏。所以这里提醒一次。
                final_reminders -= 1
                messages.append({
                    "role": "user",
                    "content": ("你还没有往容器里放任何节点——它现在是空的。"
                                "请不要用文字描述你的计划，直接调用 build "
                                "把完整网络提交上来，建好之后调用 finish。"),
                })
                continue

            if built_anything and not still_empty:
                # 建出来了，只是没走 finish 那道手续。替它把 finish 那套校验
                # 跑一遍（cook + 收报错），拿**真实结果**说话，然后按正常
                # 收尾走。这不是「替它宣布成功」——cook 不过照样报失败。
                finished = in_main(schema.dispatch, node, "finish",
                                   {"summary": narration or
                                    "（模型没有给文字说明，按容器里的实际产出判定）"})
                finished["self_ended"] = True
                if narration and not record.get("summary"):
                    record["summary"] = narration
                break

            record["ok"] = False
            record["error"] = "没有提交任何构建就结束了"
            if narration:
                record["summary"] = narration
            in_main(node_mod.set_status, node, "failed",
                    "没有提交任何构建就结束了",
                    error=narration[:1500],
                    full=narration[:1500])
            in_main(session.save, node, data)
            _notify()
            return

        results = []
        for call in tool_uses:
            name = call.get("name")
            args = call.get("input") or {}
            # 正在跑的这一步只占**最后一行**（实时状态），不进历史。
            live = _describe(name, args)
            in_main(node_mod.set_status, node, "working", live)

            output = in_main(schema.dispatch, node, name, args)
            # 跑完了才记进历史——这样「历史」里全是真干完的事，
            # 而且当前这一步不会同时出现在历史和实时那行里。
            # （push_activity 只写 userData，注释要等下一次 set_status 才重画，
            #   所以不会出现"同一句话出现两次"的中间态。）
            # `_describe` 带的是进行中的省略号（"提交网络（5 个节点）…"），
            # 进了历史就是过去式了，去掉。
            in_main(node_mod.push_activity, node, live.rstrip("…").strip())
            # ★ 图像单独走图像块。base64 塞进 JSON 文本里会让模型把
            #   几万个字符当文字读一遍，白烧几千 token（而且读不懂）。
            images = []
            if isinstance(output, dict) and "__images__" in output:
                images = output.pop("__images__") or []

            if images:
                content = [{"type": "text", "text": _compact(output)}]
                for raw in images:
                    try:
                        content.append({
                            "type": "image",
                            "source": {
                                "type": "base64",
                                "media_type": "image/png",
                                "data": base64.b64encode(raw).decode("ascii"),
                            },
                        })
                    except Exception:
                        pass
            else:
                content = _compact(output)

            results.append({
                "type": "tool_result",
                "tool_use_id": call.get("id"),
                "content": content,
            })
            _trace(record, name, args, output, images)

            if name in ("build", "update_parms") and output.get("ok"):
                built_anything = True

            if name == "finish":
                if output.get("rejected"):
                    # 被守卫拒了（summary 空 / 容器空）。★ 这**不是** cook 失败，
                    # 所以绝不能动 `repair_left`——那是"cook 报错让不让它再修"
                    # 的名额，被拒属于"你没交代清楚"，回灌一句让它重来就行。
                    # 不这么写的话，被拒的 finish 会掉进下面的 `repair_left > 0`
                    # 分支，报一句"cook 有报错"（假的），还白烧一个修订名额。
                    results.append({
                        "type": "text",
                        "text": "finish 被拒了：" +
                                "；".join(output.get("problems") or []),
                    })
                elif output.get("cook_ok"):
                    finished = output
                    if output.get("summary"):
                        record["summary"] = output["summary"]
                elif repair_left > 0:
                    repair_left -= 1
                    results.append({
                        "type": "text",
                        "text": "刚才 finish 时 cook 有报错，请修正后重新 finish。",
                    })
                else:
                    finished = output
                    record["summary"] = (output.get("summary") or "") + \
                        "\n（还有 cook 报错没解决，我停下来了）"

        # 注意这里**没有**任何「跑了几步就提醒它」的逻辑——那是中途干涉，
        # 也是用户明确否掉的东西。要提醒只在它打算结束对话时提醒（见上面）。
        messages.append({"role": "user", "content": results})
        record["messages"] = messages
        in_main(session.save, node, data)

        if finished is not None:
            break

    # ---- 收尾 ----
    # ★ 判据是**容器里真有什么**，不是「它有没有走完我那道手续」。
    if finished is None and not in_main(_container_is_empty, node):
        # 被时间截停，但容器里已经建出东西了——照样按事实判，
        # 别因为「不是它自己收的尾」就一律报失败。
        finished = in_main(schema.dispatch, node, "finish",
                           {"summary": record.get("summary") or
                            "（模型没有给文字说明，按容器里的实际产出判定）"})

    record["ok"] = True
    if finished is None:
        # 走到这儿只剩一种情况：容器是空的（上面已经把「建出东西了」接管了）。
        record["ok"] = False
        reason = stop_note or "没有提交任何构建就结束了"
        record["error"] = reason
        in_main(node_mod.set_status, node, "failed", reason,
                error=record.get("summary") or "")
    elif not finished.get("cook_ok") and finished.get("cook_problems"):
        record["ok"] = False
        in_main(node_mod.set_status, node, "failed",
                "建好了但 cook 还有报错（看下方细节）",
                error="\n".join(finished["cook_problems"])[:1500])
    else:
        points = finished.get("output_points")
        detail = "%s | %s模式 %d 步 | 模型 %s" % (
            "产出 %s 点" % points if points is not None else "已完成",
            depth_label, record["turns"], cfg.get("model"))
        if finished.get("self_ended"):
            detail += " | 它自己收工的（没调 finish）"
        if stop_note:
            detail += " | %s" % stop_note
        usage = record.get("usage") or {}
        if usage:
            # 只报数字，不做任何拦截。见 `_add_usage` 的说明。
            # ★ 写成「新算的 + 缓存读的」而不是「输入 A（缓存读 B）」：后者
            # 看着像总共只输入了 A，而 A 只是**没走缓存的那部分**（实测一次
            # 深入档：新算 4 万 + 缓存读 132 万）。
            detail += " | 输出 %s token；输入 新算 %s + 缓存读 %s" % (
                usage.get("output_tokens", 0),
                usage.get("input_tokens", 0),
                usage.get("cache_read_input_tokens", 0))
        summary = record.get("summary") or "做完了"
        if stop_note:
            summary = "%s（%s）" % (_one_line(summary, 90), stop_note)
        in_main(_finish_ok, node, summary, detail)

    record["round"] = len(data["rounds"])
    in_main(session.save, node, data)
    _notify()


def _container_is_empty(node):
    """容器里一个节点都没有。用来判断「它是不是真的什么都没干」。"""
    try:
        return len(node.children()) == 0
    except Exception:
        return True


def _promoted_note(node, data):
    """这一轮要不要把「容器外面现在有哪些可调参数」推进给模型？

    ★★ 为什么需要它（用户 2026-09-29 报的问题：「给迭代反馈的时候，他并没有
    暴露需要的参数」）：旧轮次会被压成两行摘要（`session.MAX_FULL_ROUNDS`），
    那两行**不含**提升清单——迭代到第 3 轮以后模型就看不到自己上几轮把哪些
    旋钮搬到外面了，只能赌它这一轮老实调了 `list_container`。**没有兜底。**

    触发条件和 `_memory_note` 完全一样：指纹变、或已经滚出完整回放窗口、
    或注入过的那一轮已经不存在（回退过）。清单没变时它已经在模型历史里，
    重发是纯冗余。

    ⚠️ 这**不是催办**——只陈述现状 + 契约，不含任何「你该再多加几个旋钮」。
    按状态催它，是本项目明令禁止的中途干涉。
    """
    try:
        from . import builder

        digest = builder.promoted_digest(node)
        if not digest:
            return ""
        upcoming = len(data.get("rounds") or []) + 1
        last = data.get("promotions_injected") or {}
        if last.get("digest") == digest:
            try:
                age = upcoming - int(last.get("round") or 0)
            except Exception:
                age = -1
            # `0 <= age`：负数是"注入过的那一轮已经没了"（回退过），
            # 那时那段文本也随历史消失了，必须补发。
            if 0 <= age < session.MAX_FULL_ROUNDS:
                return ""
        text = builder.promoted_brief(node)
        if not text:
            return ""
        data["promotions_injected"] = {"digest": digest, "round": upcoming}
        return text
    except Exception:
        return ""


def _memory_note(data):
    """这一轮要不要把共享经验写给模型？要就返回那一段文本，不要就返回 ""。

    ★★ 为什么不是"每轮都注入"（用户 2026-09-29 问的）：
    「模型既然是在同一个 session 里做的，为什么需要每轮都注入？」

    他说得对。**这个节点自己写的条目本来就在它自己的对话历史里**，每轮重发
    一遍是纯冗余。共享记忆存在的意义是那两种它**看不到**的东西：

      * **别的 AI 节点写的** —— 每个节点的历史是独立的，A 看不到 B 的对话；
      * **你在面板里手改的** —— 那更不会出现在它的历史里。

    所以按指纹（`memory.digest`）比对：**没变就不发**。

    ★ 再补一条兜底：注入过的那一轮如果已经被挤出"完整回放"的窗口
    （`session.MAX_FULL_ROUNDS`），即使指纹没变也得重发一次——否则那段文本
    会随轮次推进**悄悄从上下文里消失**（旧轮次会被压成两行摘要，我们拼在
    user 消息里的这段自然也一起没了）。

    读不出来就当没有——记忆不是必需品，绝不能因为它把一轮跑挂。
    """
    try:
        from . import memory

        digest = memory.digest()
        if not digest:
            return ""
        upcoming = len(data.get("rounds") or []) + 1
        last = data.get("memory_injected") or {}
        if last.get("digest") == digest:
            try:
                age = upcoming - int(last.get("round") or 0)
            except Exception:
                age = -1
            # ★ `0 <= age`：如果被注入的那一轮**已经不存在了**（你回退过轮次），
            # age 会变成负数——那时候那段文本已经从历史里消失，必须重新注入。
            if 0 <= age < session.MAX_FULL_ROUNDS:
                return ""
        text = memory.brief()
        if not text:
            return ""
        data["memory_injected"] = {"digest": digest, "round": upcoming}
        return text
    except Exception:
        return ""


def _snapshot(node, data):
    """这一轮动手之前的容器样子。回退用的。"""
    from . import builder

    try:
        spec = builder.summarize(node)
    except Exception:
        return
    upcoming = len(data["rounds"]) + 1
    session.add_snapshot(data, spec, upcoming)


def _finish_ok(node, summary, detail):
    # 短的那句给面板表格；完整的 summary 进节点注释——那是唯一你会看的地方。
    node_mod.set_status(node, "done", _one_line(summary), detail=detail,
                        full=summary)
    node_mod.set_round(node, node_mod.round_of(node) + 1)
    _tell_user("AI 节点 %s 建好了：%s" % (node.name(), _one_line(summary, 70)),
               error=False)


def _tell_user(message, error=False):
    """在 Houdini 状态栏上吱一声。

    这一条是为「AI 在后台干活」这个需求服务的：你多半已经切去干别的了，
    节点变色你看不见。状态栏是开销最小、又不打断你的那个信号。
    """
    import hou

    try:
        hou.ui.setStatusMessage(
            message,
            severity=(hou.severityType.Error if error
                      else hou.severityType.Message))
    except Exception:
        pass


def _one_line(text, limit=90):
    text = (text or "").strip().replace("\n", " ")
    return text if len(text) <= limit else text[:limit] + "…"


# 实时状态那一行显示多少个字。用户要的是「他思考的最后几个字儿」，
# 不是整段思考——注释那一行还得放得下，多了就折行把 4 行的版式撑破。
LIVE_TAIL_CHARS = 44
# 一秒钟最多重画几次注释。Deltas 可能一秒来几十个，每个都去写 userData
# 是白费（而且每次都要回主线程）。
LIVE_THROTTLE = 0.5
# 秒表线程多久跳一次。就算模型一个字都不吐，这个数也在涨——
# 用户要的「不然我都不知道他有没有卡住」就是靠它。
LIVE_BEAT = 1.0


class LiveStatus(object):
    """把模型正在想/正在说的内容实时写到节点注释的最后一行。

    用户原话：「实时状态能不能不要老是写思考中，说实话，思考中我完全看不出来
    他在干什么，就比如说，可能每隔一会儿更新一下他思考的最后几个字儿之类的
    ……不然的话，思考中我都不知道他有没有卡住什么的」。

    所以两条腿都要，缺一条都不成立：

    * **有内容** —— 流式收 delta，显示"想：…"或"说：…"的尾巴。
      只有秒表没有内容，用户还是不知道它在干嘛。
    * **有秒表** —— 独立线程每秒跳一次。只有内容的话，模型沉默的那段时间
      （想之前、或者卡住了）界面完全不动，看着就像死了。

    线程安全：`on_delta` 在工作线程被调，秒表是另一个线程，
    `set_status` 又要回主线程——所以缓冲区用锁护住。
    """

    def __init__(self, node, in_main, throttle=LIVE_THROTTLE):
        self.node = node
        self.in_main = in_main
        self.throttle = throttle
        self._lock = threading.Lock()
        self._kind = ""          # thinking / text
        self._tail = ""
        self._started = time.time()
        self._last_push = 0.0
        self._done = threading.Event()
        self._thread = None

    # -- 流式回调（工作线程） -------------------------------------------
    def on_delta(self, kind, piece):
        with self._lock:
            if kind != self._kind:
                # 换了来源（思考 → 正文）就重新起头，别把两段拼在一起。
                self._kind = kind
                self._tail = ""
            # 只留尾巴。思考可能有几万字，全留着是浪费（而且拼接会越来越慢）。
            self._tail = (self._tail + piece)[-(LIVE_TAIL_CHARS * 4):]
        self._push()

    # -- 秒表 ------------------------------------------------------------
    def start(self):
        self._thread = threading.Thread(target=self._beat, daemon=True,
                                        name="houdini-ai-live")
        self._thread.start()
        self._push(force=True)

    def _beat(self):
        while not self._done.wait(LIVE_BEAT):
            self._push(force=True)   # 秒数要跳，节流对它无效

    def stop(self):
        self._done.set()
        thread, self._thread = self._thread, None
        if thread is not None:
            thread.join(timeout=2.0)

    # -- 渲染 ------------------------------------------------------------
    def text(self):
        with self._lock:
            kind, tail = self._kind, self._tail
        seconds = int(time.time() - self._started)
        body = " ".join(tail.split())          # 思考里有换行，注释只留一行
        if body:
            body = body[-LIVE_TAIL_CHARS:]
            head = "想：…%s" % body if kind == "thinking" else "说：%s" % body
        else:
            head = "思考中…"
        return "%s（%d 秒）" % (head, seconds)

    def _push(self, force=False):
        now = time.time()
        with self._lock:
            if not force and (now - self._last_push) < self.throttle:
                return
            self._last_push = now
        try:
            self.in_main(node_mod.set_status, self.node, "working", self.text())
        except Exception:
            pass


def _describe(name, args):
    """给面板看的一句话：模型现在在干嘛。"""
    if name == "list_container":
        return "查看容器现状…"
    if name == "search_node_types":
        return "找节点类型：%s" % args.get("query", "")
    if name == "get_node_type":
        return "查参数：%s" % args.get("type", "")
    if name == "get_node_docs":
        return "查文档：%s" % args.get("type", "")
    if name == "get_geometry":
        return "看产出：%s" % (args.get("node") or "整体")
    if name == "get_errors":
        return "检查报错…"
    if name == "list_context":
        return "看容器外面的情况…"
    if name == "look":
        bits = []
        view = str(args.get("view") or "").strip()
        if view:
            # 视角写在最前面——它是必填的，也是这次出图最关键的一个选择。
            # 用中文标签（"前视"/"六视图"），面板上比 `+z` 好读。
            try:
                from . import viewport

                bits.append(viewport.VIEWS.get(view, {}).get("label") or view)
            except Exception:
                bits.append(view)
        elif args:
            # 没给视角：这正是用户要堵的那种调用，面板上直接显出来
            bits.append("没给视角")
        if args.get("preset"):
            bits.append(str(args["preset"]))
        if args.get("attribute"):
            bits.append("按 %s 上色" % args["attribute"])
        # ★ 别动「看一眼」这三个字——有测试断言它（面板历史行）。
        return "看一眼（%s）…" % "，".join(bits) if bits else "看一眼…"
    if name == "remember":
        return "记一条共享经验…"
    if name == "build":
        count = len(args.get("nodes") or [])
        return "提交网络（%d 个节点）…" % count
    if name == "update_parms":
        return "调参数（%d 处）…" % len(args.get("ops") or [])
    if name == "finish":
        return "收尾…"
    return name


def _compact(output):
    """工具返回序列化成字符串。**不截断**。

    ★ 以前这里有个 `limit=6000`，超了就砍。拆掉了——用户 2026-09-29 定：
    「不要截断任何内容，只在 API 上控制思考强度」。砍断的代价很具体：
    `get_node_type("polyextrude")` 是 14,393 字符的完整参数表，砍到 6000
    之后模型拿到的是一份**残缺 JSON**，而且**没有任何办法**要回剩下那半——
    它只能猜参数名，猜错被拒。真相完整比省这点上下文值钱。

    （tool_result 的 content 必须是字符串，所以 json.dumps 这一层要留着。）
    """
    try:
        return json.dumps(output, ensure_ascii=False, default=str)
    except Exception:
        return str(output)


def _trace(record, name, args, output, images=None):
    """给面板留一行短记录。"""
    trace = record.setdefault("trace", [])
    line = _describe(name, args)
    if images:
        line += " 出了 %d 张图 ^" % len(images)
    if output.get("rejected"):
        problems = output.get("problems") or []
        line += " 被拒：%s" % (problems[0] if problems else "")
    elif output.get("error"):
        line += " 出错：%s" % str(output["error"])[:120]
    elif name == "build":
        points = output.get("output_points")
        line += " 成功，产出 %s 点" % points
        if output.get("cook_problems"):
            line += "，但有报错"
    elif name == "search_node_types":
        line += " 命中 %d 个" % output.get("count", 0)
    elif name == "get_geometry":
        line += " %s 点" % output.get("points")
    trace.append(line)
    if len(trace) > 200:
        del trace[:-200]


# --------------------------------------------------------------------------
# 给面板用的便捷入口
# --------------------------------------------------------------------------

def snapshot_rounds(node):
    data = session.load(node)
    return [(s.get("round"), s.get("label")) for s in data.get("snapshots", [])]


def restore_round(node, round_number):
    """把容器恢复成第 round_number 轮开始前的样子。"""
    from . import builder

    note_main_thread()
    data = session.load(node)
    snap = session.snapshot_for_round(data, round_number)
    if snap is None:
        node_mod.set_status(node, "failed", "找不到那一轮的快照")
        return False
    spec = snap.get("spec") or {}
    # 快照要**完整**重建：参数值、表达式（提升关系就藏在里面）、
    # 容器名字、提升清单，一个都不能漏。漏了的话回退会静默把提升关系丢掉。
    rebuild = {
        "nodes": [{"name": n["name"], "type": n["type"],
                   "params": n.get("params") or {},
                   "expressions": n.get("expressions") or {}}
                  for n in spec.get("nodes", [])],
        "connections": [c for c in spec.get("connections", [])
                        if c.get("from") != "（容器外或容器输入）"],
    }
    if spec.get("name"):
        rebuild["name"] = spec["name"]
    if spec.get("promoted"):
        rebuild["promote"] = spec["promoted"]
    try:
        builder.apply(node, rebuild, group_label="AI: 回退到第 %d 轮" % round_number)
    except builder.SpecError as exc:
        node_mod.set_status(node, "failed", "回退失败", error="\n".join(exc.problems))
        return False
    node_mod.set_status(node, "idle", "已回退到第 %d 轮之前" % round_number)
    return True


def time_now():
    return time.strftime("%H:%M:%S")
