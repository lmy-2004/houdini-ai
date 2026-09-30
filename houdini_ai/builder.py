# -*- coding: utf-8 -*-
"""把模型给的一份网络描述，校验后落地成真的节点网络。

三条设计纪律，都是为了让「AI 搭网络」这件事不至于失控：

1. **边界是结构性保证的，不是靠模型自觉。** 描述格式里根本没有「路径」这个概念，
   节点只用名字引用，而名字只可能在容器内部解析。模型**没法表达**「去动容器外
   的节点」，因为语法里不存在这种说法。这比在运行时黑名单过滤要牢靠得多。

2. **整批校验通过才落地。** 先规划（校验 + 归一化取值），规划全过了才动场景，
   所以不会留下半成品。模型下一轮照着错误清单改就行。

3. **参数以真实实例为准。** 见 catalog._live_parms 的说明——类型模板会说谎。

落地一律包在 `hou.undos.group()` 里，所以你在 Houdini 里按一次 Ctrl+Z
就能把 AI 这一整轮的操作全退掉。
"""

import json

from . import catalog

# 描述里引用 Subnet 自己输入端的保留名。对应 sub.indirectInputs()[i]。
INPUT_PREFIX = "__input"
INPUT_SUFFIX = "__"
MAX_INPUTS = 4

# Houdini 数值参数没设范围时会返回一个巨大的哨兵值，这种就别当范围使。
_SENTINEL = 1e29

_VALID_NAME = None


class SpecError(Exception):
    """校验不通过。problems 是给模型看的错误清单。"""

    def __init__(self, problems):
        self.problems = list(problems)
        super(SpecError, self).__init__("\n".join(self.problems))


def _name_ok(name):
    global _VALID_NAME
    if _VALID_NAME is None:
        import re

        _VALID_NAME = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
    return bool(_VALID_NAME.match(name or ""))


def is_input_token(name):
    """'__input0__' -> 0；不是这个形式就返回 None。"""
    if not isinstance(name, str):
        return None
    if not (name.startswith(INPUT_PREFIX) and name.endswith(INPUT_SUFFIX)):
        return None
    middle = name[len(INPUT_PREFIX):-len(INPUT_SUFFIX)]
    if not middle.isdigit():
        return None
    idx = int(middle)
    return idx if 0 <= idx < MAX_INPUTS else None


def input_token(index):
    return "%s%d%s" % (INPUT_PREFIX, index, INPUT_SUFFIX)


# --------------------------------------------------------------------------
# 取值归一化
# --------------------------------------------------------------------------

def _coerce(parm_info, value, by_component=False):
    """把一个值转成该参数能接受的形式。

    返回 (ok, 归一后的值, 错误说明)。

    by_component=True 表示模型写的是分量名（sizex）而不是元组名（size），
    这时按单个数处理。同一个参数两种写法都该能用。
    """
    kind = parm_info.get("kind")
    ncomp = 1 if by_component else parm_info.get("components", 1)
    label = parm_info.get("name") if not by_component else parm_info.get("name")

    if kind == "menu":
        options = parm_info.get("options") or []
        wanted = value
        if isinstance(wanted, bool):
            wanted = int(wanted)
        if isinstance(wanted, int):
            if 0 <= wanted < len(options):
                return True, options[wanted]["value"], ""
            return False, None, "%s: 下标 %s 超范围（共 %d 项）" % (
                label, wanted, len(options))
        text = str(wanted).strip()
        for opt in options:
            if text == str(opt["value"]).strip():
                return True, opt["value"], ""
        for opt in options:
            if text.lower() == str(opt["label"]).strip().lower():
                return True, opt["value"], ""
        # ★ 合法取值**列全**。这里曾经只列前 12 个（用户 2026-09-29 定：
        # 不要截断）。这条报错是模型**唯一一次**能改对的机会——只给它 12 个
        # 选项，等于逼它猜，猜错再来一轮，那才是真浪费。
        valid = ", ".join("%s(%s)" % (o["value"], o["label"]) for o in options)
        return False, None, "%s: %r 不是合法取值。可选：%s" % (
            label, value, valid)

    if kind == "toggle":
        if isinstance(value, str):
            low = value.strip().lower()
            if low in ("1", "true", "on", "yes", "是"):
                return True, 1, ""
            if low in ("0", "false", "off", "no", "否"):
                return True, 0, ""
            return False, None, "%s: %r 不是开关值（用 true/false）" % (label, value)
        return True, 1 if value else 0, ""

    if kind in ("int", "float"):
        values = value if isinstance(value, (list, tuple)) else [value]
        if ncomp > 1 and len(values) != ncomp:
            comps = parm_info.get("component_names") or []
            return False, None, "%s: 需要 %d 个数（%s），收到 %d 个" % (
                label, ncomp, "/".join(comps), len(values))
        out = []
        for item in values:
            if isinstance(item, bool):
                item = int(item)
            if not isinstance(item, (int, float)):
                try:
                    item = float(str(item))
                except (TypeError, ValueError):
                    return False, None, "%s: %r 不是数字" % (label, item)
            if kind == "int":
                if float(item) != int(item):
                    return False, None, "%s: 需要整数，收到 %r" % (label, item)
                item = int(item)
            else:
                item = float(item)
            out.append(item)

        # 只有 strict 的范围才是硬限制。box 的 size 报 -1..1 但那只是滑杆范围，
        # 设成 2 是合法需求——拿滑杆范围拦人会把正常东西全拒掉。
        lo = parm_info.get("min") if parm_info.get("min_strict") else None
        hi = parm_info.get("max") if parm_info.get("max_strict") else None
        for item in out:
            if isinstance(lo, (int, float)) and abs(lo) < _SENTINEL and item < lo:
                return False, None, "%s: %s 小于允许的最小值 %s" % (label, item, lo)
            if isinstance(hi, (int, float)) and abs(hi) < _SENTINEL and item > hi:
                return False, None, "%s: %s 大于允许的最大值 %s" % (label, item, hi)

        return True, (tuple(out) if ncomp > 1 else out[0]), ""

    if kind == "string":
        if isinstance(value, (list, tuple)):
            value = value[0] if value else ""
        return True, str(value), ""

    return False, None, "%s: 这种参数（%s）不支持直接设值" % (label, kind)


_PATH_LITERAL = None


def _path_problem(literal):
    """一个路径字面量有没有跳出容器。返回原因或 None。"""
    text = (literal or "").strip()
    if not text:
        return None
    if text.startswith("/"):
        return "用了绝对路径 %r" % literal

    parts = text.split("/")
    ups = parts.count("..")
    if ups == 0:
        return None                       # 纯向下的相对路径，还在容器里
    if ups > 1:
        return "往外跳了不止一层（%r）" % literal
    if parts[0] != "..":
        return "路径中间夹着 ..（%r）" % literal
    return None


def expression_scope_problem(expression):
    """表达式有没有引用容器外的东西。

    ★ 这是个**白名单**，不是黑名单：允许的只有相对路径，而且最多往上跳一层。
    因为升参数用的就是 `ch("../<容器参数>")`，那是唯一正当的形式。

    为什么要堵：Hscript 表达式能读容器外的参数（`ch("/obj/其他/参数")`），
    所以不堵的话「读被限制在容器内」这句话就不成立——而这条边界是整套东西
    的安全前提。

    ⚠️ 这是尽力而为的静态检查：它看的是**引号里的字符串**。Hscript 的路径
    参数都是字符串字面量，所以实际能挡住；但真要有人用字符串拼接绕，这条路
    是通的（比如 `ch(".." + "/x")`——不过那个拼出来是 `../x`，仍在容器内）。
    """
    global _PATH_LITERAL
    if _PATH_LITERAL is None:
        import re

        _PATH_LITERAL = re.compile(r'"([^"]*)"' + r"|'([^']*)'")

    for match in _PATH_LITERAL.finditer(expression or ""):
        literal = match.group(1) if match.group(1) is not None else match.group(2)
        problem = _path_problem(literal)
        if problem:
            return problem
    return None


def _set_parm(node, parm_name, parm_info, coerced):
    """把归一后的值写到节点上。元组名走 parmTuple，分量名走 parm。"""
    is_tuple = (parm_name == parm_info.get("name")
                and parm_info.get("components", 1) > 1)
    if is_tuple:
        tup = node.parmTuple(parm_name)
        if tup is not None:
            tup.set(tuple(coerced))
            return True
        return False
    parm = node.parm(parm_name)
    if parm is not None:
        parm.set(coerced)
        return True
    return False


# --------------------------------------------------------------------------
# 摆放节点
# --------------------------------------------------------------------------

# 提升到容器上的参数存在这里，用 userData；界面不显示，跟着 .hip 走。
PROMOTED_KEY = "houdini_ai_promoted"

# 布局间距。None = 用 Houdini 自己的默认值。
LAYOUT_H_SPACING = None
LAYOUT_V_SPACING = None


def layout(container, horizontal_spacing=LAYOUT_H_SPACING,
           vertical_spacing=LAYOUT_V_SPACING):
    """把容器内部的节点排好。

    ★ 直接调 **Houdini 自己的** `Node.layoutChildren()`——这是网络编辑器里
    「Layout」命令用的同一个 API。**从上到下**排主链，分支横向分叉，
    汇合点回到中间列。

    自己手搓布局算法是走弯路了（我一开始就是这么干的，还错把它排出来的
    竖排当成 bug）。Houdini 的实现还会考虑注释、网络框、点线这些我们不该
    操心、也拿不到的信息；而且用户按 Layout 按钮得到的结果跟这个一致，
    不会出现「AI 排的跟 Houdini 排的不一样」。
    """
    if not container.children():
        return False

    kwargs = {}
    if horizontal_spacing:
        kwargs["horizontal_spacing"] = horizontal_spacing
    if vertical_spacing:
        kwargs["vertical_spacing"] = vertical_spacing

    try:
        container.layoutChildren(**kwargs)
        return True
    except Exception:
        # 极少见。退而求其次：至少别让节点叠在一起。
        for child in container.children():
            try:
                child.moveToGoodPosition()
            except Exception:
                pass
        return False


def _parm_expr(parm):
    """这个参数是不是表达式驱动的。是就返回表达式，否则返回空串。"""
    try:
        return parm.expression() or ""
    except Exception:
        return ""


def _clear_expression(parm):
    """把参数的表达式清掉，退回常数。

    注意：`parm.set(v)` **不会**清掉表达式（实测 set 之后 expression 还在），
    `revertToDefaults()` 也不会。只有 `setExpression("")` 管用。
    """
    import hou

    if not _parm_expr(parm):
        return
    try:
        parm.setExpression("", hou.exprLanguage.Hscript)
    except Exception:
        pass


# --------------------------------------------------------------------------
# 把内层参数提升到容器上（「在外面直接调」）
# --------------------------------------------------------------------------

def load_promotions(container):
    """容器上记录着的提升清单。"""
    try:
        raw = container.userData(PROMOTED_KEY)
    except Exception:
        return []
    if not raw:
        return []
    try:
        data = json.loads(raw)
    except ValueError:
        return []
    return data if isinstance(data, list) else []


def _save_promotions(container, promotions):
    try:
        container.setUserData(PROMOTED_KEY,
                              json.dumps(promotions, ensure_ascii=False))
    except Exception:
        pass


# --------------------------------------------------------------------------
# 把当前清单**每轮推进消息**给模型（不靠它主动调 list_container）
#
# 为什么需要：旧轮次会被压成两行摘要（session.MAX_FULL_ROUNDS），那两行不含
# 提升清单——迭代到第 3 轮以后，模型就**看不到自己上几轮把哪些旋钮搬到外面了**，
# 只能赌它这一轮老实调了 list_container。没兜底。
#
# ★ 分工（别把这四件事混起来，混起来就是每次请求都重发同一段话）：
#   * 准则（挑哪些、什么算值得）      → prompt.SYSTEM
#   * 字段契约（不给=沿用、给=列全）  → schema 的 build.promote description
#   * 现在外面有哪几个                → 下面这两个函数（每轮注入）
#   * 主动查容器时的提醒              → summarize 里的 promoted_note（不动）
# --------------------------------------------------------------------------

PROMOTED_HEADER = "# 容器外面现在这些可调参数"


def promoted_digest(container):
    """当前提升清单的指纹。**只认身份，不含当前数值。**

    含数值的话，用户在参数面板里拖一下滑杆就会让下一轮重发一整块——纯冗余。
    所以这里只哈希「有哪些旋钮」这件事本身。没提升返回空串。
    """
    import hashlib

    items = load_promotions(container)
    if not items:
        return ""
    try:
        blob = "\n".join(
            json.dumps(_canonical_promotion(e), ensure_ascii=False,
                       sort_keys=True) for e in items)
    except Exception:
        return ""
    return hashlib.sha1(blob.encode("utf-8")).hexdigest()[:16]


def promoted_brief(container):
    """这一轮要注入给模型的清单全文。

    ★ **不放当前数值**：放了就是「拿陈旧数字当现状」，那是这套东西最忌的
    静默错误（用户拖过滑杆之后，注入里的数就不对了）。要精确值模型自己
    `list_container`，那条路带 `value`。
    """
    items = load_promotions(container)
    if not items:
        return ""
    lines = []
    for entry in items:
        label = entry.get("label") or entry.get("as") or entry.get("parm")
        lines.append("· %s（容器参数 %s → 内部 %s.%s）"
                     % (label, entry.get("as"),
                        entry.get("node"), entry.get("parm")))
    # 最后那一句是**契约**。它和 summarize 里的 promoted_note 重复，但分属两个
    # 通道、各自只在一种情况下被看到（这里是保证看到，那里是主动查容器时）。
    # 缺了它，模型很容易只填一个新旋钮而把原有的全冲掉——那正是"参数又没了"。
    return (PROMOTED_HEADER + "\n" + "\n".join(lines) + "\n"
            "（要改这套旋钮，`promote` 里必须把想保留的都列全——它要的是完整"
            "清单，不是增量。）")


def clear_promotions(container):
    """把之前提升上来的备用参数全部摘掉。

    ★ 顺序要紧：**先把内层参数的表达式清掉，再删容器的备用参数**。
    反过来的话，内层会留着一个指向已删参数的表达式，节点上直接挂报错。
    """
    promotions = load_promotions(container)
    for entry in promotions:
        node_name = entry.get("node") or ""
        parm_name = entry.get("parm") or ""
        inner = container.node(node_name)
        if inner is not None:
            parm = inner.parm(parm_name)
            if parm is not None:
                _clear_expression(parm)

    if promotions:
        try:
            group = container.parmTemplateGroup()
            for entry in promotions:
                as_name = entry.get("as")
                if not as_name:
                    continue
                template = group.find(as_name)
                if template is not None:
                    group.remove(template)
            container.setParmTemplateGroup(group)
        except Exception:
            pass

    _save_promotions(container, [])


def _promote_template(container, entry, inner, meta):
    """给一个提升项造出容器上的备用参数模板。"""
    import hou

    label = entry.get("label") or meta.get("label") or entry["parm"]
    default = inner.parm(entry["parm"]).eval()
    kind = meta.get("kind")

    if kind == "menu":
        options = meta.get("options") or []
        template = hou.MenuParmTemplate(
            entry["as"], label,
            tuple(o["value"] for o in options),
            menu_labels=tuple(o["label"] for o in options),
            default_value=0)
    elif kind == "toggle":
        template = hou.ToggleParmTemplate(
            entry["as"], label, default_value=bool(default))
    elif kind == "int":
        template = hou.IntParmTemplate(
            entry["as"], label, 1, default_value=(int(default),))
    elif kind == "string":
        template = hou.StringParmTemplate(
            entry["as"], label, 1, default_value=(str(default),))
    else:
        template = hou.FloatParmTemplate(
            entry["as"], label, 1, default_value=(float(default),))

    for key, setter in (("min", "setMinValue"), ("max", "setMaxValue"),
                        ("min_strict", "setMinIsStrict"),
                        ("max_strict", "setMaxIsStrict")):
        if key in entry:
            try:
                getattr(template, setter)(entry[key])
            except Exception:
                pass
        elif key in meta:
            try:
                getattr(template, setter)(meta[key])
            except Exception:
                pass

    try:
        template.setHelp("由 AI 节点提升上来的参数，直接改这里就能调内部网络。"
                         "（内部：%s / %s）" % (entry["node"], entry["parm"]))
    except Exception:
        pass
    return template


def apply_promotions(container, promotions, cat_name):
    """建容器上的备用参数，并把内层参数改成引用它们。

    返回 (成功清单, 问题清单)。
    """
    import hou

    done, problems = [], []
    group = container.parmTemplateGroup()
    added = []

    for entry in promotions:
        inner = container.node(entry["node"])
        if inner is None:
            # 措辞要**可行动**：这条多半来自「沿用上一轮」而模型改过节点名。
            problems.append(
                "提升 %s：新网络里没有这个节点（改名或删掉了？）。"
                "要保留它，请按新名字重新列在 promote 里。" % entry["node"])
            continue
        index = catalog.parm_index(cat_name, _resolved_type(cat_name, inner),
                                   probe_parent=container) or {}
        meta = index.get(entry["parm"])
        if meta is None:
            problems.append("提升 %s.%s：没有这个参数"
                            % (entry["node"], entry["parm"]))
            continue
        # 分量名可以提升（只调某一个轴），整条向量不行。
        if meta.get("components", 1) > 1 and meta.get("name") == entry["parm"]:
            problems.append(
                "提升 %s.%s：整条多分量参数提升不了（Houdini 的 ParmTuple "
                "没有 setExpression），改提升其中某个分量"
                % (entry["node"], entry["parm"]))
            continue
        if container.parmTuple(entry["as"]) is not None:
            problems.append("提升 %s：容器上已经有叫 %s 的参数了"
                            % (entry["node"], entry["as"]))
            continue

        try:
            template = _promote_template(container, entry, inner, meta)
            group.append(template)
            added.append((entry, inner, template))
        except Exception as exc:
            problems.append("提升 %s.%s 建参数失败：%s"
                            % (entry["node"], entry["parm"], exc))

    if added:
        try:
            container.setParmTemplateGroup(group)
        except Exception as exc:
            problems.append("把提升的参数装到容器上失败：%s" % exc)
            return [], problems

    for entry, inner, _template in added:
        parm = inner.parm(entry["parm"])
        if parm is None:
            continue
        relative = entry.get("relative") or inner.relativePathTo(container)
        expression = 'ch("%s/%s")' % (relative, entry["as"])
        try:
            parm.setExpression(expression, hou.exprLanguage.Hscript)
            done.append(dict(entry, expression=expression))
        except Exception as exc:
            problems.append("把 %s.%s 指向容器参数失败：%s"
                            % (entry["node"], entry["parm"], exc))

    # 只存规范字段。不要把 expression / value 这类运行时附加信息存进去，
    # 否则快照回滚时会把它们当规格再喂一遍，越滚越脏。
    _save_promotions(container, [_canonical_promotion(e) for e in done])
    return done, problems


_PROMOTION_FIELDS = ("node", "parm", "as", "label", "min", "max",
                     "min_strict", "max_strict")


def _canonical_promotion(entry):
    return dict((k, entry[k]) for k in _PROMOTION_FIELDS if k in entry)


def validate_promotions(container, promotions, plan_nodes):
    """校验提升清单。返回问题清单（空 = 可以落地）。"""
    problems = []
    if not isinstance(promotions, list):
        return ["promote 必须是清单"]

    seen_as = set()
    for i, entry in enumerate(promotions):
        if not isinstance(entry, dict):
            problems.append("promote[%d]: 必须是对象" % i)
            continue
        node_name = entry.get("node")
        parm_name = entry.get("parm")
        if node_name not in plan_nodes:
            problems.append("promote[%d]: 节点 %r 不在 nodes 里" % (i, node_name))
            continue
        if not parm_name:
            problems.append("promote[%d]: 缺 parm" % i)
            continue

        # 参数必须真实存在，而且必须是单值的——多分量参数提升不了
        # （Houdini 的 ParmTuple 没有 setExpression，没法把整条向量指到外面）。
        # 这一步放在校验期，别等落地到一半才发现。
        index = plan_nodes.get(node_name, {}).get("parms") or {}
        meta = index.get(parm_name)
        if meta is None:
            near = _close_names(parm_name, index)
            hint = ("，最接近的是：" + ", ".join(near)) if near else ""
            problems.append("promote[%d]: %s 没有参数 %r%s"
                            % (i, node_name, parm_name, hint))
            continue
        # 整条向量提升不了：Houdini 的 ParmTuple 没有 setExpression，
        # 没有接口把一整条向量指到外面的参数上。
        # 但**单个分量**可以——写 `parm: "sizex"` 就能只提升 X 轴。
        is_component = (meta.get("name") != parm_name)
        if meta.get("components", 1) > 1 and not is_component:
            names = meta.get("component_names") or []
            problems.append(
                "promote[%d]: %s.%s 是多分量参数（%s），不能整条提升"
                "（Houdini 没给整条向量的表达式接口）。"
                "要提升其中某一个分量就写分量名，比如 %s。"
                % (i, node_name, parm_name, "/".join(names),
                   names[0] if names else "sizex"))
            continue

        as_name = entry.get("as") or _default_as(node_name, parm_name)
        if not _name_ok(as_name):
            problems.append("promote[%d]: 参数名 %r 不合法" % (i, as_name))
            continue
        if as_name in seen_as:
            problems.append("promote[%d]: 参数名 %r 重复" % (i, as_name))
            continue
        if as_name in _RESERVED_PARMS:
            problems.append("promote[%d]: %r 是工具自己用的参数名，换一个"
                            % (i, as_name))
            continue
        seen_as.add(as_name)
        entry["as"] = as_name
    return problems


def _default_as(node_name, parm_name):
    return "%s_%s" % (node_name, parm_name)


# 工具自己在容器上用的参数名，别被提升的参数撞了
_RESERVED_PARMS = (
    "ai_prompt", "ai_state", "ai_status", "ai_detail",
    "ai_build", "ai_stop", "ai_round", "ai_error",
    "label1", "label2", "label3", "label4",
)


def _resolved_type(cat_name, node):
    return catalog.resolve_type(cat_name, node.type().name())


# --------------------------------------------------------------------------
# 改名
# --------------------------------------------------------------------------

# 长得像自动生成的名字，可以放心改掉
_AUTO_NAME = None


def _looks_auto(name):
    global _AUTO_NAME
    if _AUTO_NAME is None:
        import re

        _AUTO_NAME = re.compile(
            r"^(ai|ai_node|subnet|subnetwork|subnet_ai)\d*$", re.IGNORECASE)
    return bool(_AUTO_NAME.match(name or ""))


def normalize_node_name(name):
    """把模型给的名字收拾成合法的 Houdini 节点名。"""
    import re

    name = re.sub(r"[^0-9A-Za-z_]+", "_", (name or "").strip())
    name = re.sub(r"_+", "_", name).strip("_").lower()
    if not name or name[0].isdigit():
        name = "ai_" + name if name else ""
    return name[:60]


NAME_KEY = "houdini_ai_name"


def _last_proposed_name(container):
    """上一轮 AI 自己想给它起的名字。

    用来判断「现在的名字是 AI 起的还是用户起的」——用户亲手起过的名字
    我们绝不覆盖。
    """
    try:
        return container.userData(NAME_KEY) or ""
    except Exception:
        return ""


def _save_proposed_name(container, name):
    try:
        container.setUserData(NAME_KEY, name or "")
    except Exception:
        pass


def maybe_rename(container, proposed, last_proposed=""):
    """按需改名。

    只在两种情况下改：
      * 现在的名字看着是自动生成的（AI、AI1、subnet1 …）
      * 现在的名字就是上一轮 AI 自己起的（说明是它在迭代自己的命名）
    用户自己起过名字的话**绝不碰**——那多半是他的意图。
    """
    new_name = normalize_node_name(proposed)
    if not new_name:
        return False, "没给名字"
    current = container.name()
    if current == new_name:
        return False, "名字没变"
    if not (_looks_auto(current) or current == last_proposed):
        return False, "你自己起过名字，保留 %r 不动" % current
    try:
        container.setName(new_name)
        return True, "改名为 %s" % new_name
    except Exception as exc:
        return False, "改名失败：%s" % exc


# --------------------------------------------------------------------------
# 规划（校验 + 归一化，不碰场景）
# --------------------------------------------------------------------------

class _Plan(object):
    def __init__(self):
        self.nodes = []          # [(name, type, params_raw)]
        self.parm_sets = []      # [(name, parm_name, parm_info, coerced)]
        self.expressions = []    # [(name, parm_name, expression)]
        self.connections = []    # [(src_or_None, src_input_idx, dst, slot)]
        self.output = None
        self.positions = {}
        self.name = None         # 容器想改成的名字
        self.promote = None      # None = 不动已有的提升；[] = 全部撤掉


def _plan(container, spec):
    """校验并归一化。返回 (problems, plan)。problems 非空则 plan 不可用。"""
    problems = []

    if not isinstance(spec, dict):
        return ["spec 必须是一个对象（JSON object）"], None

    cat_name = _cat_name(container)
    if cat_name is None:
        return ["容器 %s 不是一个能装节点的网络" % container.path()], None

    nodes = spec.get("nodes")
    if not isinstance(nodes, list) or not nodes:
        return ["spec.nodes 必须是非空的节点清单"], None

    plan = _Plan()
    by_name = {}

    # --- 节点类型 ---
    for i, node in enumerate(nodes):
        if not isinstance(node, dict):
            problems.append("nodes[%d]: 必须是对象" % i)
            continue
        name = node.get("name")
        ntype = node.get("type")

        if not _name_ok(name):
            problems.append(
                "nodes[%d]: 名字 %r 不合法（只能字母数字下划线，且不以数字开头）"
                % (i, name))
            continue
        if name in by_name:
            problems.append("nodes[%d]: 名字 %r 重复" % (i, name))
            continue
        if not ntype:
            problems.append("nodes[%d] (%s): 缺 type" % (i, name))
            continue

        resolved = catalog.resolve_type(cat_name, ntype)
        if resolved is None:
            close = catalog.search(cat_name, ntype, limit=5)
            hint = ("，你是不是想用：" +
                    ", ".join(c["name"] for c in close)) if close else ""
            problems.append(
                "%s: 节点类型 %r 在 %s 里不存在%s"
                % (name, ntype, cat_name, hint))
            continue

        # 参数索引从真实实例上探测（说见 catalog._live_parms）
        index = catalog.parm_index(cat_name, resolved, probe_parent=container) or {}
        by_name[name] = {"index": i, "type": resolved, "parms": index}
        plan.nodes.append((name, resolved, node.get("params") or {}))

        # 位置不再由模型决定——apply() 最后会统一做分层布局（左进右出）。
        # 让模型自己摆位置只会摆成竖着一条线，而且它也没法预知布局结果。
        for parm_name, expression in (node.get("expressions") or {}).items():
            info = index.get(parm_name)
            if info is None:
                problems.append("%s: expressions 里的参数 %r 不存在"
                                % (name, parm_name))
                continue
            expression = str(expression)
            # ★ 越界检查：表达式不能引用容器外的东西。
            # 不堵的话「读被限制在容器内」就不成立——那是整套东西的安全前提。
            scope = expression_scope_problem(expression)
            if scope:
                problems.append(
                    "%s: 表达式 %s 想引用容器外的东西（%s）。"
                    "你只能引用这个容器内部：写 ../参数名 引用容器上的参数，"
                    "或者写同级/下级节点的名字。"
                    % (name, expression, scope))
                continue
            plan.expressions.append((name, parm_name, expression))

    # --- 参数 ---
    for name, entry in by_name.items():
        params = nodes[entry["index"]].get("params") or {}
        if not isinstance(params, dict):
            problems.append("%s: params 必须是对象" % name)
            continue
        for parm_name, value in params.items():
            info = entry["parms"].get(parm_name)
            if info is None:
                near = _close_names(parm_name, entry["parms"])
                hint = ("，最接近的是：" + ", ".join(near)) if near else ""
                problems.append("%s: 参数 %r 不存在%s" % (name, parm_name, hint))
                continue
            by_component = parm_name != info.get("name")
            ok, coerced, why = _coerce(info, value, by_component)
            if not ok:
                problems.append("%s: %s" % (name, why))
                continue
            plan.parm_sets.append((name, parm_name, info, coerced))

    # --- 连接 ---
    connections = spec.get("connections") or []
    if not isinstance(connections, list):
        problems.append("connections 必须是清单")
        connections = []

    # 校验连接时要知道目标的输入槽上限，这里补一份类型信息
    max_inputs = {}
    for name, entry in by_name.items():
        info = catalog.type_info(cat_name, entry["type"], probe_parent=container)
        max_inputs[name] = (info or {}).get("max_inputs", 0)

    seen_slots = set()
    for i, conn in enumerate(connections):
        if not isinstance(conn, dict):
            problems.append("connections[%d]: 必须是对象" % i)
            continue
        src = conn.get("from")
        dst = conn.get("to")
        slot = conn.get("input", 0)

        if not isinstance(slot, int) or isinstance(slot, bool) or slot < 0:
            problems.append("connections[%d]: input 必须是非负整数" % i)
            continue
        if dst not in by_name:
            problems.append("connections[%d]: 目标节点 %r 不在 nodes 里" % (i, dst))
            continue

        src_idx = is_input_token(src)
        if src_idx is None and src not in by_name:
            problems.append(
                "connections[%d]: 来源 %r 既不是 nodes 里的节点，"
                "也不是容器输入（%s 这种写法）。容器最多 %d 个输入。"
                % (i, src, input_token(0), MAX_INPUTS))
            continue

        limit = max_inputs.get(dst, 0)
        if slot >= limit:
            problems.append(
                "connections[%d]: %s 最多只有 %d 个输入，用不了第 %d 个"
                % (i, dst, limit, slot))
            continue

        key = (dst, slot)
        if key in seen_slots:
            problems.append("connections[%d]: %s 的第 %d 个输入被接了两次"
                            % (i, dst, slot))
            continue
        seen_slots.add(key)
        plan.connections.append((src, src_idx, dst, slot))

    # --- 输出 ---
    out = spec.get("output")
    if out is not None and out not in by_name:
        problems.append("output: %r 不在 nodes 里" % out)
    plan.output = out

    # --- 容器改名 ---
    proposed = spec.get("name")
    if proposed is not None:
        cleaned = normalize_node_name(proposed)
        if not cleaned:
            problems.append("name: %r 收拾不出合法的节点名" % proposed)
        else:
            plan.name = cleaned

    # --- 参数提升 ---
    if "promote" in spec:
        promote = spec.get("promote")
        if promote is None:
            promote = []
        promote = [dict(e) if isinstance(e, dict) else e for e in promote] \
            if isinstance(promote, list) else promote
        problems.extend(validate_promotions(container, promote, by_name))
        plan.promote = promote
    # 没给 promote 这个键 -> plan.promote 保持 None，表示「沿用已有的提升」

    if problems:
        return problems, None
    return [], plan


def _close_names(wanted, available):
    """给拼错的参数名找几个接近的，模型看了更容易改对。"""
    low = str(wanted).lower()
    scored = []
    for name in available:
        n = name.lower()
        if low in n or n in low:
            scored.append((abs(len(n) - len(low)), name))
    scored.sort()
    return [name for _, name in scored[:4]]


def _cat_name(container):
    cat = catalog.category_of_node(container)
    return cat.name() if cat is not None else None


def validate(container, spec):
    """返回问题清单。空清单 = 可以落地。"""
    problems, _ = _plan(container, spec)
    return problems


# --------------------------------------------------------------------------
# 落地
# --------------------------------------------------------------------------

def apply(container, spec, group_label="AI: 构建网络"):
    """校验并落地。校验不过抛 SpecError，容器保持原样。

    返回一份摘要，会被塞回给模型看结果。
    """
    problems, plan = _plan(container, spec)
    if problems:
        raise SpecError(problems)

    import hou

    cat_name = _cat_name(container)
    # 读一次旧的提升清单：plan.promote 为 None 表示「模型没提这事，沿用旧的」。
    # 必须在清空之前读。
    inherited = load_promotions(container) if plan.promote is None else None
    last_proposed = _last_proposed_name(container)

    created = {}
    with hou.undos.group(group_label):
        # 摘掉旧的提升。内层马上要重建，老表达式会指向不存在的节点。
        clear_promotions(container)

        # build 的语义是「用这份描述替换容器内容」。全量替换而不是增量修改，
        # 是为了不让模型脑内的状态和真实网络漂移——它每轮看到的都是真相。
        for child in list(container.children()):
            child.destroy()

        for name, resolved, _params in plan.nodes:
            created[name] = container.createNode(resolved, name)

        for name, parm_name, info, coerced in plan.parm_sets:
            node = created.get(name)
            if node is None:
                continue
            try:
                _set_parm(node, parm_name, info, coerced)
            except Exception:
                # 规划阶段已经校验过，这里再出错是 Houdini 自己的怪脾气，
                # 不该炸掉整次构建——下一个工具的返回里会体现出来。
                pass

        for name, parm_name, expression in plan.expressions:
            node = created.get(name)
            if node is None:
                continue
            parm = node.parm(parm_name)
            if parm is None:
                continue
            try:
                parm.setExpression(expression, hou.exprLanguage.Hscript)
            except Exception:
                pass

        for src, src_idx, dst, slot in plan.connections:
            target = created.get(dst)
            if target is None:
                continue
            try:
                if src_idx is not None:
                    inputs = container.indirectInputs()
                    if src_idx < len(inputs):
                        target.setInput(slot, inputs[src_idx])
                else:
                    upstream = created.get(src)
                    if upstream is not None:
                        target.setInput(slot, upstream)
            except Exception:
                pass

        # 收尾：建 Output SOP 并把结果接上去。Houdini 自己 collapse 成 subnet 时
        # 就是这么干的（实测 collapseIntoSubnet 的结果里有 output0）。
        if created:
            last = created.get(plan.output) or created[plan.nodes[-1][0]]
            try:
                out_node = last.createOutputNode("output")
                out_node.setInput(0, last)
                out_node.setDisplayFlag(True)
                out_node.setRenderFlag(True)
            except Exception:
                # 退而求其次：直接给末端节点挂 display 标记。
                # 实测这样几何一样能流出去。
                last.setDisplayFlag(True)
                last.setRenderFlag(True)

        # 摆位置。放在最后做，这样 Output 节点也在布局里（会落到最右列）。
        layout(container)

        # 把参数提升到容器上，让你在外面就能调。
        if plan.promote is not None:
            promotions = plan.promote
        else:
            # 不给 promote = 沿用上一轮的清单。
            # ★★ 这里**不要**先按节点名过滤。以前写的是
            #   `[e for e in inherited if e.get("node") in created]` —— 那是
            #   **静默丢弃**：模型重建时给内部节点改了名，那条提升会无声消失，
            #   用户在外面就少了那个旋钮，而模型和用户都不知道发生过什么。
            #   原样交给 `apply_promotions` 逐条报进 `promote_problems`，
            #   模型下一屏就看得见、能自己按新名字补回来。
            promotions = list(inherited or [])
        promote_problems = []
        if promotions:
            _done, promote_problems = apply_promotions(
                container, promotions, cat_name)

        # 改名放最后：前面所有操作都用旧路径，改完这一轮就结束了。
        if plan.name:
            _renamed, _why = maybe_rename(container, plan.name, last_proposed)
            _save_proposed_name(container, plan.name)

    result = summarize(container)
    if promote_problems:
        result["promote_problems"] = promote_problems
    return result


def set_parms(container, ops, group_label="AI: 调参数"):
    """只改参数值的增量操作。不碰结构，所以最安全。

    ops = [{"node": "名字", "parm": "参数名", "value": ...}, ...]
    """
    cat_name = _cat_name(container)
    if cat_name is None:
        raise SpecError(["容器不是能装节点的网络"])

    if not isinstance(ops, list) or not ops:
        raise SpecError(["ops 必须是非空清单"])

    children = {c.name(): c for c in container.children()}
    problems = []
    plan = []

    for i, op in enumerate(ops):
        if not isinstance(op, dict):
            problems.append("ops[%d]: 必须是对象" % i)
            continue
        name = op.get("node")
        parm_name = op.get("parm")
        if name not in children:
            problems.append("ops[%d]: 容器里没有叫 %r 的节点。现有：%s"
                            % (i, name, ", ".join(sorted(children)) or "（空）"))
            continue
        node = children[name]
        type_name = catalog.resolve_type(cat_name, node.type().name())
        index = catalog.parm_index(cat_name, type_name,
                                   probe_parent=container) or {}
        info = index.get(parm_name)
        if info is None:
            near = _close_names(parm_name, index)
            hint = ("，最接近的是：" + ", ".join(near)) if near else ""
            problems.append("ops[%d]: %s 没有参数 %r%s"
                            % (i, name, parm_name, hint))
            continue
        by_component = parm_name != info.get("name")
        ok, coerced, why = _coerce(info, op.get("value"), by_component)
        if not ok:
            problems.append("ops[%d]: %s" % (i, why))
            continue
        plan.append((node, parm_name, info, coerced))

    if problems:
        raise SpecError(problems)

    import hou

    with hou.undos.group(group_label):
        for node, parm_name, info, coerced in plan:
            try:
                _set_parm(node, parm_name, info, coerced)
            except Exception:
                pass

    return summarize(container)


# --------------------------------------------------------------------------
# 现状描述 / 快照
# --------------------------------------------------------------------------

def summarize(container, include_parms=True):
    """容器现在长什么样。既是给模型看的现状，也是回滚用的快照格式。

    参数只列**非默认值**——全列会把上下文撑爆，而且默认值本来就无所谓。
    """
    cat_name = _cat_name(container)
    children = list(container.children())
    child_names = {c.name() for c in children}

    nodes = []
    for child in children:
        try:
            type_name = child.type().name()
        except Exception:
            continue
        entry = {
            "name": child.name(),
            "type": type_name,
            "pos": [round(v, 1) for v in child.position()],
        }
        if child.isDisplayFlagSet():
            entry["display"] = True
        if child.isRenderFlagSet():
            entry["render"] = True

        if include_parms:
            params = {}
            expressions = {}
            try:
                for tup in child.parmTuples():
                    # ★ 表达式驱动的参数必须单独报出来。
                    # 只报求值结果的话，模型下一轮会以为那是个常数，
                    # 一重建就把「提升到容器」的关系**静默丢掉**。
                    # （实测踩过：外面改成 45，快照里存成 strength: 45。）
                    expr = _parm_expr(tup[0]) if len(tup) else ""
                    if expr:
                        expressions[tup.name()] = expr
                    if tup.isAtDefault():
                        continue
                    value = _tuple_value(tup)
                    if value is not None:
                        params[tup.name()] = value
            except Exception:
                pass
            if params:
                entry["params"] = params
            if expressions:
                entry["expressions"] = expressions
        nodes.append(entry)

    connections = []
    for child in children:
        try:
            max_in = child.type().maxNumInputs()
        except Exception:
            max_in = 0
        for slot in range(min(max_in, 8)):
            try:
                upstream = child.input(slot)
            except Exception:
                continue
            if upstream is None:
                continue
            if upstream.name() in child_names:
                connections.append(
                    {"from": upstream.name(), "to": child.name(), "input": slot})
            else:
                connections.append(
                    {"from": "（容器外或容器输入）", "to": child.name(),
                     "input": slot})

    result = {
        "container": container.path(),
        "name": container.name(),
        "category": cat_name,
        "nodes": nodes,
        "connections": connections,
    }

    # 提升到容器上的参数：模型上一轮把哪些参数搬到了外面，以及现在值是多少。
    # 模型看到这个才会知道「这些旋钮已经在外面了」，不会重复提升。
    promoted = load_promotions(container)
    if promoted:
        entries = []
        for item in promoted:
            shown = dict(item)
            parm = container.parm(item.get("as"))
            if parm is not None:
                try:
                    shown["value"] = parm.eval()
                except Exception:
                    pass
            entries.append(shown)
        result["promoted"] = entries
        result["promoted_note"] = (
            "这些参数已经被提升到容器上了，用户在**外面**直接就能调。"
            "这一轮的 build 如果不给 promote 字段，它们会被沿用；"
            "要给的话必须把想保留的都列全。")

    # 容器自己的输入线接了没有——模型据此决定要不要用 __inputN__
    wired = []
    for i in range(MAX_INPUTS):
        try:
            if container.input(i) is not None:
                wired.append(input_token(i))
        except Exception:
            break
    if wired:
        result["available_inputs"] = wired
    return result


def _tuple_value(tup):
    """读一个参数的元组值。读到什么算什么，读不出来返回 None。"""
    import hou

    try:
        template = tup.parmTemplate()
    except Exception:
        template = None
    if template is None:
        try:
            template = tup[0].parmTemplate()
        except Exception:
            return None

    if isinstance(template, (hou.ButtonParmTemplate, hou.LabelParmTemplate,
                             hou.SeparatorParmTemplate,
                             hou.FolderParmTemplate, hou.FolderSetParmTemplate,
                             hou.RampParmTemplate)):
        return None

    try:
        if isinstance(template, hou.StringParmTemplate):
            text = tup.evalAsString()
            return text if text else None
        if isinstance(template, (hou.IntParmTemplate, hou.ToggleParmTemplate)):
            values = list(tup.evalAsInts())
            return values[0] if len(values) == 1 else values
        if isinstance(template, hou.FloatParmTemplate):
            values = [round(v, 4) for v in tup.evalAsFloats()]
            return values[0] if len(values) == 1 else values
    except Exception:
        return None
    return None
