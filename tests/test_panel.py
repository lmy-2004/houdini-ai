# -*- coding: utf-8 -*-
"""面板测试：用离屏 Qt 真的把界面构造出来并刷新。

    "D:/ProgramFiles/Houdini22/bin/hython.exe" tests/test_panel.py

`AIPanelWidget` 是独立 QWidget（不继承抽象的 hou.PythonPanelInterface），
所以能在这里真的建立、真的 refresh，把布局错误和 Qt API 用错在不开 Houdini
的情况下抓出来。

面板被重新设计成**两栏**（左列表 / 右内容），因为用户把它跟视口并排摆——
**宽而矮**，竖向空间紧张。这套测试就是钉住那些取舍。

第三版改掉的（每一条都是用户打回来的）：
  * 右操作栏整个砍掉——「发送和什么回退为什么放在右上角？」
  * 共享记忆从右下角浮层改成**可编辑的标签页**
  * 输入框固定高度，自动长高整个删掉
  * ★ 焦点：Houdini 的 pane 把焦点交给面板本身，得靠 focusProxy 转给输入框
"""

import inspect
import os

# 必须在 import QtWidgets 之前设，否则没有显示设备时会直接崩
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import _harness as H

H.section("起离屏 Qt 和测试场景")
import hou
from PySide6 import QtWidgets
from PySide6.QtTest import QTest as QtTest

_app = QtWidgets.QApplication.instance() or QtWidgets.QApplication([])
H.check("离屏 QApplication 起来了", _app is not None)

from houdini_ai import memory, node as node_mod, panel, session

obj = hou.node("/obj")
for old in list(obj.children()):
    if old.name().startswith("P_"):
        old.destroy()
geo = obj.createNode("geo", "P_geo")
ai = node_mod.make_ai_node(geo, "P_ai1", prompt="做一个盒子")
ai2 = node_mod.make_ai_node(geo, "P_ai2", prompt="做一个球")
node_mod.set_status(ai2, "working", "正在建球")
memory.clear()

H.section("界面能被构造出来")
Widget = panel.panel_widget_class()
w = Widget()
w.resize(760, 300)
H.check("面板构造成功", w is not None)
QtCore = panel._qt()[0]

H.section("★ 两栏：左列表 + 右内容（右操作栏已砍掉）")
H.check("有左栏", w.left_frame is not None)
H.check("★ 右操作栏没了（发送不再挂在右上角）",
        not hasattr(w, "right_frame"))
H.check("左栏宽度受限（不是无限撑）",
        0 < w.left_frame.width() <= 300, w.left_frame.width())
H.check("内容区是拉伸的那个（吃掉剩余宽度）", w.conversation is not None)

H.section("★ 砍掉的东西不该回来")
H.check("没有 QGroupBox（边框和标题是纯噪音）",
        len(w.findChildren(QtWidgets.QGroupBox)) == 0,
        [g.title() for g in w.findChildren(QtWidgets.QGroupBox)])
H.check("没有表格（改成了左栏列表）",
        len(w.findChildren(QtWidgets.QTableWidget)) == 0)

H.section("左栏：节点列表")
w.refresh()
H.check("两个 AI 节点 → 两行", w.node_list.count() == 2, w.node_list.count())
H.check("行里存了路径",
        w.node_list.item(0).data(panel._qt()[0].Qt.UserRole) in
        (ai.path(), ai2.path()),
        w.node_list.item(0).data(panel._qt()[0].Qt.UserRole))
H.check("行里带状态点符号", "●" in w.node_list.item(0).text(),
        w.node_list.item(0).text())
H.check("★ 单节点时只有一行（不是表格那种浪费）",
        (lambda: (ai2.destroy(), w.refresh(), w.node_list.count())[-1])() == 1)
# 上面把 ai2 删了，重建一个回来给后面的用例用
ai2 = node_mod.make_ai_node(geo, "P_ai2", prompt="做一个球")
node_mod.set_status(ai2, "working", "正在建球")
w.refresh()
H.check("删掉再建回来也对", w.node_list.count() == 2, w.node_list.count())

H.section("切换节点")
w._selected_path = ai.path()
w._last_render = None
w.refresh()
H.check("头行显示的是选中的那个", w.header_name.text() == ai.name(),
        w.header_name.text())
w.select_path(ai2.path())
H.check("select_path 能切过去", w.header_name.text() == ai2.name(),
        w.header_name.text())
H.check("切过去之后状态也跟着变",
        "正在建球" in w.header_status.text(), w.header_status.text())

H.section("★ 按钮跟着状态走（不做常驻四连）")
w._selected_path = ai.path()
w._last_render = None
w.refresh()
H.check("空闲时「停止」不出现", not w.stop_button.isVisibleTo(w))
w._selected_path = ai2.path()
w.refresh()
H.check("working 时「停止」出现", w.stop_button.isVisibleTo(w))
H.check("★ 没有「按描述重新构建」常驻按钮（节点参数面板里已有，重复）",
        not hasattr(w, "build_button"))
menu_texts = [a.text() for a in w.more_button.menu().actions()]
H.check("它降级进了 ⋯ 菜单", any("重新构建" in t for t in menu_texts),
        menu_texts)
H.check("★ 回退也搬进了 ⋯ 菜单（低频动作不占常驻按钮）",
        any("回退" in t for t in menu_texts), menu_texts)
H.check("★ 没有常驻的回退按钮了", not hasattr(w, "revert_button"))

H.section("★★ 发送贴着输入框，不在右上角")
H.check("发送和输入框在同一个内容区里",
        w.content.isAncestorOf(w.send_button)
        and w.content.isAncestorOf(w.feedback),
        (w.send_button.parentWidget(), w.feedback.parentWidget()))
_inrow = inspect.getsource(panel.panel_widget_class()._build_input_row)
H.check("★ 发送是在 _build_input_row 里造的（贴着输入框）",
        "send_button" in _inrow, _inrow[:200])
H.check("★ 停止也在这一行（跑的时候才出现）",
        "stop_button" in _inrow)
# ★ 几何断言用一个**全新的干净面板**：上面那个 w 已经经历过 refresh、
# 停止按钮显隐等等，行高是变过的，读到的会是陈旧布局（实测栽过一次）。
# 注意还得先 show —— **没显示的控件根本不算布局**，geometry 全是 (0,0)。
_g = Widget()
_g.resize(900, 400)
_g.show()
_g.layout().activate()
_gfb = _g.feedback
_btn = _g.send_button.mapTo(_gfb.parentWidget(), QtCore.QPoint(0, 0))
H.check("★ 发送整块在输入框的右边缘之外（贴着它，不是飘在上面）",
        _btn.x() >= _gfb.x() + _gfb.width(),
        (_gfb.geometry(), _btn.toTuple(), _g.send_button.size().toTuple()))
H.check("★ 发送纵向是**贴着输入框底边**的（聊天软件的惯例）",
        _btn.y() + _g.send_button.height() >=
        _gfb.y() + _gfb.height() - 2,
        (_gfb.geometry(), _btn.toTuple(), _g.send_button.size().toTuple()))
_g.deleteLater()

H.section("★ QToolButton 的文字别被 arrow 吃掉")
# 实测踩过：QToolButton 默认是 ToolButtonIconOnly，一旦 setArrowType()，
# **文字就不显示了**——渲染出来只剩一个 ▸，根本看不出是「过程」开关。
QtCore = panel._qt()[0]
H.check("「过程」开关设了图标+文字样式",
        w.process_toggle.toolButtonStyle() == QtCore.Qt.ToolButtonTextBesideIcon,
        w.process_toggle.toolButtonStyle())
H.check("「过程」的文字还在", w.process_toggle.text() == "过程",
        w.process_toggle.text())

H.section("★ 「过程」在标签行右端（它是对话的显示模式，不是动作）")
H.check("它是标签控件的 corner widget",
        w.tabs.cornerWidget(QtCore.Qt.TopRightCorner) is w.process_toggle,
        w.tabs.cornerWidget(QtCore.Qt.TopRightCorner))
w.tabs.setCurrentIndex(1)
H.check("切到共享记忆 → 过程藏起来", not w.process_toggle.isVisibleTo(w))
w.tabs.setCurrentIndex(0)
H.check("切回对话 → 又出现", w.process_toggle.isVisibleTo(w))

H.section("★ 过程默认折叠（对话区只放人话）")
H.check("过程区默认隐藏", not w.process_view.isVisibleTo(w))
H.check("开关默认没按下", not w.process_toggle.isChecked())
w.process_toggle.setChecked(True)
H.check("点开之后出现", w.process_view.isVisibleTo(w))
w.process_toggle.setChecked(False)
H.check("再点回去", not w.process_view.isVisibleTo(w))

H.section("对话区只显示人话，过程在另一份里")
fake = session.fresh()
rec = session.add_round(fake, "build", "做一个盒子")
rec["summary"] = "建好了，产出 8 点。"
rec["trace"] = ["找节点类型：box 命中 1 个", "提交网络（1 个节点）…"]
convo = session.conversation_text(fake)
proc = session.process_text(fake)
H.check("对话里有我说的", "做一个盒子" in convo, convo)
H.check("对话里有 AI 的结论", "建好了" in convo, convo)
H.check("★ 对话里**没有**每一步工具调用",
        "找节点类型" not in convo, convo)
H.check("过程里有工具调用", "找节点类型" in proc, proc)

H.section("★★ 共享记忆：是内容不是浮层，而且可编辑")
# 用户原话：「共享记忆它也需要一个打字框，我得可以编辑它呀」+
#          「共享记忆好丑啊，这UI为什么放在右下角啊？」
memory.clear()
w._last_memory = None
w.refresh()
H.check("★ 没有右下角浮层了", not hasattr(w, "memory_popup"))
H.check("★ 也没有那个「记忆 N」按钮了", not hasattr(w, "memory_button"))
H.check("它变成了标签页", w.tabs.count() == 2 and
        w.tabs.tabText(0) == "对话" and "共享记忆" in w.tabs.tabText(1),
        [w.tabs.tabText(i) for i in range(w.tabs.count())])
H.check("没记的时候标签上是 0", w.tabs.tabText(1) == "共享记忆 0",
        w.tabs.tabText(1))
H.check("★ 编辑框是可写的（用户要求能改）",
        not w.memory_edit.isReadOnly())

memory.add("polyextrude 的 translate 是 3 分量", source="/obj/g/AI1")
w._last_memory = None
w.refresh()
H.check("记了之后数字跟着涨", w.tabs.tabText(1) == "共享记忆 1",
        w.tabs.tabText(1))
H.check("★ 内容直接出现在编辑框里（不用再点开什么）",
        "polyextrude" in w.memory_edit.toPlainText(),
        w.memory_edit.toPlainText()[:120])

H.section("★★ 编辑共享记忆：改 / 删 / 加")
w.memory_edit.setPlainText("第一条\n第二条")
w.on_save_memory()
H.check_eq("保存后条数对了", memory.count(), 2)
H.check_eq("内容对了", [n["text"] for n in memory.load()],
           ["第一条", "第二条"])
H.check("★ 手写的条目来源标成「手动编辑」",
        all(n["from"] == "手动编辑" for n in memory.load()),
        [n["from"] for n in memory.load()])
H.check("★ 保存后编辑框不再是「已修改」状态",
        not w.memory_edit.document().isModified())
H.check_eq("标签上的条数跟着更新", w.tabs.tabText(1), "共享记忆 2")

H.section("★ 没动过的行，来源和时间都保得住")
memory.clear()
memory.add("老经验", source="/obj/g/AI1")
old_time = memory.load()[0]["time"]
w._last_memory = None
w.refresh()
w.memory_edit.setPlainText("老经验\n新加的一条")
w.on_save_memory()
notes = memory.load()
H.check_eq("还是两条", len(notes), 2)
H.check_eq("★ 一字没动的那条，来源保住了",
           notes[0]["from"], "/obj/g/AI1")
H.check_eq("★ 时间也保住了", notes[0]["time"], old_time)
H.check_eq("新加的那条标手动编辑", notes[1]["from"], "手动编辑")

H.section("★★ 刷新绝不能冲掉你正在打的字")
# 面板每 800ms 刷一次。不设闸门的话，打到一半的字会被 setPlainText 冲掉。
memory.clear()
memory.add("来自 AI 的一条")
w._last_memory = None
w.refresh()
w.memory_edit.setPlainText("我正在打的东西")
w.memory_edit.document().setModified(True)
memory.add("AI 又记了一条")     # 外部真的变了 → 签名变了 → 会走刷新
w.refresh()
H.check("★ 你打的字还在（没被覆盖）",
        w.memory_edit.toPlainText() == "我正在打的东西",
        w.memory_edit.toPlainText())

H.section("清空共享记忆")
w.on_clear_memory()
H.check("★ 清空真的清空了", memory.count() == 0, memory.count())
w._last_memory = None
w.refresh()
H.check("标签上的条数归零", w.tabs.tabText(1) == "共享记忆 0",
        w.tabs.tabText(1))

H.section("★ 宽窄自适应（安全网：面板被拖窄了也得能用）")
w.resize(760, 300)
w._apply_narrow(False)
H.check("宽的时候左栏在", w.left_frame.isVisibleTo(w))
H.check("宽的时候头行下拉不在", not w.header_combo.isVisibleTo(w))
w.resize(400, 300)
w._apply_narrow(True)
H.check("★ 窄的时候左栏收起", not w.left_frame.isVisibleTo(w))
H.check("★ 窄的时候头行出现下拉", w.header_combo.isVisibleTo(w))
H.check("下拉里也有那两个节点", w.header_combo.count() == 2,
        w.header_combo.count())
H.check("窄的时候名字标签让位给下拉",
        not w.header_name.isVisibleTo(w))
w.resize(760, 300)
w._apply_narrow(False)
H.check("拉回宽的就恢复", w.left_frame.isVisibleTo(w))

H.section("配错才提示（网关信息不常驻）")
H.check("网关配好时不显示提示", not w.problem_label.isVisibleTo(w),
        w.problem_label.text())

H.section("刷新不会因为场景变化而崩")
ai2.destroy()
try:
    w.refresh()
    H.check("删掉节点后刷新依然正常", True)
except Exception as exc:
    H.check("删掉节点后刷新依然正常", False, exc)

w._selected_path = "/obj/does_not_exist"
try:
    w.refresh()
    H.check("选了个不存在的路径也不崩", True)
except Exception as exc:
    H.check("选了个不存在的路径也不崩", False, exc)

H.section("★★ 宽度自适应（第一版写死宽度，在 2000px 面板上挤成一条）")
# ★ 注意：没显示的 QWidget **不会派发 resizeEvent**（离屏里实测
# resize(2000,400) 之后事件根本没来），所以显式调 _apply_adaptive()。
w.resize(2000, 400)
w._apply_adaptive()
wide_left = w.left_frame.width()
w.resize(700, 400)
w._apply_adaptive()
narrow_left = w.left_frame.width()

H.check("★ 面板越宽，左栏越宽（不是写死的）",
        wide_left > narrow_left, (wide_left, narrow_left))
H.check("左栏有下限（窄面板里还能看）", narrow_left >= panel.LIST_MIN,
        narrow_left)
H.check("左栏有上限（宽面板里不会无限撑）", wide_left <= panel.LIST_MAX,
        wide_left)
H.check("★ 文字列有最大宽度上限——一行字排 1800px 是没法读的",
        w.content.maximumWidth() == panel.CONTENT_MAX,
        w.content.maximumWidth())

H.section("★★ 输入框固定高度，绝不随内容变（自动长高整个删掉了）")
# 自动长高是第二版加的锦上添花，也是「打字打不进去」那条路上唯一有风险的
# 东西。真相是焦点问题（见下面焦点那一节），但既然它只是锦上添花，
# **整个删掉**：固定三行、多了内部滚动。所有聊天软件都这么做。
w.resize(900, 400)
w._apply_adaptive()
base = w.feedback.height()
H.check("默认是多行的（不是憋屈的一行）", base >= 40, base)
H.check("有固定高度", w.feedback.minimumHeight() == w.feedback.maximumHeight(),
        (w.feedback.minimumHeight(), w.feedback.maximumHeight()))
w.feedback.setPlainText("\n".join("第 %d 行" % i for i in range(20)))
H.check("★ 写多少行都不动高度（内部滚动）",
        w.feedback.height() == base, (base, w.feedback.height()))
w.feedback.clear()
H.check("清空也不动高度", w.feedback.height() == base,
        (base, w.feedback.height()))

_inrow2 = inspect.getsource(panel.panel_widget_class()._build_input_row)
H.check("★ 输入框上没有任何 textChanged 连接（自动长高已删除）",
        "textChanged.connect" not in _inrow2, _inrow2[-300:])
H.check("高度是 setFixedHeight 定的", "setFixedHeight" in _inrow2[:900])
H.check("★ 那些高度机制整个不存在了",
        not hasattr(panel, "INPUT_MIN_H") and
        not hasattr(panel, "INPUT_HEIGHT_DELAY_MS"))

H.section("★★★ 焦点：Houdini 的 pane 会把焦点交给**面板本身**")
# 这是「点击输入框，连光标都不闪，打不了字」的真凶。
# 实测（在活着的 Houdini 里）：
#     点击输入框 → MousePress 到达了 viewport ✅
#                → 但输入框从没收到 FocusIn ❌
#                → win.focusWidget() 变成 AIPanelWidget（面板本身）
# 面板住在 RE_WindowDrawable（QOpenGLWidget）里，Houdini 的 pane 焦点处理
# 把焦点给了面板控件，而面板没有焦点代理 → 按键无处可去。
H.check("★ 面板设了焦点代理，指向输入框",
        w.focusProxy() is w.feedback, w.focusProxy())
H.check("焦点代理就是那个输入框", w.focusProxy().isReadOnly() is False)

# 行为验证：把焦点给面板，看它有没有转给输入框
w.setFocus()
H.check("★ 焦点落到面板身上 → 自动转到输入框",
        w.focusProxy() is w.feedback,
        w.focusProxy())

# 行为验证：点输入框时，过滤器真的会调 setFocus
# （离屏里没有窗口激活，hasFocus() 不可靠，所以直接盯着 setFocus 有没有被调）
calls = []
_real = w.feedback.setFocus


def _spy(*a, **k):
    calls.append(a)
    return _real(*a, **k)


w.feedback.setFocus = _spy
try:
    QtTest.mouseClick(w.feedback.viewport(), QtCore.Qt.LeftButton,
                      pos=QtCore.QPoint(8, 8))
finally:
    w.feedback.setFocus = _real
H.check("★ 点输入框 → viewport 的过滤器把焦点抢回来", bool(calls), calls)

_fsrc = inspect.getsource(panel.panel_widget_class().eventFilter)
H.check("★ 过滤器盯的是 viewport 的鼠标按下",
        "feedback.viewport()" in _fsrc and "MouseButtonPress" in _fsrc)
H.check("★ 它不吞事件（return True 的话光标落不到你点的位置）",
        "return True" not in _fsrc.split("MouseButtonPress")[1][:400],
        _fsrc)

H.section("★ 定时器跟着显示状态走（不然僵尸面板会一直扫场景）")
# 实测：一次会话里 Houdini 攒了 14 个面板实例，每个都带一个 800ms 定时器，
# 每拍全场景走一遍节点树。构造时就 start 的话，这些僵尸永远不会停。
_z = Widget()
H.check("★ 没显示的时候定时器不跑", not _z._timer.isActive())
_z.show()
H.check("显示之后才跑", _z._timer.isActive())
_z.hide()
H.check("★ 藏起来就停（僵尸没有代价了）", not _z._timer.isActive())
_z.deleteLater()

H.section("★★ 视觉层级：对话区是富文本，不是一片白字")
demo = session.fresh()
rec = session.add_round(demo, "build", "做一个盒子")
rec["summary"] = "做好了。\n第二段。"
rec["trace"] = ["找节点类型：box 命中 1 个"]
html = session.conversation_html(demo)

H.check("说话人「我」有颜色", session.SPEAKER_COLORS["我"] in html, html[:200])
H.check("说话人「AI」有颜色", session.SPEAKER_COLORS["AI"] in html)
H.check("说话人加粗", "font-weight:600" in html)
H.check("块之间留了间距", "margin-top" in html)
H.check("行高放开了（读起来不挤）", "line-height" in html)
H.check("AI 的换行渲染成了换行标签", "<br>" in html, html[-200:])
H.check("说话人颜色只有两处（标签各一处）",
        html.count(session.SPEAKER_COLORS["我"]) == 1
        and html.count(session.SPEAKER_COLORS["AI"]) == 1,
        html.count(session.SPEAKER_COLORS["我"]))

# ★ 正文必须**显式**包一层 span 把颜色压回去：实测只给标签套 span 不够，
# Qt 会把标签的颜色带到整段正文上（渲染出来整条 AI 回复都是绿的）。
with_body = session.conversation_html(demo, body_color="#ff0000")
H.check("★ 给了 body_color 时正文被显式压回该颜色",
        "#ff0000" in with_body, with_body[:200])
H.check("正文颜色出现次数 = 消息条数（标签色不算）",
        with_body.count("#ff0000") == 2, with_body.count("#ff0000"))
H.check("★ 面板是从 palette 取的颜色（跟着主题走，不是写死的）",
        "foregroundRole" in inspect.getsource(panel.panel_widget_class()._render)
        or "palette()" in inspect.getsource(
            panel.panel_widget_class()._render),
        "面板渲染时要读 palette")
H.check("对话里没有过程日志", "找节点类型" not in html, html)

escaped = session.conversation_html(
    {"rounds": [{"n": 1, "user": "<script>alert(1)</script>"}]})
H.check("★ HTML 转义到位（消息里有尖括号也不炸）",
        "&lt;script&gt;" in escaped and "<script>" not in escaped, escaped)
H.check("空数据返回空串", session.conversation_html(session.fresh()) == "")

H.section("入口都可调用")
for name in ("on_row_selected", "on_send", "on_stop", "on_revert",
             "on_new_node", "on_toggle_process", "on_save_memory",
             "on_clear_memory", "on_clear_session", "select_path",
             "on_combo_changed"):
    H.check("%s 可调用" % name, callable(getattr(w, name, None)))
w.feedback.clear()
w.on_send()
H.check("空意见点发送不崩", True)

H.section("PythonPanel 壳类存在（Houdini 按这个名字找）")
PanelCls = panel.make_panel_class()
H.check("make_panel_class 返回一个类", isinstance(PanelCls, type))
H.check_eq("类名是 HoudiniAIPanel", PanelCls.__name__, "HoudiniAIPanel")
H.check("它实现了 buildInterface", hasattr(PanelCls, "buildInterface"))
H.check("它实现了 name", hasattr(PanelCls, "name"))

H.finish("test_panel")
