# -*- coding: utf-8 -*-
"""AI 面板：左列表 + 右内容，两栏。

## 布局为什么是这样（第三版，前两版都被打回）

用户定的形状：**左列表 / 中对话 / 右操作**，面板**跟视口/网络编辑器并排
（宽而矮）**。核心约束是 **能一行的绝不用两行** —— 竖向空间紧张，横向富余。

前两版栽的地方，以及第三版怎么改的：

1. **右栏被砍掉了。** 第二版把「发送 / 停止 / 回退」竖着摆在右边一列，
   用户的原话是「你的发送和什么回退为什么放在右上角？」。
   动作按钮就该**贴着它作用的东西**：发送贴在输入框右边。回退是低频动作，
   收进 ⋯ 菜单。右栏没了，面板从三栏变两栏，也更好读。

2. **共享记忆从浮层改成了标签页。** 第二版把它做成一个浮在右下角的
   `QFrame`，用户的原话是「共享记忆好丑啊，这UI为什么放在右下角啊？」。
   它其实是**内容**不是**动作**，所以放到内容区当一个标签页（对话 / 共享记忆），
   而且**可编辑** —— 用户要能自己改、删、加。

3. **输入框不再自动长高。** 第二版在 `textChanged` 里改控件高度，
   我怀疑那是「打字打不进去」的原因，加了三层保护也没治好。
   真相见下面第 4 条 —— 根本是另一回事。既然自动长高只是我的锦上添花，
   而它是这条路上唯一有风险的东西，**整个删掉**：固定三行、多了内部滚动。
   所有聊天软件都是这么做的。

4. **★ 焦点：真正的病根。** 用户报「点击输入框，连光标都不闪，打不了字」。
   在活着的 Houdini 里量出来的机制是：

       点击输入框 → MousePress 到达了 viewport ✅
                  → 但输入框**从没收到 FocusIn** ❌
                  → win.focusWidget() 变成 AIPanelWidget（面板本身，focusPolicy=0）

   原因：面板住在 `RE_WindowDrawable`（一个 `QOpenGLWidget`）里，
   **Houdini 的 pane 焦点处理会把焦点交给"面板控件本身"**，而不是里面的输入框。
   面板没有焦点代理，按键就无处可去 —— 没光标、打不了字。
   浮动窗口版没这个问题（那里没有 Houdini 的 pane 接管），所以一直测不出来。

   两道修法，互相独立：
     * `setFocusProxy(输入框)` —— 焦点一旦落到面板身上，**立刻转给输入框**
     * 输入框 viewport 上挂 `MouseButtonPress` → 强制 `setFocus()`
       （这个事件是**实测确实到达**的，所以这条路一定生效）

## 宽度自适应

左右两栏写死宽度在宽面板上会挤成一条（用户原话「宽度非常不合理」）。
现在节点列表**按面板宽度成比例**并夹在上下限之间；内容列**有最大宽度上限**
（`CONTENT_MAX`）—— 一行字排到 1800px 是没法读的，超过就把余量让给两侧留白。
窄到 `NARROW_AT` 以下，列表收进状态行的下拉里。

## 视觉层级

对话区用富文本（见 `session.conversation_html`）：说话人上色加粗、每条之间留白、
正文继承 Houdini 主题色（只给标签上色，这样换主题不会瞎）。
"""

from . import memory as memory_mod
from . import node as node_mod, runner, session

REFRESH_MS = 800

# ---- 宽度策略 ----
LIST_RATIO, LIST_MIN, LIST_MAX = 0.13, 120, 210
# 内容列最宽多少。再宽一行字就排得太长，读起来累。
CONTENT_MAX = 900
# 窄到这个宽度以下就把节点列表收进状态行的下拉里
NARROW_AT = 520

# ---- 输入框 ----
# 固定三行（按字体度量算，见 _build_input_row）。**不自动长高**，理由见模块头。
INPUT_LINES = 3

_WIDGET_CLASS = None


def _qt():
    from PySide6 import QtCore, QtGui, QtWidgets

    return QtCore, QtGui, QtWidgets


def _rgb(color):
    return tuple(int(round(255 * c)) for c in color.rgb()[:3])


def _clamp(value, low, high):
    return max(low, min(high, value))


def _build_widget_class():
    QtCore, QtGui, QtWidgets = _qt()
    import hou

    def _dot(color, size=9):
        """状态点。用一个方 QLabel 画圆，比画图标省事也够看。"""
        label = QtWidgets.QLabel()
        label.setFixedSize(size, size)
        label.setStyleSheet(
            "background-color: rgb(%d,%d,%d); border-radius: %dpx;"
            % (_rgb(color) + (size // 2,)))
        return label

    def _vline():
        line = QtWidgets.QFrame()
        line.setFrameShape(QtWidgets.QFrame.VLine)
        line.setFrameShadow(QtWidgets.QFrame.Plain)
        line.setStyleSheet("color: rgba(128,128,128,60);")
        return line

    def _hint(text):
        """灰色小字提示。"""
        label = QtWidgets.QLabel(text)
        label.setWordWrap(True)
        label.setStyleSheet("color: rgba(150,150,150,255); font-size: 11px;")
        return label

    class AIPanelWidget(QtWidgets.QWidget):
        """AI 面板的全部界面与逻辑。不依赖 PythonPanelInterface。"""

        def __init__(self, parent=None):
            super(AIPanelWidget, self).__init__(parent)
            self._selected_path = None
            self._last_render = None
            self._last_nodes = None
            self._last_memory = None
            self._narrow = None
            self._timer = None
            self._build_ui()
            self._build_timer()
            self.refresh()

        # ================================================================
        # 界面
        # ================================================================

        def _build_ui(self):
            row = QtWidgets.QHBoxLayout(self)
            row.setContentsMargins(0, 0, 0, 0)
            row.setSpacing(0)
            row.addWidget(self._build_left())
            row.addWidget(_vline())
            row.addWidget(self._build_content(), 1)

            # ★★ 焦点代理：Houdini 的 pane 会把焦点交给**面板控件本身**，
            # 焦点到了这里就立刻转给输入框。没有这一行，点输入框是没反应的。
            self.setFocusProxy(self.feedback)

        # ---- 左栏：节点列表 ----

        def _build_left(self):
            self.left_frame = QtWidgets.QFrame()
            self.left_frame.setFixedWidth(LIST_MIN)
            col = QtWidgets.QVBoxLayout(self.left_frame)
            col.setContentsMargins(6, 8, 4, 8)
            col.setSpacing(6)

            self.node_list = QtWidgets.QListWidget()
            self.node_list.setFrameShape(QtWidgets.QFrame.NoFrame)
            self.node_list.setSelectionMode(
                QtWidgets.QAbstractItemView.SingleSelection)
            self.node_list.setToolTip("这个 .hip 里的 AI 节点")
            # 行高撑开一点，不然一串节点挨在一起很难点
            self.node_list.setStyleSheet(
                "QListWidget::item { padding: 6px 4px; }")
            self.node_list.currentItemChanged.connect(self.on_row_selected)
            col.addWidget(self.node_list, 1)

            self.new_button = QtWidgets.QPushButton("+ 新建")
            self.new_button.setMinimumHeight(28)
            self.new_button.setToolTip("新建一个 AI 节点")
            self.new_button.clicked.connect(self.on_new_node)
            col.addWidget(self.new_button)
            return self.left_frame

        # ---- 右栏：状态行 + 标签页 + 输入行 ----

        def _build_content(self):
            middle = QtWidgets.QWidget()
            outer = QtWidgets.QHBoxLayout(middle)
            outer.setContentsMargins(0, 0, 0, 0)
            outer.setSpacing(0)

            # ★ 内容居中并限宽：面板很宽时，一行字排 1800px 是没法读的。
            self.content = QtWidgets.QWidget()
            self.content.setMaximumWidth(CONTENT_MAX)
            col = QtWidgets.QVBoxLayout(self.content)
            col.setContentsMargins(12, 8, 12, 8)
            col.setSpacing(8)
            col.addLayout(self._build_status_row())
            col.addWidget(self._build_tabs(), 1)
            col.addLayout(self._build_input_row())

            # ★ 内容的 stretch 给一个大数（CONTENT_MAX），两侧留白给 1。
            # 如果三方都是 1，Qt 会**平分**——内容只能拿到三分之一宽
            # （实测 1500px 的面板里内容只有 405px，而不是上限 900px）。
            outer.addStretch(1)
            outer.addWidget(self.content, CONTENT_MAX)
            outer.addStretch(1)
            return middle

        def _build_status_row(self):
            row = QtWidgets.QHBoxLayout()
            row.setSpacing(7)

            self.header_dot = _dot(node_mod.hou_color("idle"))
            row.addWidget(self.header_dot)

            self.header_combo = QtWidgets.QComboBox()
            self.header_combo.setSizeAdjustPolicy(
                QtWidgets.QComboBox.AdjustToContents)
            self.header_combo.currentIndexChanged.connect(self.on_combo_changed)
            self.header_combo.hide()
            row.addWidget(self.header_combo)

            self.header_name = QtWidgets.QLabel("（没有 AI 节点）")
            font = self.header_name.font()
            font.setBold(True)
            font.setPointSizeF(font.pointSizeF() * 1.1)
            self.header_name.setFont(font)
            row.addWidget(self.header_name)

            self.header_status = QtWidgets.QLabel("")
            self.header_status.setStyleSheet("color: rgba(150,150,150,255);")
            row.addWidget(self.header_status, 1)

            self.problem_label = QtWidgets.QLabel("")
            self.problem_label.setStyleSheet("color: rgb(220,90,70);")
            self.problem_label.hide()
            row.addWidget(self.problem_label)

            self.more_button = QtWidgets.QToolButton()
            self.more_button.setText("⋯")
            self.more_button.setToolTip("更多")
            self.more_button.setPopupMode(QtWidgets.QToolButton.InstantPopup)
            menu = QtWidgets.QMenu(self.more_button)
            menu.addAction("按描述重新构建", self.on_build)
            # ★ 回退从右栏搬到这里：它是低频动作，不值得占一个常驻按钮。
            menu.addAction("回退到上一轮之前", self.on_revert)
            menu.addAction("新建 AI 节点", self.on_new_node)
            menu.addAction("打开动作面板", self.on_open_motion_panel)
            menu.addSeparator()
            menu.addAction("清空这个节点的对话", self.on_clear_session)
            menu.addAction("网关信息…", self.on_show_gateway)
            self.more_button.setMenu(menu)
            row.addWidget(self.more_button)
            return row

        # ---- 标签页：对话 / 共享记忆 ----

        def _build_tabs(self):
            self.tabs = QtWidgets.QTabWidget()
            self.tabs.setDocumentMode(True)
            self.tabs.addTab(self._build_conversation_page(), "对话")
            self.tabs.addTab(self._build_memory_page(), "共享记忆")
            self.tabs.currentChanged.connect(self._on_tab_changed)

            # ★ 「过程」是**对话的显示模式**，所以它属于标签行，不属于输入行。
            # 第二版把它塞在输入框旁边，跟"发送"混在一起，语义不清。
            self.process_toggle = QtWidgets.QToolButton()
            self.process_toggle.setText("过程")
            # ★ QToolButton 默认是 ToolButtonIconOnly——一旦设了 arrow 就只画三角，
            # **文字会被吃掉**（渲染出来只剩一个 ▸）。要显式要图标+文字。
            self.process_toggle.setToolButtonStyle(
                QtCore.Qt.ToolButtonTextBesideIcon)
            self.process_toggle.setCheckable(True)
            self.process_toggle.setArrowType(QtCore.Qt.RightArrow)
            self.process_toggle.setToolTip("显示每一步工具调用（排查时用）")
            self.process_toggle.toggled.connect(self.on_toggle_process)
            self.tabs.setCornerWidget(self.process_toggle,
                                      QtCore.Qt.TopRightCorner)
            return self.tabs

        def _build_conversation_page(self):
            page = QtWidgets.QWidget()
            col = QtWidgets.QVBoxLayout(page)
            col.setContentsMargins(0, 6, 0, 0)
            col.setSpacing(4)

            # 富文本：说话人上色加粗、每条之间留白。纯文本做不出层级，
            # 用户看到的就是「全都是一样白字」。
            self.conversation = QtWidgets.QTextEdit()
            self.conversation.setReadOnly(True)
            self.conversation.setFrameShape(QtWidgets.QFrame.NoFrame)
            self.conversation.setPlaceholderText(
                "还没有跑过。在节点参数里写好「描述」和「思考深度」，"
                "点「开始构建」。")
            col.addWidget(self.conversation, 1)

            self.process_view = QtWidgets.QPlainTextEdit()
            self.process_view.setReadOnly(True)
            self.process_view.setFrameShape(QtWidgets.QFrame.NoFrame)
            self.process_view.setMaximumHeight(150)
            self.process_view.setStyleSheet(
                "color: rgba(150,150,150,255); font-size: 11px;")
            self.process_view.hide()
            col.addWidget(self.process_view)
            return page

        def _build_memory_page(self):
            """共享记忆 —— **可编辑**的一页。

            用户的原话：「共享记忆它也需要一个打字框，我得可以编辑它呀」。
            一行一条；跟原来一字不差的行，来源和时间会保留（见 memory.from_text）。
            """
            page = QtWidgets.QWidget()
            col = QtWidgets.QVBoxLayout(page)
            col.setContentsMargins(0, 6, 0, 0)
            col.setSpacing(6)

            col.addWidget(_hint(
                "一行一条。AI 觉得有别的节点也用得上的经验，会自己记在这里；"
                "你可以直接改、删、加。跟着这个 .hip 走。"))

            self.memory_edit = QtWidgets.QPlainTextEdit()
            self.memory_edit.setFrameShape(QtWidgets.QFrame.NoFrame)
            self.memory_edit.setPlaceholderText(
                "还没有记下任何经验。\n"
                "也可以自己写一条，比如「这个项目的单位是厘米」。")
            col.addWidget(self.memory_edit, 1)

            foot = QtWidgets.QHBoxLayout()
            foot.setSpacing(6)
            self.memory_count = QtWidgets.QLabel("")
            self.memory_count.setStyleSheet(
                "color: rgba(150,150,150,255); font-size: 11px;")
            foot.addWidget(self.memory_count, 1)

            self.memory_clear_button = QtWidgets.QPushButton("清空")
            self.memory_clear_button.setToolTip("把所有共享经验删掉")
            self.memory_clear_button.clicked.connect(self.on_clear_memory)
            foot.addWidget(self.memory_clear_button)

            self.memory_save_button = QtWidgets.QPushButton("保存")
            self.memory_save_button.setToolTip("把上面的内容存进这个 .hip")
            self.memory_save_button.clicked.connect(self.on_save_memory)
            foot.addWidget(self.memory_save_button)
            col.addLayout(foot)
            return page

        # ---- 输入行：输入框 + 发送 / 停止 ----

        def _build_input_row(self):
            row = QtWidgets.QHBoxLayout()
            row.setSpacing(8)

            self.feedback = QtWidgets.QPlainTextEdit()
            self.feedback.setPlaceholderText(
                "写修改意见，比如「再高一点，顶面加点噪声」　Ctrl+Enter 发送")
            # ★ 固定高度，**绝不随内容变化**。自动长高在 textChanged 里改控件
            # 尺寸，是这条路上唯一有风险的东西，而它只是锦上添花——删掉。
            metrics = QtGui.QFontMetrics(self.feedback.font())
            self._input_height = int(metrics.lineSpacing() * INPUT_LINES + 16)
            self.feedback.setFixedHeight(self._input_height)
            self.feedback.installEventFilter(self)
            # ★ 第二道焦点修法：鼠标按下时强制聚焦。
            # 实测这个事件**确实会到达** viewport，所以这条路一定生效。
            self.feedback.viewport().installEventFilter(self)
            row.addWidget(self.feedback, 1)

            # ★ 按钮要**贴底**（聊天软件的惯例：三行的输入框，按钮在最下面）。
            # 直接 `row.addLayout(buttons)` 是不行的——布局项当子项时拿不到
            # 整行高度，`addStretch(1)` 没有多余空间可分，按钮就飘到顶上了
            # （实测：框在 y=330..388，按钮却在 326，比框还高 4px）。
            # 包一层 QWidget，它默认的 Preferred 策略才会被撑满。
            button_box = QtWidgets.QWidget()
            buttons = QtWidgets.QVBoxLayout(button_box)
            buttons.setContentsMargins(0, 0, 0, 0)
            buttons.setSpacing(6)
            buttons.addStretch(1)

            self.send_button = QtWidgets.QPushButton("发送")
            self.send_button.setMinimumHeight(30)
            self.send_button.setToolTip("把修改意见发给 AI（Ctrl+Enter）")
            self.send_button.clicked.connect(self.on_send)
            buttons.addWidget(self.send_button)

            self.stop_button = QtWidgets.QPushButton("停止")
            self.stop_button.setMinimumHeight(30)
            self.stop_button.setToolTip("中断正在跑的这一轮")
            self.stop_button.clicked.connect(self.on_stop)
            self.stop_button.hide()          # 只在跑的时候出现
            buttons.addWidget(self.stop_button)

            row.addWidget(button_box)
            return row

        # ================================================================
        # 自适应
        # ================================================================

        def resizeEvent(self, event):
            super(AIPanelWidget, self).resizeEvent(event)
            self._apply_adaptive()

        def _apply_adaptive(self):
            """按当前宽度重算左栏宽度和窄模式。

            单独抽成一个方法是为了能测：**没显示的 QWidget 不会派发
            resizeEvent**（离屏里实测 `resize(2000,400)` 之后事件根本没来），
            测试只能直接调它。
            """
            width = self.width()
            self.left_frame.setFixedWidth(
                int(_clamp(width * LIST_RATIO, LIST_MIN, LIST_MAX)))
            self._apply_narrow(width < NARROW_AT)

        def _apply_narrow(self, narrow):
            if narrow == self._narrow:
                return
            self._narrow = narrow
            self.left_frame.setVisible(not narrow)
            self.header_combo.setVisible(narrow)
            self.header_name.setVisible(not narrow)

        # ================================================================
        # 刷新
        # ================================================================

        def _build_timer(self):
            self._timer = QtCore.QTimer(self)
            self._timer.setInterval(REFRESH_MS)
            self._timer.timeout.connect(self.refresh)
            # ★ 不在这里 start()。定时器由 showEvent 管理：
            # Houdini 每次 reloadActiveInterface 都会造新面板，旧的留在内存里
            # （实测一次会话里攒了 14 个）。如果构造时就 start，那些僵尸会
            # **每一个都继续每 800ms 扫一遍全场景**。跟着显示状态走就没这问题。

        def showEvent(self, event):
            super(AIPanelWidget, self).showEvent(event)
            if self._timer is not None and not self._timer.isActive():
                self._timer.start()
            self.refresh()

        def hideEvent(self, event):
            super(AIPanelWidget, self).hideEvent(event)
            # 看不见的面板没有任何理由刷新。顺带把僵尸实例的代价清零。
            if self._timer is not None:
                self._timer.stop()

        def refresh(self):
            try:
                self._refresh_nodes()
                self._refresh_status()
                self._refresh_buttons()
                self._refresh_memory()
                self._render()
            except Exception:
                # 面板刷新绝不能把 Houdini 搞崩。场景正在被改的时候偶发取不到
                # 节点是正常的，下一拍就好了。
                pass

        def _nodes(self):
            return node_mod.all_ai_nodes()

        def _refresh_nodes(self):
            nodes = self._nodes()
            paths = [n.path() for n in nodes]
            if paths != self._last_nodes:
                self._last_nodes = paths
                self.node_list.blockSignals(True)
                self.node_list.clear()
                for node in nodes:
                    state = node_mod.current_state(node)
                    item = QtWidgets.QListWidgetItem("● %s" % node.name())
                    item.setData(QtCore.Qt.UserRole, node.path())
                    item.setForeground(QtGui.QColor(*_rgb(
                        node_mod.hou_color(state))))
                    item.setToolTip(node.path())
                    self.node_list.addItem(item)
                self.node_list.blockSignals(False)

                self.header_combo.blockSignals(True)
                self.header_combo.clear()
                for node in nodes:
                    self.header_combo.addItem(node.name(), node.path())
                self.header_combo.blockSignals(False)

            if self._selected_path not in paths:
                self._selected_path = paths[0] if paths else None
            self._select_in_widgets(paths)

        def _select_in_widgets(self, paths):
            if not self._selected_path:
                return
            for row, path in enumerate(paths):
                if path != self._selected_path:
                    continue
                if self.node_list.currentRow() != row:
                    self.node_list.blockSignals(True)
                    self.node_list.setCurrentRow(row)
                    self.node_list.blockSignals(False)
                index = self.header_combo.findData(path)
                if index >= 0 and self.header_combo.currentIndex() != index:
                    self.header_combo.blockSignals(True)
                    self.header_combo.setCurrentIndex(index)
                    self.header_combo.blockSignals(False)
                break

        def selected_node(self):
            if not self._selected_path:
                return None
            try:
                return hou.node(self._selected_path)
            except Exception:
                return None

        def _refresh_status(self):
            node = self.selected_node()
            if node is None:
                self.header_dot.setStyleSheet("background-color: transparent;")
                self.header_name.setText("（没有 AI 节点）")
                self.header_status.setText("")
                return
            state = node_mod.current_state(node)
            self.header_dot.setStyleSheet(
                "background-color: rgb(%d,%d,%d); border-radius: 4px;"
                % _rgb(node_mod.hou_color(state)))
            self.header_name.setText(node.name())
            text = node_mod.status_line(node) or node_mod.LABEL_OF.get(state, "")
            round_no = node_mod.round_of(node)
            if round_no:
                text += "　· %d 轮" % round_no
            self.header_status.setText(text)
            self.header_status.setToolTip(node_mod.detail_of(node))

        def _refresh_buttons(self):
            node = self.selected_node()
            state = node_mod.current_state(node) if node is not None else "idle"
            self.stop_button.setVisible(state == "working")
            self.send_button.setEnabled(node is not None)
            self._refresh_problem()

        def _refresh_problem(self):
            """只有出问题的时候才在状态行里吱一声。

            网关地址平时不显示——它占两三行，而唯一有用的时刻就是"连不上"的时候。
            """
            from . import config, gateway

            problem = ""
            try:
                cfg = config.load()
                ok, why = gateway.Gateway(cfg).ready()
                if not ok:
                    problem = "网关没配好：%s" % why
            except Exception as exc:
                problem = "读配置失败：%s" % exc
            self.problem_label.setText(problem)
            self.problem_label.setVisible(bool(problem))

        def _refresh_memory(self):
            notes = memory_mod.load()
            signature = [(n.get("text"), n.get("from")) for n in notes]
            if signature == self._last_memory:
                return
            self._last_memory = signature
            self.tabs.setTabText(1, "共享记忆 %d" % len(notes))
            self.memory_count.setText("%d 条" % len(notes) if notes else "")
            # ★ 你正在编辑的时候绝不能覆盖你的输入。
            # 面板每 800ms 刷一次，不设这道闸门的话，打到一半的字会被冲掉。
            if not self.memory_edit.document().isModified():
                self.memory_edit.setPlainText(memory_mod.to_text())
                self.memory_edit.document().setModified(False)

        def _render(self):
            node = self.selected_node()
            if node is None:
                self.conversation.clear()
                self.process_view.setPlainText("")
                self._last_render = None
                return

            data = session.load(node)
            signature = (node.path(), len(data.get("rounds") or []),
                         node_mod.status_line(node))
            if self._last_render == signature:
                return
            self._last_render = signature

            # 正文颜色从当前 palette 取——主题换了它跟着换，
            # 不写死在 session.py 里（那样换浅色主题会看不见）。
            body_color = self.conversation.palette().color(
                self.conversation.foregroundRole()).name()
            self.conversation.setHtml(
                session.conversation_html(data, body_color))
            self.process_view.setPlainText(session.process_text(data))

        # ================================================================
        # 交互
        # ================================================================

        def on_row_selected(self, current=None, previous=None):
            if current is None:
                return
            path = current.data(QtCore.Qt.UserRole)
            if path and path != self._selected_path:
                self._selected_path = path
                self._last_render = None
                self._refresh_status()
                self._refresh_buttons()
                self._render()

        def on_combo_changed(self, index):
            path = self.header_combo.itemData(index)
            if path and path != self._selected_path:
                self._selected_path = path
                self._last_render = None
                self.refresh()

        def select_path(self, path):
            """把面板切到某个节点上。

            .pypanel 的 onNodePathChanged 会调它——你在网络编辑器里点中一个
            AI 节点，面板就跟着切过去，不用再去列表里找。
            """
            if path and path != self._selected_path:
                self._selected_path = path
                self._last_render = None
                self.refresh()

        def _on_tab_changed(self, index):
            # 「过程」只对「对话」那一页有意义
            self.process_toggle.setVisible(index == 0)

        def on_toggle_process(self, checked):
            self.process_view.setVisible(bool(checked))
            self.process_toggle.setArrowType(
                QtCore.Qt.DownArrow if checked else QtCore.Qt.RightArrow)

        # ---- 共享记忆 ----

        def on_save_memory(self):
            runner.note_main_thread()
            text = self.memory_edit.toPlainText()
            ok = memory_mod.from_text(text)
            self.memory_edit.document().setModified(False)
            self._last_memory = None          # 强制重算条数和标签
            self._refresh_memory()
            self._say("共享记忆已保存" if ok else "保存失败")

        def on_clear_memory(self):
            if not memory_mod.count():
                return
            try:
                choice = hou.ui.displayMessage(
                    "确定要清空这个 .hip 的共享经验吗？\n"
                    "各个 AI 节点记下的东西会一起没掉。",
                    buttons=("清空", "取消"), severity=hou.severityType.Warning)
            except Exception:
                choice = 0
            if choice != 0:
                return
            memory_mod.clear()
            self.memory_edit.document().setModified(False)
            self._last_memory = None
            self._refresh_memory()

        def _say(self, message):
            """状态栏一句话。绝不弹对话框——用户明确要求过。"""
            try:
                hou.ui.setStatusMessage(message)
            except Exception:
                pass

        # ---- 动作 ----

        def on_new_node(self):
            runner.note_main_thread()
            from . import shelf

            node = shelf.new_node_ui(None)
            if node is not None:
                self._selected_path = node.path()
                self._last_nodes = None
                self._last_render = None
                self.refresh()

        def on_send(self):
            runner.note_main_thread()
            node = self.selected_node()
            if node is None:
                return
            text = self.feedback.toPlainText().strip()
            if not text:
                return
            self.feedback.clear()
            runner.request_followup(node, text)
            self._last_render = None
            self.refresh()

        def on_build(self):
            runner.note_main_thread()
            node = self.selected_node()
            if node is None:
                return
            runner.request_build(node)
            self._last_render = None
            self.refresh()

        def on_stop(self):
            runner.note_main_thread()
            node = self.selected_node()
            if node is not None:
                runner.request_stop(node)

        def on_revert(self):
            runner.note_main_thread()
            node = self.selected_node()
            if node is None:
                return
            rounds = session.load(node).get("rounds") or []
            if not rounds:
                node_mod.set_status(node, "idle", "还没有轮次可以回退")
                return
            if runner.restore_round(node, len(rounds)):
                self._last_render = None
                self.refresh()

        def on_clear_session(self):
            node = self.selected_node()
            if node is None:
                return
            session.clear(node)
            self._last_render = None
            self.refresh()

        def on_show_gateway(self):
            from . import config

            try:
                cfg = config.load()
                hou.ui.displayMessage(config.describe(cfg), title="网关")
            except Exception as exc:
                hou.ui.displayMessage("读配置失败：%s" % exc)

        def on_open_motion_panel(self):
            try:
                from . import motion_panel

                motion_panel.open_pane_tab()
            except Exception as exc:
                self._say("开动作面板失败：%s" % exc)

        def eventFilter(self, obj, event):
            # ① 在输入框里按 Ctrl+Enter = 发送
            if obj is self.feedback and event.type() == QtCore.QEvent.KeyPress:
                if (event.key() in (QtCore.Qt.Key_Return, QtCore.Qt.Key_Enter)
                        and event.modifiers() & QtCore.Qt.ControlModifier):
                    self.on_send()
                    return True

            # ② ★ 鼠标按在输入框上 → 强制聚焦。
            # 实测：鼠标事件确实到达 viewport，但输入框收不到 FocusIn
            # （Houdini 的 pane 把焦点给了面板本身）。这里直接抢回来。
            #   注意**不要 return True**：让点击继续往下走，
            #   光标才会落在你点的那个字符位置上。
            if (obj is self.feedback.viewport()
                    and event.type() == QtCore.QEvent.MouseButtonPress):
                self.feedback.setFocus(QtCore.Qt.MouseFocusReason)

            return False

        # ---- 焦点兜底 ----

        def focusInEvent(self, event):
            """万一焦点还是落到了面板身上，转手给输入框。

            `setFocusProxy` 正常工作时这个不会触发；留着是因为它是**便宜的**，
            而用户在这个问题上已经被坑了三轮。
            """
            super(AIPanelWidget, self).focusInEvent(event)
            if self.feedback is not None:
                self.feedback.setFocus(QtCore.Qt.OtherFocusReason)

    return AIPanelWidget


def panel_widget_class():
    global _WIDGET_CLASS
    if _WIDGET_CLASS is None:
        _WIDGET_CLASS = _build_widget_class()
    return _WIDGET_CLASS


def make_panel_class():
    """造出 Houdini 找的那个 PythonPanel 类（薄薄一层壳）。"""
    import hou

    Widget = panel_widget_class()

    class HoudiniAIPanel(hou.PythonPanelInterface):
        def __init__(self):
            super(HoudiniAIPanel, self).__init__()
            self._widget = None

        def name(self):
            return "AI 节点"

        def buildInterface(self):
            self._widget = Widget()
            return self._widget

    return HoudiniAIPanel


# --------------------------------------------------------------------------
# 浮动窗口兜底
# --------------------------------------------------------------------------

_floating = []


def show_floating():
    """不想开面板时，弹一个独立窗口。功能和停靠面板完全一样。"""
    import hou

    for window in _floating:
        try:
            window.show()
            window.raise_()
            return window
        except Exception:
            _floating.remove(window)

    QtCore, QtGui, QtWidgets = _qt()
    window = QtWidgets.QMainWindow(hou.qt.mainWindow())
    window.setWindowTitle("Houdini AI 节点")
    window.setCentralWidget(panel_widget_class()())
    window.resize(1000, 420)
    window.show()
    _floating.append(window)
    return window
