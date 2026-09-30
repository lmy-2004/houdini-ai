# -*- coding: utf-8 -*-
"""网关配置。

设计要点：不硬编码任何密钥。读取顺序是

    环境变量  ->  ~/.claude/settings.json 的 env 段  ->  工具自带的 config.json

中间那一层就是「复用网关」的实现：你 Claude Code 用的那套
ANTHROPIC_BASE_URL / ANTHROPIC_AUTH_TOKEN / ANTHROPIC_MODEL，这里原样捡起来用。
协议是 Anthropic Messages API，所以换成 api.anthropic.com 或网关上别的模型
都只是改配置，不用改代码。
"""

import json
import os

_TOOL_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
CONFIG_PATH = os.path.join(_TOOL_ROOT, "config.json")
CLAUDE_SETTINGS = os.path.join(os.path.expanduser("~"), ".claude", "settings.json")

# 不带 [1m] 后缀的那个是实测可用的；[1m] 是 Claude Code 的 1M 上下文写法，
# 直连是否被接受要现场试，所以默认用保险的那个。
DEFAULT_MODEL = "deepseek-flash"

# --------------------------------------------------------------------------
# 思考深度
#
# ★ 用户 2026-09-28 定：**不要**用一个固定的「查了多少步就催它动手」的硬限制
# 去打断模型的探索——那是它的正当思考。
# ★ 用户 2026-09-29 又推了一步：「不要限制 AI，不要限制 AI！等 AI 自己结束，
# 再去判定它有没有构建完成」。原来深度里那个 `max_tool_rounds`（快 12 / 标准
# 30 / 深入 80）就是**在够用之前先把人掐死**——实测它第 12 步还在正常干活，
# 被截停后界面报「这一轮没收尾」，看着像它失败，其实是我的上限太紧。
#
# 所以现在：**深度只管模型的思考预算**，不管跑几步。一轮什么时候结束由模型
# 自己决定——它不再调工具了，就是它说「我干完了」。判断做没做完是**事后**的
# 事（看容器里真有什么、cook 过不过），不是事前拿数字卡。
# --------------------------------------------------------------------------

DEPTH_PRESETS = {
    "fast": {
        "label": "快",
        "help": "不思考，动手快。适合需求很明确的小东西。",
        "thinking": {"type": "disabled"},
    },
    "standard": {
        "label": "标准",
        "help": "默认。开思考（4000 token 预算），够它查清节点和参数。",
        "thinking": {"type": "enabled", "budget_tokens": 4096},
    },
    "deep": {
        "label": "深入",
        "help": "充分探索：思考预算 16000。适合复杂需求，慢且贵。",
        "thinking": {"type": "enabled", "budget_tokens": 16000},
    },
}

DEPTH_ORDER = ["fast", "standard", "deep"]
DEFAULT_DEPTH = "standard"

# 这里曾经有个 DEPTH_WARN_TAIL（步数快用完时提醒它）。
# 用户 2026-09-28 明确要求**不中途干涉**，那个删掉了。
# 用户 2026-09-29 把最后那点「硬上限」也否掉了（见上）。
# 深度现在**只**负责「想多少」，一步都不管。


def depth_preset(name):
    return DEPTH_PRESETS.get(name) or DEPTH_PRESETS[DEFAULT_DEPTH]


DEFAULTS = {
    "base_url": "https://api.deepseek.com/anthropic",
    "api_key": "",
    "model": DEFAULT_MODEL,
    # 新建的 AI 节点默认用哪个思考深度。改了不影响已经建好的节点——
    # 那些节点上各自存着自己的选择。
    "default_depth": DEFAULT_DEPTH,
    # finish 之后自动校验发现报错，再回灌给它修几轮。
    "max_repair_rounds": 3,

    # ---- 唯一的熔断：时间 ----
    # ★★ 用户 2026-09-29：「不要再记录步数了，不要有任何的熔断措施，**除非它
    # 已经运行了 60 分钟以上**」。
    # 所以**没有步数上限，也没有任何"原地打转就停"的检测**——它想跑多少步就跑
    # 多少步，想反复看同一个东西就看。唯一会掐它的是墙上的钟：一轮超过这么多
    # 分钟就收手（这一轮已经跑出来的东西照样按事实判定，不报假失败）。
    # 填 0 表示连时间也不管。
    "runaway_minutes": 60,

    # 同时跑几个节点的构建。同一条链上的操作永远串行。
    "max_concurrent": 2,

    # ---- 单次回复的长度上限 ----
    # ★★ 用户 2026-09-29：「直接不要 max tokens」。
    # **留空（None）= 这个字段一个都不发**，模型的输出不再有人为上限。
    # 为什么该留空：**思考和答案是共享这一个上限的**，而「思考深度」要控制的
    # 正是思考预算——留着它就等于给思考加了一道天花板。实测「深入」档承诺
    # 的 budget_tokens=16000 大于旧上限 8000，**该档的深度物理上够不着**。
    # 另一次实测：上限 8000 时一轮 11 步有 3 步撞顶，那 3 步吃掉 65% 的时间和
    # 67% 的输出；而且**撞顶时工具参数会变成空字典**，模型白干一步。
    # 实测本机网关不传这个字段也接受，它自己的天花板是 65536。
    # 只有在**官方 Anthropic 端点**（那边必填）才需要填一个大值；代码里已按
    # 域名自动兜底，这一项通常不用动。
    "max_tokens": None,
    # 上面兜底用的大值。只有官方端点、或网关明确要求 max_tokens 时才用得到。
    "max_tokens_fallback": 64000,
    # 单次 HTTP 请求超时（秒）。模型要思考，别设太短。
    "timeout": 300,
    # 要不要把文档抓取缓存到磁盘（首抓联网，之后离线可用）。
    "doc_cache": True,
    "verbose": True,

    # ---- 图像通道：AI 可以要一张渲染截图自己看 ----
    # 用 opengl ROP 离屏渲染：不开窗口、不碰视口、不抢焦点。
    "screenshot_enabled": True,
    # ★ 一个 Houdini 会话里允不允许出**第二张**图。
    # 实测（无界面）opengl ROP 在同一进程里渲第二次会**崩**，虽然那大概率是
    # 无界面特有的（Houdini 用户拿它渲多帧序列是常规操作），但我没法验证 GUI，
    # 而赌错就是崩掉你的 Houdini。所以默认只许一张，等你确认过再打开。
    # 详见 houdini_ai/viewport.py 顶部那段说明。
    "screenshot_allow_repeat": False,
    # 出图分辨率。图要发给模型，越大越贵。
    "screenshot_size": [800, 600],
}


class Config(dict):
    """点号访问的配置字典。config.model 比 config['model'] 好读。"""

    def __getattr__(self, name):
        try:
            return self[name]
        except KeyError:
            raise AttributeError(name)

    def __setattr__(self, name, value):
        self[name] = value

    def safe_api_key(self):
        """给日志/界面显示的脱敏版本。"""
        k = self.get("api_key") or ""
        if len(k) <= 10:
            return "***" if k else "(未设置)"
        return "%s***%s" % (k[:6], k[-4:])


def _from_env():
    out = {}
    for key, env in (
        ("base_url", "HOUDINI_AI_BASE_URL"),
        ("api_key", "HOUDINI_AI_API_KEY"),
        ("model", "HOUDINI_AI_MODEL"),
    ):
        val = os.environ.get(env)
        if val:
            out[key] = val
    return out


def _from_claude_settings():
    """从 Claude Code 的 settings.json 里捡网关信息。

    Claude Code 把配置放在 env 段里。缺文件、坏 JSON、缺字段都只是安静跳过，
    不能因为读不到就让工具起不来。
    """
    out = {}
    try:
        with open(CLAUDE_SETTINGS, "r", encoding="utf-8") as fh:
            data = json.load(fh)
    except Exception:
        return out

    env = data.get("env") or {}
    if not isinstance(env, dict):
        return out

    base = env.get("ANTHROPIC_BASE_URL")
    if base:
        out["base_url"] = base

    # AUTH_TOKEN 是 Claude Code 走第三方网关时的字段，API_KEY 是官方字段，两个都认。
    key = env.get("ANTHROPIC_AUTH_TOKEN") or env.get("ANTHROPIC_API_KEY")
    if key:
        out["api_key"] = key

    model = env.get("ANTHROPIC_MODEL")
    if model:
        # [1m] 这类 Claude Code 私有的后缀去掉，直连网关不认。
        out["model"] = model.split("[")[0].strip()

    return out


def _from_file():
    try:
        with open(CONFIG_PATH, "r", encoding="utf-8") as fh:
            data = json.load(fh)
    except Exception:
        return {}
    if not isinstance(data, dict):
        return {}
    # 下划线开头的键是给人看的注释（沿用 worldforge 的 $comment 习惯）。
    return {k: v for k, v in data.items() if not k.startswith("_")}


def load(overrides=None):
    """按 默认值 -> settings.json -> config.json -> 环境变量 -> 显式覆盖 合并。

    顺序说明：settings.json 提供「网关在哪」，所以比 config.json 早；
    环境变量最后是因为它最适合临时试东西。
    """
    cfg = Config(DEFAULTS)
    cfg.update(_from_claude_settings())
    cfg.update(_from_file())
    cfg.update(_from_env())
    if overrides:
        cfg.update({k: v for k, v in overrides.items() if v is not None})
    return cfg


def describe(cfg):
    """一行摘要，给界面和日志用。绝不打印完整密钥。"""
    return "网关=%s | 模型=%s | 密钥=%s" % (
        cfg.get("base_url"),
        cfg.get("model"),
        cfg.safe_api_key(),
    )
