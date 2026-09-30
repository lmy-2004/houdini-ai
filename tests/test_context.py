# -*- coding: utf-8 -*-
"""向上读一级的测试。

    "D:/ProgramFiles/Houdini22/bin/hython.exe" tests/test_context.py

最要紧的一条是**越不过父网络**：读的范围放宽了一级，那条边界必须是真的，
不是"我们没写去找更深的东西"这种口头保证。
"""

import json

import _harness as H

H.section("搭场景：gltf → color → 容器 → xform")
import hou
from houdini_ai import context, node as node_mod

obj = hou.node("/obj")
for old in list(obj.children()):
    if old.name().startswith("C_"):
        old.destroy()

# 父网络外面刻意放一个有特征名字的节点，用来证明读不到它
outside = obj.createNode("geo", "C_OUTSIDE_MARKER")

geo = obj.createNode("geo", "C_geo")
upstream = geo.createNode("gltf", "C_upstream")
middle = geo.createNode("color", "C_middle")
middle.setInput(0, upstream)

ai = node_mod.make_ai_node(geo, "C_ai", prompt="x")
ai.setInput(0, middle)

downstream = geo.createNode("xform", "C_downstream")
downstream.setInput(0, ai)

# 容器**内部**放一个有特征名字的节点，用来证明 context 只讲"外面"那一层
inside = ai.createNode("box", "C_INSIDE_MARKER")

info = context.describe(ai)

H.section("父网络这一层")
H.check("报的是父网络的路径和类型",
        info["parent"]["path"] == geo.path()
        and info["parent"]["type"] == "geo",
        info.get("parent"))

H.section("上游：容器每个输入接的是谁")
H.check("上游解析到了 color 那个节点",
        len(info["upstream"]) == 1
        and info["upstream"][0]["name"] == "C_middle",
        info.get("upstream"))
H.check("上游带了类型",
        info["upstream"][0]["type"].split("::")[0] == "color",
        info["upstream"])
H.check("标了是第几个输入", info["upstream"][0]["input"] == 0)

H.section("★ 下游：谁在消费容器的输出")
H.check("下游解析到了 xform 那个节点",
        len(info["downstream"]) == 1
        and info["downstream"][0]["name"] == "C_downstream",
        info.get("downstream"))
H.check("下游带了类型",
        info["downstream"][0]["type"].split("::")[0] == "xform",
        info.get("downstream"))
H.check("★ 有下游时会明确提醒它小心",
        any("下游" in n and "输出" in n for n in info.get("notes") or []),
        info.get("notes"))

H.section("同级名单")
sibling_names = [s["name"] for s in info["siblings"]]
H.check("同级里有上游和下游",
        "C_upstream" in sibling_names and "C_downstream" in sibling_names,
        sibling_names)
H.check("★ 同级里**不含**容器自己",
        "C_ai" not in sibling_names, sibling_names)
H.check("同级只有名字和类型，没有参数值",
        all(set(s.keys()) <= {"name", "type"} for s in info["siblings"]),
        info["siblings"][:2])

H.section("★★ 越不过父网络")
blob = json.dumps(info, ensure_ascii=False)
H.check("★ 看不到父网络之外的节点（C_OUTSIDE_MARKER）",
        "C_OUTSIDE_MARKER" not in blob, blob[:300])
H.check("★ 也看不到容器内部的节点（那是它自己该读 list_container 的事）",
        "C_INSIDE_MARKER" not in blob, blob[:300])
H.check("返回里压根没有深路径（只有父网络的）",
        blob.count(geo.path()) >= 1 and "/obj/C_OUTSIDE" not in blob)
H.check("没有递归字段（只讲这一层）",
        set(info.keys()) <= {"parent", "container", "upstream", "downstream",
                             "siblings", "notes"},
        sorted(info.keys()))

H.section("没有上游时会说清楚")
geo2 = obj.createNode("geo", "C_geo2")
ai2 = node_mod.make_ai_node(geo2, "C_ai2", prompt="x")
info2 = context.describe(ai2)
H.check("upstream 是空的", info2["upstream"] == [])
H.check("下游也是空的", info2["downstream"] == [])
H.check("has a note 说明可以自由造几何",
        any("没有节点接到容器" in n for n in info2.get("notes") or []),
        info2.get("notes"))

H.section("★ 同级名单**全列**，一个都不许砍")
# 2026-09-29 用户定：不要截断任何内容。这里原来断言"截到 60 条 + 报告截了 8 个"，
# 现在**反转**成断言完整——不是删掉断言，是换成"完整性"这条。
BULK = 68
geo3 = obj.createNode("geo", "C_geo3")
ai3 = node_mod.make_ai_node(geo3, "C_ai3", prompt="x")
for i in range(BULK):
    geo3.createNode("null", "C_bulk_%03d" % i)
info3 = context.describe(ai3)
H.check("★ 同级全部列出（%d 个，一个不少）" % BULK,
        len(info3["siblings"]) == BULK, len(info3["siblings"]))
H.check("★ 不许再有 siblings_truncated 这个字段（截断整个拆掉了）",
        "siblings_truncated" not in info3, sorted(info3.keys()))
H.check("名字都在（抽头尾各一个核对）",
        any(s.get("name") == "C_bulk_000" for s in info3["siblings"])
        and any(s.get("name") == "C_bulk_%03d" % (BULK - 1)
                for s in info3["siblings"]))

H.section("派发成工具也走同一条路")
from houdini_ai import schema

via_tool = schema.dispatch(ai, "list_context", {})
H.check("工具返回的父网络对",
        via_tool["parent"]["path"] == geo.path(), via_tool.get("parent"))
H.check("工具返回里同样越不过父网络",
        "C_OUTSIDE_MARKER" not in json.dumps(via_tool, ensure_ascii=False))

H.finish("test_context")
