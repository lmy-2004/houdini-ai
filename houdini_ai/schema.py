# -*- coding: utf-8 -*-
"""给模型看的工具定义，以及这些工具的落地实现。

工具分两类：
  * **只读**（list_container / search_node_types / get_node_type / get_node_docs /
    get_geometry / get_errors）——便宜，可以随便调，鼓励它先查后写。
  * **写入**（build / update_parms / finish）——批量的，一次提交整张网络。

刻意**没有**的东西：执行任意 Python、读写文件、调 shell、渲染、导出。
调研 §8.1 指出 MCP 方案的本质是「在你 Houdini 里开一个无沙箱的 shell」，
这里从根本上不给这个能力。
"""

from . import builder, catalog

# ★ 这里曾经有 `MAX_DOC_CHARS = 6000`：官方文档正文超过 6000 字符就砍。
# **拆掉了**（用户 2026-09-29 定：不要截断任何内容）。文档是模型用来
# 搞清楚"这个节点到底怎么用"的，砍一半它就会按猜的来。

# --------------------------------------------------------------------------
# 定义（发给模型的 tools 字段）
# --------------------------------------------------------------------------

_NODE_SPEC = {
    "type": "object",
    "properties": {
        "name": {"type": "string",
                 "description": "节点在本网络内的唯一名字，字母数字下划线"},
        "type": {"type": "string",
                 "description": "节点类型名，必须是 search_node_types 返回过的"},
        "params": {"type": "object",
                   "description": "参数名到值的映射。多分量可以写元组名 "
                                  "{\"size\":[2,2,0.4]} 或分量名 {\"sizex\":2}。"
                                  "以 get_node_type 返回的为准"},
        "expressions": {"type": "object",
                        "description": "可选。参数名到 Hscript 表达式的映射。"
                                       "一般不用手写——提升参数（promote）会自动"
                                       "生成。只有你要自己建引用关系时才用"},
    },
    "required": ["name", "type"],
}

_PROMOTE_SPEC = {
    "type": "object",
    # 准则（挑哪些、什么算值得）在系统提示词里；这里只留逐字段的契约。
    # ★ 但**用途**那半句要留在 `build.promote` 的 description 里——见那儿的注释。
    "description": "一个要提升到容器上的参数。",
    "properties": {
        "node": {"type": "string", "description": "内部节点名"},
        "parm": {"type": "string", "description": "该节点上的参数名（元组名）"},
        "as": {"type": "string",
               "description": "可选，在容器上显示成什么参数名。省略则用 "
                              "节点名_参数名"},
        "label": {"type": "string",
                  "description": "可选，参数界面上显示的中文标签"},
        "min": {"type": "number", "description": "可选，滑杆最小值"},
        "max": {"type": "number", "description": "可选，滑杆最大值"},
    },
    "required": ["node", "parm"],
}

TOOL_DEFS = [
    {
        "name": "list_container",
        "description": "看看容器内部现在有什么：节点、连接、非默认参数值，"
                       "以及容器自己的哪几个输入接了线。每轮开工前先调它。",
        "input_schema": {"type": "object", "properties": {}},
    },
    {
        "name": "search_node_types",
        "description": "在 Houdini 真实的节点类型目录里搜索（SOP 有 1690 个，"
                       "带官方描述）。",
        "input_schema": {
            "type": "object",
            "properties": {
                "query": {"type": "string",
                          "description": "关键词，比如 twist / noise / extrude / "
                                         "scatter / copy"},
                "limit": {"type": "integer",
                          "description": "最多返回几条，默认 20"},
            },
            "required": ["query"],
        },
    },
    {
        "name": "get_node_type",
        "description": "查一个节点类型的完整参数真相：名称、标签、类型、默认值、"
                       "合法菜单取值、范围。",
        "input_schema": {
            "type": "object",
            "properties": {"type": {"type": "string"}},
            "required": ["type"],
        },
    },
    {
        "name": "get_node_docs",
        "description": "取某个节点的 SideFX 官方文档正文。不确定一个节点怎么用时看它。"
                       "首次调用要联网，之后走本地缓存。",
        "input_schema": {
            "type": "object",
            "properties": {"type": {"type": "string"}},
            "required": ["type"],
        },
    },
    {
        "name": "get_geometry",
        "description": "看某个节点的产出：点数、面数、属性、包围盒。用来验证"
                       "「跑通了但没东西出来」这类问题。",
        "input_schema": {
            "type": "object",
            "properties": {"node": {"type": "string",
                                    "description": "节点名，或留空看容器整体"}},
        },
    },
    {
        "name": "get_errors",
        "description": "让容器 cook 一遍，取回报错和警告原文。建完一定要调一次。",
        "input_schema": {"type": "object", "properties": {}},
    },
    {
        "name": "list_context",
        "description": "看容器**外面一层**：上游接的是谁、下游有谁在消费你的输出、"
                       "同一个网络里还有哪些节点。只读，只到父网络这一层。\n"
                       "要**大改输出结构之前**先调它一次。",
        "input_schema": {"type": "object", "properties": {}},
    },
    {
        "name": "look",
        "description": "把容器渲染成一张图给你自己看（离屏，不开窗口、不动用户的"
                       "视口；相机自动框住容器）。用法和限制见系统提示词。",
        "input_schema": {
            "type": "object",
            "properties": {
                "view": {
                    "type": "string",
                    "enum": ["+z", "-z", "+x", "-x", "+y", "-y", "six"],
                    "description": "**必填。** 从哪个方向看："
                                   "+z=前视、-z=后视、+x=右视、-x=左视、"
                                   "+y=俯视、-y=仰视；"
                                   "six=六视图（六个方向排成一张图，"
                                   "想一次看全就用它）",
                },
                "preset": {
                    "type": "string",
                    "description": "渲染方式。shaded=带光影(默认)；flat=无光影"
                                   "看剪影和结构；wireframe=线框；"
                                   "wireframe_ghost=线框+半透明面看内部；"
                                   "hidden_line=隐藏线；shaded_wire=光影+线框",
                },
                "attribute": {
                    "type": "string",
                    "description": "可选。想看某个**属性的分布**时填属性名"
                                   "（比如 density、height、mask），"
                                   "系统会把它烘成颜色再渲。属性必须真实存在",
                },
            },
            # ★ 本仓库**第一个必填工具参数**。注意 `schema.dispatch` 不做
            #   input_schema 校验，所以"必填"要在 `_t_look` 里自己判一遍。
            "required": ["view"],
        },
    },
    {
        "name": "build",
        "description": "一次提交整张网络。会替换容器里的全部内容；有任何一处不"
                       "合法会被**整份拒绝**并告诉你原因，不会留下半成品。",
        "input_schema": {
            "type": "object",
            "properties": {
                "nodes": {"type": "array", "items": _NODE_SPEC,
                          "description": "全部节点"},
                "connections": {
                    "type": "array",
                    "description": "连线。from 写节点名，或写容器的输入"
                                   "（__input0__ …）",
                    "items": {
                        "type": "object",
                        "properties": {
                            "from": {"type": "string"},
                            "to": {"type": "string"},
                            "input": {"type": "integer",
                                      "description": "接到目标的第几个输入，默认 0"},
                        },
                        "required": ["from", "to"],
                    },
                },
                "output": {"type": "string",
                           "description": "哪个节点的结果作为容器输出。"
                                          "省略则取最后一个"},
                "name": {"type": "string",
                         "description": "给**容器自己**起的名字，英文小写下划线"},
                "promote": {"type": "array", "items": _PROMOTE_SPEC,
                            # ★ 用途那半句**必须留在这里**：模型填这个字段时看的
                            # 就是它。上一轮去重把这半句删了、只留沿用/撤销的契约，
                            # 结果一个只看工具定义的模型根本不知道这字段是干嘛的。
                            # 「挑哪些参数」仍然只在系统提示词里（准则不该抄两遍）。
                            "description": "把内部网络的参数提升成容器自己的参数，"
                                           "让用户在容器外面直接就能调。\n"
                                           "不给这个字段 = 沿用上一轮的提升清单；"
                                           "给空数组 = 全部撤掉；给了就必须把想保留的"
                                           "都列全（它要的是完整清单，不是增量）。\n"
                                           "挑哪些参数、什么算值得挑，见系统提示词。"},
            },
            "required": ["nodes"],
        },
    },
    {
        "name": "update_parms",
        "description": "只改参数值，不动结构。适合「再高一点」这种微调。",
        "input_schema": {
            "type": "object",
            "properties": {
                "ops": {
                    "type": "array",
                    "items": {
                        "type": "object",
                        "properties": {
                            "node": {"type": "string"},
                            "parm": {"type": "string"},
                            "value": {},
                        },
                        "required": ["node", "parm", "value"],
                    },
                },
            },
            "required": ["ops"],
        },
    },
    {
        "name": "remember",
        "description": "往**这个 .hip 的共享经验**里记一条，给同一个文件里其它 AI "
                       "节点看（记忆跟着 .hip 文件走）。\n"
                       "该记的，比如：\n"
                       "  · 「这个项目里 gltf 导入的 uv 是 3 分量」\n"
                       "  · 「用户偏好用 attribwrangle 而不是堆一堆小节点」\n"
                       "  · 「polyextrude 的 translate 是 3 分量参数，不是单值」\n"
                       "**不要**记这一次建了什么、这一轮的结果——那些已经在对话历史"
                       "和 finish 摘要里了。",
        "input_schema": {
            "type": "object",
            "properties": {
                "text": {"type": "string",
                         "description": "一到两句话，能独立读懂，不用「它」「这个」"
                                        "指代上文"},
            },
            "required": ["text"],
        },
    },
    {
        "name": "finish",
        "description": "声明做完了。调用后系统会自动 cook 一遍做校验；"
                       "如果有报错会把报错发回给你继续修。\n"
                       "两道硬性要求：**summary 不能是空的**；**容器里必须有"
                       "节点**。不满足会被拒（不会静默当成完成）。",
        "input_schema": {
            "type": "object",
            "properties": {
                "summary": {"type": "string",
                            "description": "中文，写给用户看。内容要求见系统提示词"},
            },
            "required": ["summary"],
        },
    },
]


# --------------------------------------------------------------------------
# 实现
# --------------------------------------------------------------------------

def dispatch(container, name, args):
    """执行一个工具调用，返回一个可 JSON 序列化的结果字典。

    任何工具抛异常都会被转成 {"error": ...} 交回给模型——工具报错是它继续
    干活的输入，不该让整个循环崩掉。
    """
    args = args or {}
    try:
        handler = _HANDLERS.get(name)
        if handler is None:
            return {"error": "没有叫 %r 的工具" % name}
        return handler(container, args)
    except builder.SpecError as exc:
        return {"ok": False, "rejected": True, "problems": exc.problems}
    except Exception as exc:
        import traceback

        return {"error": "%s: %s" % (type(exc).__name__, exc),
                "traceback": traceback.format_exc()[-1200:]}


def _cat(container):
    cat = catalog.category_of_node(container)
    return cat.name() if cat else None


def _t_list_container(container, args):
    return builder.summarize(container)


def _t_search_node_types(container, args):
    cat_name = _cat(container)
    query = (args.get("query") or "").strip()
    limit = args.get("limit") or 20
    try:
        limit = max(1, min(int(limit), 60))
    except Exception:
        limit = 20
    found = catalog.search(cat_name, query, limit=limit)
    result = {
        "category": cat_name,
        "query": query,
        "count": len(found),
        "results": found,
        "note": "这些是 Houdini 里真实存在的类型，只能用这里出现过的名字",
    }
    if not found:
        result["hint"] = ("没命中。换一个**单个**的英文关键词再试，"
                          "比如 box / twist / noise / scatter / extrude / "
                          "subdivide / copy。别把好几个词塞在一起。")
    return result


def _t_get_node_type(container, args):
    cat_name = _cat(container)
    type_name = args.get("type") or ""
    info = catalog.type_info(cat_name, type_name, probe_parent=container)
    if info is None:
        close = catalog.search(cat_name, type_name, limit=6)
        return {
            "error": "类型 %r 在 %s 里不存在" % (type_name, cat_name),
            "did_you_mean": [c["name"] for c in close],
        }
    return info


def _t_get_node_docs(container, args):
    cat_name = _cat(container)
    type_name = args.get("type") or ""
    if catalog.resolve_type(cat_name, type_name) is None:
        return {"error": "类型 %r 不存在" % type_name}
    text, why = catalog.fetch_doc(cat_name, type_name)
    if text is None:
        return {"error": "取不到文档（%s）" % why,
                "note": "没文档也能干活，用 get_node_type 的参数信息就够了"}
    return {"type": type_name, "docs": text}


def _find_in_container(container, node_name):
    if not node_name:
        return container
    for child in container.children():
        if child.name() == node_name:
            return child
    return None


def _t_get_geometry(container, args):
    node = _find_in_container(container, args.get("node"))
    if node is None:
        have = ", ".join(sorted(c.name() for c in container.children()))
        return {"error": "容器里没有叫 %r 的节点。现有：%s"
                         % (args.get("node"), have or "（空）")}
    geo = node.geometry()
    if geo is None:
        return {"node": node.name(), "has_geometry": False, "points": 0,
                "note": "没有几何。多半是上游没接东西，或者节点还没 cook"}

    result = {
        "node": node.name(),
        "points": geo.pointCount(),
        "primitives": geo.primCount(),
        "vertices": geo.vertexCount(),
    }
    try:
        bbox = geo.boundingBox()
        result["bbox"] = {
            "min": [round(v, 4) for v in bbox.minvec()],
            "max": [round(v, 4) for v in bbox.maxvec()],
            "size": [round(v, 4) for v in bbox.sizevec()],
        }
    except Exception:
        pass

    try:
        attrs = {}
        for group, label in ((geo.pointAttribs(), "point"),
                             (geo.primAttribs(), "prim"),
                             (geo.vertexAttribs(), "vertex"),
                             (geo.globalAttribs(), "detail")):
            names = [a.name() for a in group]
            if names:
                attrs[label] = names
        if attrs:
            result["attributes"] = attrs
    except Exception:
        pass

    try:
        result["groups"] = [g.name() for g in geo.primGroups()]
    except Exception:
        pass

    if result["points"] == 0:
        result["warning"] = "点数为 0——网络跑通了但没有产出，检查上游和参数"
    return result


def _t_get_errors(container, args):
    problems = []
    try:
        container.cook(force=True)
    except Exception as exc:
        problems.append("cook 失败：%s" % exc)

    for node in [container] + list(container.allSubChildren()):
        try:
            for message in node.errors():
                problems.append("[%s] 错误：%s" % (node.path(), message))
            for message in node.warnings():
                problems.append("[%s] 警告：%s" % (node.path(), message))
        except Exception:
            continue

    # ★ 去重聚合，而不是截断（以前是 `problems[:40]`，砍掉的就永远看不到了）。
    # 同一条报错常常在每个子节点上重复一遍——一个没连线的上游，下面十个节点
    # 会报同一句话。聚成「这条 × N ← 在哪些节点上」**信息一条不丢**，
    # 而且比逐条列出来短得多。这是"信息效率"，不是"剪短"。
    order = []
    seen = {}
    for item in problems:
        path, sep, body = item.partition("] ")
        body = body if sep else item
        entry = seen.get(body)
        if entry is None:
            entry = seen[body] = {"first": item, "places": []}
            order.append(entry)
        if sep:
            entry["places"].append(path.lstrip("["))
    messages = []
    for entry in order:
        if len(entry["places"]) > 1:
            messages.append("%s  ← 同一条在 %d 处：%s"
                            % (entry["first"], len(entry["places"]),
                               "、".join(entry["places"])))
        else:
            messages.append(entry["first"])

    result = {
        "ok": not problems,
        "count": len(problems),
        "messages": messages,
    }
    try:
        result["output_points"] = (
            container.geometry().pointCount()
            if container.geometry() is not None else 0)
    except Exception:
        pass
    return result


def _t_build(container, args):
    spec = {
        "nodes": args.get("nodes") or [],
        "connections": args.get("connections") or [],
        "output": args.get("output"),
    }
    if args.get("name"):
        spec["name"] = args["name"]
    # 注意这里用 in 而不是真值判断：给了空数组和没给是**两种不同的意思**
    # （撤掉全部提升 vs 沿用上一轮），不能被 falsy 判断吞掉。
    if "promote" in args:
        spec["promote"] = args.get("promote")
    # expressions 是**每个节点自己**的字段（在 nodes[] 里），不是顶层字段

    summary = builder.apply(container, spec)
    result = {"ok": True, "built": summary}
    # 建完顺手把报错带回去，模型不用再多调一次
    errors = _t_get_errors(container, {})
    if not errors["ok"]:
        result["cook_problems"] = errors["messages"]
        result["hint"] = "网络建好了但 cook 有报错，请修正"
    result["output_points"] = errors.get("output_points")
    return result


def _t_update_parms(container, args):
    summary = builder.set_parms(container, args.get("ops") or [])
    result = {"ok": True, "now": summary}
    errors = _t_get_errors(container, {})
    result["output_points"] = errors.get("output_points")
    if not errors["ok"]:
        result["cook_problems"] = errors["messages"]
    return result


def _t_list_context(container, args):
    from . import context

    return context.describe(container)


def _t_remember(container, args):
    from . import memory

    text = (args.get("text") or "").strip()
    ok, notes, why = memory.add(text, source=container.path())
    result = {
        "ok": ok,
        "message": why,
        "count": len(notes),
        "all_notes": [{"from": n.get("from") or "?", "text": n["text"]}
                      for n in notes],
    }
    if ok:
        result["note"] = ("这是当前全部共享经验。下次记之前先看一遍，"
                          "别重复记已经有了的。")
    return result


def _t_look(container, args):
    """出图。返回里带 `__images__`，由 runner 拆成图像块发给模型。"""
    from . import config, viewport

    cfg = config.load()
    if not cfg.get("screenshot_enabled"):
        return {"error": "图像通道被关掉了（config.json 的 screenshot_enabled）"}

    # ★ `view` 是**必填**（用户 2026-09-29 定：接口必须强制一个视角）。
    #   校验放在这里、**排在 render() 之前**（也就排在那道"一个会话一张图"的
    #   保险之前）。理由：缺参/取值非法是**调用方的契约错误**——模型改一个词
    #   就能过，不用等下一轮；而"这个会话已经出过图"是**运行时状态**。
    #   两者同时成立时，报契约错误更有用。
    view = (args.get("view") or "").strip()
    if not view:
        return {"error": "必须指定视角（view）。可选：%s"
                         % "；".join(viewport.views_menu()),
                "views": viewport.view_names()}
    if view not in viewport.VIEWS:
        return {"error": "没有 %r 这个视角。可选：%s"
                         % (view, "；".join(viewport.views_menu())),
                "views": viewport.view_names()}

    preset = (args.get("preset") or viewport.DEFAULT_PRESET).strip()
    attribute = (args.get("attribute") or "").strip() or None
    size = cfg.get("screenshot_size") or None
    if size:
        try:
            size = (int(size[0]), int(size[1]))
        except Exception:
            size = None

    try:
        data, note = viewport.render(
            container, preset=preset, size=size, attribute=attribute,
            view=view,
            allow_repeat=bool(cfg.get("screenshot_allow_repeat")))
    except viewport.ShotError as exc:
        return {"error": str(exc), "presets": viewport.preset_names(),
                "views": viewport.view_names()}
    except Exception as exc:
        return {"error": "%s: %s" % (type(exc).__name__, exc)}

    return {
        "ok": True,
        "note": note,
        "view": view,
        "presets": viewport.preset_names(),
        "views": viewport.view_names(),
        "__images__": [data],
    }


def _t_finish(container, args):
    """收尾。★ **两道守卫**——它们是防「假成功」的最后一道闸。

    实测踩过：模型输出被长度上限截断时，工具调用的参数会变成**空字典 `{}`**
    （`stop_reason="max_tokens"`）。于是 `finish` 会拿着空 summary 走完全程，
    这一轮被记成绿色「完成」，而节点注释上一片空白——这套工具集最忌讳的就是
    假成功（界面说成了，实际什么都没交代）。宁可拒绝它、让它重发一次。

    第二道守卫（容器是空的）防的是另一种假成功：网络一个节点都没建出来，
    模型却写了一篇像模像样的说明就收工。`build` 那条路早就被 `SpecError`
    拦住了，只有 `finish` 漏了。
    """
    summary = (args.get("summary") or "").strip()
    if not summary:
        return {
            "ok": False,
            "rejected": True,
            "problems": [
                "finish 必须带 summary，这次是空的。如果你确实发了参数"
                "（比如回复中途被长度上限截断），请重新发一次；否则用中文写清楚"
                "你建了什么、大致原理、以及提升了哪几个参数让他调。",
            ],
        }

    try:
        empty = not container.children()
    except Exception:
        empty = False
    if empty:
        return {
            "ok": False,
            "rejected": True,
            "problems": [
                "容器里一个节点都没有，没有可收尾的网络。请先用 build 把完整"
                "网络提交上来，建好之后再来 finish。",
            ],
        }

    errors = _t_get_errors(container, {})
    return {
        "ok": True,
        "finished": True,
        "summary": summary,
        "cook_ok": errors["ok"],
        "cook_problems": errors["messages"] if not errors["ok"] else [],
        "output_points": errors.get("output_points"),
    }


_HANDLERS = {
    "list_container": _t_list_container,
    "list_context": _t_list_context,
    "search_node_types": _t_search_node_types,
    "get_node_type": _t_get_node_type,
    "get_node_docs": _t_get_node_docs,
    "get_geometry": _t_get_geometry,
    "get_errors": _t_get_errors,
    "look": _t_look,
    "build": _t_build,
    "update_parms": _t_update_parms,
    "remember": _t_remember,
    "finish": _t_finish,
}


def tool_names():
    return sorted(_HANDLERS)
