# -*- coding: utf-8 -*-
"""向上读一级：让 AI 节点对自己所处的上下文有知觉。

## 为什么只允许一级

写被结构性限制在容器内（描述格式里没有"路径"概念）。读这边我们放开一级，
因为有些信息确实影响它该建什么：

  * **上游是谁** —— `gltf`（扫描件）还是 `box`（程序化），决定它该用什么思路。
    （上游的**几何**它其实已经能量到了：容器内把 `__input0__` 接个 null 再
    `get_geometry` 就行。但"上游是哪个节点"从容器内查不到。）
  * **★ 下游有谁在消费容器的输出** —— 这条最要紧。容器后面如果接着节点，
    AI 大改输出结构（增删属性）会**悄悄搞坏下游**，而它自己永远不会知道。
  * **同级有哪些节点** —— 这个 geo 里已经有什么、有没有别的 AI 节点。

## 边界（重要）

**只遍历 `parent.children()` 一次，绝不递归、绝不访问父网络的父。**

有个天然的理由让这条边界很干净：Houdini 里一个节点的输入输出**只能连到同一个
网络内的节点**，所以"上游"和"下游"必然都是同级节点——它们本来就在父网络这一层里。
换句话说，**一级就是全部**，不存在"顺着连线爬出去"的可能。

`describe()` 的返回里不会出现父网络之外的任何节点名字。这条有专门的测试盯着
（`tests/test_context.py`），测试名就叫"越不过父网络"。
"""

# ★ 这里曾经有 `MAX_SIBLINGS = 60`（同级节点砍到 60 个）。**拆掉了**——
# 见 `describe` 里那段说明。

# 容器最多 4 个输入（Subnet SOP 的限制）
MAX_INPUTS = 4


def _brief(node):
    """一个节点的最小身份：名字 + 类型。不给参数、不给几何、不给路径。"""
    if node is None:
        return None
    try:
        type_name = node.type().name()
    except Exception:
        type_name = "?"
    try:
        name = node.name()
    except Exception:
        name = "?"
    return {"name": name, "type": type_name}


def describe(container):
    """父网络这一层的上下文。只读，绝不越过父网络。"""
    try:
        parent = container.parent()
    except Exception:
        parent = None

    if parent is None:
        # 容器直接在根上，没有父网络可言
        return {
            "parent": None,
            "note": "这个容器不在任何父网络里（已经是顶层了）。",
        }

    result = {
        "parent": {"path": parent.path(), "type": parent.type().name()},
        "container": container.name(),
    }

    # ---- 上游：容器的每个输入接的是谁 ----
    upstream = []
    try:
        max_inputs = min(container.type().maxNumInputs(), MAX_INPUTS)
    except Exception:
        max_inputs = MAX_INPUTS
    for index in range(max_inputs):
        try:
            source = container.input(index)
        except Exception:
            source = None
        if source is None:
            continue
        brief = _brief(source)
        brief["input"] = index
        upstream.append(brief)
    result["upstream"] = upstream

    # ---- 下游：谁在消费容器的输出 ----
    # ★ 这是最要紧的一条：知道后面有东西接着，它改输出结构时就该保守。
    downstream = []
    try:
        for connection in container.outputConnections():
            try:
                target = connection.outputNode()
                index = connection.outputIndex()
            except Exception:
                continue
            brief = _brief(target)
            if brief is None:
                continue
            brief["from_output"] = index
            downstream.append(brief)
    except Exception:
        pass
    result["downstream"] = downstream

    # ---- 同级：父网络里除我之外还有谁 ----
    siblings = []
    try:
        for child in parent.children():
            try:
                if child == container:
                    continue
            except Exception:
                pass
            brief = _brief(child)
            if brief is not None:
                siblings.append(brief)
    except Exception:
        pass

    # ★ 同级**全列**。这里曾经砍到 60 条（用户 2026-09-29 定：不要截断）。
    # 每条只有名字和类型，本来就最省；而且"这个网络里还有什么"正是模型
    # 判断要不要复用已有节点、会不会撞上别人的依据，砍掉等于让它瞎猜。
    result["siblings"] = siblings

    # 给模型的使用提示，省得它误以为这里能看到更深的东西
    notes = []
    if not upstream:
        notes.append("上游：没有节点接到容器的输入上（你可以自由造几何）。")
    if downstream:
        notes.append(
            "★ 下游有 %d 个节点在消费这个容器的输出。"
            "改输出结构（删属性、改点序、换拓扑）之前想清楚会不会把它们搞坏。"
            % len(downstream))
    if notes:
        result["notes"] = notes

    return result
