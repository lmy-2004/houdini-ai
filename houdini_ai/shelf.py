# -*- coding: utf-8 -*-
"""工具架按钮的入口。

沿用本机已跑通的约定（见 D:\\FabLibrary\\_tools\\fab_library.py）：
逻辑函数不弹窗，`*_ui` 后缀的负责提示；工具架的按钮脚本只做
`sys.path.insert` + 调这里的 `*_ui` 函数。

★ 这里**一律用状态栏消息，不弹对话框**。建个节点就弹个窗要手动关掉很烦，
而且网络编辑器右下角本来就有状态栏。真的失败了也在状态栏里说，
颜色用 Error 让你能注意到。
"""

from . import node as node_mod, runner

SOP = "Sop"
OBJ = "Object"


def _say(message, error=False):
    """往 Houdini 状态栏上写一句。不打断你。"""
    import hou

    try:
        hou.ui.setStatusMessage(
            message,
            severity=(hou.severityType.Error if error
                      else hou.severityType.Message))
    except Exception:
        pass


def _current_network():
    import hou

    try:
        desktop = hou.ui.curDesktop()
        editor = desktop.paneTabOfType(hou.paneTabType.NetworkEditor)
        if editor is not None:
            return editor.pwd()
    except Exception:
        pass
    return None


def _first_geo():
    import hou

    obj = hou.node("/obj")
    if obj is None:
        return None
    for child in obj.children():
        try:
            if child.type().name() == "geo":
                return child
        except Exception:
            continue
    return None


def target_sop_network():
    """找一个能放 SOP 的网络。

    在 SOP 网络里就放当前网络；在 OBJ 层就找/建一个 geo。
    """
    import hou

    network = _current_network()
    if network is not None:
        try:
            cat = network.childTypeCategory()
            if cat is not None and cat.name() == SOP:
                return network
        except Exception:
            pass

    try:
        for selected in hou.selectedNodes():
            if selected.type().name() == "geo":
                return selected
            cat = selected.childTypeCategory()
            if cat is not None and cat.name() == SOP:
                return selected
    except Exception:
        pass

    geo = _first_geo()
    if geo is None:
        obj = hou.node("/obj")
        if obj is None:
            return None
        geo = obj.createNode("geo")
    return geo


def _focus(node):
    import hou

    try:
        node.setSelected(True, clear_all_selected=True)
    except Exception:
        pass
    try:
        desktop = hou.ui.curDesktop()
        editor = desktop.paneTabOfType(hou.paneTabType.NetworkEditor)
        if editor is not None:
            editor.setPwd(node.parent())
            editor.homeToSelection()
    except Exception:
        pass


# --------------------------------------------------------------------------
# 工具架按钮
# --------------------------------------------------------------------------

def _pane_from(kwargs):
    """从工具脚本的 kwargs 里拿当前的网络编辑器面板。"""
    if not kwargs:
        return None
    pane = kwargs.get("pane")
    if pane is None:
        return None
    import hou

    if isinstance(pane, hou.NetworkEditor):
        return pane
    return None


def _create_with_placement(kwargs):
    """用 **Houdini 自己的工具助手**建节点，这样才有「虚影 + 点击落位」。

    ★ 这是本模块最要紧的一条。裸调 `parent.createNode()` 会跳过整套放置交互：
    没有跟着鼠标的虚影、没法自己选落点、也不会自动接到你选中的节点上。

    Houdini 自己的建节点工具（内置工具架里那些）全都走
    `soptoolutils.genericTool`，它内部会：

        pane.pushEventContext('nodegraphselectpos', context_data)   # 虚影 + 等点击
        if 'pos' not in context_data:
            raise hou.OperationInterrupted("Node creation cancelled")
        pos = context_data['pos']                                    # 你点的位置

    也就是说**工具架工具完全能拿到这套交互**，前提是别绕开它。
    顺带还能白拿两个原生行为：自动接到选中的节点上、display/render 标记处理。

    不在网络编辑器里（比如从工具架标签点、或没有 pane 上下文）时返回 None，
    调用方走普通创建路径。
    """
    import hou

    pane = _pane_from(kwargs)
    if pane is None:
        return None

    try:
        category = pane.pwd().childTypeCategory()
        in_sop = category is not None and category.name() == SOP
    except Exception:
        in_sop = False
    if not in_sop:
        # 在 OBJ 层的话落点语义不一样（要先进一个 geo），交给调用方处理
        return None

    try:
        import soptoolutils

        return soptoolutils.genericTool(kwargs, "subnet", "AI")
    except hou.OperationInterrupted:
        # 用户按 Esc 取消了放置。这不是错误，安静收场。
        return False
    except Exception:
        return None


def new_node_ui(kwargs=None):
    """新建一个 AI 节点。

    在 SOP 网络里时走 Houdini 自己的放置交互（虚影 + 点击落位）；
    其他地方退回普通创建。**不弹窗**——建完在节点注释里说明下一步。
    """
    runner.note_main_thread()

    created = _create_with_placement(kwargs)
    if created is False:
        # 用户按 Esc 取消了放置。什么都不做，也别报错。
        return None

    if created is not None:
        node = created
        try:
            # genericTool 已经把节点建好了，但它是按普通 subnet 建的；
            # 我们的备用参数和回调得补上。
            node_mod.ensure_parms(node)
            node_mod.repair_callback(node)
        except Exception as exc:
            _say("AI 参数加失败：%s" % exc, error=True)
            return node
    else:
        network = target_sop_network()
        if network is None:
            _say("找不到可以放 AI 节点的网络。请在 SOP 网络里用这个按钮。",
                 error=True)
            return None
        try:
            node = node_mod.make_ai_node(network)
            node.moveToGoodPosition()
        except Exception as exc:
            _say("建 AI 节点失败：%s" % exc, error=True)
            return None

    _make_it_visible(node)
    node_mod.set_status(
        node, "idle", "填好「描述」再点「开始构建」",
        full="参数面板上：\n"
             "  · 「描述」——用中文写你想要什么\n"
             "  · 「思考深度」——快 / 标准 / 深入\n"
             "  · 「开始构建」——开工\n"
             "  · 「修改意见」+「发送修改意见」——对结果不满意时改这一轮\n"
             "  · 「停止」——中断\n"
             "构建在后台跑，不挡你操作；进度和结果都显示在这里（节点下方）。")
    _focus(node)
    _say("AI 节点已建好：%s" % node.path())
    return node


def _make_it_visible(node):
    """让新建的 AI 节点成为被显示的那个。

    ★ 实测发现的缺口：`parent.createNode()` **不会**给新节点挂显示标记，
    而新建的 geo 里是**第一个 SOP** 持有它。所以用户建完 AI 节点、AI 建完
    网络之后，视口里看到的还是上游那个节点——**看不到结果**。

    Houdini 自己的行为是：从 Tab 菜单新建的节点会成为被显示的那个。
    这里跟它保持一致。只在新建时做一次；之后 `build` 不碰父网络的标记，
    那是用户的领地——他要是自己去显示别的节点，我们不抢回来。
    """
    for method in ("setDisplayFlag", "setRenderFlag"):
        try:
            getattr(node, method)(True)
        except Exception:
            pass


def convert_ui(kwargs=None):
    """把当前选中的 Subnet 就地变成 AI 节点。"""
    import hou

    runner.note_main_thread()
    selected = [n for n in hou.selectedNodes() if n.type().name() == "subnet"]
    if not selected:
        _say("先选中一个 Subnet 节点。", error=True)
        return None

    for node in selected:
        node_mod.ensure_parms(node)
        node_mod.repair_callback(node)
        if node_mod.current_state(node) == "idle":
            node_mod.set_status(node, "idle", "填好「描述」再点「开始构建」")

    _say("已把 %d 个 Subnet 变成 AI 节点（原来里面的东西没动）：%s"
         % (len(selected), "、".join(n.name() for n in selected)))
    return selected


def open_panel_ui(kwargs=None):
    """开一个浮动面板。停靠版在 Houdini 的面板标签菜单里。"""
    runner.note_main_thread()
    from . import panel

    try:
        return panel.show_floating()
    except Exception as exc:
        _say("开面板失败：%s" % exc, error=True)
        return None


# ★ 工具架只有三个按钮：New Node / Panel / Reload。别再加了。
#
# 砍掉过的，以及为什么：
#   * build_selected_ui —— 和节点自己的「开始构建」按钮完全重复
#   * open_selected_network_ui —— Subnet 双击就有，是 Houdini 原生功能
#   * convert_ui 的**菜单入口** —— 把已有 Subnet 改造成 AI 节点是低频操作，
#     而且这是我自作主张加的，不在用户的工作流里
# 加按钮之前先问：Houdini 原生做不到吗？节点自己已经有吗？
#
# convert_ui 函数留着（下面），但**不在任何菜单上**。真要用就在 Python Shell 里：
#     import houdini_ai.shelf as s; s.convert_ui()
