# -*- coding: utf-8 -*-
"""节点类型目录 + 参数真相。

这个模块是整套东西防幻觉的地基：模型只能用**从正在运行的 Houdini 里查出来的**
节点类型和参数。它编一个不存在的节点名，或者给一个菜单参数塞非法取值，
都会在 builder 落地之前被拦下来。

实测（Houdini 22.0.368）：
  * sopNodeTypeCategory() 有 1690 个类型，objNodeTypeCategory() 有 186 个，
    全部带官方 description，且 minNumInputs/maxNumInputs 可读。
  * NodeType.parmTemplateGroup() 返回的是顶层条目，FolderParmTemplate 要递归
    展开才有真参数——polyextrude 顶层只有 4 个 folder，真参数在里面。
  * 参数名是内部名（polyextrude 的 divs / outputfront / lt / lr / ls），
    跟界面上看到的一致，可以直接用来设值。

所有函数都是普通 hou 调用；「必须在主线程」这条纪律由调用方（runner）负责，
这样测试里能在 hython 直接跑。
"""

import json
import os
import re
import urllib.error
import urllib.request

_TOOL_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DOC_CACHE_DIR = os.path.join(_TOOL_ROOT, ".cache", "docs")

# 官方文档路径里的分类目录名。§6.7：每一页都能取 .md 原文。
_DOC_FOLDERS = {
    "Sop": "sop",
    "Object": "obj",
    "Dop": "dop",
    "Cop2": "cop2",
    "Cop": "cop",
    "Top": "top",
    "Lop": "lop",
    "Chop": "chop",
    "Vop": "vop",
    "Shop": "shop",
    "Driver": "out",
    "Manager": "manager",
}

# ★ 这里曾经有 `MAX_PARMS = 120`（参数多的节点砍到 120 条）。**拆掉了**——
# 见 `type_info` 里那段说明。参数表是模型写参数名的唯一依据，砍不得。

_TYPE_CACHE = {}
_PARM_CACHE = {}
_DOC_MEM = {}


def _categories():
    import hou

    return hou.nodeTypeCategories()


def category(name):
    """按名字取分类对象。'Sop' / 'Object' / 大小写不敏感。"""
    cats = _categories()
    if name in cats:
        return cats[name]
    for key, cat in cats.items():
        if key.lower() == str(name).lower():
            return cat
    return None


def category_of_node(node):
    """节点所在网络里能放什么类型——即它的 childTypeCategory。"""
    try:
        child = node.childTypeCategory()
        if child is not None:
            return child
    except Exception:
        pass
    return None


def _catalog(cat_name):
    """某个分类下全部类型的 {name: 简介}。构建一次后进程内缓存。"""
    if cat_name in _TYPE_CACHE:
        return _TYPE_CACHE[cat_name]

    import hou

    out = {}
    cat = category(cat_name)
    if cat is None:
        _TYPE_CACHE[cat_name] = out
        return out

    for name, ntype in cat.nodeTypes().items():
        try:
            desc = ntype.description()
        except Exception:
            desc = ""
        try:
            min_in = ntype.minNumInputs()
            max_in = ntype.maxNumInputs()
        except Exception:
            min_in, max_in = 0, 0
        out[name] = {
            "name": name,
            "description": desc,
            "min_inputs": min_in,
            "max_inputs": max_in,
        }
    _TYPE_CACHE[cat_name] = out
    return out


def search(cat_name, query, limit=25):
    """在真实类型里搜节点。空 query 返回该分类下最常用的一批名字。

    单词：按 精确 > 前缀 > 名字包含 > 描述包含 分桶。
    多词：按命中词数打分——模型很爱一次塞好几个词，比如
    "revolve lathe profile axis"。早期版本拿整串当子串匹配，这种查询
    必然 0 命中，白白浪费好几轮。现在拆开算。
    """
    catalog = _catalog(cat_name)
    q = (query or "").strip().lower()

    if not q:
        names = sorted(catalog)
        return [catalog[n] for n in names[:limit]]

    terms = [t for t in re.split(r"[\s,，、;；/|]+", q) if t]

    # 整串正好是个类型名，直接给它
    if q in catalog:
        return [catalog[q]][:limit]

    if len(terms) > 1:
        scored = []
        for name, info in catalog.items():
            low = name.lower()
            desc = (info["description"] or "").lower()
            score = 0
            for term in terms:
                if term == low:
                    score += 8
                elif low.startswith(term):
                    score += 4
                elif term in low:
                    score += 3
                elif term in desc:
                    score += 1
            if score > 0:
                scored.append((score, len(name), name, info))
        scored.sort(key=lambda item: (-item[0], item[1], item[2]))
        return [info for _, _, _, info in scored[:limit]]

    exact, prefix, in_name, in_desc = [], [], [], []
    for name, info in catalog.items():
        low = name.lower()
        if low == q:
            exact.append(info)
        elif low.startswith(q):
            prefix.append(info)
        elif q in low:
            in_name.append(info)
        elif q in (info["description"] or "").lower():
            in_desc.append(info)

    # 短名字优先：搜 "box" 时 box 该排在 box_clip 前面。
    for bucket in (prefix, in_name, in_desc):
        bucket.sort(key=lambda i: (len(i["name"]), i["name"]))

    return (exact + prefix + in_name + in_desc)[:limit]


def resolve_type(cat_name, type_name):
    """把类型名归一成分类目录里真实存在的那个名字。

    节点的 type().name() 常带命名空间和版本（polyextrude::2.0），
    而目录里存的是 'polyextrude'。两种写法都得认，否则回滚快照
    会解析不了自己导出的东西。
    """
    if not type_name:
        return None
    catalog = _catalog(cat_name)
    if type_name in catalog:
        return type_name
    bare = type_name.split("::")[0]
    if bare in catalog:
        return bare
    # 有些类型的 name() 带版本后缀但目录里是全名
    for key in catalog:
        if key.split("::")[0] == bare:
            return key
    return None


def has_type(cat_name, type_name):
    return resolve_type(cat_name, type_name) is not None


def _parm_kind(template):
    """把 ParmTemplate 归类成模型看得懂的几种。返回 (kind, settable)。"""
    import hou

    if isinstance(template, hou.FolderParmTemplate):
        return "folder", False
    if isinstance(template, hou.FolderSetParmTemplate):
        return "folder", False
    if isinstance(template, hou.ButtonParmTemplate):
        return "button", False
    if isinstance(template, hou.LabelParmTemplate):
        return "label", False
    if isinstance(template, hou.SeparatorParmTemplate):
        return "separator", False
    if isinstance(template, hou.MenuParmTemplate):
        return "menu", True
    if isinstance(template, hou.ToggleParmTemplate):
        return "toggle", True
    if isinstance(template, hou.StringParmTemplate):
        return "string", True
    if isinstance(template, hou.IntParmTemplate):
        return "int", True
    if isinstance(template, hou.FloatParmTemplate):
        return "float", True
    if isinstance(template, hou.RampParmTemplate):
        return "ramp", False
    if isinstance(template, hou.DataParmTemplate):
        return "data", False
    # IntParmTemplate 的子类（如菜单）已在上面处理，剩下的按数值当浮点
    return "other", False


def _describe_parm(template, default=None):
    info = {
        "name": template.name(),
        "kind": _parm_kind(template)[0],
    }
    # label 只在**跟名字不一样**时才给。绝大多数参数两者同形（`group` /
    # "Group"），逐条重复一遍纯属白占地方；真正不同的那些（`splittype` /
    # "Divide Into"）才是有用的信息，一条都不能少。
    label = template.label()
    if label and label != template.name():
        info["label"] = label

    ncomp = 1
    try:
        ncomp = template.numComponents()
    except Exception:
        pass
    if ncomp > 1:
        info["components"] = ncomp
    # 注：这里原来**重复赋值了一次 default**（同一个键写两遍），删掉了。
    if default is not None:
        info["default"] = _jsonable(default)

    # 菜单参数：给合法取值，这是模型最容易搞错的地方。
    if info["kind"] == "menu":
        try:
            items = list(template.menuItems())
            labels = list(template.menuLabels()) or items
            info["options"] = [
                {"value": v, "label": l} for v, l in zip(items, labels)
            ]
        except Exception:
            pass

    # 数值范围。注意：min/max 是**滑杆范围**，只有 minIsStrict/maxIsStrict
    # 为真时才是硬限制。box 的 size 是 -1..1 且不 strict，设成 2 完全合法——
    # 拿滑杆范围当硬限制会把正常需求全拒掉（实测踩过这个坑）。
    for key, getter, strict_getter in (
        ("min", "minValue", "minIsStrict"),
        ("max", "maxValue", "maxIsStrict"),
    ):
        try:
            val = getattr(template, getter)()
            if val is not None:
                info[key] = _jsonable(val)
            strict = getattr(template, strict_getter)()
            if strict:
                info["%s_strict" % key] = True
        except Exception:
            pass

    return info


def _jsonable(value):
    try:
        if isinstance(value, (list, tuple)):
            return [_jsonable(v) for v in value]
        if isinstance(value, (int, float, str, bool)) or value is None:
            return value
        return str(value)
    except Exception:
        return str(value)


def _scratch_parent(cat_name):
    """给探测用的临时落脚点。返回 (parent, 需要销毁的节点或 None)。"""
    import hou

    obj = hou.node("/obj")
    if obj is None:
        return None, None
    if cat_name == "Sop":
        geo = obj.createNode("geo", "__ai_probe__")
        return geo, geo
    return obj, None


def _live_parms(node):
    """从一个**真实实例**上读参数结构。

    这是本模块最重要的一条教训：类型模板会说谎。
    polyextrude 的模板里有 `lt`，但实例上根本没有这个参数——Houdini 用
    `stdswitcher` 机制把模板里的候选页折叠成生效页，实例上叫 `translate`
    （分量叫 translatex/translatey/translatez）。如果照着模板告诉模型用 `lt`，
    模型会写一个永远设不上、又不报错的参数——静默失效，最难查的那种 bug。

    多分量参数的分量命名也没规律：size->sizex/sizey/sizez，但
    divrate->divrate1/2/3。只能读，不能猜。所以这里一律以实例为准。
    """
    out = []
    try:
        tuples = node.parmTuples()
    except Exception:
        return out

    for tup in tuples:
        try:
            template = tup.parmTemplate()
        except Exception:
            template = None
        if template is None:
            try:
                template = tup[0].parmTemplate()
            except Exception:
                continue

        kind, settable = _parm_kind(template)
        if not settable:
            continue

        try:
            default = tup.eval()
        except Exception:
            default = None

        try:
            comps = [p.name() for p in tup]
        except Exception:
            comps = []

        info = _describe_parm(template, default)
        info["name"] = tup.name()
        if len(comps) > 1:
            info["components"] = len(comps)
            info["component_names"] = comps
        out.append(info)

    return out


def type_info(cat_name, type_name, probe_parent=None):
    """某个节点类型的参数真相——**从一个真实实例上读出来的**。

    probe_parent 给定时在它内部临时建一个探测节点；没给就自己搭个临时 geo。
    探测节点读完立刻销毁，场景里不会留下东西。

    返回 None 表示这个类型压根不存在——这本身就是最有用的答案，
    因为它意味着模型在编。
    """
    cat = category(cat_name)
    if cat is None:
        return None
    resolved = resolve_type(cat_name, type_name)
    if resolved is None:
        return None

    cache_key = (cat_name, resolved)
    if cache_key in _PARM_CACHE:
        return _PARM_CACHE[cache_key]

    ntype = cat.nodeTypes().get(resolved)
    if ntype is None:
        return None

    result = {
        "name": resolved,
        "description": ntype.description() if hasattr(ntype, "description") else "",
        "min_inputs": ntype.minNumInputs(),
        "max_inputs": ntype.maxNumInputs(),
    }

    probe = None
    scratch = None
    parms = []
    try:
        parent = probe_parent
        if parent is None:
            parent, scratch = _scratch_parent(cat_name)
        if parent is not None:
            probe = parent.createNode(resolved, "__ai_probe_node__")
            parms = _live_parms(probe)
    except Exception as exc:
        result["parms_error"] = "%s: %s" % (type(exc).__name__, exc)
    finally:
        try:
            if probe is not None:
                probe.destroy()
        except Exception:
            pass
        try:
            if scratch is not None:
                scratch.destroy()
        except Exception:
            pass

    # ★ 参数表**给全**。这里曾经有个 `MAX_PARMS = 120` 的砍刀（用户
    # 2026-09-29 定：不要截断任何内容）。砍掉的正是模型**最需要**的东西：
    # 它写参数名靠的就是这张表，砍到 120 条之后剩下的那些参数它**永远
    # 查不到**，只能猜——猜错就被拒，白烧一个回合。
    result["parm_count"] = len(parms)
    result["parms"] = parms

    _PARM_CACHE[cache_key] = result
    return result


def parm_index(cat_name, type_name, probe_parent=None):
    """{参数名: 参数信息}，**同时收录元组名和分量名**。

    模型写 "size": [2,2,0.4]（元组名）和写 "sizex": 2（分量名）都该能用，
    所以两个名字都要能在索引里查到，指回同一条信息。
    """
    info = type_info(cat_name, type_name, probe_parent=probe_parent)
    if not info:
        return None
    index = {}
    for parm in info.get("parms", []):
        index[parm["name"]] = parm
        for comp in parm.get("component_names", []):
            index.setdefault(comp, parm)
    return index


def parm_names(cat_name, type_name, probe_parent=None):
    index = parm_index(cat_name, type_name, probe_parent=probe_parent)
    return None if index is None else set(index)


# 类型名尾部的版本后缀，如 kinefx::adapttoterrain::2.0
_NS_VERSION = re.compile(r"::\d+(\.\d+)?$")


def doc_url(cat_name, type_name):
    folder = _DOC_FOLDERS.get(cat_name)
    if not folder:
        return ""
    # 命名空间类型在文档里写作 <命名空间>--<类型>，且**不带版本后缀**。
    # 实测（本机 nodes.zip，5034 个条目）：
    #   kinefx::mocapimport          → sop/kinefx--mocapimport.txt   ✓
    #   kinefx::adapttoterrain::2.0  → sop/kinefx--adapttoterrain.txt（带 ::2.0 的那份不存在）
    # 旧写法把命名空间整个丢掉（split("::")[-1]），130 个 kinefx 类型只命中 1 个；
    # 且只判 ::1.0，而实际版本号有 2.0 和 3.0，所以按正则剥。
    bare = _NS_VERSION.sub("", type_name)
    bare = bare.replace("::", "--")
    return "https://www.sidefx.com/docs/houdini/nodes/%s/%s.md" % (folder, bare)


def fetch_doc(cat_name, type_name, timeout=20, use_cache=True):
    """抓节点官方文档（.md 原文），磁盘缓存。

    离线或抓不到时返回 (None, 原因)，调用方优雅降级——文档是加分项，
    不是必需品，模型靠参数真相也能干活。
    """
    url = doc_url(cat_name, type_name)
    if not url:
        return None, "这个分类没有公开文档路径"

    if url in _DOC_MEM:
        return _DOC_MEM[url], ""

    cache_path = ""
    if use_cache:
        safe = re.sub(r"[^A-Za-z0-9_.-]", "_", "%s_%s" % (cat_name, type_name))
        cache_path = os.path.join(DOC_CACHE_DIR, safe + ".md")
        if os.path.isfile(cache_path):
            try:
                with open(cache_path, "r", encoding="utf-8") as fh:
                    text = fh.read()
                _DOC_MEM[url] = text
                return text, ""
            except Exception:
                pass

    try:
        req = urllib.request.Request(url, headers={"user-agent": "houdini-ai/1.0"})
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            text = resp.read().decode("utf-8", "replace")
    except urllib.error.HTTPError as exc:
        return None, "HTTP %s" % exc.code
    except Exception as exc:
        return None, str(exc)

    _DOC_MEM[url] = text
    if cache_path:
        try:
            os.makedirs(os.path.dirname(cache_path), exist_ok=True)
            with open(cache_path, "w", encoding="utf-8") as fh:
                fh.write(text)
        except Exception:
            pass
    return text, ""


def clear_caches():
    _TYPE_CACHE.clear()
    _PARM_CACHE.clear()
    _DOC_MEM.clear()
