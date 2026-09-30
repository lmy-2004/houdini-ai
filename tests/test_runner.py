# -*- coding: utf-8 -*-
"""端到端测试：用一个假网关跑完整的一轮。

这是最有价值的一份测试，因为它**不花钱**——把网关换成回放脚本，
就能反复验证「模型调工具 → 我们执行 → 结果回灌 → 它接着调」整个循环，
包括模型犯错被拒、然后自己改对的那条路径。

    "D:/ProgramFiles/Houdini22/bin/hython.exe" tests/test_runner.py
"""

import base64
import threading
import time

import _harness as H

H.section("搭场景")
import hou
import houdini_ai.gateway as gateway_mod
from houdini_ai import builder, node as node_mod, runner, session

_counter = [0]


def tu(name, args):
    """造一个 tool_use 响应。"""
    _counter[0] += 1
    return {
        "content": [{"type": "tool_use", "id": "call_%d" % _counter[0],
                     "name": name, "input": args}],
        "stop_reason": "tool_use",
    }


def say(text):
    return {"content": [{"type": "text", "text": text}], "stop_reason": "end_turn"}


class FakeGateway(object):
    """按脚本回放的假网关。连 thinking 参数一起记下来，方便断言。

    `deltas` 是一个 `[(kind, piece), …]` 列表，用来冒充流式：给了就在返回
    响应之前，逐段喂给 `on_delta`——这样"实时状态显示它在想什么"这条
    路径在离线测试里也能走通（而且不花钱）。
    """

    def __init__(self, script, deltas=None):
        self.script = list(script)
        self.deltas = list(deltas or [])
        self.seen = []
        self.streamed = []

    def ready(self):
        return True, ""

    def messages(self, system, messages, tools=None, max_tokens=None,
                 thinking=None, on_delta=None):
        self.seen.append({"system": system, "messages": list(messages),
                          "thinking": thinking, "tools": tools,
                          "stream": on_delta is not None})
        if on_delta is not None:
            self.streamed.append(True)
            for kind, piece in self.deltas:
                on_delta(kind, piece)
        if not self.script:
            return say("（脚本用完了）")
        return self.script.pop(0)


def run_once(node, script, text="做一个方盒然后挤出", kind="build"):
    fake = FakeGateway(script)
    real = gateway_mod.Gateway
    gateway_mod.Gateway = lambda cfg: fake
    try:
        runner._run_round(node.path(), kind, text, threading.Event())
    finally:
        gateway_mod.Gateway = real
    return fake


obj = hou.node("/obj")
for old in list(obj.children()):
    if old.name().startswith("R_"):
        old.destroy()
geo = obj.createNode("geo", "R_geo")
geo_child = geo
ai = node_mod.make_ai_node(geo_child, "ai1", prompt="做一个方盒然后挤出")

H.section("AI 节点本身")
H.check("建出来了", ai is not None and ai.type().name() == "subnet")
H.check("被识别成 AI 节点", node_mod.is_ai_node(ai))
H.check("全场景枚举找得到它", ai in node_mod.all_ai_nodes())
H.check("备用参数齐全",
        all(ai.parm(p) is not None for p in node_mod.VISIBLE_PARMS),
        [p for p in node_mod.VISIBLE_PARMS if ai.parm(p) is None])
H.check("工具自己的参数只有那四个（描述/思考深度/开始构建/停止）",
        sorted(p.name() for p in ai.parmTuples()
               if p.name().startswith("ai_")) == sorted(node_mod.VISIBLE_PARMS),
        [p.name() for p in ai.parmTuples() if p.name().startswith("ai_")])
H.check("老版本的状态参数没被留下",
        not any(ai.parm(p) is not None for p in node_mod.DEPRECATED_PARMS),
        [p for p in node_mod.DEPRECATED_PARMS if ai.parm(p) is not None])
H.check("初始状态是空闲", node_mod.current_state(ai) == "idle")
H.check("节点下方显示了状态字", bool(ai.comment()), ai.comment())

H.section("端到端：模型先查再建（正常路径）")
script = [
    tu("list_container", {}),
    tu("search_node_types", {"query": "box"}),
    tu("get_node_type", {"type": "box"}),
    tu("get_node_type", {"type": "polyextrude"}),
    tu("build", {
        "nodes": [
            {"name": "base", "type": "box", "params": {"size": [2, 2, 0.5]}},
            {"name": "twist", "type": "polyextrude",
             "params": {"divs": 10, "translate": [0, 0, 2]}},
        ],
        "connections": [{"from": "base", "to": "twist", "input": 0}],
        "output": "twist",
    }),
    tu("finish", {"summary": "建了一个 2x2x0.5 的方盒，向上挤出 2 个单位。"}),
]
fake = run_once(ai, script)

kids = {c.name(): c for c in ai.children()}
H.check("建出了 base", "base" in kids)
H.check("建出了 twist", "twist" in kids)
geo_now = ai.geometry()
H.check("几何非空", geo_now is not None and geo_now.pointCount() > 0,
        "点数 %s" % (geo_now.pointCount() if geo_now else None))
H.check_eq("最终状态是完成", node_mod.current_state(ai), "done")
H.check("状态字里有摘要",
        "方盒" in node_mod.status_line(ai), node_mod.status_line(ai))
H.check("细节里带上了产出点数",
        "产出" in (node_mod.detail_of(ai) or ""), node_mod.detail_of(ai))
H.check("轮次计数涨到 1", node_mod.round_of(ai) == 1, node_mod.round_of(ai))
H.check("★ 完整说明进了节点注释（用户就看这个）",
        "方盒" in ai.comment(), ai.comment())
H.check("节点颜色变成完成色",
        tuple(round(c, 2) for c in ai.color().rgb()) == (0.30, 0.72, 0.35),
        ai.color().rgb())
H.check("状态没做成参数（参数面板保持干净）",
        ai.parm("ai_status") is None and ai.parm("ai_detail") is None)

H.section("会话被存进节点，跟着 .hip 走")
data = session.load(ai)
H.check("有 1 轮记录", len(data["rounds"]) == 1, len(data["rounds"]))
H.check("记录了用户的原始描述",
        "方盒" in data["rounds"][0]["user"], data["rounds"][0]["user"])
H.check("存了完整消息历史", len(data["rounds"][0]["messages"]) >= 6,
        len(data["rounds"][0]["messages"]))
H.check("有快照可回退", len(data["snapshots"]) >= 1)
H.check("trace 有可读的过程记录",
        any("提交网络" in t for t in data["rounds"][0].get("trace", [])),
        data["rounds"][0].get("trace"))
H.check("工具返回里没有把系统提示词漏进去",
        all("Houdini 里的一个程序化建模助手" not in str(m)
            for m in data["rounds"][0]["messages"]))

H.section("系统提示词确实带上了容器路径")
H.check("系统提示词里提到容器",
        any("R_geo/ai1" in entry["system"] for entry in fake.seen),
        fake.seen[0]["system"][-200:] if fake.seen else "")

H.section("端到端：模型犯错 → 被拒 → 自己改对（关键路径）")
geo2 = obj.createNode("geo", "R_geo2")
ai2 = node_mod.make_ai_node(geo2, "ai2", prompt="做个球")
script = [
    tu("list_container", {}),
    # 第一次提交：编了个不存在的节点类型，还写了个不存在的参数
    tu("build", {
        "nodes": [
            {"name": "s", "type": "sphereinator", "params": {"radius": 2}},
            {"name": "noise", "type": "attribnoise", "params": {"nosuchparm": 1}},
        ],
    }),
    # 收到拒绝后改对
    tu("get_node_type", {"type": "sphere"}),
    tu("build", {
        "nodes": [{"name": "s", "type": "sphere", "params": {"radx": 2, "rady": 2}}],
        "output": "s",
    }),
    tu("finish", {"summary": "做了个半径 2 的球。"}),
]
fake2 = run_once(ai2, script, text="做个球")

kids2 = {c.name(): c for c in ai2.children()}
H.check("最终建出了球", "s" in kids2)
H.check_eq("球的类型正确", kids2["s"].type().name().split("::")[0], "sphere")
H.check_eq("半径落对了", round(kids2["s"].parm("radx").eval(), 3), 2.0)
geo2_now = ai2.geometry()
H.check("球有几何", geo2_now is not None and geo2_now.pointCount() > 0)
H.check_eq("最终状态是完成", node_mod.current_state(ai2), "done")

trace2 = session.load(ai2)["rounds"][0].get("trace", [])
H.check("过程记录里有被拒的那一次",
        any("被拒" in t for t in trace2), trace2)
H.check("被拒原因点明了类型不存在",
        any("sphereinator" in t for t in trace2), trace2)

# 第二次提交前，模型确实看到了我们的拒绝理由
second_call_messages = fake2.seen[2]["messages"] if len(fake2.seen) > 2 else []
blob = str(second_call_messages)
H.check("拒绝理由被回灌给了模型",
        "sphereinator" in blob and "不存在" in blob,
        blob[-500:] if blob else "(没有第三次调用)")
H.check("假参数也被一起报了出来",
        "nosuchparm" in blob, blob[-500:] if blob else "")

H.section("第二轮对话（提修改意见）")
script = [
    tu("list_container", {}),
    tu("update_parms", {"ops": [{"node": "s", "parm": "radx", "value": 5},
                                {"node": "s", "parm": "rady", "value": 5}]}),
    tu("finish", {"summary": "把球调到半径 5。"}),
]
run_once(ai2, script, text="再大一点", kind="followup")
kids2 = {c.name(): c for c in ai2.children()}
H.check_eq("第二轮真的改到了参数",
           round(kids2["s"].parm("radx").eval(), 3), 5.0)
data2 = session.load(ai2)
H.check("现在有 2 轮记录", len(data2["rounds"]) == 2, len(data2["rounds"]))
H.check_eq("第二轮被标成修改意见",
           data2["rounds"][1]["kind"], "followup")
H.check_eq("轮次计数涨到 2", node_mod.round_of(ai2), 2)

api_msgs = session.api_messages(data2)
H.check("发给模型的历史里带了第一轮的上下文",
        any("做个球" in str(m) for m in api_msgs), api_msgs[:2])

H.section("老轮次会被压缩，上下文不会无限膨胀")
data3 = session.fresh()
for i in range(6):
    rec = session.add_round(data3, "build", "第 %d 次要求" % i)
    rec["messages"] = [{"role": "user", "content": "很长的一串工具记录" * 50}]
    rec["summary"] = "第 %d 次做完了" % i
api3 = session.api_messages(data3)
H.check("6 轮压成了有限条消息", len(api3) < 12, len(api3))
H.check("最早的轮次被压成摘要",
        any("第 0 次要求" in str(m) and "很长的一串" not in str(m)
            for m in api3), api3[:2])

H.section("★★ 存盘只留最近几轮的完整记录（模型看到的不变，.hip 不再长）")
# 实测：一轮完整记录约 48KB，而 save 会把它写进节点 userData（跟着 .hip 走），
# 只增不减。剪掉的是一份**永远不会被发出去**的副本——api_messages 本来就把
# 更早的轮次压成两行，从不读它们的 messages。
import copy as _copy
import json as _json3

_BIG = "很长的工具记录 " * 20          # 模拟一张大参数表

data4 = session.fresh()
for i in range(8):
    rec = session.add_round(data4, "build", "第 %d 次要求" % i)
    rec["messages"] = [{"role": "user", "content": "第 %d 次要求" % i},
                       {"role": "assistant", "content": _BIG * 400}]
    rec["summary"] = "第 %d 次做完了" % i

# ★ 核心断言：prune 前后，**发给模型的消息逐字节相同**
_unpruned = _copy.deepcopy(data4)
_dropped = session.prune_messages(data4)
H.check("确实清掉了旧轮次（%d 个）" % _dropped, _dropped == 8 - session.MAX_FULL_ROUNDS,
        _dropped)
H.check("★ 旧轮次的 messages 已经清空",
        all(not r["messages"] for r in data4["rounds"][:_dropped]))
H.check("★ 最近几轮的完整记录**留着**（那是要发给模型的）",
        all(r["messages"] for r in data4["rounds"][_dropped:]))
H.check_eq("★★ prune 前后发给模型的消息**逐字节相同**",
           _json3.dumps(session.api_messages(data4), ensure_ascii=False),
           _json3.dumps(session.api_messages(_unpruned), ensure_ascii=False))

# ★ 兜底：万一以后把 MAX_FULL_ROUNDS 调大，被清掉的轮次也不能**静默少一轮**
_wide = session.api_messages(data4, max_full_rounds=99)
H.check("★ 调大窗口后每一轮仍有内容（退回两行摘要，不是凭空消失）",
        all(any(("第 %d 次要求" % i) in _json3.dumps(m, ensure_ascii=False)
                for m in _wide) for i in range(8)),
        [i for i in range(8)
         if not any(("第 %d 次要求" % i) in _json3.dumps(m, ensure_ascii=False)
                    for m in _wide)])

# ★ 每一轮都要有贡献（静默丢轮是这类优化最容易出的错）。
# 只查**最后一条之前**的轮次：最后一条是"正在跑的这一轮"，它此刻本来就还没有
# messages——那不是"被丢掉了"，是还没开始。第一版就是在这儿判错的。
_whole = _json3.dumps(session.api_messages(data4), ensure_ascii=False)
H.check("★ 已经结束的每一轮都不会被静默丢掉",
        all(("第 %d 次要求" % i) in _whole for i in range(8)), _whole[:200])
H.check("★ 正在跑的那一轮**不**提前补摘要（补了的话本轮消息会出现两遍）",
        session.api_messages({"rounds": [r for r in data4["rounds"][:1]]}) == [],
        session.api_messages({"rounds": [r for r in data4["rounds"][:1]]}))

H.section("模型只说不做时必须报失败，绝不能假成功")
# 实测：模型会查完参数就开始用文字描述它「打算」怎么建，从头到尾不调 build。
# 早期版本把「没有工具调用」当成做完了，界面显示绿色「完成」而产出是 0 点。
# 这是最坏的一种失败——用户以为成了。
geo_s = obj.createNode("geo", "R_geoS")
ai_s = node_mod.make_ai_node(geo_s, "aiS", prompt="做个东西")
run_once(ai_s, [
    tu("get_node_type", {"type": "box"}),
    say("我打算这样做：先建一个 box，然后接一个 polyextrude，再……"),
], text="做个东西")

H.check("没调 build 就不许报完成",
        node_mod.current_state(ai_s) != "done",
        "状态是 %s / %s" % (node_mod.current_state(ai_s),
                            node_mod.status_line(ai_s)))
H.check_eq("状态是失败", node_mod.current_state(ai_s), "failed")
H.check("状态字说明了真因",
        "没有提交" in node_mod.status_line(ai_s)
        or "没收尾" in node_mod.status_line(ai_s),
        node_mod.status_line(ai_s))
H.check("容器确实是空的（证明它真的没干活）",
        len(ai_s.children()) == 0,
        sorted(c.name() for c in ai_s.children()))

H.section("只说不做 → 结束对话时被提醒 → 动起手来")
geo_t = obj.createNode("geo", "R_geoT")
ai_t = node_mod.make_ai_node(geo_t, "aiT", prompt="做个盒子")
fake_t = run_once(ai_t, [
    say("我打算先建个 box。"),                       # 只说不做，想收工
    tu("build", {"nodes": [{"name": "b", "type": "box"}], "output": "b"}),
    tu("finish", {"summary": "做了个 box。"}),
], text="做个盒子")
H.check_eq("被提醒之后成功完成", node_mod.current_state(ai_t), "done")
H.check("提醒的那句话确实发出去了",
        any("还没有往容器里放任何节点" in str(m)
            for m in fake_t.seen[1]["messages"]),
        str(fake_t.seen[1]["messages"])[-300:])

H.section("★ 节点参数面板的布局：两个大文本框 + 三个按钮排一行")
geo_u = obj.createNode("geo", "R_geoU")
ai_u = node_mod.make_ai_node(geo_u, "aiU", prompt="x")

order = [t.name() for t in ai_u.parmTemplateGroup().parmTemplates()
         if t.name().startswith("ai_")]
H.check("参数顺序：描述 / 修改意见 / 思考深度 / 三个按钮",
        order == [node_mod.PROMPT, node_mod.FEEDBACK, node_mod.DEPTH,
                  node_mod.BUILD, node_mod.SEND, node_mod.STOP],
        order)

# 两个文本框要做成大的：靠 editor 标签（Houdini 没有多行字符串参数类型）
for name, label in ((node_mod.PROMPT, "描述"),
                    (node_mod.FEEDBACK, "修改意见")):
    tpl = ai_u.parmTuple(name).parmTemplate()
    tags = dict(tpl.tags())
    H.check("%s 带 editor 标签（变成多行大框）" % label,
            tags.get("editor") == "1", tags)
    H.check("%s 带 editorlines（框更大了）" % label,
            "editorlines" in tags, tags)
    H.check("%s 是纯文本（不带语法高亮）" % label,
            "editorlang" not in tags, tags)
    H.check("%s 标签是中文" % label, tpl.label() == label, tpl.label())

# 三个按钮排一行：setJoinWithNext 让本参数和下一个显示在同一行
group = ai_u.parmTemplateGroup()
joined = {t.name(): t.joinWithNext()
          for t in group.parmTemplates() if t.name().startswith("ai_")
          and t.name() in (node_mod.BUILD, node_mod.SEND, node_mod.STOP)}
H.check("★ 开始构建 和下一个同行", joined.get(node_mod.BUILD) is True, joined)
H.check("★ 发送修改意见 和下一个同行", joined.get(node_mod.SEND) is True, joined)
H.check("★ 停止 不拽别人（否则会连累下一个参数）",
        not joined.get(node_mod.STOP), joined)

H.check("三个按钮都有回调脚本",
        all("houdini_ai" in (
            ai_u.parm(n).parmTemplate().scriptCallback() or "")
            for n in (node_mod.BUILD, node_mod.SEND, node_mod.STOP)))

H.section("★ 节点上的「发送修改意见」真的能用")
H.check("初始是空的", node_mod.feedback_text(ai_u) == "")
ai_u.parm(node_mod.FEEDBACK).set("再高一点，加点噪声")
H.check("写进去读得出来",
        node_mod.feedback_text(ai_u) == "再高一点，加点噪声")

# 空内容点发送：不该提交任何东西，也不该清空（本来就是空的）
ai_u.parm(node_mod.FEEDBACK).set("   ")
submitted = []
_real_followup = runner.request_followup
runner.request_followup = lambda node, text: submitted.append(text)
try:
    node_mod._handle_button(ai_u, node_mod.SEND)
    H.check("空内容点发送不提交", not submitted, submitted)
    H.check("空内容也不会崩", True)

    ai_u.parm(node_mod.FEEDBACK).set("再高一点")
    node_mod._handle_button(ai_u, node_mod.SEND)
    H.check("★ 有内容时提交了修改意见", submitted == ["再高一点"], submitted)
    H.check("★ 提交后输入框被清空（防止手抖重复发）",
            node_mod.feedback_text(ai_u) == "", node_mod.feedback_text(ai_u))

    ai_u.parm(node_mod.FEEDBACK).set("换个方向")
    node_mod._handle_button(ai_u, node_mod.BUILD)
finally:
    runner.request_followup = _real_followup

H.section("★★ 只有 thinking 块的回复不能让历史里出现空 content")
# 实测踩过：模型某一步只回了 thinking 块（没有 text 也没有 tool_use），
# 去完 thinking 就是空列表，被当成 assistant 消息塞进历史，
# 下一次请求直接被网关拒掉：
#   HTTP 400: messages.N: all messages must have non-empty content
def thinking_only(text="让我想想…"):
    return {"content": [{"type": "thinking", "thinking": text,
                         "signature": "sig"}],
            "stop_reason": "end_turn"}


geo_e = obj.createNode("geo", "R_geoE")
ai_e = node_mod.make_ai_node(geo_e, "aiE", prompt="做个盒子")
fake_e = run_once(ai_e, [
    thinking_only(),
    thinking_only("还是没想好"),
    tu("build", {"nodes": [{"name": "b", "type": "box"}], "output": "b"}),
    tu("finish", {"summary": "做了个盒子。"}),
], text="做个盒子")

H.check_eq("有 thinking-only 回复也能正常跑完",
           node_mod.current_state(ai_e), "done")
data_e = session.load(ai_e)
history = data_e["rounds"][0]["messages"]
empty = [i for i, m in enumerate(history)
         if not m.get("content")]
H.check("★ 历史里没有任何空 content 的消息", not empty, empty)
H.check("thinking 块没被塞进历史",
        not any(b.get("type") == "thinking"
                for m in history if isinstance(m.get("content"), list)
                for b in m["content"]))
H.check("补了非空的占位文本",
        any("没有输出内容" in str(m) for m in history))

# 真发给网关的那几轮，逐条检查非空（这才是会被拒的地方）
sent_empty = []
for entry in fake_e.seen:
    for i, m in enumerate(entry["messages"]):
        if not m.get("content"):
            sent_empty.append((i, m.get("role")))
H.check("★ 实际发出去的每一轮请求都没有空 content",
        not sent_empty, sent_empty)

H.section("★★ 思考深度：由用户在节点上选，不是一个写死的催命数字")
from houdini_ai import config as config_mod

geo_d = obj.createNode("geo", "R_geoD")
ai_d = node_mod.make_ai_node(geo_d, "aiD", prompt="做个盒子")

H.check("新节点默认是标准深度",
        node_mod.depth_of(ai_d) == config_mod.DEFAULT_DEPTH,
        node_mod.depth_of(ai_d))
H.check("深度参数是个菜单",
        ai_d.parm(node_mod.DEPTH) is not None
        and ai_d.parm(node_mod.DEPTH).parmTemplate().menuItems()
        == tuple(config_mod.DEPTH_ORDER),
        ai_d.parm(node_mod.DEPTH).parmTemplate().menuItems())

# 快模式：不思考
ai_d.parm(node_mod.DEPTH).set("fast")
fake_fast = run_once(ai_d, [
    tu("build", {"nodes": [{"name": "b", "type": "box"}], "output": "b"}),
    tu("finish", {"summary": "盒子"}),
], text="做个盒子")
H.check("快模式把 thinking 设成 disabled",
        fake_fast.seen[0]["thinking"] == {"type": "disabled"},
        fake_fast.seen[0]["thinking"])
H.check("★ 思考深度**不进**系统提示词（它是 API 参数，别搞错层次）",
        "# 补充要求" not in fake_fast.seen[0]["system"],
        fake_fast.seen[0]["system"][-260:])
H.check("★ 提示词里也不该出现「思考深度」「最多 N 步」这类字眼",
        "思考深度" not in fake_fast.seen[0]["system"]
        and "最多 12 步" not in fake_fast.seen[0]["system"])

# 深入模式：思考预算大
geo_d2 = obj.createNode("geo", "R_geoD2")
ai_d2 = node_mod.make_ai_node(geo_d2, "aiD2", prompt="做个盒子")
ai_d2.parm(node_mod.DEPTH).set("deep")
fake_deep = run_once(ai_d2, [
    tu("build", {"nodes": [{"name": "b", "type": "box"}], "output": "b"}),
    tu("finish", {"summary": "盒子"}),
], text="做个盒子")
H.check("深入模式开了 thinking 并给了预算",
        fake_deep.seen[0]["thinking"] == {"type": "enabled",
                                          "budget_tokens": 16000},
        fake_deep.seen[0]["thinking"])
H.check("深入模式同样不把深度写进提示词",
        "# 补充要求" not in fake_deep.seen[0]["system"]
        and "最多 80 步" not in fake_deep.seen[0]["system"],
        fake_deep.seen[0]["system"][-260:])
H.check("★ 系统提示词里只有容器处境这一块可变的",
        "# 本轮容器" in fake_deep.seen[0]["system"])

H.section("★★ 提示词与工具定义不许重复（固定开销每次请求都重发）")
# 2026-09-29 去重前的基线：tools 5075 / system 3615 字符。同一件事在系统提示词
# 和工具 schema 里各说一遍，是**每一次请求**都白花两遍的钱。
# 阈值不是为了卡数字，是为了**防止以后又长回去**——比人肉 review 可靠。
import json as _json2

from houdini_ai import prompt as prompt_mod, schema as schema_mod

_tools_len = len(_json2.dumps(schema_mod.TOOL_DEFS, ensure_ascii=False))
_sys_len = len(prompt_mod.SYSTEM)
# ★ 阈值**放宽过一次**（2026-09-29 晚），不是为了让红变绿——新增的四块内容逐条列在
# 这里，都是用户点名要的，而且**都只加在唯一该在的地方**（没有跨面重复）：
#   ① promote 那节重写：删掉「3-6 个」，换成"每个功能块都要有自己那套精简有效的
#      可调参数" + 正/反面判据 + min/max 如实描述（拿掉做不到的承诺）
#   ② build.promote 字段的 description 补回**用途**（上一轮去重削空了）
# 定量：system 3362 → 3639（+277），tools 4444 → 4531（+87）。
# 迭代那个**动作**没有加进 SYSTEM（那会重复），它只在 followup_message 里。
# ★ 又放宽过一次（2026-09-29 晚）：给 `look` 加**必填的视角枚举**——这是用户
#   点名的要求（「接口应该强制需要一个视角的输入」），enum 的七个取值加说明
#   约 200 字符。定量的历史：4444 → 4531（promote 用途）→ 4731（view 枚举）。
#   两个数都还远低于去重前的 5075 / 3615。
H.check("工具定义没有重新长回去（基线 5075，去重后 4444，现在 4731）",
        _tools_len <= 4850, _tools_len)
H.check("系统提示词没有重新长回去（去重后 3362，加判据后 3639）",
        _sys_len <= 3750, _sys_len)
H.check("固定开销仍然低于去重前的 8690",
        _tools_len + _sys_len < 8600, _tools_len + _sys_len)

# ★ 用户 2026-09-29 明确要求：**提示词里连"几个参数"都不要提**。
H.check("★ 提示词里不再有任何参数个数限制（删掉了「3-6 个」）",
        "3-6" not in prompt_mod.SYSTEM and "3-6" not in _json2.dumps(
            schema_mod.TOOL_DEFS, ensure_ascii=False))
# ★ 判据要**只看 promote 那一段**：整篇 SYSTEM 里出现「容器最多有 4 个输入」
# 是架构事实，跟"参数个数"无关——第一版拿整篇去搜，被这句话误伤了。
_p0 = prompt_mod.SYSTEM.index("**二、把关键参数提升到容器上")
_p1 = prompt_mod.SYSTEM.index("**三、说清楚你做了什么")
_promote_para = prompt_mod.SYSTEM[_p0:_p1]
# 只看**散文**——JSON 例子里的 `min: 0, max: 720` 是字段值，不是"个数"，
# 拿整个段落去搜数字会误伤（第一版就是这么错的）。
_prose = "".join(_promote_para.split("```")[::2])
H.check("★ promote 那段的**散文里一个阿拉伯数字都没有**（连几个都不要提）",
        not any(ch.isdigit() for ch in _prose), _prose[:120])
H.check("★ 也没有换成别的数量说法",
        not any(w in _promote_para
                for w in ("至少", "不超过", "最多", "一两个", "若干个", "几个")) , _promote_para[:120])
H.check("★ 新要求写进去了：每个功能块都要有自己那套精简有效的可调参数",
        "精简但有效" in prompt_mod.SYSTEM
        and "每一件都要有" in prompt_mod.SYSTEM)
H.check("★ 补上了**反面**判据（以前只有正面例子）",
        "都不要提升" in prompt_mod.SYSTEM)
H.check("★ min/max 不再被说成能拦住超范围的值（它只是滑杆范围）",
        "不是硬限制" in prompt_mod.SYSTEM
        and "别让他把值拉到崩掉" not in prompt_mod.SYSTEM)

# ★ 迭代那个**动作**只在 followup_message 里说一次（不重复进系统提示词）
_fu = prompt_mod.followup_message("把塔身再扭曲一点")
H.check("★ 修改意见那轮明确要求过一遍旋钮清单",
        "promote" in _fu and "旋钮" in _fu, _fu)
H.check("★ 而且系统提示词里不重复这段动作（准则归 SYSTEM，动作归这里）",
        "顺带过一遍" not in prompt_mod.SYSTEM)

# ★ build.promote 的字段说明必须说清**用途**（上一轮去重把它削空了）
_promote_desc = ""
for _t in schema_mod.TOOL_DEFS:
    if _t["name"] == "build":
        _promote_desc = _t["input_schema"]["properties"]["promote"]["description"]
H.check("★ build.promote 的说明说清了用途（不只是沿用/撤销的契约）",
        "提升" in _promote_desc and "就能调" in _promote_desc, _promote_desc)
H.check("★ 也说了「给了就必须列全」（不然模型只会填一个新的、把旧的冲掉）",
        "列全" in _promote_desc, _promote_desc)
H.check("★ 但不声明 min_strict/max_strict（会把滑杆范围诱成硬限制、挡合法值）",
        "min_strict" not in _json2.dumps(schema_mod.TOOL_DEFS,
                                         ensure_ascii=False))

# ★ 具体到"哪条规则只留哪一处"：promote 的完整规则（含 JSON 例子）只该出现在
# 系统提示词里，工具 schema 里只剩"字段契约"。
H.check("★ promote 的 JSON 例子只在系统提示词里，没有在工具定义里再来一份",
        '"promote": [' in prompt_mod.SYSTEM
        and '"promote": [' not in _json2.dumps(schema_mod.TOOL_DEFS,
                                               ensure_ascii=False))
H.check("★ 「不用进容器就能调」这句话只出现一次（去重前 system/tools 各一份）",
        (prompt_mod.SYSTEM + _json2.dumps(schema_mod.TOOL_DEFS,
                                          ensure_ascii=False)).count("不用进容器") == 1)
H.check("★ 那个和「三个动作·三」逐字重复的「说话方式」段落整个没了",
        "说话方式" not in prompt_mod.SYSTEM)
H.check("★ 「多分量两种写法」只在工具 schema 的字段契约里说一次",
        prompt_mod.SYSTEM.count('"sizex"') == 0)

H.section("★★ 不中途干涉：跑多少步都不插话")
# 用户 2026-09-28 明确要求：「你只需要调整他的思考模式就可以了，不需要中途去
# 干涉他的任何的行为。然后他最后结束对话的时候，如果说他并没有摆放任何的节点，
# 并且做出任何的修改的话，那你再提醒他」。
# 所以按步数催命的逻辑整个删掉了——跑 15 步探索也一个字都不该插。
#
# 注：这 15 步每步都不一样，但这**不是**为了绕开什么检测——现在连「原地打转」
# 都不判了（见下面「不数步数」那一节，那儿专门用 40 次**一模一样**的调用来验）。
geo_n = obj.createNode("geo", "R_geoN")
ai_n = node_mod.make_ai_node(geo_n, "aiN", prompt="做个盒子")
script_n = [tu("get_node_type", {"type": "类型%d" % i}) for i in range(15)]
script_n.append(tu("build", {"nodes": [{"name": "b", "type": "box"}],
                             "output": "b"}))
script_n.append(tu("finish", {"summary": "盒子"}))
fake_n = run_once(ai_n, script_n, text="做个盒子")

H.check_eq("探索 15 步之后建出来了", node_mod.current_state(ai_n), "done")
# 注意跳过 messages[0]——那是开头的「我要做的东西：…」，不是插话。
injected = [(i, m) for i, entry in enumerate(fake_n.seen)
            for m in entry["messages"][1:]
            if isinstance(m.get("content"), str)]
H.check("★ 中途一个字都没插进去", not injected, injected[:2])
H.check("★ 也没有任何催命字眼",
        not any(k in str(entry["messages"])
                for entry in fake_n.seen[1:]
                for k in ("最多", "尽快", "别查了", "还剩")),
        [k for k in ("最多", "尽快", "别查了", "还剩")
         if any(k in str(e["messages"]) for e in fake_n.seen[1:])])
H.check("15 步探索走完了 build",
        any("提交网络" in t
            for t in session.load(ai_n)["rounds"][0].get("trace", [])))

H.section("★ 只在「结束对话 + 容器空 + 什么都没提交」时才提醒")
# 这个场景就是截图里那个：它查完参数，用文字描述了一遍自己打算怎么建就收工了。
geo_n2 = obj.createNode("geo", "R_geoN2")
ai_n2 = node_mod.make_ai_node(geo_n2, "aiN2", prompt="做个盒子")
fake_n2 = run_once(ai_n2, [
    tu("get_node_type", {"type": "box"}),
    say("我打算先建一个 box，然后接一个 polyextrude……"),   # 只说不做
    tu("build", {"nodes": [{"name": "b", "type": "box"}], "output": "b"}),
    tu("finish", {"summary": "建了个盒子。"}),
], text="做个盒子")

H.check_eq("被提醒之后真的动手了", node_mod.current_state(ai_n2), "done")
H.check("★ 提醒确实发出去了",
        any("还没有往容器里放任何节点" in str(entry["messages"])
            for entry in fake_n2.seen))
H.check("提醒发生在它「打算结束」之后（不在探索中途）",
        "还没有往容器里放任何节点" not in str(fake_n2.seen[1]["messages"]),
        str(fake_n2.seen[1]["messages"])[-200:])

H.section("★ 容器本来就有东西时不打扰它（第二轮可能真的不需要改）")
geo_n3 = obj.createNode("geo", "R_geoN3")
ai_n3 = node_mod.make_ai_node(geo_n3, "aiN3", prompt="x")
builder.apply(ai_n3, {"nodes": [{"name": "old", "type": "box"}],
                      "output": "old"})
H.check("容器里确实已经有节点", len(ai_n3.children()) > 0)
fake_n3 = run_once(ai_n3, [
    tu("list_container", {}),
    say("我看了一下，现在的网络已经符合要求，不需要改动。"),
], text="再高一点", kind="followup")
H.check("★ 容器非空时不会被塞提醒",
        not any("还没有往容器里放任何节点" in str(e["messages"])
                for e in fake_n3.seen))

H.section("深度取不到时退回默认，不会炸")
plain = geo_n2.createNode("subnet", "R_plain")   # 没有深度参数的老节点
H.check_eq("没有深度参数的节点退回默认",
           node_mod.depth_of(plain), config_mod.DEFAULT_DEPTH)
H.check_eq("config 里乱填也退回默认",
           config_mod.depth_preset("乱写")["label"],
           config_mod.DEPTH_PRESETS[config_mod.DEFAULT_DEPTH]["label"])
for key in config_mod.DEPTH_ORDER:
    H.check("预设 %s 有 label/help/thinking" % key,
            all(k in config_mod.depth_preset(key)
                for k in ("label", "help", "thinking")),
            config_mod.depth_preset(key))

H.section("★★ 深度只管 thinking，不管跑几步")
# 用户 2026-09-29：「不要限制 AI，不要限制 AI！等 AI 自己结束，再去判定它
# 有没有构建完成」+「深度不管一轮最大几步，只是用网关输思考深度」。
# 上一版深度里带着 `max_tool_rounds`（快 12 / 标准 30 / 深入 80），
# 结果它第 12 步还在正常干活就被截停，界面报「没收尾」——那是**我**掐的，
# 却写得像它失败。这个断言就是不许那个数字悄悄爬回来。
H.check("★ 预设里不许再有任何步数上限",
        not any("max_tool_rounds" in config_mod.depth_preset(k)
                or "max_steps" in config_mod.depth_preset(k)
                for k in config_mod.DEPTH_ORDER),
        [config_mod.depth_preset(k) for k in config_mod.DEPTH_ORDER])
H.check("深度之间的差别只有 thinking（外加给人看的 label/help）",
        all(set(config_mod.depth_preset(k)) == {"label", "help", "thinking"}
            for k in config_mod.DEPTH_ORDER),
        [sorted(config_mod.depth_preset(k)) for k in config_mod.DEPTH_ORDER])
H.check("快模式真的是不思考（thinking 关掉）",
        config_mod.depth_preset("fast")["thinking"] == {"type": "disabled"},
        config_mod.depth_preset("fast")["thinking"])

H.section("★★ 它自己收工就是收工，不许再报「没收尾」")
# 这就是截图里那条：「跑了 12 步也没调用 finish」。当时快模式卡在 12 步，
# 网络其实建出来了，界面却是红的。现在的判据是**容器**，不是它走没走手续。
geo_f = obj.createNode("geo", "R_geoF")
ai_f = node_mod.make_ai_node(geo_f, "aiF", prompt="做个盒子")
ai_f.parm(node_mod.DEPTH).set("fast")     # 上一版的快模式只给 12 步
script_f = [tu("get_node_type", {"type": "类型%d" % i}) for i in range(15)]
script_f.append(tu("build", {"nodes": [{"name": "b", "type": "box"}],
                             "output": "b"}))
script_f.append(say("盒子建好了，我收工。"))     # ★ 没调 finish
fake_f = run_once(ai_f, script_f, text="做个盒子")

H.check("★ 15 步探索 + 没调 finish，照样算做成",
        node_mod.current_state(ai_f) == "done",
        "%s / %s" % (node_mod.current_state(ai_f), node_mod.status_line(ai_f)))
H.check("★ 状态字里没有「没收尾」「没收敛」「没调用 finish」这类冤枉话",
        not any(k in node_mod.status_line(ai_f)
                for k in ("没收尾", "没收敛", "没调用 finish", "用完")),
        node_mod.status_line(ai_f))
H.check("它自己说的那句话被当成了结果摘要",
        "收工" in (node_mod.status_line(ai_f) + (ai_f.comment() or "")),
        ai_f.comment())
H.check("★ 收尾照样跑了 cook 校验（不是无脑宣布成功）",
        node_mod.current_state(ai_f) == "done",
        node_mod.status_line(ai_f))
H.check("断言它确实跑满了 12 步以上（证明上一版会在这儿被掐死）",
        len(fake_f.seen) >= 16, len(fake_f.seen))

H.section("★★ 不数步数：反复做同一件事、跑很多步都不许被拦")
# 用户 2026-09-29：「**不要再记录步数了，不要有任何的熔断措施**，除非它已经
# 运行了 60 分钟以上」。
# 所以曾经那道「连着 6 次动作+结果都一模一样就停」的死循环闸**也删掉了**：
# 它想反复看同一个东西、想跑一百步，那是它的事。
geo_r = obj.createNode("geo", "R_geoR")
ai_r = node_mod.make_ai_node(geo_r, "aiR", prompt="做个盒子")
# ★ 故意每次**完全一样**（参数一样、结果也一样）——上一版这里会被判成打转。
script_r = [tu("list_container", {}) for _ in range(40)]
script_r.append(tu("build", {"nodes": [{"name": "b", "type": "box"}],
                             "output": "b"}))
script_r.append(tu("finish", {"summary": "盒子"}))
fake_r = run_once(ai_r, script_r, text="做个盒子")
H.check("★ 40 次一模一样的调用全都放行（没有打转检测）",
        len(fake_r.seen) >= 42, len(fake_r.seen))
H.check_eq("照常做完了", node_mod.current_state(ai_r), "done")
H.check("★ runner 里不许再有步数/打转相关的常量",
        not hasattr(runner, "REPEAT_STOP")
        and not hasattr(runner, "_sig"),
        [n for n in ("REPEAT_STOP", "_sig") if hasattr(runner, n)])
H.check("config 里也不许再让步数熔断的配置冒出来",
        not any("step" in k.lower() for k in config_mod.DEFAULTS)
        and "runaway_limit" not in config_mod.DEFAULTS,
        sorted(config_mod.DEFAULTS))

H.section("★ 唯一的熔断是时间：跑满 runaway_minutes 才停")
geo_z = obj.createNode("geo", "R_geoZ")
ai_z = node_mod.make_ai_node(geo_z, "aiZ", prompt="做个盒子")
_real_load = config_mod.load
_real_time = runner.time


class _Clock(object):
    """假的钟：只看它想问时间，就往前推 `step` 秒。

    别的属性（sleep 之类）原样透给真的 time 模块。
    """

    def __init__(self, real, step):
        self._real = real
        self._step = step
        self.t = real.time()

    def __getattr__(self, name):
        return getattr(self._real, name)

    def time(self):
        self.t += self._step
        return self.t


def _tight(overrides=None):
    cfg = _real_load(overrides)
    cfg["runaway_minutes"] = 60      # 和产品默认值一样，这里只是写明
    return cfg


config_mod.load = _tight
runner.time = _Clock(_real_time, 600.0)     # 每问一次时间 = 过了 10 分钟
try:
    fake_z = run_once(ai_z, [tu("get_node_type", {"type": "类型%d" % i})
                             for i in range(40)], text="做个盒子")
finally:
    config_mod.load = _real_load
    runner.time = _real_time

H.check("★ 是时间把它停下来的，不是步数（40 步的脚本没跑完）",
        0 < len(fake_z.seen) < 40, len(fake_z.seen))
H.check("★ 停下来的状态字说的是钟，不是步数",
        node_mod.current_state(ai_z) == "failed"
        and "分钟" in node_mod.status_line(ai_z),
        "%s / %s" % (node_mod.current_state(ai_z),
                     node_mod.status_line(ai_z)))
H.check("状态字里没有「步」这个字",
        "步" not in node_mod.status_line(ai_z),
        node_mod.status_line(ai_z))
H.check("默认是 60 分钟",
        float(config_mod.DEFAULTS["runaway_minutes"]) == 60.0,
        config_mod.DEFAULTS["runaway_minutes"])

# 时间限制**只在两轮之间**检查，而且填 0 就彻底不管时间。
geo_z2 = obj.createNode("geo", "R_geoZ2")
ai_z2 = node_mod.make_ai_node(geo_z2, "aiZ2", prompt="做个盒子")


def _no_timeout(overrides=None):
    cfg = _real_load(overrides)
    cfg["runaway_minutes"] = 0
    return cfg


config_mod.load = _no_timeout
runner.time = _Clock(_real_time, 600.0)
try:
    fake_z2 = run_once(ai_z2, [tu("get_node_type", {"type": "类型%d" % i})
                               for i in range(12)], text="做个盒子")
finally:
    config_mod.load = _real_load
    runner.time = _real_time
H.check("★ 填 0 = 连时间都不管（跑到底）", len(fake_z2.seen) >= 12,
        len(fake_z2.seen))

H.section("★★ 共享记忆：写进去 → 下一轮自动读出来")
from houdini_ai import memory as memory_mod

memory_mod.clear()
geo_m = obj.createNode("geo", "R_geoM")
ai_m = node_mod.make_ai_node(geo_m, "aiM", prompt="做个盒子")

fake_m = run_once(ai_m, [
    tu("remember", {"text": "这个项目里 polyextrude 的 translate 是 3 分量参数"}),
    tu("build", {"nodes": [{"name": "b", "type": "box"}], "output": "b"}),
    tu("finish", {"summary": "盒子"}),
], text="做个盒子")

H.check("记忆被写进去了", memory_mod.count() == 1, memory_mod.dump())
H.check("来源记的是这个容器的路径",
        memory_mod.load()[0]["from"] == ai_m.path(), memory_mod.load()[0])
H.check("过程记录里能看到它记了一条",
        any("共享经验" in t
            for t in session.load(ai_m)["rounds"][0].get("trace", [])),
        session.load(ai_m)["rounds"][0].get("trace"))

# ★ 判据要用**注入块特有的标题**（带 "# " 前缀），不能用「共享经验」这种词——
# prompt.py 的静态文案里本来就提到过它，拿那个当判据会永远为真。
INJECTED_HEADER = "# 这个 .hip 里的共享经验"


def last_user_text(fake, index=0):
    """第 index 次请求里**最后一条** user 消息的文本。

    ★ 必须只看最后一条——历史里的旧 user 消息也可能带着注入块，
    拿整个 messages 去搜的话永远为真，那等于没测。
    """
    msgs = fake.seen[index]["messages"]
    for message in reversed(msgs):
        if message.get("role") != "user":
            continue
        content = message.get("content")
        if isinstance(content, str):
            return content
        if isinstance(content, list):
            return "\n".join(b.get("text") or "" for b in content
                             if b.get("type") == "text")
    return ""


# 第二轮：换一个节点，断言它拿得到上一条经验
geo_m2 = obj.createNode("geo", "R_geoM2")
ai_m2 = node_mod.make_ai_node(geo_m2, "aiM2", prompt="做个球")
fake_m2 = run_once(ai_m2, [
    tu("build", {"nodes": [{"name": "s", "type": "sphere"}], "output": "s"}),
    tu("finish", {"summary": "球"}),
], text="做个球")

user_m2 = last_user_text(fake_m2)
H.check("★ 另一个节点**拿到了**这条经验（它看不到别人的对话）",
        "polyextrude" in user_m2, user_m2[-400:])
H.check("★ 带上了来源路径（看得出是谁总结的）",
        ai_m.path() in user_m2, user_m2[-400:])
H.check("有标题说明这是共享经验", INJECTED_HEADER in user_m2, user_m2[:200])
H.check("★ 注入进的是 **user 消息**，不再挂系统提示词（那会毁掉缓存前缀）",
        INJECTED_HEADER not in fake_m2.seen[0]["system"],
        fake_m2.seen[0]["system"][-300:])
H.check("★ 第一轮那边没有注入块（那时记忆还是空的）",
        INJECTED_HEADER not in last_user_text(fake_m),
        last_user_text(fake_m)[:200])

H.section("没有记忆时不注入空段落")
memory_mod.clear()
geo_m3 = obj.createNode("geo", "R_geoM3")
ai_m3 = node_mod.make_ai_node(geo_m3, "aiM3", prompt="x")
fake_m3 = run_once(ai_m3, [
    tu("build", {"nodes": [{"name": "b", "type": "box"}], "output": "b"}),
    tu("finish", {"summary": "x"}),
], text="x")
H.check("★ 空记忆不塞空段落（user 消息里没有那个标题）",
        INJECTED_HEADER not in last_user_text(fake_m3),
        last_user_text(fake_m3)[:200])
H.check("空记忆时系统提示词里当然也没有",
        INJECTED_HEADER not in fake_m3.seen[0]["system"])

H.section("★★ 变了才注入（没变不重复发）")
# 用户 2026-09-29 的原话：「模型既然是在同一个 session 里做的，为什么需要
# 每轮都注入？」——按指纹比对，没变就不发。
memory_mod.clear()
geo_m5 = obj.createNode("geo", "R_geoM5")
ai_m5 = node_mod.make_ai_node(geo_m5, "aiM5", prompt="x")
memory_mod.add("别的节点留下的经验 ABC", source="/obj/elsewhere")

_script = [tu("build", {"nodes": [{"name": "b", "type": "box"}], "output": "b"}),
           tu("finish", {"summary": "盒子"})]

f1 = run_once(ai_m5, list(_script), text="x")
H.check("第一轮：有记忆 → 注入（它历史里没有）",
        "ABC" in last_user_text(f1), last_user_text(f1)[:160])

f2 = run_once(ai_m5, list(_script), text="再来", kind="followup")
H.check("★ 第二轮：指纹没变 → **不再重复注入**",
        "ABC" not in last_user_text(f2), last_user_text(f2)[:160])
H.check("（但历史里那份还在，不该消失）",
        "ABC" in str(f2.seen[0]["messages"]))

memory_mod.add("后来又有一条 XYZ", source="/obj/elsewhere")
f3 = run_once(ai_m5, list(_script), text="第三次", kind="followup")
H.check("★ 第三轮：指纹变了 → 重新注入，两条都带上",
        "ABC" in last_user_text(f3) and "XYZ" in last_user_text(f3),
        last_user_text(f3)[:200])

H.section("★★ 当前旋钮清单每轮推进消息（用户：不靠它自己 list_container）")
# 用户 2026-09-29 报的：「给迭代反馈的时候，他并没有暴露需要的参数」。一个成因是
# 上一轮的清单在旧轮次被压成两行摘要后**从上下文里消失了**，而模型没义务每轮都去
# 调 list_container。这一段把「清单始终在上下文里」钉住。
from houdini_ai import builder as builder_mod

INJECTED_PROMO = builder_mod.PROMOTED_HEADER

geo_pr = obj.createNode("geo", "R_geoPR")
ai_pr = node_mod.make_ai_node(geo_pr, "aiPR", prompt="做个扭曲的盒子")

f_pr0 = run_once(ai_pr, [
    tu("build", {"nodes": [{"name": "tw", "type": "twist",
                            "params": {"strength": 60}}],
                 "output": "tw",
                 "promote": [{"node": "tw", "parm": "strength",
                              "as": "twist_amt", "label": "扭曲强度"}]}),
    tu("finish", {"summary": "好了"}),
], text="做个扭曲的盒子")
H.check("第一轮建完，容器上有一个旋钮了",
        [e["as"] for e in builder_mod.load_promotions(ai_pr)] == ["twist_amt"])
H.check("★ 第一轮那边**没有**注入块（那时还没有旋钮）",
        INJECTED_PROMO not in last_user_text(f_pr0), last_user_text(f_pr0)[:200])

f_pr1 = run_once(ai_pr, [tu("finish", {"summary": "调好了"})],
                 text="再扭曲一点", kind="followup")
_u1 = last_user_text(f_pr1)
H.check("★ 第二轮直接列出当前旋钮（中文标签 + 外部名 + 内部位置）",
        INJECTED_PROMO in _u1 and "扭曲强度" in _u1
        and "twist_amt" in _u1 and "tw.strength" in _u1, _u1[:300])
H.check("★ 注入进的是 **user 消息**，不进系统提示词（那会毁掉缓存前缀）",
        INJECTED_PROMO not in f_pr1.seen[0]["system"],
        f_pr1.seen[0]["system"][-200:])

f_pr2 = run_once(ai_pr, [tu("finish", {"summary": "x"})],
                 text="再改", kind="followup")
H.check("指纹没变且在完整窗口内 → **不重复注入**",
        INJECTED_PROMO not in last_user_text(f_pr2), last_user_text(f_pr2)[:160])

# MAX_FULL_ROUNDS=2：第四轮时第一轮的注入已经滚出完整回放窗口，必须补发，
# 否则清单会**悄悄从上下文里消失**——这正是上面那个成因。
f_pr3 = run_once(ai_pr, [tu("finish", {"summary": "x"})],
                 text="再改一次", kind="followup")
H.check("★★ 滚出完整回放窗口后必须**补发**（不然清单悄悄没了）",
        INJECTED_PROMO in last_user_text(f_pr3), last_user_text(f_pr3)[:160])
H.check("注入记录存在 session 里（跟着 .hip 走）",
        bool((session.load(ai_pr).get("promotions_injected") or {}).get("digest")),
        session.load(ai_pr).get("promotions_injected"))

# 清单变了 → 下一轮必须重新注入（否则模型看到的是过期的那套）
builder_mod.apply(ai_pr, {
    "nodes": [{"name": "tw", "type": "twist", "params": {"strength": 60}}],
    "output": "tw",
    "promote": [{"node": "tw", "parm": "strength", "as": "twist_amt",
                 "label": "扭曲强度"},
                {"node": "tw", "parm": "roll", "as": "twist_roll",
                 "label": "整体翻滚"}],
})
f_pr4 = run_once(ai_pr, [tu("finish", {"summary": "x"})],
                 text="还改", kind="followup")
_u4 = last_user_text(f_pr4)
H.check("★ 清单变了 → 重新注入，两个旋钮都在",
        INJECTED_PROMO in _u4 and "整体翻滚" in _u4 and "扭曲强度" in _u4,
        _u4[:300])

H.section("向上读一级的那个工具在循环里能用")
geo_c = obj.createNode("geo", "R_geoC")
up_c = geo_c.createNode("box", "R_upC")
ai_c = node_mod.make_ai_node(geo_c, "aiC", prompt="x")
ai_c.setInput(0, up_c)
down_c = geo_c.createNode("xform", "R_downC")
down_c.setInput(0, ai_c)
fake_c = run_once(ai_c, [
    tu("list_context", {}),
    tu("build", {"nodes": [{"name": "b", "type": "box"}], "output": "b"}),
    tu("finish", {"summary": "x"}),
], text="x")
trace_c = session.load(ai_c)["rounds"][0].get("trace", [])
H.check("过程记录里有「看容器外面的情况」",
        any("外面" in t for t in trace_c), trace_c)
H.check("这一轮正常跑完了", node_mod.current_state(ai_c) == "done")

H.section("★★ 图像通道：图走图像块，base64 不能混进文本")
# 关键：base64 塞进 JSON 文本里会让模型把几万个字符当文字读一遍，
# 白烧几千 token 而且读不懂。必须拆成 image 块。
from houdini_ai import viewport

viewport.reset_guard()
geo_img = obj.createNode("geo", "R_geoImg")
ai_img = node_mod.make_ai_node(geo_img, "aiImg", prompt="x")
builder.apply(ai_img, {
    "nodes": [{"name": "t", "type": "tube",
               "params": {"rad": [0.7, 0.7], "height": 7}}],
    "output": "t"})
geo_img.setDisplayFlag(True)
geo_img.cook(force=True)

fake_img = run_once(ai_img, [
    tu("look", {"view": "+z", "preset": "wireframe"}),
    tu("build", {"nodes": [{"name": "t", "type": "tube",
                            "params": {"rad": [0.7, 0.7], "height": 7}}],
                 "output": "t"}),
    tu("finish", {"summary": "x"}),
], text="x")

image_blocks = []
text_blobs = []
_seen_image_data = set()
for entry in fake_img.seen:
    for message in entry["messages"]:
        content = message.get("content")
        if isinstance(content, str):
            text_blobs.append(content)
            continue
        if not isinstance(content, list):
            continue
        for block in content:
            if not isinstance(block, dict):
                continue
            if block.get("type") == "image":
                image_blocks.append(block)
            if block.get("type") == "tool_result":
                # 图**嵌在 tool_result 的 content 里面**（它本身是个块列表），
                # 不在消息 content 的顶层——第一版就是栽在这儿。
                inner = block.get("content")
                if isinstance(inner, str):
                    text_blobs.append(inner)
                elif isinstance(inner, list):
                    for sub in inner:
                        if not isinstance(sub, dict):
                            continue
                        if sub.get("type") == "text":
                            text_blobs.append(sub.get("text") or "")
                        elif sub.get("type") == "image":
                            # ★ 每条历史都会在**后续每一次请求**里重发一遍，
                            # 所以同一张图会被数到 N 次。按数据去重，
                            # 断言「只出了一张**不同的**图」才说得通。
                            data = (sub.get("source") or {}).get("data") or ""
                            if data and data not in _seen_image_data:
                                _seen_image_data.add(data)
                                image_blocks.append(sub)

H.check("★ 图确实以 image 块发出去了", len(image_blocks) == 1,
        len(image_blocks))
if image_blocks:
    src = image_blocks[0].get("source") or {}
    H.check("  是 base64 PNG",
            src.get("type") == "base64" and src.get("media_type") == "image/png",
            (src.get("type"), src.get("media_type")))
    H.check("  数据是 PNG 的 base64（解出来魔数对）",
            base64.b64decode(src.get("data") or "").startswith(
                b"\x89PNG\r\n\x1a\n"))
    H.check("  数据够大（不是空图）",
            len(src.get("data") or "") > 1000, len(src.get("data") or ""))

blob = " ".join(text_blobs)
H.check("★ 文本部分里**没有** base64（不然白烧 token）",
        "iVBORw0KGgo" not in blob and len(blob) < 20000,
        len(blob))
H.check("过程记录里标出了这一次出图",
        any("出了 1 张图" in t
            for t in session.load(ai_img)["rounds"][0].get("trace", [])),
        session.load(ai_img)["rounds"][0].get("trace"))

H.section("★★ 运行中的注释：3 行历史 + 1 行实时状态")
# 用户原话：「应该分，比如说4行，最后一行显示它实时的状态，然后第1~3行应该是
# 它的这个历史……这些应该不包括思考中……记录一下他有没有看，然后有没有找一些
# 节点，就是看一下他之前在干什么，比如说是不是在写代码之类的」
geo_act = obj.createNode("geo", "R_geoAct")
ai_act = node_mod.make_ai_node(geo_act, "aiAct", prompt="x")
saw_comment = []


class WatchingGateway(FakeGateway):
    """每次请求网关时拍一张注释。

    这个时点正好是「上一轮的工具刚跑完、下一步还没开始」——最能看出
    历史和实时状态的分界。
    """

    def messages(self, *a, **kw):
        try:
            saw_comment.append(ai_act.comment())
        except Exception:
            pass
        return FakeGateway.messages(self, *a, **kw)


# ⚠ 这里**故意不 reset_guard**：上一个 section 已经真渲过一张了，
#   出图是保命的功能，不能为了测文字再去冒一次崩进程的险。
#   所以这次 look 会被保险拦下（返回 error），但动作描述照样进历史——
#   要测的是注释，不是图。
fake_act = WatchingGateway([
    tu("list_container", {}),
    tu("search_node_types", {"query": "extrude"}),
    tu("get_node_type", {"type": "polyextrude"}),
    tu("look", {"view": "+z", "preset": "wireframe"}),
    tu("build", {"nodes": [{"name": "t", "type": "tube",
                            "params": {"rad": [0.7, 0.7], "height": 7}}],
                 "output": "t"}),
    tu("finish", {"summary": "做完了"}),
])
_real = gateway_mod.Gateway
gateway_mod.Gateway = lambda cfg: fake_act
try:
    runner._run_round(ai_act.path(), "build", "x", threading.Event())
finally:
    gateway_mod.Gateway = _real


def _lines(text):
    return [line for line in (text or "").splitlines() if line.strip()]


H.check("跑的过程中拍到了注释", len(saw_comment) >= 4, len(saw_comment))
H.check("★ 运行中的注释不超过 4 行",
        all(len(_lines(c)) <= 4 for c in saw_comment),
        [len(_lines(c)) for c in saw_comment])

last = _lines(saw_comment[-1])
H.check("★ 最后一行是实时状态（带状态标签 + 思考中）",
        last[-1].startswith("[构建中]") and "思考中" in last[-1], last[-1])
histories = [_lines(c)[:-1] for c in saw_comment]
H.check("上面全是历史行（最多 3 条）",
        all(len(h) <= 3 for h in histories), [len(h) for h in histories])
H.check("历史行都带 · 前缀",
        all(line.startswith("· ") for h in histories for line in h),
        [line for h in histories for line in h][:4])
# ★ 这条是补的：第一版直接把 userData 里的字典格式化了，注释上打出了
#   `· {'t': '查参数：polyextrude', 'n': 1}`——而上面那些「包含某字样」的
#   断言全都照样通过（dict 的 repr 里也有那些字）。所以必须专门盯住
#   「不许出现字典的痕迹」，否则这类渲染 bug 测试是看不见的。
H.check("★★ 历史行里不许漏出数据结构（不是 dict 的 repr）",
        not any(("{" in line or "'t'" in line or '"t"' in line)
                for h in histories for line in h),
        [line for h in histories for line in h])
H.check("★★ 历史里**没有**「思考中」（用户明确要求这三条不记思考）",
        not any("思考中" in line for h in histories for line in h),
        [line for h in histories for line in h])
_flat = [line for h in histories for line in h]
H.check("★ 记下了它「有没有找节点」",
        any("找节点类型" in line for line in _flat), _flat)
H.check("★ 记下了它「是不是在写网络」",
        any("提交网络" in line for line in _flat), _flat)
H.check("★ 记下了它「有没有看一眼画面」",
        any("看一眼" in line for line in _flat), _flat)
H.check("跑完之后注释不再堆历史（那时要看的是结果）",
        "· " not in ai_act.comment(), ai_act.comment())
H.check("收尾的注释里是摘要", "做完了" in ai_act.comment(), ai_act.comment())

H.section("★★ 历史每条**只占一行**（不换行，省空间）")
# 用户 2026-09-29：「前三条历史就不要换行，这样的话，能节省空间，让这个空间
# 更紧凑一点」。注释框窄（约 40 列），长条目一折行 3 条历史就变 6 行。
H.check("短的**一字不动**",
        node_mod._fit_one_line("查参数：polyextrude") == "查参数：polyextrude",
        node_mod._fit_one_line("查参数：polyextrude"))
_long = "提交网络（12 个节点）成功，产出 1234 点"
_fit = node_mod._fit_one_line(_long)
H.check("★ 长的被压到一行以内（按显示列宽，中文算 2 列）",
        node_mod._display_width(_fit) <= node_mod.HISTORY_COLS,
        (node_mod._display_width(_fit), node_mod.HISTORY_COLS))
H.check("★ 压完之后看得出来被截了（带省略号），不是静默改内容",
        _fit.endswith("…"), _fit)
H.check("★ 中文的列宽确实算 2（不是按字符数算）",
        node_mod._display_width("中文") == 4, node_mod._display_width("中文"))

node_mod.set_status(ai_act, "working", "干活中")
node_mod.clear_activity(ai_act)
for _t in ("查参数：polyextrude",
           "提交网络（12 个节点）成功，产出 1234 点然后还写了一大堆别的东西",
           "看一眼（六视图，wireframe）"):
    node_mod.push_activity(ai_act, _t)
_hist = [ln for ln in ai_act.comment().splitlines() if ln.startswith("· ")]
H.check("三条历史都在", len(_hist) == 3, _hist)
H.check("★ 每一行的显示宽度都在预算内（都不会折行）",
        all(node_mod._display_width(ln) <= node_mod.HISTORY_COLS + 2
            for ln in _hist),
        [(ln, node_mod._display_width(ln)) for ln in _hist])

H.section("历史合并与上限")
node_mod.clear_activity(ai_act)
H.check("清空后没有历史", node_mod.activity_of(ai_act) == [],
        node_mod.activity_of(ai_act))
node_mod.push_activity(ai_act, "查看容器现状")
node_mod.push_activity(ai_act, "查看容器现状")
node_mod.push_activity(ai_act, "查看容器现状")
H.check("★ 连着三件同样的事合并成一行 ×3",
        node_mod.activity_of(ai_act) == ["查看容器现状 ×3"],
        node_mod.activity_of(ai_act))
node_mod.push_activity(ai_act, "提交网络（2 个节点）")
H.check("换了一件事就另起一条",
        node_mod.activity_of(ai_act) ==
        ["查看容器现状 ×3", "提交网络（2 个节点）"],
        node_mod.activity_of(ai_act))
node_mod.set_status(ai_act, "working", "思考中…（第 2 步）")
H.check("★ ×3 真的渲染到注释上了",
        "· 查看容器现状 ×3" in ai_act.comment(), ai_act.comment())
for i in range(6):
    node_mod.push_activity(ai_act, "动作 %d" % i)
H.check("★ 只保留最近 3 条",
        node_mod.activity_of(ai_act) == ["动作 3", "动作 4", "动作 5"],
        node_mod.activity_of(ai_act))
H.check("★★ 模块级的共享默认值没被污染（_EMPTY 是同一个列表对象）",
        node_mod._EMPTY["activity"] == [], node_mod._EMPTY["activity"])
fresh = node_mod.make_ai_node(obj.createNode("geo", "R_geoFresh"),
                              "aiFresh", prompt="x")
H.check("★ 新节点的历史是空的（没沾上别人的）",
        node_mod.activity_of(fresh) == [], node_mod.activity_of(fresh))

H.section("★★ 实时状态：不要老是写「思考中」")
# 用户原话：「实时状态能不能不要老是写思考中，说实话，思考中我完全看不出来
# 他在干什么……可能每隔一会儿更新一下他思考的最后几个字儿……不然的话，
# 思考中我都不知道他有没有卡住什么的」。所以两条腿都要测。
geo_live = obj.createNode("geo", "R_geoLive")
ai_live = node_mod.make_ai_node(geo_live, "aiLive", prompt="x")


class _Direct(object):
    """把 in_main 换成一个直接调用 + 记账的假货（测试就在主线程里跑）。"""

    def __init__(self):
        self.texts = []

    def __call__(self, fn, *a, **k):
        if fn is node_mod.set_status and len(a) >= 3:
            self.texts.append(a[2])
        return fn(*a, **k)


cap = _Direct()
live = runner.LiveStatus(ai_live, cap)
H.check("一个字都没来时：思考中 + 秒数",
        live.text().startswith("思考中…") and "秒" in live.text(), live.text())

live.on_delta("thinking", "我先想想这个塔怎么搭")
H.check("★ 思考来了就显示它想的尾巴（想：…）",
        live.text().startswith("想：…") and "我先想想这个塔怎么搭" in live.text(),
        live.text())
live.on_delta("thinking", "，用 tube 再叠个 twist")
H.check("后续增量是追加的",
        "我先想想这个塔怎么搭，用 tube 再叠个 twist" in live.text(),
        live.text())
live.on_delta("text", "我要建一个高塔")
H.check("★ 换成正文时重新起头，不和思考粘在一起",
        live.text().startswith("说：") and "我先想想" not in live.text(),
        live.text())

live.on_delta("text", "字" * 200)
body = live.text()
tail = body.split("（")[0]
H.check("★ 只留尾巴那几个字（注释那一行放得下）",
        len(body) <= runner.LIVE_TAIL_CHARS + 24, len(body))
H.check("留的是**最后**那几个字，不是开头",
        tail.count("字") == runner.LIVE_TAIL_CHARS, tail.count("字"))

live.on_delta("thinking", "换回思考了")
H.check("从正文换回思考也重新起头",
        live.text().startswith("想：…") and "字" not in live.text(), live.text())

H.check("★ 收尾不会因为回调抛异常而炸",
        (lambda: (runner.LiveStatus(ai_live,
                                    lambda fn, *a, **k: fn(*a, **k)).on_delta(
                                        "text", "x"), True)[1])())

# 秒表：模型沉默的时候界面也得动，否则"看着像卡死了"
cap2 = _Direct()
beat = runner.LiveStatus(ai_live, cap2, throttle=0.0)
beat.start()
time.sleep(1.4)
beat.stop()
H.check("★ 一个字都没来，秒表也在更新注释（能看出没卡住）",
        len(cap2.texts) >= 2, len(cap2.texts))
beat.stop()
H.check("重复 stop 不会炸（调用方两条路径都会 stop）",
        beat.stop() is None)

H.section("★★ 流式（SSE）拼回普通响应——形状必须一模一样")
_gw = gateway_mod      # 别名一下，这段读起来清楚

_SSE = [
    'data: {"type":"message_start","message":{"id":"m1"}}',
    '',                                          # 空行要忽略
    ': 这是一条注释行，要忽略',
    'data: {"type":"ping"}',                     # 未知类型要忽略
    'data: {"type":"content_block_start","index":0,'
    '"content_block":{"type":"thinking","thinking":""}}',
    'data: {"type":"content_block_delta","index":0,'
    '"delta":{"type":"thinking_delta","thinking":"先想想要几个节点"}}',
    'data: {"type":"content_block_delta","index":0,'
    '"delta":{"type":"signature_delta","signature":"abc"}}',
    'data: {"type":"content_block_stop","index":0}',
    'data: {"type":"content_block_start","index":1,'
    '"content_block":{"type":"tool_use","id":"toolu_1","name":"build",'
    '"input":{}}}',
    # ★ 工具参数是**分片**送来的，这一段不拼的话 input 永远是空的
    'data: {"type":"content_block_delta","index":1,'
    '"delta":{"type":"input_json_delta","partial_json":"{\\"nodes\\": [{\\"name\\""}}',
    'data: {"type":"content_block_delta","index":1,'
    '"delta":{"type":"input_json_delta","partial_json":": \\"t\\", \\"type\\": \\"tube\\"}]}"}}',
    'data: {"type":"content_block_stop","index":1}',
    'data: {"type":"message_delta","delta":{"stop_reason":"tool_use"},'
    '"usage":{"output_tokens":7}}',
    'data: {"type":"message_stop"}',
    'data: [DONE]',
]


class _FakeStream(object):
    def __init__(self, lines):
        self._lines = [line.encode("utf-8") for line in lines]
        self.closed = False

    def __iter__(self):
        return iter(self._lines)

    def close(self):
        self.closed = True


_captured = {}


def _fake_urlopen(req, timeout=None):
    _captured["req"] = req
    _captured["timeout"] = timeout
    if isinstance(_captured.get("raise"), Exception):
        raise _captured["raise"]
    return _FakeStream(_captured.get("lines") or [])


_real_urlopen = _gw.urllib.request.urlopen
_gw.urllib.request.urlopen = _fake_urlopen
try:
    _captured["lines"] = list(_SSE)
    _captured.pop("raise", None)
    deltas = []
    gw_real = _gw.Gateway({"base_url": "https://x/anthropic", "api_key": "k",
                           "model": "m"})
    resp = gw_real.messages("sys", [{"role": "user", "content": "hi"}],
                            tools=[{"name": "build"}],
                            thinking={"type": "enabled", "budget_tokens": 100},
                            on_delta=lambda kind, piece: deltas.append((kind, piece)))

    kinds = [b.get("type") for b in resp["content"]]
    H.check("★ 拼出来的块类型和顺序都对",
            kinds == ["thinking", "tool_use"], kinds)
    H.check("思考的增量拼成了完整一段",
            resp["content"][0].get("thinking") == "先想想要几个节点",
            resp["content"][0].get("thinking"))
    H.check("★★ 工具参数拼好了（不拼就是空 input）",
            resp["content"][1].get("input") ==
            {"nodes": [{"name": "t", "type": "tube"}]},
            resp["content"][1].get("input"))
    H.check("stop_reason 拿到了", resp["stop_reason"] == "tool_use",
            resp["stop_reason"])
    H.check("usage 拿到了", resp["usage"].get("output_tokens") == 7,
            resp["usage"])
    H.check("★ 拼完不带临时字段（_partial 不许漏出去）",
            not any(k.startswith("_") for b in resp["content"] for k in b),
            [sorted(b) for b in resp["content"]])
    H.check("流式回调只收到思考/正文，没收到工具参数的碎片",
            [k for k, _ in deltas] == ["thinking"], deltas)
    H.check("回调拼起来就是整段思考",
            "".join(p for _, p in deltas) == "先想想要几个节点", deltas)
    H.check("发出的请求确实带了 stream:true",
            b'"stream": true' in _captured["req"].data, _captured["req"].data[:80])

    # 形状必须和一次性请求一模一样——这是"调用方不该分情况"的保证
    H.check("★★ 形状和非流式一致（strip_thinking 之后只剩 tool_use）",
            [b.get("type") for b in
             gateway_mod.strip_thinking(resp.get("content"))] == ["tool_use"],
            gateway_mod.strip_thinking(resp.get("content")))
    H.check("工具名取得到",
            (gateway_mod.blocks_of_type(resp, "tool_use")[0].get("name")
             == "build"))

    _captured["lines"] = [
        'data: {"type":"content_block_start","index":0,'
        '"content_block":{"type":"tool_use","id":"t","name":"finish",'
        '"input":{}}}',
        'data: {"type":"content_block_stop","index":0}',
        'data: {"type":"message_stop"}',
    ]
    resp2 = gw_real.messages("s", [{"role": "user", "content": "x"}],
                             on_delta=lambda k, p: None)
    H.check("工具参数一片都没来时 input 是空字典（不是报错）",
            resp2["content"][0].get("input") == {},
            resp2["content"][0].get("input"))

    # ★★ 参数拼不出合法 JSON：**不许抛异常**，要把证据交出来。
    # 用户 2026-09-29 真的撞上了：节点上打出「工具参数不是合法 JSON」然后整轮就
    # 结束了。以前这里抛 GatewayError，它的 status 是 None、不在"流式被拒"那几个
    # 码里，runner 把它当网关故障 → 直接 return。**那一步是模型自己能改的事，
    # 不该让整轮死掉。**
    _captured["lines"] = [
        'data: {"type":"content_block_start","index":0,'
        '"content_block":{"type":"tool_use","id":"toolu_bad","name":"build",'
        '"input":{}}}',
        # 参数写到一半停了（就是被长度上限截断的样子）
        'data: {"type":"content_block_delta","index":0,'
        '"delta":{"type":"input_json_delta","partial_json":"{\\"nodes\\": [{\\"name\\": \\"sr"}}',
        'data: {"type":"content_block_stop","index":0}',
        'data: {"type":"message_delta","delta":{"stop_reason":"max_tokens"}}',
        'data: {"type":"message_stop"}',
    ]
    resp3 = gw_real.messages("s", [{"role": "user", "content": "x"}],
                             on_delta=lambda k, p: None)
    H.check("★★ 参数拼不出 JSON **不抛异常**（以前抛了，整轮就死在这儿）",
            True)
    H.check("★ 那一块仍然在，input 是空字典（回灌给网关的历史是干净的）",
            resp3["content"][0].get("input") == {}
            and resp3["content"][0].get("name") == "build",
            resp3["content"][0])
    H.check("★ 不许有下划线开头的临时字段漏进内容块",
            not any(k.startswith("_") for k in resp3["content"][0]),
            sorted(resp3["content"][0]))
    _bad = resp3.get("bad_tool_json") or []
    H.check("★ 坏掉的块被单独报出来（形状不变，多的键只在真出事时非空）",
            len(_bad) == 1 and _bad[0]["name"] == "build", _bad)
    H.check("★★ 留下了**可诊断**的证据：长度 + 开头 + 结尾",
            _bad and _bad[0]["chars"] == len('{"nodes": [{"name": "sr')
            and "nodes" in _bad[0]["head"] and "sr" in _bad[0]["tail"]
            and _bad[0].get("why"),
            _bad[0] if _bad else None)
    H.check("★★ `stop_reason` 拿到了——**这才是真因**（截断，不是「真拼错」）",
            resp3.get("stop_reason") == "max_tokens", resp3.get("stop_reason"))
    H.check("（对照）参数正常时这个键是空列表，不是缺失",
            resp["bad_tool_json"] == [], resp.get("bad_tool_json"))

    # 裸 tab 是 SSE 里**允许**的裸控制字符（裸换行不行，换行会终止 data 字段）。
    # `strict=False` 让这种本来合法、只是 Python 默认不认的情况能过。
    _captured["lines"] = [
        'data: {"type":"content_block_start","index":0,'
        '"content_block":{"type":"tool_use","id":"t2","name":"build","input":{}}}',
        'data: {"type":"content_block_delta","index":0,'
        '"delta":{"type":"input_json_delta","partial_json":"{\\"a\\": \\"x\\ty\\"}"}}',
        'data: {"type":"content_block_stop","index":0}',
        'data: {"type":"message_stop"}',
    ]
    resp4 = gw_real.messages("s", [{"role": "user", "content": "x"}],
                             on_delta=lambda k, p: None)
    H.check("★ 字符串里的裸 tab 能解析（strict=False 的便宜保险）",
            resp4["content"][0].get("input") == {"a": "x\ty"},
            resp4["content"][0].get("input"))
    H.check("这种情况**不算**坏（不报 bad_tool_json）",
            resp4.get("bad_tool_json") == [], resp4.get("bad_tool_json"))

    _captured["lines"] = []
    try:
        gw_real.messages("s", [{"role": "user", "content": "x"}],
                         on_delta=lambda k, p: None)
        H.check("空流报错", False, "居然没抛")
    except gateway_mod.GatewayError as exc:
        H.check("★ 一个块都没有的流会被明确报错（不当成空响应）",
                "内容块" in str(exc), str(exc)[:80])

    _captured["lines"] = [
        'data: {"type":"error","error":{"message":"模型过载"}}',
    ]
    try:
        gw_real.messages("s", [{"role": "user", "content": "x"}],
                         on_delta=lambda k, p: None)
        H.check("流里的 error 事件要被抛出来", False, "居然没抛")
    except gateway_mod.GatewayError as exc:
        H.check("流里的 error 事件要被抛出来", "模型过载" in str(exc), str(exc)[:80])

    # ★★ 这条以前断言的是「半截的 JSON 要**抛异常**」。2026-09-29 反转了：
    # 抛异常意味着整轮死掉，而那一步是**模型自己能改**的。现在要的是
    # 「不抛 + 把证据交给 runner」，由 runner 回灌给它重发。
    _captured["lines"] = [
        'data: {"type":"content_block_start","index":0,'
        '"content_block":{"type":"tool_use","id":"t","name":"build","input":{}}}',
        'data: {"type":"content_block_delta","index":0,'
        '"delta":{"type":"input_json_delta","partial_json":"{\\"nodes\\": ["}}',
        'data: {"type":"content_block_stop","index":0}',
        'data: {"type":"message_stop"}',
    ]
    try:
        _half = gw_real.messages("s", [{"role": "user", "content": "x"}],
                                 on_delta=lambda k, p: None)
        H.check("★★ 半截的 JSON **不抛异常**了（以前抛，整轮就死在这儿）", True)
        H.check("★ 而是报成 bad_tool_json 交给 runner 处置",
                len(_half.get("bad_tool_json") or []) == 1,
                _half.get("bad_tool_json"))
        H.check("★ 也不静默给空——块还在，只是 input 是空字典",
                _half["content"][0].get("input") == {}
                and _half["content"][0].get("name") == "build",
                _half["content"][0])
    except gateway_mod.GatewayError as exc:
        H.check("★★ 半截的 JSON **不抛异常**了（以前抛，整轮就死在这儿）",
                False, str(exc)[:80])
        H.check("★ 而是报成 bad_tool_json 交给 runner 处置", False)
        H.check("★ 也不静默给空——块还在，只是 input 是空字典", False)

    # 回调自己抛异常，绝不能把整轮请求带崩（那会白花钱）
    _captured["lines"] = [
        'data: {"type":"content_block_start","index":0,'
        '"content_block":{"type":"text","text":""}}',
        'data: {"type":"content_block_delta","index":0,'
        '"delta":{"type":"text_delta","text":"你好"}}',
        'data: {"type":"message_stop"}',
    ]

    def _boom(kind, piece):
        raise RuntimeError("界面炸了")

    resp3 = gw_real.messages("s", [{"role": "user", "content": "x"}],
                             on_delta=_boom)
    H.check("★★ 回调抛异常不影响结果（进度显示是旁路，不能拖垮请求）",
            resp3["content"][0].get("text") == "你好", resp3["content"])
finally:
    _gw.urllib.request.urlopen = _real_urlopen

H.section("网关不认流式就自动退回一次性请求")
geo_fb = obj.createNode("geo", "R_geoFb")
ai_fb = node_mod.make_ai_node(geo_fb, "aiFb", prompt="x")


class _NoStreamGateway(FakeGateway):
    """冒充"网关没实现 SSE"：带 on_delta 就报 400，并且**像真网关那样记住**。

    记住这件事是要测的——不记的话每一轮都要白撞一次请求。
    """

    def __init__(self, script):
        FakeGateway.__init__(self, script)
        self.stream_ok = True
        self.rejections = 0

    def messages(self, system, messages, tools=None, max_tokens=None,
                 thinking=None, on_delta=None):
        self.seen.append({"stream": on_delta is not None and self.stream_ok})
        if on_delta is not None and self.stream_ok:
            self.rejections += 1
            self.stream_ok = False
            raise gateway_mod.GatewayError("网关返回 HTTP 400：不认识 stream",
                                           status=400)
        # 不调父类，免得把整份 messages 记进 seen（断言输出会被撑爆）
        if not self.script:
            return say("（脚本用完了）")
        return self.script.pop(0)


fake_fb = _NoStreamGateway([
    tu("build", {"nodes": [{"name": "t", "type": "tube",
                            "params": {"rad": [0.7, 0.7], "height": 7}}],
                 "output": "t"}),
    tu("finish", {"summary": "退回成功了"}),
])
_real2 = gateway_mod.Gateway
gateway_mod.Gateway = lambda cfg: fake_fb
try:
    runner._run_round(ai_fb.path(), "build", "x", threading.Event())
finally:
    gateway_mod.Gateway = _real2
H.check("★ 流式被拒后自动退回了一次性请求，这一轮照样跑完",
        node_mod.current_state(ai_fb) == "done",
        node_mod.current_state(ai_fb))
flags = [s.get("stream") for s in fake_fb.seen if "stream" in s]
H.check("★ 流式只试了一次（不认就记住，不每轮白撞）",
        fake_fb.rejections == 1, fake_fb.rejections)
H.check("第一轮之后全走一次性请求",
        flags[:1] == [True] and bool(flags[1:]) and
        all(f is False for f in flags[1:]),
        flags)

H.section("停止按钮")
ai3 = node_mod.make_ai_node(obj.createNode("geo", "R_geo3"), "ai3", prompt="x")
cancel = threading.Event()
cancel.set()
script = [tu("list_container", {})]
fake3 = FakeGateway(script)
real = gateway_mod.Gateway
gateway_mod.Gateway = lambda cfg: fake3
try:
    runner._run_round(ai3.path(), "build", "x", cancel)
finally:
    gateway_mod.Gateway = real
H.check_eq("被取消后状态是已停止", node_mod.current_state(ai3), "stopped")

H.section("网关没配好时的表现")
ai4 = node_mod.make_ai_node(obj.createNode("geo", "R_geo4"), "ai4", prompt="x")


class BadGateway(object):
    def ready(self):
        return False, "没配置密钥"

    def messages(self, *a, **k):
        raise AssertionError("不该走到这里")


real = gateway_mod.Gateway
gateway_mod.Gateway = lambda cfg: BadGateway()
try:
    runner._run_round(ai4.path(), "build", "x", threading.Event())
finally:
    gateway_mod.Gateway = real
H.check_eq("网关没配好时状态是失败", node_mod.current_state(ai4), "failed")
H.check("错误信息里说清了原因",
        "密钥" in node_mod.status_line(ai4), node_mod.status_line(ai4))

H.section("回退到某一轮之前")
geo5 = obj.createNode("geo", "R_geo5")
ai5 = node_mod.make_ai_node(geo5, "ai5", prompt="先做个盒子")
run_once(ai5, [
    tu("build", {"nodes": [{"name": "b", "type": "box"}], "output": "b"}),
    tu("finish", {"summary": "盒子"}),
])
H.check("第一轮后有 box", "b" in {c.name() for c in ai5.children()})
run_once(ai5, [
    tu("build", {"nodes": [{"name": "t", "type": "torus"}], "output": "t"}),
    tu("finish", {"summary": "换成圆环"}),
], text="换成圆环", kind="followup")
H.check("第二轮后变成 torus", "t" in {c.name() for c in ai5.children()})

ok = runner.restore_round(ai5, 2)
H.check("回退成功", ok)
H.check("回到了第一轮的样子（box 回来了）",
        "b" in {c.name() for c in ai5.children()},
        sorted(c.name() for c in ai5.children()))
H.check("第二轮的 torus 没了",
        "t" not in {c.name() for c in ai5.children()})

H.section("★★ 工具返回在传输层**一个字都不许砍**（2026-09-29 用户定）")
# 两道关卡以前都在砍：当轮 `runner._compact(limit=6000)`、历史回放
# `session.trim_tool_results`（砍到 2000）。砍出来的是一份**残缺 JSON**，
# 而模型**没有第二次机会**去要剩下那半。这里把两道都钉死。
import json as _json

_big = {"type": "polyextrude", "parms": [
    {"name": "p%03d" % i, "kind": "float", "label": "参数 %d" % i}
    for i in range(200)]}
_text = runner._compact(_big)
H.check("★ _compact 不砍了（旧上限 6000）", len(_text) > 6000, len(_text))
H.check("★ 没有「已截断」字样", "已截断" not in _text)
H.check("★ 是个**能解析的完整 JSON**（砍过的那个不是）",
        isinstance(_json.loads(_text), dict))
H.check("最后一个参数也在（被砍的话最先丢的就是它）",
        "p199" in _text)

# 历史回放那条路
_fat = "x" * 9000
_msgs = [{"role": "user", "content": [
    {"type": "tool_result", "tool_use_id": "c1", "content": _fat}]}]
_out = session.trim_tool_results(_msgs)
_body = _out[0]["content"][0]["content"]
H.check("★ trim_tool_results 不再砍到 2000", len(_body) == 9000, len(_body))
H.check("★ 没有「已截断」字样", "已截断" not in _body)
H.check("原消息没被改动（返回的是新列表）",
        len(_msgs[0]["content"][0]["content"]) == 9000)

# 但**压缩**要还在：那是换形式不是砍内容，删了上下文会随轮数线性膨胀
H.check("★ MAX_FULL_ROUNDS 还在（压缩 ≠ 截断）",
        session.MAX_FULL_ROUNDS == 2, session.MAX_FULL_ROUNDS)
H.check("★ 也没有 MAX_TOOL_RESULT_CHARS 这个东西了",
        not hasattr(session, "MAX_TOOL_RESULT_CHARS"))

H.finish("test_runner")
