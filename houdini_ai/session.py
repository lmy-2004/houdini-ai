# -*- coding: utf-8 -*-
"""每个 AI 节点的对话状态。

存在节点的 userData 里——**跟着 .hip 走、界面上不显示**。
这样你把 .hip 拷到别的机器上，AI 节点的历史和上下文都还在，
不需要额外的边车文件。

历史不能无限长，否则每轮的上下文会越滚越大、钱也越烧越多。策略是：
最近 max_full_rounds 轮保留完整的工具调用记录（模型需要看到自己查过什么），
更早的轮次压缩成「我要求了什么 / 我做了什么」两行。
"""

import json

USER_DATA_KEY = "houdini_ai_session"
VERSION = 1

# ★★ 这里曾经有个 `MAX_TOOL_RESULT_CHARS = 2000`：工具返回带进下一轮时
# 从 2000 字符处砍断。**拆掉了**（用户 2026-09-29 定：「不要截断任何内容，
# 只在 API 上控制思考强度」）。
# 砍断的坏处是实打实的：模型当时看到的是一份**残缺的 JSON**，而它**没有
# 第二次机会**去要剩下那半——只能猜，猜错被拒，白烧一个回合。真相完整比
# 省下那点上下文值钱得多。
# 控制上下文膨胀的正当手段是**压缩**（见下面 `MAX_FULL_ROUNDS`）和把工具
# 返回设计得更有信息效率，**不是偷偷剪短**。

# 保留几轮的完整工具记录。★ 注意这是**压缩**不是截断：信息还在，只是旧轮次
# 换成"我要求了什么/结果是什么"两行。删掉它上下文会随轮数线性膨胀。
MAX_FULL_ROUNDS = 2


def fresh():
    return {"version": VERSION, "rounds": [], "snapshots": []}


def load(node):
    try:
        raw = node.userData(USER_DATA_KEY)
    except Exception:
        return fresh()
    if not raw:
        return fresh()
    try:
        data = json.loads(raw)
    except ValueError:
        return fresh()
    if not isinstance(data, dict):
        return fresh()
    data.setdefault("version", VERSION)
    data.setdefault("rounds", [])
    data.setdefault("snapshots", [])
    return data


def save(node, data):
    try:
        prune_messages(data)
        node.setUserData(USER_DATA_KEY, json.dumps(data, ensure_ascii=False))
        return True
    except Exception:
        return False


def prune_messages(data, max_full_rounds=MAX_FULL_ROUNDS):
    """把**旧轮次**的完整消息记录丢掉。返回清掉了几个轮次。

    ★★ 这**不改变模型看到的任何东西**，是纯赚的一刀：
    `api_messages` 本来就把更早的轮次压成「我要求了什么 / 结果是什么」两行，
    **从不读它们的 `messages`**。所以丢掉的是一份**永远不会被发出去**的副本。

    为什么值得做：实测每轮完整记录约 **48 KB**，而 `save` 会把它写进节点
    userData（跟着 .hip 走）。20 轮就是 1 MB，而且只增不减。

    ⚠️ **耦合点**：这里用的界和 `api_messages` 用的是同一个
    `MAX_FULL_ROUNDS`——**必须一致**。存得比发得少，就会静默少给模型几轮；
    存得比发得多，就白占地方。
    万一以后有人把常量**调大**，已经存过盘的旧轮次是补不回来的；这种情况由
    `api_messages` 里的兜底接住（没有 messages 就退回两行摘要，
    **绝不静默少给一轮**）。
    """
    rounds = data.get("rounds") or []
    cutoff = max(0, len(rounds) - max_full_rounds)
    dropped = 0
    for record in rounds[:cutoff]:
        if record.get("messages"):
            record["messages"] = []
            dropped += 1
    return dropped


def clear(node):
    try:
        node.destroyUserData(USER_DATA_KEY)
    except Exception:
        pass


def add_round(data, kind, user_text):
    """开一轮。返回这一轮的记录，调用方往里填 messages / summary。"""
    record = {
        "n": len(data["rounds"]) + 1,
        "kind": kind,
        "user": user_text,
        "messages": [],
        "summary": "",
        "ok": None,
        "turns": 0,
    }
    data["rounds"].append(record)
    return record


def add_snapshot(data, spec, round_number, label=""):
    """存一份网络快照。

    round_number 是**即将开始的那一轮**的编号，所以语义是「第 N 轮动手之前
    容器长什么样」。回退到第 N 轮 = 撤销第 N 轮及其之后的全部改动。
    """
    data.setdefault("snapshots", []).append({
        "round": round_number,
        "label": label or ("第 %d 轮之前" % round_number),
        "spec": spec,
    })


def snapshot_for_round(data, round_number):
    for snap in reversed(data.get("snapshots", [])):
        if snap.get("round") == round_number:
            return snap
    return None


def trim_tool_results(messages):
    """历史回放：**原样返回**，一个字都不改。

    ★ 以前这里做的是"把工具返回砍到 2000 字符"。拆掉了——见文件顶部那段
    说明。函数名保留是因为它已经被测试和语义引用（"回放时要过一道处理"），
    但结构上它现在是个直通。
    """
    return list(messages or [])


def _digest(record):
    """把一轮压成两行：我要求了什么 / 结果是什么。"""
    out = []
    asked = (record.get("user") or "").strip()
    did = (record.get("summary") or "").strip()
    if asked:
        out.append({"role": "user", "content": "第 %d 轮我的要求：%s"
                    % (record["n"], asked)})
    if did:
        out.append({"role": "assistant",
                    "content": "第 %d 轮的结果：%s" % (record["n"], did)})
    return out


def api_messages(data, max_full_rounds=MAX_FULL_ROUNDS):
    """拼出这轮要发给模型的消息列表。

    最近几轮给完整的工具记录；更早的压成两行摘要。
    """
    rounds = data.get("rounds") or []
    if not rounds:
        return []

    cutoff = max(0, len(rounds) - max_full_rounds)
    out = []

    for record in rounds[:cutoff]:
        out.extend(_digest(record))

    for index, record in enumerate(rounds[cutoff:]):
        msgs = record.get("messages")
        # ★★ 这里要分清三种「空」，判错一种就会出真 bug：
        #   ① **正在跑的这一轮**——它此刻 messages 还是空的（我们马上要往里
        #      追加本轮的用户消息）。跳过它是对的。
        #   ② 存盘时被清掉了（`prune_messages` 只留最近几轮）。
        #   ③ 那一轮一条消息都没攒下就结束了（比如网关在第一步就报错）。
        # ②③ **都不能静默少给一轮**——少给一轮的话模型会莫名其妙丢一段上下文，
        # 而且没有任何症状能提示这件事发生过。所以要退回两行摘要。
        # （第一版没分这三种，把①也补了一条摘要，于是本轮的用户消息在请求里
        #   出现了两遍。测试当场抓到的。）
        if not msgs and (cutoff + index) < len(rounds) - 1:
            out.extend(_digest(record))
            continue
        out.extend(trim_tool_results(msgs or []))

    return out


def _esc(text):
    return (text.replace("&", "&amp;").replace("<", "&lt;")
                .replace(">", "&gt;"))


# 说话人标签的颜色。选这两种是因为它们在深色和浅色主题下都还能看——
# 正文**不指定颜色**，让它跟着 Houdini 自己的主题走，这样换主题不会瞎。
SPEAKER_COLORS = {"我": "#7fa8d8", "AI": "#63bd8d"}


def conversation_html(data, body_color=None):
    """对话区的富文本。

    纯文本没法做层级——用户看到的就是「全都是一样白字」。这里给说话人
    上色加粗、给每条之间留出间距。

    ★ `body_color` 为什么要显式传进来：实测只给标签套 `<span>` 是**不够**的，
    Qt 的富文本解析会把那个颜色**带到整段正文**上（渲染出来整个 AI 的回复
    都泛绿）。所以正文得自己包一层 span 把颜色压回去。
    颜色由**调用方从当前 palette 取**（面板那边读 `QPalette.Text`），
    不写死在模块里——Houdini 的主题用户能换，写死的话换到浅色主题就瞎了。
    """
    blocks = []
    for record in data.get("rounds") or []:
        asked = (record.get("user") or "").strip()
        if asked:
            blocks.append(("我", asked))
        said = (record.get("summary") or "").strip()
        if said:
            blocks.append(("AI", said))
        elif record.get("error"):
            blocks.append(("AI", "（这一轮出错了，展开「过程」看原因）"))

    if not blocks:
        return ""

    body_style = ("color:%s;" % body_color) if body_color else ""
    parts = []
    for index, (who, text) in enumerate(blocks):
        color = SPEAKER_COLORS.get(who, "#888888")
        body = _esc(text).replace("\n\n", "<br><br>").replace("\n", "<br>")
        # 每块之间留白；同一个人连着说就少留一点
        top = 0 if index == 0 else (16 if who == "我" else 8)
        parts.append(
            '<p style="margin-top:%dpx; margin-bottom:0px; '
            'line-height:150%%;">'
            '<span style="color:%s; font-weight:600;">%s</span>'
            '&nbsp;&nbsp;<span style="%s">%s</span></p>'
            % (top, color, _esc(who), body_style, body))
    return "".join(parts)


def conversation_text(data):
    """只给人话：你说了什么、AI 的结论是什么。

    面板默认显示的就是这个。每一步工具调用（"找节点类型：tube 命中 6 个"）
    全在 `process_text` 里，默认折叠——那些是排查时才看的，平时只会把
    真正要读的东西淹掉。
    """
    blocks = []
    for record in data.get("rounds") or []:
        asked = (record.get("user") or "").strip()
        if asked:
            blocks.append(("我", asked))
        said = (record.get("summary") or "").strip()
        if said:
            blocks.append(("AI", said))
        elif record.get("error"):
            blocks.append(("AI", "（这一轮出错了，展开「过程」看原因）"))
    if not blocks:
        return ""
    width = max(len(who) for who, _ in blocks)
    return "\n\n".join("%s  %s" % (who.ljust(width), text.replace("\n", "\n" + " " * (width + 2)))
                       for who, text in blocks)


def process_text(data):
    """过程：每一步工具调用 + 报错原文。给「显示过程」用的。"""
    lines = []
    for record in data.get("rounds") or []:
        head = "── 第 %d 轮" % record.get("n", 0)
        if record.get("kind") == "followup":
            head += "（修改意见）"
        lines.append(head)
        for entry in record.get("trace") or []:
            lines.append("  · %s" % entry)
        if record.get("error"):
            lines.append("  报错：%s" % record["error"])
        lines.append("")
    return "\n".join(lines).strip() or "（还没有跑过）"


def transcript(data):
    """给人看的对话记录（面板里显示用）。"""
    lines = []
    for record in data.get("rounds") or []:
        head = "第 %d 轮" % record["n"]
        if record.get("kind") == "followup":
            head += "（修改意见）"
        lines.append("── %s ──" % head)
        lines.append("我：%s" % (record.get("user") or "").strip())
        for entry in record.get("trace") or []:
            lines.append("  · %s" % entry)
        if record.get("summary"):
            lines.append("AI：%s" % record["summary"].strip())
        if record.get("error"):
            lines.append("出错：%s" % record["error"])
        lines.append("")
    return "\n".join(lines).strip()
