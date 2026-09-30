# -*- coding: utf-8 -*-
"""构建器的测试。这是整套东西里最要紧的一份——校验器必须比模型固执。

    "D:/ProgramFiles/Houdini22/bin/hython.exe" tests/test_builder.py
"""

import _harness as H

H.section("搭测试场景")
import hou
from houdini_ai import builder, catalog
from houdini_ai import node as node_mod


_SEQ = [0]


def make_container(with_upstream=True):
    """建一个干净的 geo + subnet，可选地给它接一个上游 box。

    每次都新建、绝不清扫别的测试留下的节点——早期版本会清掉先前的 T_ 节点，
    结果前面拿到的 sub 变成已删除对象，报一堆看不懂的错。
    """
    _SEQ[0] += 1
    tag = "T%d" % _SEQ[0]
    obj = hou.node("/obj")
    geo = obj.createNode("geo", "%s_geo" % tag)
    sub = geo.createNode("subnet", "%s_ai" % tag)
    if with_upstream:
        up = geo.createNode("box", "%s_up" % tag)
        sub.setInput(0, up)
    return geo, sub


def points(node):
    """节点当前产出的点数。没几何时 geometry() 返回 None，别直接取属性。"""
    geo = node.geometry()
    return 0 if geo is None else geo.pointCount()


def expr_of(node, parm_name):
    """参数上的表达式（没有就返回空串）。

    注意别直接调 parm.expression()——参数**没有表达式时它会抛异常**
    （hou.OperationFailed: Parameter is not animated）。
    builder._parm_expr 就是为这个封的。
    """
    parm = node.parm(parm_name)
    return builder._parm_expr(parm) if parm is not None else ""


geo, sub = make_container()
H.check("容器建好了", sub is not None and sub.type().name() == "subnet")
H.check("容器能装 SOP", catalog.category_of_node(sub).name() == "Sop")
H.check("容器有 4 个 indirect input", len(sub.indirectInputs()) == 4)

H.section("正常构建：box -> polyextrude -> output")
spec = {
    "nodes": [
        {"name": "base", "type": "box", "params": {"size": [2, 2, 0.4]}},
        {"name": "twist", "type": "polyextrude",
         "params": {"divs": 12, "outputfront": 1, "translate": [0, 0, 1.5]}},
    ],
    "connections": [
        {"from": "base", "to": "twist", "input": 0},
    ],
    "output": "twist",
}
result = builder.apply(sub, spec)
H.check("构建返回摘要", isinstance(result, dict))
kids = {c.name(): c for c in sub.children()}
H.check("建出了 base", "base" in kids)
H.check("建出了 twist", "twist" in kids)
H.check_eq("twist 的类型正确", kids["twist"].type().name().split("::")[0], "polyextrude")
H.check_eq("base 的 size 落对了",
           tuple(kids["base"].parmTuple("size").eval()), (2.0, 2.0, 0.4))
H.check_eq("divs 落对了", kids["twist"].parm("divs").eval(), 12)
H.check("几何真的产出了", points(sub) > 0, "点数 %d" % points(sub))
H.check_eq("多分量参数 translate 落对了",
           tuple(kids["twist"].parmTuple("translate").eval()), (0.0, 0.0, 1.5))
H.check("收尾建了 output 节点",
        any(c.type().name() == "output" for c in sub.children()))
H.check("容器没有报错", sub.errors() == (), sub.errors())
out_kids = [c for c in sub.children() if c.type().name() == "output"]
if out_kids:
    H.check_eq("output 接到了 twist", out_kids[0].input(0).name(), "twist")

H.section("接入上游几何：__input0__")
geo2, sub2 = make_container(with_upstream=True)
spec = {
    "nodes": [{"name": "twist", "type": "polyextrude", "params": {"divs": 5}}],
    "connections": [{"from": "__input0__", "to": "twist", "input": 0}],
}
builder.apply(sub2, spec)
H.check("接了上游后几何非空", points(sub2) > 0, "点数 %d" % points(sub2))
H.check("容器无报错", sub2.errors() == (), sub2.errors())

H.section("容器输入没接线时用 __input0__ 不报错但产出为空")
geo3, sub3 = make_container(with_upstream=False)
builder.apply(sub3, {
    "nodes": [{"name": "pe", "type": "polyextrude"}],
    "connections": [{"from": "__input0__", "to": "pe", "input": 0}],
})
H.check("没上游时确实没点", points(sub3) == 0, "点数 %d" % points(sub3))

H.section("校验器：编造的节点类型必须被拒")
def build(spec, container=None):
    return builder.apply(container or sub, spec)

problems = builder.validate(sub, {
    "nodes": [{"name": "x", "type": "boxinator"}],
})
H.check("假类型被拒", bool(problems))
H.check("错误里点明了类型不存在", any("boxinator" in p for p in problems), problems)
H.check("错误里给了接近的建议",
        any("box" in p for p in problems), problems)

H.section("校验器：参数错误")
problems = builder.validate(sub, {
    "nodes": [{"name": "x", "type": "box", "params": {"nosuchparm": 1}}],
})
H.check("假参数被拒", bool(problems), problems)

problems = builder.validate(sub, {
    "nodes": [{"name": "x", "type": "box", "params": {"size": [1, 2]}}],
})
H.check("向量分量数不对被拒", bool(problems), problems)
H.check("说明了需要几个数", any("需要 3 个数" in p for p in problems), problems)

problems = builder.validate(sub, {
    "nodes": [{"name": "x", "type": "polyextrude", "params": {"divs": 2.5}}],
})
H.check("整数参数收到小数被拒", bool(problems), problems)

problems = builder.validate(sub, {
    "nodes": [{"name": "x", "type": "box", "params": {"size": ["大", "大", "大"]}}],
})
H.check("非数字被拒", bool(problems), problems)

H.section("参数以真实实例为准：模板说谎的名字必须被拒")
# polyextrude 的类型模板里有 lt / lr / ls（stdswitcher 的候选页），
# 但实例上根本没有这些参数，真名是 translate / rotate / scale。
# 照着模板放行的话，模型会写一个永远设不上、又不报错的参数。
H.check("模板里确实有 lt 这个假名字",
        "lt" in {p["name"] for p in catalog.type_info("Sop", "polyextrude")["parms"]}
        or not catalog.type_info("Sop", "polyextrude").get("parms_error"))
pe_index = catalog.parm_index("Sop", "polyextrude", probe_parent=sub)
H.check("探测出的参数索引里没有 lt", "lt" not in pe_index)
H.check("探测出的参数索引里有 translate", "translate" in pe_index)

problems = builder.validate(sub, {
    "nodes": [{"name": "x", "type": "polyextrude", "params": {"lt": [0, 0, 1]}}],
})
H.check("模板谎报的 lt 被拒", bool(problems), problems)

H.section("元组名和分量名两种写法都该能用")
H.check_eq("translate 有 3 个分量",
           pe_index["translate"].get("components"), 3)
H.check("translate 的分量名被收录进索引",
        "translatey" in pe_index, sorted(k for k in pe_index if "translate" in k))
H.check_eq("分量名指回同一条信息",
           pe_index["translatey"]["name"], "translate")

problems = builder.validate(sub, {
    "nodes": [{"name": "x", "type": "polyextrude",
               "params": {"translate": [0, 0, 2]}}],
})
H.check("元组名 + 3 个数 合法", not problems, problems)

problems = builder.validate(sub, {
    "nodes": [{"name": "x", "type": "polyextrude",
               "params": {"translatey": 2}}],
})
H.check("分量名 + 单个数 合法", not problems, problems)

problems = builder.validate(sub, {
    "nodes": [{"name": "x", "type": "polyextrude", "params": {"translate": 2}}],
})
H.check("元组名只给 1 个数被拒", bool(problems), problems)

geo8, sub8 = make_container(with_upstream=False)
builder.apply(sub8, {
    "nodes": [{"name": "pe", "type": "polyextrude",
               "params": {"translatey": 3.5}}],
})
H.check_eq("按分量名写也能真的落值",
           round(sub8.node("pe").parmTuple("translate").eval()[1], 3), 3.5)

H.section("校验器：滑杆范围不是硬限制，strict 的才是")
# box 的 size 报 -1..1 但不 strict——那是滑杆范围，设成 2 是合法需求。
problems = builder.validate(sub, {
    "nodes": [{"name": "x", "type": "box", "params": {"size": [2, 2, 0.4]}}],
})
H.check("超出滑杆范围的值不该被拒", not problems, problems)

# polyextrude 的 divs 是 minIsStrict，0 必须被拒。
divs_info = None
for p in catalog.type_info("Sop", "polyextrude")["parms"]:
    if p["name"] == "divs":
        divs_info = p
        break
H.check("divs 被标为 strict 下限", divs_info and divs_info.get("min_strict"))
problems = builder.validate(sub, {
    "nodes": [{"name": "x", "type": "polyextrude", "params": {"divs": 0}}],
})
H.check("strict 下限被真正执行", bool(problems), problems)

H.section("校验器：菜单参数的合法取值")
from houdini_ai import catalog as _cat
pe_info = _cat.type_info("Sop", "polyextrude")
menu_parm = None
for p in pe_info["parms"]:
    if p["kind"] == "menu" and p.get("options"):
        menu_parm = p
        break
H.check("找得到一个菜单参数做样本", menu_parm is not None)

if menu_parm:
    pname = menu_parm["name"]
    bad = "绝对不是合法取值"
    problems = builder.validate(sub, {
        "nodes": [{"name": "x", "type": "polyextrude", "params": {pname: bad}}],
    })
    H.check("非法菜单取值被拒", bool(problems), problems)
    H.check("错误里列出了可选值",
            any("可选" in p or "不是合法取值" in p for p in problems), problems)

    # label 当 value 填，应该被救回来
    good_label = menu_parm["options"][0]["label"]
    good_value = menu_parm["options"][0]["value"]
    problems = builder.validate(sub, {
        "nodes": [{"name": "x", "type": "polyextrude", "params": {pname: good_label}}],
    })
    H.check("菜单 label 能被救成合法 token", not problems, problems)

    # 下标也应该能用
    problems = builder.validate(sub, {
        "nodes": [{"name": "x", "type": "polyextrude", "params": {pname: 0}}],
    })
    H.check("菜单下标 0 合法", not problems, problems)
    problems = builder.validate(sub, {
        "nodes": [{"name": "x", "type": "polyextrude",
                   "params": {pname: len(menu_parm["options"]) + 5}}],
    })
    H.check("越界下标被拒", bool(problems), problems)

H.section("校验器：连接错误")
problems = builder.validate(sub, {
    "nodes": [{"name": "a", "type": "box"}],
    "connections": [{"from": "ghost", "to": "a", "input": 0}],
})
H.check("来源节点不存在被拒", bool(problems), problems)

problems = builder.validate(sub, {
    "nodes": [{"name": "a", "type": "box"}],
    "connections": [{"from": "a", "to": "ghost", "input": 0}],
})
H.check("目标节点不存在被拒", bool(problems), problems)

problems = builder.validate(sub, {
    "nodes": [{"name": "a", "type": "box"}, {"name": "b", "type": "box"}],
    "connections": [{"from": "a", "to": "b", "input": 3}],
})
H.check("输入槽位越界被拒", bool(problems), problems)
H.check("说明了该节点有几个输入", any("最多只有" in p for p in problems), problems)

problems = builder.validate(sub, {
    "nodes": [{"name": "a", "type": "box"}, {"name": "b", "type": "subdivide"}],
    "connections": [
        {"from": "a", "to": "b", "input": 0},
        {"from": "a", "to": "b", "input": 0},
    ],
})
H.check("同一槽位接两次被拒", bool(problems), problems)

H.section("校验器：命名与结构错误")
problems = builder.validate(sub, {
    "nodes": [{"name": "a", "type": "box"}, {"name": "a", "type": "box"}],
})
H.check("重名被拒", bool(problems), problems)

problems = builder.validate(sub, {"nodes": [{"name": "2bad", "type": "box"}]})
H.check("非法名字被拒", bool(problems), problems)

problems = builder.validate(sub, {"nodes": [{"name": "a", "type": "box"}],
                                  "output": "ghost"})
H.check("output 指向不存在的节点被拒", bool(problems), problems)

problems = builder.validate(sub, {"nodes": []})
H.check("空节点清单被拒", bool(problems), problems)

problems = builder.validate(sub, {"nodes": [{"name": "a"}]})
H.check("缺 type 被拒", bool(problems), problems)

H.section("校验失败时容器保持原样（不留半成品）")
geo4, sub4 = make_container(with_upstream=False)
builder.apply(sub4, {"nodes": [{"name": "keepme", "type": "box"}]})
before = sorted(c.name() for c in sub4.children())
try:
    builder.apply(sub4, {
        "nodes": [
            {"name": "good", "type": "box"},
            {"name": "bad", "type": "totally_fake_node"},
        ],
    })
    H.check("非法 spec 应当抛异常", False, "居然没抛")
except builder.SpecError as exc:
    H.check("非法 spec 抛 SpecError", True)
    H.check("异常里带问题清单", len(exc.problems) > 0)
after = sorted(c.name() for c in sub4.children())
H.check_eq("容器内容没被动过", after, before)

H.section("全量替换语义：再 build 一次不留旧节点")
builder.apply(sub4, {"nodes": [{"name": "fresh", "type": "sphere"}]})
names = {c.name() for c in sub4.children()}
H.check("旧节点 keepme 没了", "keepme" not in names)
H.check("新节点 fresh 在了", "fresh" in names)

H.section("增量改参数 set_parms")
builder.apply(sub4, {"nodes": [{"name": "s", "type": "sphere", "params": {"radx": 1.0}}]})
builder.set_parms(sub4, [{"node": "s", "parm": "radx", "value": 3.5}])
H.check_eq("参数改成功了", sub4.node("s").parm("radx").eval(), 3.5)

try:
    builder.set_parms(sub4, [{"node": "ghost", "parm": "radx", "value": 1.0}])
    H.check("改不存在的节点应当报错", False, "居然没抛")
except builder.SpecError as exc:
    H.check("改不存在的节点被拒", True)
    H.check("错误里列出了现有节点",
            any("现有" in p for p in exc.problems), exc.problems)

try:
    builder.set_parms(sub4, [{"node": "s", "parm": "nosuch", "value": 1.0}])
    H.check("改不存在的参数应当报错", False, "居然没抛")
except builder.SpecError:
    H.check("改不存在的参数被拒", True)

H.section("现状描述 / 快照往返")
geo5, sub5 = make_container(with_upstream=False)
original = {
    "nodes": [
        {"name": "b", "type": "box", "params": {"size": [1, 1, 3]}},
        {"name": "p", "type": "polyextrude", "params": {"divs": 7}},
    ],
    "connections": [{"from": "b", "to": "p", "input": 0}],
    "output": "p",
}
builder.apply(sub5, original)
snapshot = builder.summarize(sub5)
H.check("快照里有节点", len(snapshot["nodes"]) >= 2)
H.check("快照记录了连接", len(snapshot["connections"]) >= 1)
H.check("快照里有非默认参数值",
        any(n.get("params") for n in snapshot["nodes"]), snapshot["nodes"])

# 拿快照当 spec 重建，应该等价
rebuildable = {
    "nodes": [{"name": n["name"], "type": n["type"], "params": n.get("params") or {}}
              for n in snapshot["nodes"]],
    "connections": [c for c in snapshot["connections"]
                    if c["from"] != "（容器外或容器输入）"],
}
problems = builder.validate(sub5, rebuildable)
H.check("快照能被自己的校验器接受", not problems, problems)

H.section("摘要只在有输入线时报告可用输入")
geo6, sub6 = make_container(with_upstream=False)
summary = builder.summarize(sub6)
H.check("没接上游时不报 available_inputs",
        "available_inputs" not in summary, summary.get("available_inputs"))
geo7, sub7 = make_container(with_upstream=True)
summary = builder.summarize(sub7)
H.check("接了上游就报 available_inputs",
        summary.get("available_inputs") == ["__input0__"],
        summary.get("available_inputs"))

H.section("★ 自动排版：从上到下（用 Houdini 自己的 layoutChildren）")
# 布局调的是 Houdini 自己的 Node.layoutChildren()——网络编辑器「Layout」
# 命令用的同一个 API。主链从上往下走，分支横向分叉。
# （我一开始手搓了个「从左到右」的算法，方向就是错的：用户要的是从上到下。）
geo_l, sub_l = make_container(with_upstream=False)
builder.apply(sub_l, {
    "nodes": [
        {"name": "base", "type": "box"},
        {"name": "sub", "type": "subdivide"},
        {"name": "pe", "type": "polyextrude"},
        {"name": "tw", "type": "twist"},
        {"name": "nrm", "type": "normal"},
    ],
    "connections": [
        {"from": "base", "to": "sub", "input": 0},
        {"from": "sub", "to": "pe", "input": 0},
        {"from": "pe", "to": "tw", "input": 0},
        {"from": "tw", "to": "nrm", "input": 0},
    ],
    "output": "nrm",
})
pos = dict((c.name(), c.position()) for c in sub_l.children())
chain = ["base", "sub", "pe", "tw", "nrm"]
ys = [pos[n][1] for n in chain]
H.check("链上的节点 y 严格递减（从上到下）",
        all(ys[i] > ys[i + 1] for i in range(len(ys) - 1)), ys)
H.check("整条链在同一列上（不歪）",
        len({round(pos[n][0], 3) for n in chain}) == 1,
        [pos[n][0] for n in chain])
if "output0" in pos:
    H.check("Output 节点在最下面",
            pos["output0"][1] < pos["nrm"][1],
            (pos["output0"], pos["nrm"]))
H.check("没有两个节点叠在一起",
        len({(round(p[0], 2), round(p[1], 2)) for p in pos.values()}) == len(pos),
        pos)

H.section("自动排版：分支横向分叉，汇合点回到中间")
geo_b, sub_b = make_container(with_upstream=False)
builder.apply(sub_b, {
    "nodes": [
        {"name": "a", "type": "box"},
        {"name": "b", "type": "sphere"},
        {"name": "m", "type": "merge"},
    ],
    "connections": [
        {"from": "a", "to": "m", "input": 0},
        {"from": "b", "to": "m", "input": 1},
    ],
    "output": "m",
})
pb = dict((c.name(), c.position()) for c in sub_b.children())
H.check("两支横向分开（x 不同）",
        abs(pb["a"][0] - pb["b"][0]) > 0.5, (pb["a"], pb["b"]))
H.check("两支都在汇合点上方",
        pb["a"][1] > pb["m"][1] and pb["b"][1] > pb["m"][1],
        (pb["a"], pb["b"], pb["m"]))
if "output0" in pb:
    H.check("Output 在最下面",
            pb["output0"][1] < pb["m"][1], (pb["output0"], pb["m"]))

H.section("布局是调 Houdini 自己的 API（不是自己手搓）")
H.check("layout() 返回了成功", builder.layout(sub_b) is True)
H.check("layoutChildren 存在（这就是用的那个 API）",
        hasattr(hou.Node, "layoutChildren"))

H.section("★★ 参数提升：在外面就能调")
geo_p, sub_p = make_container(with_upstream=False)
res = builder.apply(sub_p, {
    "nodes": [
        {"name": "base", "type": "box"},
        {"name": "tw", "type": "twist", "params": {"strength": 90}},
    ],
    "connections": [{"from": "base", "to": "tw", "input": 0}],
    "output": "tw",
    "promote": [
        {"node": "tw", "parm": "strength", "label": "扭曲强度",
         "min": 0, "max": 720},
    ],
})
H.check("容器上多了一个备用参数", sub_p.parm("tw_strength") is not None)
H.check("标签是中文",
        sub_p.parmTuple("tw_strength").parmTemplate().label() == "扭曲强度",
        sub_p.parmTuple("tw_strength").parmTemplate().label())
H.check("内层参数变成表达式驱动",
        bool(expr_of(sub_p.node("tw"), "strength")),
        expr_of(sub_p.node("tw"), "strength"))

sub_p.parm("tw_strength").set(300)
H.check_eq("★ 外面改 300，里面跟着变",
           sub_p.node("tw").parm("strength").eval(), 300.0)
sub_p.parm("tw_strength").set(42)
H.check_eq("★ 外面改 42，里面跟着变",
           sub_p.node("tw").parm("strength").eval(), 42.0)

H.check("提升清单被记下来了",
        len(builder.load_promotions(sub_p)) == 1,
        builder.load_promotions(sub_p))

H.section("★ 摘要如实报告提升关系（不然下一轮会静默丢掉）")
snap = builder.summarize(sub_p)
H.check("摘要里有容器名字", "name" in snap, snap.get("name"))
H.check("摘要里有 promoted 清单", len(snap.get("promoted") or []) == 1,
        snap.get("promoted"))
H.check("promoted 里带了当前值",
        snap["promoted"][0].get("value") == 42.0, snap["promoted"][0])
tw_entry = [n for n in snap["nodes"] if n["name"] == "tw"][0]
H.check("★ 内层节点的表达式被单独报出来了",
        tw_entry.get("expressions", {}).get("strength"),
        tw_entry.get("expressions"))

H.section("不带 promote 重提交：提升关系自动沿用")
builder.apply(sub_p, {
    "nodes": [
        {"name": "base", "type": "box"},
        {"name": "tw", "type": "twist", "params": {"strength": 90}},
    ],
    "connections": [{"from": "base", "to": "tw", "input": 0}],
    "output": "tw",
})
H.check("提升参数还在", sub_p.parm("tw_strength") is not None)
H.check("内层还是表达式驱动",
        bool(expr_of(sub_p.node("tw"), "strength")))
sub_p.parm("tw_strength").set(123)
H.check_eq("外面调还是能传到里面",
           sub_p.node("tw").parm("strength").eval(), 123.0)

H.section("给空 promote：全部撤掉，且不留悬空表达式")
builder.apply(sub_p, {
    "nodes": [
        {"name": "base", "type": "box"},
        {"name": "tw", "type": "twist", "params": {"strength": 90}},
    ],
    "connections": [{"from": "base", "to": "tw", "input": 0}],
    "output": "tw",
    "promote": [],
})
H.check("提升参数被摘掉了", sub_p.parm("tw_strength") is None)
H.check("★ 内层的表达式也被清干净了（不留悬空引用）",
        not expr_of(sub_p.node("tw"), "strength"),
        expr_of(sub_p.node("tw"), "strength"))
H.check("撤掉之后容器没有报错", sub_p.errors() == (), sub_p.errors())
H.check("内层参数回到常数", sub_p.node("tw").parm("strength").eval() == 90.0)

H.section("★★ 沿用提升时不静默丢弃：内部节点改名必须**报出来**")
# 2026-09-29 修的。以前沿用路径有一层 `if e.get("node") in created` 的预过滤，
# 模型重建时给内部节点改了名，那条提升就**无声消失**——用户在外面少了那个旋钮，
# 而模型和用户都不知道发生过什么。这正是用户抱怨的「参数没暴露」的一种成因。
# ⚠️ 修法是**报出来**（进 promote_problems），不是拒绝整次 build：改名是正常操作，
# 为一条模型没主动提及的旧提升退掉整次构建，语义混乱。
geo_pd, sub_pd = make_container(with_upstream=False)
builder.apply(sub_pd, {
    "nodes": [{"name": "tw", "type": "twist", "params": {"strength": 60}}],
    "output": "tw",
    "promote": [{"node": "tw", "parm": "strength", "as": "twist_amt",
                 "label": "扭曲强度"}],
})
H.check("先有一次正常的提升", sub_pd.parm("twist_amt") is not None)

_res = builder.apply(sub_pd, {          # 不给 promote = 沿用；但节点改名了
    "nodes": [{"name": "curly", "type": "twist", "params": {"strength": 60}}],
    "output": "curly",
})
_probs = _res.get("promote_problems") or []
H.check("★ 改名后沿用失败，必须报进 promote_problems（绝不静默丢）",
        bool(_probs), _res.get("promote_problems"))
H.check("★ 提示里点了旧节点名", any("tw" in p for p in _probs), _probs)
H.check("★ 措辞是可行动的（告诉它按新名字重列）",
        any("promote" in p for p in _probs), _probs)
H.check("★ 旧旋钮也如实消失（不留悬空参数）", sub_pd.parm("twist_amt") is None)
H.check("★ 清单也清干净了（不是留着一份对不上的）",
        builder.load_promotions(sub_pd) == [],
        builder.load_promotions(sub_pd))

H.section("★★ 注入用的摘要与指纹（每轮推进消息那一套）")
geo_pb, sub_pb = make_container(with_upstream=False)
H.check("没有提升时指纹是空的（= 不注入）",
        builder.promoted_digest(sub_pb) == "")
H.check("没有提升时摘要也是空的（不注入空段落）",
        builder.promoted_brief(sub_pb) == "")

builder.apply(sub_pb, {
    "nodes": [{"name": "tw", "type": "twist", "params": {"strength": 60}}],
    "output": "tw",
    "promote": [{"node": "tw", "parm": "strength", "as": "twist_amt",
                 "label": "扭曲强度"}],
})
_d1 = builder.promoted_digest(sub_pb)
_brief = builder.promoted_brief(sub_pb)
H.check("有提升就有指纹", bool(_d1) and len(_d1) >= 8, _d1)
H.check("摘要里有抬头", builder.PROMOTED_HEADER in _brief)
H.check("★ 摘要里说的是**人话**：中文标签 + 外部名 + 内部位置",
        "扭曲强度" in _brief and "twist_amt" in _brief and "tw.strength" in _brief,
        _brief)
H.check("★ 摘要里带一句契约（不给=沿用、给=列全），否则模型会只填一个新的把旧的冲掉",
        "列全" in _brief, _brief)

# ★ 指纹**只认身份、不认当前值**：用户在面板里拖一下滑杆不该触发下轮重发。
sub_pb.parm("twist_amt").set(123.0)
H.check("★★ 改了参数值，指纹**不变**（拖滑杆不该触发重发）",
        builder.promoted_digest(sub_pb) == _d1)
H.check("★ 摘要里也**不放当前值**（放了就是拿陈旧数字当现状）",
        "123" not in builder.promoted_brief(sub_pb),
        builder.promoted_brief(sub_pb))

builder.apply(sub_pb, {
    "nodes": [{"name": "tw", "type": "twist", "params": {"strength": 60}}],
    "output": "tw",
    "promote": [{"node": "tw", "parm": "strength", "as": "twist_amt",
                 "label": "扭曲强度"},
                {"node": "tw", "parm": "roll", "as": "twist_roll",
                 "label": "整体翻滚"}],
})
H.check("★ 清单变了，指纹就变（这就是「变了才注入」的判据）",
        builder.promoted_digest(sub_pb) != _d1)

H.section("提升的校验")
problems = builder.validate(sub_p, {
    "nodes": [{"name": "a", "type": "box"}],
    "promote": [{"node": "ghost", "parm": "x"}],
})
H.check("提升不存在的节点被拒", bool(problems), problems)

problems = builder.validate(sub_p, {
    "nodes": [{"name": "a", "type": "box"}],
    "promote": [{"node": "a", "parm": "size"}],
})
H.check("★ 提升多分量参数被拒并说明原因",
        any("多分量" in p for p in problems), problems)

problems = builder.validate(sub_p, {
    "nodes": [{"name": "a", "type": "box"}],
    "promote": [{"node": "a", "parm": "sizex", "as": "ai_prompt"}],
})
H.check("提升参数撞上工具自己的参数名被拒",
        any("工具自己用" in p for p in problems), problems)

problems = builder.validate(sub_p, {
    "nodes": [{"name": "a", "type": "box", "params": {"sizex": 1}}],
    "promote": [{"node": "a", "parm": "sizex"}],
})
H.check("正常提升通过校验", not problems, problems)

H.section("★ 改名：自动名会改，你自己起的名字不动")
geo_r = hou.node("/obj").createNode("geo", "T_rename_geo")
auto = node_mod.make_ai_node(geo_r, "AI", prompt="x")
H.check_eq("初始名字就是 AI", auto.name(), "AI")
builder.apply(auto, {
    "nodes": [{"name": "b", "type": "box"}], "output": "b",
    "name": "twisted_tower",
})
H.check_eq("★ 自动名被改成有意义的名字", auto.name(), "twisted_tower")

geo_r2 = hou.node("/obj").createNode("geo", "T_rename_geo2")
mine = node_mod.make_ai_node(geo_r2, "my_precious_node", prompt="x")
builder.apply(mine, {
    "nodes": [{"name": "b", "type": "box"}], "output": "b",
    "name": "something_else",
})
H.check_eq("★ 用户自己起的名字不会被覆盖",
           mine.name(), "my_precious_node")

builder.apply(mine, {
    "nodes": [{"name": "b", "type": "box"}], "output": "b",
    "name": "not a valid name!!",
})
H.check("乱七八糟的名字被收拾成合法的",
        builder.normalize_node_name("not a valid name!!") ==
        "not_a_valid_name", builder.normalize_node_name("not a valid name!!"))

H.section("★ 快照往返要保住提升关系")
geo_s, sub_s = make_container(with_upstream=False)
builder.apply(sub_s, {
    "nodes": [
        {"name": "base", "type": "box"},
        {"name": "tw", "type": "twist", "params": {"strength": 60}},
    ],
    "connections": [{"from": "base", "to": "tw", "input": 0}],
    "output": "tw",
    "promote": [{"node": "tw", "parm": "strength", "as": "twist_amt"}],
})
snapshot = builder.summarize(sub_s)
sub_s.parm("twist_amt").set(200)
H.check_eq("改过之后里面是 200", sub_s.node("tw").parm("strength").eval(), 200.0)

rebuild = {
    "nodes": [{"name": n["name"], "type": n["type"],
               "params": n.get("params") or {},
               "expressions": n.get("expressions") or {}}
              for n in snapshot["nodes"]],
    "connections": [c for c in snapshot["connections"]
                    if c["from"] != "（容器外或容器输入）"],
    "promote": snapshot.get("promoted") or [],
}
problems = builder.validate(sub_s, rebuild)
H.check("快照能被自己的校验器接受（含 promote）", not problems, problems)
builder.apply(sub_s, rebuild)
H.check("★ 重建后提升参数还在", sub_s.parm("twist_amt") is not None)
sub_s.parm("twist_amt").set(77)
H.check_eq("★ 重建后提升关系依然有效",
           sub_s.node("tw").parm("strength").eval(), 77.0)

H.section("★★ 表达式不许引用容器外的东西（唯一破坏读边界的地方）")
# Hscript 表达式能读容器外的参数（ch("/obj/其他/参数")）。不堵的话
# 「读被限制在容器内」这句话就不成立——而那条边界是整套东西的安全前提。
geo_x, sub_x = make_container(with_upstream=False)


def expr_spec(expression, parm="sizex"):
    return {
        "nodes": [{"name": "b", "type": "box",
                   "expressions": {parm: expression}}],
        "output": "b",
    }


problems = builder.validate(sub_x, expr_spec('ch("/obj/other/parm")'))
H.check("★ 绝对路径被拒", bool(problems), problems)
H.check("错误里说明了只能引用容器内部",
        any("容器外" in p for p in problems), problems)

problems = builder.validate(sub_x, expr_spec('ch("../../parm")'))
H.check("★ 往上跳两层被拒", bool(problems), problems)

problems = builder.validate(sub_x, expr_spec('ch("a/../../parm")'))
H.check("路径中间夹 .. 也被拒", bool(problems), problems)

# 合法的：容器自己的参数（提升机制用的就是这个）
problems = builder.validate(sub_x, expr_spec('ch("../my_knob")'))
H.check("★ ch(\"../参数\") 放行（提升机制在用）", not problems, problems)

# 合法的：往容器里层走
problems = builder.validate(sub_x, expr_spec('ch("../child/parm")'))
H.check("往容器里层走的相对路径放行", not problems, problems)

# 合法的：纯表达式，没有路径
problems = builder.validate(sub_x, expr_spec("5 + 3"))
H.check("纯算式放行", not problems, problems)

problems = builder.validate(sub_x, expr_spec("$F * 2"))
H.check("带变量的算式放行", not problems, problems)

# ★ 除法不能被误判成绝对路径
problems = builder.validate(sub_x, expr_spec("$F / 2"))
H.check("★ 除法（$F / 2）不能被误判", not problems, problems)

H.section("提升参数时生成的表达式本身要合法")
geo_y, sub_y = make_container(with_upstream=False)
builder.apply(sub_y, {
    "nodes": [
        {"name": "base", "type": "box"},
        {"name": "tw", "type": "twist", "params": {"strength": 45}},
    ],
    "connections": [{"from": "base", "to": "tw", "input": 0}],
    "output": "tw",
    "promote": [{"node": "tw", "parm": "strength", "as": "twist_amt"}],
})
snapshot = builder.summarize(sub_y)
H.check("提升生成的表达式确实存在",
        snapshot["nodes"][1].get("expressions", {}).get("strength"),
        snapshot["nodes"][1].get("expressions"))
# 快照回滚要把表达式一起喂回来，而那必须过校验——过不了就说明白名单太紧
rebuild = {
    "nodes": [{"name": n["name"], "type": n["type"],
               "params": n.get("params") or {},
               "expressions": n.get("expressions") or {}}
              for n in snapshot["nodes"]],
    "connections": [c for c in snapshot["connections"]
                    if c["from"] != "（容器外或容器输入）"],
    "promote": snapshot.get("promoted") or [],
}
problems = builder.validate(sub_y, rebuild)
H.check("★ 回滚快照（含提升表达式）能过校验", not problems, problems)

H.finish("test_builder")
