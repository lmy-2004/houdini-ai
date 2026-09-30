# -*- coding: utf-8 -*-
"""AI 节点本身：创建、识别、状态显示。

容器就是一个普通的 **Subnet SOP**——不是 HDA。这是刻意的：HDA 的实例共享
同一份内容定义，两个 AI 节点会互相踩；而 Subnet 的子节点天然是每个实例
独立的，存进 .hip 也各存各的。

## 参数面板只有三个东西

「描述」「开始构建」「停止」——就这些。状态、细节、错误、轮次这些**都不做成
参数**（摆一排在参数面板里太臃肿），而是：

  * **节点注释**——直接显示在网络编辑器里节点下方，抬眼就能看见
  * **节点的 userData**——机器可读的那份，界面不显示，跟着 .hip 走
  * **节点颜色**——灰/蓝/绿/红/橙

「这是个 AI 节点」的标记就看有没有 `ai_prompt` 这个参数。
"""

import json
import os
import unicodedata

PROMPT = "ai_prompt"
FEEDBACK = "ai_feedback"
DEPTH = "ai_depth"
BUILD = "ai_build"
SEND = "ai_send"
STOP = "ai_stop"

# 参数面板上的全部东西。布局是按这个顺序来的：
#
#     描述        [大文本框]
#     修改意见    [大文本框]
#     思考深度    [快/标准/深入]
#     [开始构建] [发送修改意见] [停止]      ← 三个按钮排一行
#
# 三个按钮排一行靠的是 `setJoinWithNext(True)`：它让本参数和**下一个**参数
# 显示在同一行（Houdini 原生机制，不用自己画界面）。
#
# 两个文本框做成大的靠参数**标签**（见 _editor_parm）——Houdini 没有
# 「多行字符串参数」这个类型，它是用 editor 标签实现的。
VISIBLE_PARMS = (PROMPT, FEEDBACK, DEPTH, BUILD, SEND, STOP)

# 早期版本把状态做成了参数，现在都挪进 userData 了。留着会显得乱，
# ensure_parms 会把它们摘掉并把值迁移过去。
DEPRECATED_PARMS = ("ai_state", "ai_status", "ai_detail", "ai_round",
                    "ai_error")

STATE_KEY = "houdini_ai_state"

STATES = [
    ("idle", "空闲"),
    ("working", "构建中"),
    ("verifying", "校验中"),
    ("done", "完成"),
    ("failed", "失败"),
    ("stopped", "已停止"),
]

STATE_LABELS = [label for _, label in STATES]
STATE_TOKENS = [token for token, _ in STATES]
LABEL_OF = dict(STATES)

_COLORS = {
    "idle": (0.55, 0.55, 0.58),
    "working": (0.25, 0.55, 0.95),
    "verifying": (0.20, 0.70, 0.85),
    "done": (0.30, 0.72, 0.35),
    "failed": (0.90, 0.30, 0.25),
    "stopped": (0.90, 0.60, 0.20),
}

_ICONS = {
    "idle": "○",
    "working": "◐",
    "verifying": "◑",
    "done": "●",
    "failed": "✕",
    "stopped": "■",
}


def tool_root():
    return os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def is_ai_node(node):
    try:
        return node.parm(PROMPT) is not None
    except Exception:
        return False


def all_ai_nodes():
    """全场景枚举所有 AI 节点。给面板的状态表用。"""
    import hou

    root = hou.node("/")
    if root is None:
        return []
    found = []
    try:
        for node in root.allSubChildren():
            if is_ai_node(node):
                found.append(node)
    except Exception:
        pass
    return found


# --------------------------------------------------------------------------
# 状态记录（存 userData，不进参数面板）
# --------------------------------------------------------------------------

_EMPTY = {"state": "idle", "text": "", "detail": "", "error": "", "round": 0,
          "activity": []}

# 注释里「刚才干了什么」保留几条。用户要的是 3 条历史 + 1 行实时状态 = 4 行。
ACTIVITY_KEEP = 3

# 注释框大约 40 个半角列宽（从用户截图里量的）。历史每条压到这么多列以内，
# 免得折行——3 条历史一折行就变 6 行，注释框里一半是废话。
HISTORY_COLS = 36


def load_state(node):
    try:
        raw = node.userData(STATE_KEY)
    except Exception:
        raw = None
    if not raw:
        return dict(_EMPTY)
    try:
        data = json.loads(raw)
    except ValueError:
        return dict(_EMPTY)
    if not isinstance(data, dict):
        return dict(_EMPTY)
    record = dict(_EMPTY)
    record.update(data)
    # ★ `_EMPTY["activity"]` 是**模块级的同一个列表对象**。不复制的话，
    #   push_activity 会往那个共享默认值里塞东西，污染之后每一个新节点。
    record["activity"] = list(record.get("activity") or [])
    return record


def save_state(node, record):
    try:
        node.setUserData(STATE_KEY, json.dumps(record, ensure_ascii=False))
        return True
    except Exception:
        return False


def current_state(node):
    return load_state(node).get("state") or "idle"


def status_line(node):
    return load_state(node).get("text") or ""


def detail_of(node):
    return load_state(node).get("detail") or ""


def error_of(node):
    return load_state(node).get("error") or ""


def round_of(node):
    try:
        return int(load_state(node).get("round") or 0)
    except Exception:
        return 0


def set_round(node, value):
    record = load_state(node)
    record["round"] = int(value)
    save_state(node, record)


def _display_width(text):
    """一段文字的**显示列宽**：中日韩全角字符占 2 列，其余占 1 列。"""
    width = 0
    for ch in text:
        width += 2 if unicodedata.east_asian_width(ch) in ("W", "F") else 1
    return width


def _fit_one_line(text, cols=HISTORY_COLS):
    """把一段文字压到**一行**以内，超了结尾加省略号。

    ★ 为什么要压：节点注释框很窄（从用户截图里量出来大约 40 列宽），一条长
    历史会**折行**——3 条历史变成 6 行，注释框里一半是废话。用户 2026-09-29
    的原话：「前三条历史就不要换行，这样的话能节省空间，让这个空间更紧凑一点」。
    ★ 这是**只影响给人看的注释**：模型看到的内容一个字没少，完整过程仍在面板的
    「过程」里。
    """
    if _display_width(text) <= cols:
        return text
    out = []
    used = 0
    for ch in text:
        w = 2 if unicodedata.east_asian_width(ch) in ("W", "F") else 1
        if used + w > cols - 1:          # 留一列给省略号
            break
        out.append(ch)
        used += w
    return "".join(out) + "…"


def _format_activity(items):
    """历史条目渲染成一行人话。连着做了几次同样的事就带个 ×N。"""
    out = []
    for item in items:
        if isinstance(item, dict):
            text = str(item.get("t") or "")
            count = int(item.get("n") or 1)
        else:
            text, count = str(item), 1
        if not text:
            continue
        out.append("%s ×%d" % (text, count) if count > 1 else text)
    return out


def activity_of(node):
    """注释里那几条历史（已完成动作，最近的在最后）。"""
    return _format_activity(load_state(node).get("activity") or [])


def clear_activity(node):
    record = load_state(node)
    record["activity"] = []
    save_state(node, record)


def push_activity(node, text, keep=ACTIVITY_KEEP):
    """记一条「刚干完的事」。

    ★ 只记**动作**，不记「思考中」——用户明确要求过：那三行要看的是
    「它刚才在干嘛」（有没有看画面、有没有查节点、是不是在写网络），
    而"思考中"是**实时状态**，只该出现在最后一行。调用方（runner）
    只在工具真的跑完之后调这里，所以"思考中"结构上不可能混进来。

    连着做同样的事不占多行，合并成一条并计数（`×3`）——否则模型反复
    看现状的时候，三行历史会全是同一句话，等于没有信息。
    """
    record = load_state(node)
    items = record.get("activity") or []
    text = (text or "").strip()
    if text:
        last = items[-1] if items else None
        if isinstance(last, dict) and last.get("t") == text:
            last["n"] = int(last.get("n") or 1) + 1
        elif last == text:
            items[-1] = {"t": text, "n": 2}
        else:
            items.append({"t": text, "n": 1})
    del items[:-keep]
    record["activity"] = items
    save_state(node, record)


def _comment_text(state, text, full, error, activity=None):
    """节点下方显示的那段话。这是用户唯一会看的地方，要写得能读懂。

    运行中（working）是 **4 行**：上面最多 3 行是**历史**（刚干完的真事），
    最后一行是**实时状态**。用户的原话：「最后一行显示它实时的状态，
    然后第 1~3 行应该是它的这个历史」。

    其余状态（完成/失败/空闲）不显示历史——那时候用户要看的是**结果**
    和报错，历史已经在面板里了。
    """
    label = LABEL_OF.get(state, state)
    head = "%s %s" % (_ICONS.get(state, "·"), text or label)

    lines = []
    if state == "working":
        # ★ 必须过 `_format_activity`。历史在 userData 里存的是
        #   `[{"t": 文本, "n": 次数}]`，直接格式化会把字典 repr 打到注释上。
        # ★ 每条**压成一行**（`_fit_one_line`）——长条目折行会把 3 行历史变成
        #   6 行，注释框里一半是废话。用户 2026-09-29 明确要求。
        lines.extend("· %s" % _fit_one_line(item)
                     for item in _format_activity(activity or []))
    lines.append("[%s] %s" % (label, head))

    body = (full or "").strip()
    if body and body != (text or "").strip():
        lines.append(body)

    if error:
        # 只留前几行，注释框不是日志窗口
        trimmed = [line for line in error.strip().splitlines() if line.strip()]
        lines.append("—" * 12)
        lines.extend(trimmed[:4])
        if len(trimmed) > 4:
            lines.append("…（完整信息见面板）")

    return "\n".join(lines)


def set_status(node, state, text="", detail=None, error=None, full=None):
    """更新状态。

    你看到的是**节点注释**（网络编辑器里节点下方那段字）+ 节点颜色；
    机器读的是 userData。参数面板一个都不加。
    """
    import hou

    if state not in STATE_TOKENS:
        state = "idle"

    record = load_state(node)
    record["state"] = state
    if text:
        record["text"] = text
    if detail is not None:
        record["detail"] = detail
    if error is not None:
        record["error"] = error
    elif state != "failed":
        record["error"] = ""
    save_state(node, record)

    try:
        node.setComment(_comment_text(state, text, full, record.get("error"),
                                      activity=record.get("activity")))
        node.setGenericFlag(hou.nodeFlag.DisplayComment, True)
    except Exception:
        pass

    try:
        node.setColor(hou_color(state))
    except Exception:
        pass


def hou_color(state):
    import hou

    rgb = _COLORS.get(state, _COLORS["idle"])
    return hou.Color(rgb)


def prompt_text(node):
    parm = node.parm(PROMPT)
    if parm is None:
        return ""
    try:
        return parm.evalAsString()
    except Exception:
        return ""


def _string_parm(node, name):
    parm = node.parm(name)
    if parm is None:
        return ""
    try:
        return parm.evalAsString()
    except Exception:
        return ""


def feedback_text(node):
    return _string_parm(node, FEEDBACK)


def clear_feedback(node):
    parm = node.parm(FEEDBACK)
    if parm is None:
        return
    try:
        parm.set("")
    except Exception:
        pass


def _say(message, error=False):
    """状态栏一句话。不弹窗——建个节点就弹个窗很烦。"""
    import hou

    try:
        hou.ui.setStatusMessage(
            message,
            severity=(hou.severityType.Error if error
                      else hou.severityType.Message))
    except Exception:
        pass


def depth_of(node):
    """这个节点选的思考深度：fast / standard / deep。"""
    from . import config as config_mod

    parm = node.parm(DEPTH)
    if parm is None:
        return config_mod.DEFAULT_DEPTH
    try:
        value = parm.evalAsString()
    except Exception:
        return config_mod.DEFAULT_DEPTH
    if value not in config_mod.DEPTH_PRESETS:
        return config_mod.DEFAULT_DEPTH
    return value


# --------------------------------------------------------------------------
# 备用参数
# --------------------------------------------------------------------------

def _callback_script():
    root = tool_root()
    return (
        "import sys\n"
        "_p = r'%s'\n"
        "if _p not in sys.path:\n"
        "    sys.path.insert(0, _p)\n"
        "import houdini_ai.node as _ai_node\n"
        "_ai_node.on_parm_callback(kwargs)\n" % root
    )


def _editor_parm(name, label, help_text, lines="3-10"):
    """一个**大的多行文本框**。

    Houdini 没有「多行字符串参数」这个类型——它是靠参数**标签**实现的：

        editor=1          开文本框
        editorlines=3-10  最少 3 行、最多撑到 10 行
        （不带 editorlang）纯文本；带了会按对应语言做语法高亮，
                          中文描述不需要，反而会花

    实测 Houdini 自己有一批参数就是这么标的（Python SOP 的代码框是
    editor=1 / editorlang=python / editorlines=20-50；另有 40 多个是纯文本的）。
    写成普通 StringParmTemplate 的话就是一个窄窄的单行输入框，中文描述根本写不下。
    """
    import hou

    template = hou.StringParmTemplate(name, label, 1, default_value=("",))
    template.setTags({"editor": "1", "editorlines": lines})
    template.setHelp(help_text)
    return template


def _parm_templates():
    import hou

    from . import config as config_mod

    prompt = _editor_parm(
        PROMPT, "描述",
        "用中文描述你想要什么。写完点下面的「开始构建」。\n"
        "构建过程、结果摘要和报错都显示在节点下方的注释里。")

    feedback = _editor_parm(
        FEEDBACK, "修改意见",
        "对结果不满意就写在这里，点「发送修改意见」。\n"
        "比如「再高一点，顶面加点噪声」。发送之后这里会被清空。")

    # 思考深度：开工前选。它**只管一件事**——当成 `thinking` 参数发给网关的
    # 思考预算。跑几步、什么时候收工，全由模型自己决定，这个菜单一步都不管。
    tokens = tuple(config_mod.DEPTH_ORDER)
    labels = tuple(config_mod.depth_preset(k)["label"] for k in tokens)
    default_index = tokens.index(config_mod.DEFAULT_DEPTH)
    depth = hou.MenuParmTemplate(
        DEPTH, "思考深度", tokens,
        menu_labels=labels,
        default_value=default_index,
    )
    depth.setHelp("\n\n".join(
        "【%s】%s" % (config_mod.depth_preset(k)["label"],
                     config_mod.depth_preset(k)["help"])
        for k in config_mod.DEPTH_ORDER))

    build = hou.ButtonParmTemplate(
        BUILD, "开始构建",
        script_callback=_callback_script(),
        script_callback_language=hou.scriptLanguage.Python,
    )
    build.setHelp("按「描述」构建。已经有内容时按描述重做整张网络。")

    send = hou.ButtonParmTemplate(
        SEND, "发送修改意见",
        script_callback=_callback_script(),
        script_callback_language=hou.scriptLanguage.Python,
    )
    send.setHelp("把「修改意见」里的内容作为下一轮发给 AI，"
                 "它会先看容器现状再决定怎么改。")

    stop = hou.ButtonParmTemplate(
        STOP, "停止",
        script_callback=_callback_script(),
        script_callback_language=hou.scriptLanguage.Python,
    )
    stop.setHelp("中断正在跑的这一轮。已经建好的东西会留着。")

    # 三个按钮排成一行：setJoinWithNext 让本参数和**下一个**参数显示在同一行。
    # 只给前两个设，最后一个不设——否则会把再下一个参数也拽上同一行。
    build.setJoinWithNext(True)
    send.setJoinWithNext(True)

    return [prompt, feedback, depth, build, send, stop]


def _migrate_old_parms(node):
    """把老版本做成参数的状态字段搬进 userData，然后把那些参数摘掉。"""
    record = load_state(node)
    changed = False
    for old, key in (("ai_state", "state"), ("ai_status", "text"),
                     ("ai_detail", "detail"), ("ai_error", "error"),
                     ("ai_round", "round")):
        parm = node.parm(old)
        if parm is None:
            continue
        try:
            if key == "round":
                record[key] = int(parm.eval())
            else:
                value = parm.evalAsString()
                if value and not record.get(key):
                    record[key] = value
        except Exception:
            pass
        changed = True

    if changed:
        if record.get("state") not in STATE_TOKENS:
            record["state"] = "idle"
        save_state(node, record)

    group = node.parmTemplateGroup()
    removed = False
    for name in DEPRECATED_PARMS:
        template = group.find(name)
        if template is not None:
            group.remove(template)
            removed = True
    if removed:
        try:
            node.setParmTemplateGroup(group)
        except Exception:
            pass
    return changed or removed


def ensure_parms(node):
    """把缺的参数补上、把过时的参数摘掉。

    用于「把已有的 Subnet 变成 AI 节点」，也用于把早期版本建的节点升级过来。
    """
    _migrate_old_parms(node)

    missing = [t for t in _parm_templates() if node.parm(t.name()) is None]
    if not missing:
        return False

    group = node.parmTemplateGroup()
    for template in missing:
        group.append(template)
    node.setParmTemplateGroup(group)
    return True


def repair_callback(node):
    """回调脚本里嵌了工具目录的绝对路径。工具挪了位置就重写一遍。"""
    import hou

    script = _callback_script()
    group = node.parmTemplateGroup()
    changed = False
    for name in (BUILD, SEND, STOP):
        parm = node.parm(name)
        if parm is None:
            continue
        template = parm.parmTemplate()
        if template.scriptCallback() != script:
            template.setScriptCallbackLanguage(hou.scriptLanguage.Python)
            template.setScriptCallback(script)
            group.replace(template)
            changed = True
    if changed:
        node.setParmTemplateGroup(group)
    return changed


def make_ai_node(parent, name=None, prompt=""):
    """在 parent 里建一个装好全套参数的 AI 节点（Subnet SOP）。

    刻意**不弹任何对话框**——建个节点就弹窗很烦。建完的状态写在节点注释里。
    """
    import hou

    node = parent.createNode("subnet", name or "AI")
    ensure_parms(node)
    repair_callback(node)
    if prompt:
        node.parm(PROMPT).set(prompt)
    set_status(node, "idle", "等你写描述")
    try:
        node.setGenericFlag(hou.nodeFlag.DisplayComment, True)
    except Exception:
        pass
    return node


# --------------------------------------------------------------------------
# 参数回调入口
# --------------------------------------------------------------------------

def on_parm_callback(kwargs):
    """备用参数的按钮回调。"""
    node = kwargs.get("node")
    parm = kwargs.get("parm")
    if node is None and parm is not None:
        try:
            node = parm.node()
        except Exception:
            node = None
    if node is None:
        return

    name = ""
    if parm is not None:
        try:
            name = parm.name()
        except Exception:
            name = ""

    _handle_button(node, name)


def _handle_button(node, parm_name):
    from . import runner

    if parm_name == BUILD:
        runner.request_build(node)
    elif parm_name == SEND:
        text = feedback_text(node).strip()
        if not text:
            _say("「修改意见」是空的，先写点东西再发送。", error=True)
            return
        # 先清空再提交：不然你手一抖点两下会重复发同一句。
        clear_feedback(node)
        runner.request_followup(node, text)
    elif parm_name == STOP:
        runner.request_stop(node)
