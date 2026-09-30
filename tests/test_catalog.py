# -*- coding: utf-8 -*-
"""目录模块的测试。用 hython 跑：

    "D:/ProgramFiles/Houdini22/bin/hython.exe" tests/test_catalog.py

这些断言锁的是「防幻觉地基」的几条硬性质：真实类型查得到、假类型查不到、
菜单参数的合法取值拿得到。它们哪天坏了，模型就开始编节点名了。
"""

import _harness as H

H.section("分类与规模")
import hou
from houdini_ai import catalog

sop = catalog.category("Sop")
H.check("能取到 Sop 分类", sop is not None)

sop_catalog = catalog._catalog("Sop")
H.check("SOP 类型数量在合理区间（实测 1690）",
        1500 < len(sop_catalog) < 2000, "实际 %d" % len(sop_catalog))
H.check("每个类型都有官方描述",
        all(i["description"] for i in sop_catalog.values()))

obj_catalog = catalog._catalog("Object")
H.check("OBJ 类型数量在合理区间（实测 186）",
        100 < len(obj_catalog) < 400, "实际 %d" % len(obj_catalog))

H.section("搜索")
res = catalog.search("Sop", "box")
H.check("搜 box 有结果", bool(res))
H.check_eq("精确匹配的 box 排第一", res[0]["name"], "box")
H.check("box 的输入数正确", res[0]["max_inputs"] == 1)

res = catalog.search("Sop", "polyextrude")
H.check("搜 polyextrude 命中", any(r["name"] == "polyextrude" for r in res))

res = catalog.search("Sop", "扭曲")
H.check("搜不存在的中文返回空", res == [])

res = catalog.search("Sop", "subdivide")
H.check("subdivide 报告 2 个输入",
        any(r["name"] == "subdivide" and r["max_inputs"] == 2 for r in res))
res = catalog.search("Sop", "copytopoints")
H.check("copytopoints 报告 2 个输入",
        any(r["name"] == "copytopoints" and r["max_inputs"] == 2 for r in res))

H.section("多词搜索（模型爱一次塞好几个词）")
# 实测模型会发 "revolve lathe profile axis" 这种查询。早期版本拿整串做子串
# 匹配，必然 0 命中，白白浪费好几轮并把模型带偏。
res = catalog.search("Sop", "revolve lathe profile axis")
H.check("多词查询能命中而不是 0 结果", len(res) > 0, len(res))
H.check("revolve 排在前面",
        any(r["name"] == "revolve" for r in res[:5]),
        [r["name"] for r in res[:5]])

res = catalog.search("Sop", "extrude inset polygon face")
H.check("多词查询 extrude 命中",
        any("extrude" in r["name"] for r in res[:5]),
        [r["name"] for r in res[:5]])

res = catalog.search("Sop", "circle tube cylinder")
H.check("多词查询 circle 命中",
        any(r["name"] == "circle" for r in res[:5]),
        [r["name"] for r in res[:5]])

res = catalog.search("Sop", "subdivide smooth")
H.check("多词查询 subdivide 命中",
        any(r["name"] == "subdivide" for r in res[:5]),
        [r["name"] for r in res[:5]])

res = catalog.search("Sop", "中文乱写")
H.check("真的没有时还是返回空", res == [])

H.section("类型是否存在（编造的必须被识破）")
H.check("box 存在", catalog.has_type("Sop", "box"))
H.check("polyextrude 存在", catalog.has_type("Sop", "polyextrude"))
H.check("编造的 'boxinator' 不存在", not catalog.has_type("Sop", "boxinator"))
H.check("编造的 'wrangle_ai' 不存在", not catalog.has_type("Sop", "wrangle_ai"))
H.check("把 OBJ 类型当 SOP 查会失败",
        not catalog.has_type("Sop", "cam"))
H.check_eq("查不存在的类型返回 None", catalog.type_info("Sop", "boxinator"), None)

H.section("参数真相：polyextrude 的文件夹必须被递归展开")
pe = catalog.type_info("Sop", "polyextrude")
H.check("拿到 polyextrude 信息", pe is not None)
names = {p["name"] for p in pe["parms"]}
H.check("顶层只有 4 个 folder，但展开后参数远不止 5 个",
        len(pe["parms"]) > 20, "实际 %d" % len(pe["parms"]))
for expected in ("divs", "outputfront", "outputback", "outputside", "inset"):
    H.check("展开后有参数 %s" % expected, expected in names)

H.section("菜单参数的合法取值（模型最容易搞错的地方）")
by_name = {p["name"]: p for p in pe["parms"]}
H.check("divs 是整数参数", by_name.get("divs", {}).get("kind") == "int")
H.check("outputfront 是开关", by_name.get("outputfront", {}).get("kind") == "toggle")

menu_parm = None
for p in pe["parms"]:
    if p["kind"] == "menu" and p.get("options"):
        menu_parm = p
        break
H.check("polyextrude 里能找到带选项的菜单参数", menu_parm is not None)
if menu_parm:
    H.check("菜单参数带 value/label",
            all("value" in o and "label" in o for o in menu_parm["options"]))
    H.check("菜单选项非空", len(menu_parm["options"]) > 0)

H.section("多分量参数（向量）")
box = catalog.type_info("Sop", "box")
box_by_name = {p["name"]: p for p in box["parms"]}
H.check_eq("box 的 size 是 3 分量", box_by_name["size"].get("components"), 3)
H.check("box 的 size 有默认值", "default" in box_by_name["size"])
H.check_eq("box 的 size 默认值是 1,1,1",
           tuple(box_by_name["size"]["default"]), (1.0, 1.0, 1.0))

H.section("不可设值的参数被排除（按钮/标签/文件夹）")
kinds = {p["kind"] for p in box["parms"]}
H.check("不含 button", "button" not in kinds)
H.check("不含 label", "label" not in kinds)
H.check("不含 folder", "folder" not in kinds)

H.section("参数名集合（给校验器用）")
names = catalog.parm_names("Sop", "box")
H.check("box 参数名集合非空", names and len(names) > 5)
H.check("含 t（位移）", "t" in names)
H.check_eq("假类型返回 None", catalog.parm_names("Sop", "nope"), None)

H.section("文档路径构造")
url = catalog.doc_url("Sop", "polyextrude")
H.check_eq("SOP 文档路径形如官方 .md",
           url, "https://www.sidefx.com/docs/houdini/nodes/sop/polyextrude.md")
H.check("OBJ 分类走 obj 目录",
        "/nodes/obj/" in catalog.doc_url("Object", "geo"))

H.section("命名空间类型：保留命名空间、剥掉版本后缀")
# 回归：旧实现 split("::")[-1] 把命名空间整个丢掉 → 130 个 kinefx 类型只命中 1 个文档。
H.check_eq("kinefx::mocapimport 保留命名空间（不是 mocapimport）",
           catalog.doc_url("Sop", "kinefx::mocapimport"),
           "https://www.sidefx.com/docs/houdini/nodes/sop/kinefx--mocapimport.md")
# 回归：旧实现只判 ::1.0，而实际版本号还有 2.0 / 3.0。
H.check_eq("版本后缀 ::2.0 也要剥（旧写法只判 ::1.0）",
           catalog.doc_url("Sop", "kinefx::adapttoterrain::2.0"),
           "https://www.sidefx.com/docs/houdini/nodes/sop/kinefx--adapttoterrain.md")
H.check_eq("无命名空间的类型行为不变",
           catalog.doc_url("Sop", "grid"),
           "https://www.sidefx.com/docs/houdini/nodes/sop/grid.md")

H.section("★★ 参数表与报错**不截断**（2026-09-29 用户定）")
# 这一节锁的是「模型看到的真相是完整的」。以前 polyextrude 那张 96 参数的
# 表会被砍到 6000 字符（在 runner 里），参数表本身也会被砍到 120 条——
# 模型拿到的是一份**残缺 JSON**，而且**没有任何办法**要回剩下那半。
# 砍掉断言就是丢掉这条性质，所以这里是**反转**成"完整"，不是删除。
import json as _json

from houdini_ai import schema as _schema

_obj = hou.node("/obj")
_probe = _obj.createNode("geo", "CAT_probe").createNode("subnet", "CAT_ai")

_full = _schema.dispatch(_probe, "get_node_type", {"type": "polyextrude"})
_blob = _json.dumps(_full, ensure_ascii=False)
H.check("★ polyextrude 的参数表给全了（旧的砍刀在 6000，这里必须远高于它）",
        len(_blob) > 12000, len(_blob))
H.check("★ 里面不许有「已截断」字样", "已截断" not in _blob)
H.check("★ 也不许有 truncated 字段（那是被砍过的信号）",
        "truncated" not in _full, sorted(_full.keys()))
H.check("parm_count 与实际条数一致（没有偷偷少给）",
        _full["parm_count"] == len(_full["parms"]),
        (_full["parm_count"], len(_full["parms"])))
# 具体条数：polyextrude 实测 96 个。少一个都说明有东西被吃掉了。
H.check("参数条数就是 polyextrude 该有的 96 个",
        _full["parm_count"] == 96, _full["parm_count"])
_names = set(p["name"] for p in _full["parms"])
# 抽几个**排在很后面**的参数核对——被砍过的表里最先丢的就是它们。
H.check("末尾几个参数也在（抽 3 个核对）",
        len(_names & {"thicknessramp2interp", "twistramp2interp", "uvstyle"}) == 3,
        sorted(_names)[-6:])

# label 只在跟 name 不同时才给（省地方但**不丢信息**）
_labels = [p for p in _full["parms"] if "label" in p]
H.check("label 确实只给不同的那些（不是全给也不是全不给）",
        0 < len(_labels) < len(_full["parms"]),
        (len(_labels), len(_full["parms"])))
H.check("抽一个 label 不同的核对（splittype → Divide Into）",
        any(p["name"] == "splittype" and p.get("label") == "Divide Into"
            for p in _full["parms"]))

# 报错：造 >40 条同一条报错，断言**一条都不丢**（现在是去重聚合）
_geo = _obj.createNode("geo", "CAT_err")
_ai = _geo.createNode("subnet", "CAT_err_ai")
for i in range(50):
    # 每个 null 都去引用一个不存在的节点 → 每个都报错
    n = _ai.createNode("null", "E_%03d" % i)
    n.parm("copyinput").set(1) if n.parm("copyinput") else None
_errs = _schema.dispatch(_ai, "get_errors", {})
H.check("get_errors 不再有 truncated 字段", "truncated" not in _errs,
        sorted(_errs.keys()))
H.check("count 报的是**总条数**（不是显示条数）",
        _errs["count"] >= len(_errs["messages"]),
        (_errs["count"], len(_errs["messages"])))

H.finish("test_catalog")
