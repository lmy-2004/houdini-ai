# -*- coding: utf-8 -*-
"""动作生成面板。

沿用 `panel.py` 的两条设计（别改回去）：
1. **界面是独立的 QWidget**，不是 `PythonPanelInterface` 子类——后者是抽象类，
   无界面环境下实例化不了，界面逻辑就永远测不到。
2. **刷新用 QTimer 轮询**，不跨线程发 Qt 信号；工作线程只改普通 Python 状态。

交互上照 Blender 那个插件的思路（**只学交互，不抄代码**——那个仓库没有 LICENSE）：
约束是「三点式」加的 —— 把播放头拉到目标帧 → 在视口里摆好姿势 → 点约束类型按钮。
比填表格直观得多。
"""

import os

from . import motion_api, motion_npz, motion_rig, motion_session as MS

REFRESH_MS = 900
_WIDGET_CLASS = None

# 面板上约束按钮的排布（kind, 显示名）
CONSTRAINT_BUTTONS = [
    ("root2d", "根路径"),
    ("fullbody", "全身姿势"),
    ("left-hand", "左手"),
    ("right-hand", "右手"),
    ("left-foot", "左脚"),
    ("right-foot", "右脚"),
]


def _qt():
    from PySide6 import QtCore, QtGui, QtWidgets
    return QtCore, QtGui, QtWidgets


def _widget_class():
    """惰性构造 QWidget 子类（PySide6 与 hou 都在这时才 import）。"""
    global _WIDGET_CLASS
    if _WIDGET_CLASS is not None:
        return _WIDGET_CLASS

    QtCore, QtGui, QtWidgets = _qt()
    import hou

    # =====================================================================
    class TimelineView(QtWidgets.QGraphicsView):
        """自绘时间轴：刻度 + 段色块 + 约束菱形 + 播放头。

        用 QGraphicsScene 而不是裸 paintEvent——命中测试、拖拽、缩放都是白拿的。
        """

        def __init__(self, panel):
            super(TimelineView, self).__init__()
            self.panel = panel
            self.scene_ = QtWidgets.QGraphicsScene(self)
            self.setScene(self.scene_)
            self.setRenderHint(QtGui.QPainter.Antialiasing, False)
            self.setMinimumHeight(112)
            self.setHorizontalScrollBarPolicy(QtCore.Qt.ScrollBarAlwaysOff)
            self.setVerticalScrollBarPolicy(QtCore.Qt.ScrollBarAlwaysOff)
            self.setBackgroundBrush(QtGui.QBrush(QtGui.QColor(38, 38, 42)))
            self._px_per_frame = 8.0
            self._n = 90
            self._drag = None

        # ---- 坐标换算 ----
        def _x(self, f):
            return f * self._px_per_frame

        def _frame_at(self, x):
            return int(round(x / max(1e-6, self._px_per_frame)))

        # ---- 重绘 ----
        def redraw(self, total_frames, segments, constraints, playhead_asset):
            self.scene_.clear()
            n = max(1, int(total_frames or 90))
            self._n = n
            w = self._x(n)
            h = 108
            self.scene_.setSceneRect(-4, -4, w + 8, h + 8)

            # 刻度
            step = 10
            while self._x(step) < 42:
                step *= 2
            pen = QtGui.QPen(QtGui.QColor(90, 90, 96))
            for f in range(0, n + 1, step):
                self.scene_.addLine(self._x(f), h - 14, self._x(f), h - 4, pen)
                t = self.scene_.addSimpleText(str(f))
                t.setBrush(QtGui.QBrush(QtGui.QColor(150, 150, 156)))
                t.setPos(self._x(f) - 8, h - 30)
            self.scene_.addLine(0, h - 14, w, h - 14, pen)

            # 段色块
            palette = [(66, 106, 156), (96, 136, 96), (156, 116, 66),
                       (126, 86, 146), (156, 86, 96)]
            y = 8
            for i, (a, b) in enumerate(segments):
                rect = QtWidgets.QGraphicsRectItem(self._x(a), y, self._x(b - a), 34)
                c = palette[i % len(palette)]
                rect.setBrush(QtGui.QBrush(QtGui.QColor(c[0], c[1], c[2], 190)))
                rect.setPen(QtGui.QPen(QtGui.QColor(20, 20, 22), 1))
                self.scene_.addItem(rect)
                if b - a > 3:
                    t = self.scene_.addSimpleText("%d %s" % (i + 1, self.panel.segment_label(i)))
                    t.setBrush(QtGui.QBrush(QtGui.QColor(240, 240, 240)))
                    t.setPos(self._x(a) + 5, y + 8)
                y += 0

            # 约束菱形（可拖）
            for ci, c in enumerate(constraints):
                for f in c.frame_list():
                    if f < 0 or f > n:
                        continue
                    self._diamond(ci, f, c.kind, h)

            # 播放头
            self.ph = QtWidgets.QGraphicsLineItem(self._x(playhead_asset), 0,
                                                  self._x(playhead_asset), h - 14)
            self.ph.setPen(QtGui.QPen(QtGui.QColor(240, 196, 60), 2))
            self.scene_.addItem(self.ph)

            self.resetTransform()
            self.scale(min(1.0, max(0.2, self.viewport().width() / max(1.0, w + 12))), 1.0)

        def _diamond(self, ci, f, kind, h):
            color = {
                "fullbody": (240, 196, 60), "root2d": (110, 200, 240),
            }.get(kind, (240, 130, 90))
            poly = QtGui.QPolygonF([
                QtCore.QPointF(self._x(f), h - 44),
                QtCore.QPointF(self._x(f) + 6, h - 36),
                QtCore.QPointF(self._x(f), h - 28),
                QtCore.QPointF(self._x(f) - 6, h - 36),
            ])
            it = QtWidgets.QGraphicsPolygonItem(poly)
            it.setBrush(QtGui.QBrush(QtGui.QColor(*color)))
            it.setPen(QtGui.QPen(QtGui.QColor(30, 30, 32), 1))
            it.setFlag(QtWidgets.QGraphicsItem.ItemIsSelectable, True)
            it.setToolTip("%s @ 素材帧 %d" % (MS.KIND_LABELS.get(kind, kind), f))
            it.setData(0, (ci, f))
            self.scene_.addItem(it)
            return it

        # ---- 鼠标 ----
        def mousePressEvent(self, ev):
            item = self.itemAt(ev.pos())
            if isinstance(item, QtWidgets.QGraphicsPolygonItem):
                self._drag = item.data(0)
                self.panel.on_diamond_picked(self._drag[0], self._drag[1])
                return
            self._set_playhead(ev)

        def mouseMoveEvent(self, ev):
            if self._drag is not None:
                self._set_playhead(ev, move_diamond=True)
                return
            super(TimelineView, self).mouseMoveEvent(ev)

        def mouseReleaseEvent(self, ev):
            self._drag = None
            super(TimelineView, self).mouseReleaseEvent(ev)

        def wheelEvent(self, ev):
            self._px_per_frame = min(48.0, max(1.5, self._px_per_frame * (1.15 if ev.angleDelta().y() > 0 else 0.87)))
            self.panel.refresh()
            ev.accept()

        def _set_playhead(self, ev, move_diamond=False):
            p = self.mapToScene(ev.pos())
            f = max(0, self._frame_at(p.x()))
            if move_diamond:
                self.panel.on_diamond_moved(self._drag[0], self._drag[1], f)
            else:
                self.panel.on_timeline_clicked(f)

    # =====================================================================
    class MotionPanelWidget(QtWidgets.QWidget):
        """动作面板的全部界面与逻辑。"""

        def __init__(self, parent=None):
            super(MotionPanelWidget, self).__init__(parent)
            self.session = MS.MotionSession()
            self.node_path = None
            self.status_text = "空闲"
            self.log_lines = []
            self._busy = False
            self._cancel = None
            self._timer = None
            # 工作线程只往这里塞回调，主线程在 refresh 里取走执行
            # （绝不跨线程碰 Qt 对象）
            self._ui_queue = []
            self._loaded_from = None      # 当前会话是从哪个节点读来的（免得重复读）
            self._build_ui()
            self._start_timer()
            self.refresh()

        # ---- 界面 ----
        def _build_ui(self):
            outer = QtWidgets.QVBoxLayout(self)
            outer.setContentsMargins(6, 6, 6, 6)
            outer.setSpacing(5)
            outer.addWidget(self._build_header())
            outer.addWidget(self._build_timeline())
            mid = QtWidgets.QHBoxLayout()
            mid.addWidget(self._build_segments(), 1)
            mid.addWidget(self._build_constraints(), 1)
            outer.addLayout(mid, 1)
            outer.addWidget(self._build_actions())
            outer.addWidget(self._build_log())

        def _build_header(self):
            box = QtWidgets.QGroupBox("动作生成")
            grid = QtWidgets.QGridLayout(box)
            self.node_combo = QtWidgets.QComboBox()
            self.node_combo.setMinimumWidth(200)
            btn_new = QtWidgets.QPushButton("新建动作节点")
            btn_new.clicked.connect(self.on_new_node)
            self.mode_combo = QtWidgets.QComboBox()
            self.mode_combo.addItems(["单段", "时间轴（多段）"])
            self.mode_combo.currentIndexChanged.connect(self._on_mode_changed)
            self.status_label = QtWidgets.QLabel("空闲")
            grid.addWidget(QtWidgets.QLabel("目标节点"), 0, 0)
            grid.addWidget(self.node_combo, 0, 1, 1, 3)
            grid.addWidget(btn_new, 0, 4)
            grid.addWidget(QtWidgets.QLabel("模式"), 1, 0)
            grid.addWidget(self.mode_combo, 1, 1)
            grid.addWidget(QtWidgets.QLabel("状态"), 1, 2)
            grid.addWidget(self.status_label, 1, 3, 1, 2)
            self.fps_label = QtWidgets.QLabel("")
            btn_fps = QtWidgets.QPushButton("设为 30fps")
            btn_fps.clicked.connect(self.on_set_fps_30)
            grid.addWidget(self.fps_label, 2, 0, 1, 3)
            grid.addWidget(btn_fps, 2, 3, 1, 2)
            return box

        def _build_timeline(self):
            box = QtWidgets.QGroupBox("时间轴")
            lay = QtWidgets.QVBoxLayout(box)
            self.timeline = TimelineView(self)
            lay.addWidget(self.timeline)
            row = QtWidgets.QHBoxLayout()
            for label, slot in (("⟲ 回到首帧", self.on_first), ("◀◀ 上一帧", self.on_prev),
                                ("▶▶ 下一帧", self.on_next), ("⏭ 到末帧", self.on_last)):
                b = QtWidgets.QPushButton(label)
                b.clicked.connect(slot)
                row.addWidget(b)
            row.addStretch(1)
            self.hint = QtWidgets.QLabel("滚轮缩放")
            row.addWidget(self.hint)
            lay.addLayout(row)
            return box

        def _build_segments(self):
            box = QtWidgets.QGroupBox("文字段")
            lay = QtWidgets.QVBoxLayout(box)
            self.seg_list = QtWidgets.QListWidget()
            self.seg_list.setMinimumHeight(120)
            self.seg_list.currentRowChanged.connect(self._on_seg_selected)
            lay.addWidget(self.seg_list, 1)
            self.seg_text = QtWidgets.QPlainTextEdit()
            self.seg_text.setPlaceholderText("这一段的文字描述（英文，例如：a person walks forward cautiously）")
            self.seg_text.setMinimumHeight(52)
            self.seg_text.textChanged.connect(self._on_seg_text_changed)
            lay.addWidget(self.seg_text)
            row = QtWidgets.QHBoxLayout()
            self.seg_secs = QtWidgets.QDoubleSpinBox()
            self.seg_secs.setRange(0.1, MS.MAX_SEGMENT_SECONDS)
            self.seg_secs.setSingleStep(0.5)
            self.seg_secs.setValue(3.0)
            self.seg_secs.valueChanged.connect(self._on_seg_secs_changed)
            row.addWidget(QtWidgets.QLabel("时长(秒)"))
            row.addWidget(self.seg_secs)
            self.seg_frames_label = QtWidgets.QLabel("")
            row.addWidget(self.seg_frames_label)
            row.addStretch(1)
            lay.addLayout(row)
            row2 = QtWidgets.QHBoxLayout()
            for label, slot in (("+ 加一段", self.on_add_segment), ("复制", self.on_dup_segment),
                                ("↑", self.on_seg_up), ("↓", self.on_seg_down),
                                ("删掉", self.on_del_segment)):
                b = QtWidgets.QPushButton(label)
                b.clicked.connect(slot)
                row2.addWidget(b)
            lay.addLayout(row2)
            return box

        def _build_constraints(self):
            box = QtWidgets.QGroupBox("约束（把播放头拉到目标帧，摆好姿势，再点类型）")
            lay = QtWidgets.QVBoxLayout(box)
            grid = QtWidgets.QGridLayout()
            for i, (kind, label) in enumerate(CONSTRAINT_BUTTONS):
                b = QtWidgets.QPushButton(label)
                b.clicked.connect(lambda _=False, k=kind: self.on_add_constraint(k))
                grid.addWidget(b, i // 3, i % 3)
            lay.addLayout(grid)
            self.con_list = QtWidgets.QListWidget()
            lay.addWidget(self.con_list, 1)
            row = QtWidgets.QHBoxLayout()
            for label, slot in (("跳到该帧", self.on_goto_constraint),
                                ("删掉", self.on_del_constraint),
                                ("清空", self.on_clear_constraints),
                                ("自动原点", self.on_canonical)):
                b = QtWidgets.QPushButton(label)
                b.clicked.connect(slot)
                row.addWidget(b)
            lay.addLayout(row)
            return box

        def _build_actions(self):
            box = QtWidgets.QGroupBox("操作")
            row = QtWidgets.QHBoxLayout(box)
            self.btn_gen = QtWidgets.QPushButton("生成动作")
            self.btn_gen.clicked.connect(self.on_generate)
            self.btn_stop = QtWidgets.QPushButton("停止")
            self.btn_stop.clicked.connect(self.on_stop)
            self.btn_stop.setEnabled(False)
            btn_pv = QtWidgets.QPushButton("预览 JSON")
            btn_pv.clicked.connect(self.on_preview)
            for b in (self.btn_gen, self.btn_stop, btn_pv):
                row.addWidget(b)
            row.addStretch(1)
            return box

        def _build_log(self):
            box = QtWidgets.QGroupBox("进度")
            lay = QtWidgets.QVBoxLayout(box)
            self.progress = QtWidgets.QProgressBar()
            self.progress.setRange(0, 0)
            self.progress.setVisible(False)
            lay.addWidget(self.progress)
            self.log_view = QtWidgets.QPlainTextEdit()
            self.log_view.setReadOnly(True)
            self.log_view.setMaximumHeight(96)
            lay.addWidget(self.log_view)
            return box

        # ---- 定时刷新 ----
        def _start_timer(self):
            self._timer = QtCore.QTimer(self)
            self._timer.setInterval(REFRESH_MS)
            self._timer.timeout.connect(self.refresh)
            self._timer.start()

        def refresh(self):
            """绝不抛异常——面板刷新把 Houdini 搞崩是最糟的失败方式。"""
            try:
                self._drain_queue()
                self._refresh_nodes()
                self._refresh_fps()
                self._refresh_timeline()
                self._refresh_con_list()
                self._refresh_status()
            except Exception:
                pass

        def _refresh_nodes(self):
            # ★ 要保住**用户当前选的**那个：读下拉框自己的值，
            #   不能读 self.node_path（那样每次刷新都会把选择清回 None，
            #   用户永远选不中节点——这个 bug 是测试抓出来的）。
            want = self.node_combo.currentData() or self.node_path
            self.node_combo.blockSignals(True)
            self.node_combo.clear()
            self.node_combo.addItem("（还没有动作节点）", None)
            for c in motion_rig.find_motion_containers():
                self.node_combo.addItem(c, c)
            if want:
                i = self.node_combo.findData(want)
                if i >= 0:
                    self.node_combo.setCurrentIndex(i)
            self.node_combo.blockSignals(False)
            self.node_path = self.node_combo.currentData()
            # 选中变了要把会话换过来（切节点 = 切工程）
            if self.node_path != getattr(self, "_loaded_from", None):
                self._loaded_from = self.node_path
                self._load()
                self._refresh_segments()

        def _refresh_fps(self):
            scene_fps = hou.fps()
            self.fps_label.setText("场景 %.0f fps ｜ 素材 30 fps ｜ %s"
                                   % (scene_fps, MS.nice_duration(self.session.total_seconds())))

        def _refresh_timeline(self):
            n = max(1, self.session.total_frames())
            self.timeline.redraw(n, self.session.segment_frame_ranges(),
                                 self.session.active_constraints(), self._playhead_asset())

        def _refresh_con_list(self):
            cur = self.con_list.currentRow()
            self.con_list.clear()
            for c in self.session.constraints:
                mark = "☑" if c.enabled else "☐"
                extra = ""
                if c.is_root2d:
                    extra = "（%d 个路点）" % len(c.points or [])
                elif c.joint_positions is None:
                    extra = "（无姿势数据）"
                self.con_list.addItem("%s %s 素材帧 %s %s"
                                      % (mark, MS.KIND_LABELS.get(c.kind, c.kind),
                                         c.frame_list(), extra))
            if 0 <= cur < self.con_list.count():
                self.con_list.setCurrentRow(cur)

        def _refresh_status(self):
            self.status_label.setText(self.status_text)
            self.btn_gen.setEnabled(not self._busy)
            self.btn_stop.setEnabled(self._busy)
            self.progress.setVisible(self._busy)
            self.seg_frames_label.setText("= %d 帧" % int(round(self.seg_secs.value() * 30)))

        def _refresh_segments(self):
            cur = self.seg_list.currentRow()
            self.seg_list.blockSignals(True)
            self.seg_list.clear()
            for i, s in enumerate(self.session.segments):
                mark = "☑" if s.enabled else "☐"
                self.seg_list.addItem("%s %d. %s  [%s]"
                                      % (mark, i + 1, s.text.strip()[:22] or "（空）",
                                         MS.nice_duration(s.seconds)))
            self.seg_list.blockSignals(False)
            if 0 <= cur < self.seg_list.count():
                self.seg_list.setCurrentRow(cur)
            self._sync_seg_editor()

        def _sync_seg_editor(self):
            i = self.seg_list.currentRow()
            ok = 0 <= i < len(self.session.segments)
            for w in (self.seg_text, self.seg_secs):
                w.setEnabled(ok)
            if ok:
                s = self.session.segments[i]
                self.seg_text.blockSignals(True)
                self.seg_text.setPlainText(s.text)
                self.seg_text.blockSignals(False)
                self.seg_secs.blockSignals(True)
                self.seg_secs.setValue(s.seconds)
                self.seg_secs.blockSignals(False)

        # ---- 供时间轴回调 ----
        def segment_label(self, i):
            segs = self.session.active_segments
            return (segs[i].text.strip()[:12] if i < len(segs) and segs[i].text.strip() else "")

        def _playhead_asset(self):
            try:
                return MS.scene_to_asset(hou.frame(), self.first_frame(), hou.fps(), 30.0)
            except Exception:
                return 0

        def first_frame(self):
            c = self._container()
            if c is None:
                return 1
            n = c.node("motion") if hasattr(c, "node") else None
            if n is not None and n.parm("first_frame") is not None:
                try:
                    return int(n.parm("first_frame").eval())
                except Exception:
                    return 1
            return 1

        def _container(self):
            if not self.node_path:
                return None
            return hou.node(self.node_path)

        # ---- 日志 ----
        def log(self, msg):
            self.log_lines.append(str(msg))
            if len(self.log_lines) > 300:
                del self.log_lines[:150]
            self.log_view.setPlainText("\n".join(self.log_lines[-80:]))
            self.log_view.verticalScrollBar().setValue(
                self.log_view.verticalScrollBar().maximum())

        def set_status(self, text):
            self.status_text = text
            try:
                hou.ui.setStatusMessage("动作生成：%s" % text)
            except Exception:
                pass

        # =================================================================
        # 交互
        # =================================================================
        def _on_mode_changed(self, _i):
            self.refresh()

        def _on_seg_selected(self, _row):
            self._sync_seg_editor()

        def _on_seg_text_changed(self):
            i = self.seg_list.currentRow()
            if 0 <= i < len(self.session.segments):
                self.session.segments[i].text = self.seg_text.toPlainText()
                self._save()
                self._refresh_segments()

        def _on_seg_secs_changed(self, v):
            i = self.seg_list.currentRow()
            if 0 <= i < len(self.session.segments):
                self.session.segments[i].seconds = float(v)
                self._save()
                self._refresh_segments()

        def on_add_segment(self):
            self.session.add_segment("", 3.0)
            self._save()
            self._refresh_segments()
            self.seg_list.setCurrentRow(len(self.session.segments) - 1)

        def on_dup_segment(self):
            i = self.seg_list.currentRow()
            if 0 <= i < len(self.session.segments):
                s = self.session.segments[i]
                self.session.segments.insert(i + 1, MS.Segment(s.text, s.seconds, s.seed))
                self._save()
                self._refresh_segments()

        def on_del_segment(self):
            i = self.seg_list.currentRow()
            if 0 <= i < len(self.session.segments):
                self.session.remove_segment(i)
                self._save()
                self._refresh_segments()

        def on_seg_up(self):
            i = self.seg_list.currentRow()
            if self.session.move_segment(i, -1):
                self._save()
                self._refresh_segments()
                self.seg_list.setCurrentRow(i - 1)

        def on_seg_down(self):
            i = self.seg_list.currentRow()
            if self.session.move_segment(i, 1):
                self._save()
                self._refresh_segments()
                self.seg_list.setCurrentRow(i + 1)

        def on_timeline_clicked(self, asset_frame):
            try:
                hou.setFrame(MS.asset_to_scene(asset_frame, self.first_frame(), hou.fps(), 30.0))
            except Exception:
                pass
            self._refresh_timeline()

        def on_diamond_picked(self, ci, frame):
            self.con_list.setCurrentRow(ci)

        def on_diamond_moved(self, ci, old_frame, new_frame):
            if not (0 <= ci < len(self.session.constraints)):
                return
            c = self.session.constraints[ci]
            if c.is_root2d:
                return
            if new_frame != old_frame:
                c.frame = int(new_frame)
                self._save()
                self._refresh_con_list()
                self._refresh_timeline()

        def on_first(self):
            self.on_timeline_clicked(0)

        def on_last(self):
            self.on_timeline_clicked(max(0, self.session.total_frames() - 1))

        def on_prev(self):
            self.on_timeline_clicked(max(0, self._playhead_asset() - 1))

        def on_next(self):
            self.on_timeline_clicked(min(self.session.total_frames() - 1, self._playhead_asset() + 1))

        def on_set_fps_30(self):
            try:
                hou.fps(30.0)
                self.set_status("场景帧率已设为 30fps")
            except Exception as exc:
                self.set_status("改帧率失败：%s" % exc)
            self.refresh()

        def on_new_node(self):
            try:
                c = motion_rig.create_motion_container()
                self.node_path = c.path()
                self.set_status("已建动作节点 %s" % c.name())
                self.log("建了动作节点：%s" % c.path())
            except Exception as exc:
                self.set_status("建节点失败：%s" % exc)
            self.refresh()

        # ---- 约束 ----
        def _read_pose_at(self, asset_frame):
            """从 Rig Pose 处读某个素材帧的姿势（世界位置 + Kimodo 全局旋转）。"""
            c = self._container()
            if c is None:
                raise motion_rig.RigError("先选一个目标节点（或点「新建动作节点」）")
            rig = c.node("pose_rig")
            if rig is None:
                raise motion_rig.RigError("容器里没有 pose_rig 节点")
            skel = c.node("rest_skeleton")
            if skel is not None and skel.errors():
                raise motion_rig.RigError("rest 骨架节点有报错：%s" % skel.errors()[0][:200])
            scene_f = MS.asset_to_scene(asset_frame, self.first_frame(), hou.fps(), 30.0)
            hou.setFrame(scene_f)
            rig.cook(force=True)
            if rig.errors():
                raise motion_rig.RigError("pose_rig 有报错：%s" % rig.errors()[0][:200])
            return motion_rig.read_pose(rig.geometry())

        def on_add_constraint(self, kind):
            try:
                asset_f = self._playhead_asset()
                if not self.session.segments:
                    self.session.add_segment("", 3.0)
                if kind == "root2d":
                    pts = self._read_root_path_from_input()
                    if pts is None:
                        self.set_status("根路径：请把一条曲线接到动作节点的输入 0")
                        return
                    c = MS.Constraint("root2d", asset_f, points=pts["points"], frames=pts["frames"])
                else:
                    pos, rot = self._read_pose_at(asset_f)
                    c = MS.Constraint(kind, asset_f, joint_positions=pos, joint_rots=rot)
                self.session.add_constraint(c)
                self._save()
                self._refresh_con_list()
                self._refresh_timeline()
                self.set_status("已加%s @ 素材帧 %d（共 %d 条约束）"
                                % (MS.KIND_LABELS.get(kind, kind), asset_f,
                                   len(self.session.constraints)))
            except Exception as exc:
                self.set_status("加约束失败：%s" % exc)
                self.log("加约束失败：%r" % (exc,))

        def _read_root_path_from_input(self):
            c = self._container()
            if c is None:
                return None
            node = c.node("motion")
            if node is None or node.input(0) is None:
                return None
            geo = node.input(0).geometry()
            pts = list(geo.points())
            if not pts:
                return None
            out, frames = [], []
            has_frame_attr = geo.findPointAttrib("frame") is not None
            for i, p in enumerate(pts):
                q = p.position()
                out.append([float(q[0]), float(q[2])])       # Houdini 的 XZ 就是 Kimodo 的地面
                frames.append(int(p.attribValue("frame")) if has_frame_attr else i)
            if not has_frame_attr:
                # 没有 frame 属性 = 稠密路径，均匀铺开
                n = max(1, self.session.total_frames())
                frames = [int(round(i * (n - 1) / max(1, len(out) - 1))) for i in range(len(out))]
            return {"points": out, "frames": frames}

        def on_goto_constraint(self):
            i = self.con_list.currentRow()
            if 0 <= i < len(self.session.constraints):
                f = min(self.session.constraints[i].frame_list())
                self.on_timeline_clicked(f)

        def on_del_constraint(self):
            i = self.con_list.currentRow()
            if 0 <= i < len(self.session.constraints):
                self.session.remove_constraint(i)
                self._save()
                self._refresh_con_list()
                self._refresh_timeline()

        def on_clear_constraints(self):
            n = self.session.clear_constraints()
            self._save()
            self._refresh_con_list()
            self._refresh_timeline()
            self.set_status("清掉了 %d 条约束" % n)

        def on_canonical(self):
            dx, dz = self.session.apply_canonical()
            self._save()
            self._refresh_con_list()
            if dx == 0.0 and dz == 0.0:
                self.set_status("已经是规范原点了（或没有根路径可参照）")
            else:
                self.set_status("已整体平移 (%.3f, %.3f)，让最早的根路点回到原点" % (dx, dz))

        def on_preview(self):
            self._refresh_nodes()
            c = self._container()
            if c is None:
                self.set_status("先选一个目标节点")
                return
            try:
                def reader(af):
                    return self._read_pose_at(af)
                payload = motion_api.build_payload(self.session, reader, "<预览>")
                txt = _json.dumps(payload, ensure_ascii=False, indent=1)
                self.log("--- 将要发出去的 payload ---")
                self.log(txt[:3000] + ("…（截断）" if len(txt) > 3000 else ""))
                problems = self.session.validate()
                if problems:
                    self.log("--- 校验发现问题 ---")
                    for p in problems:
                        self.log("  ⚠ " + p)
                self.set_status("payload 已打到日志里（%d 条约束）" % len(payload["constraints"]))
            except Exception as exc:
                self.set_status("预览失败：%s" % exc)
                self.log("预览失败：%r" % (exc,))

        # ---- 生成 ----
        def _save(self):
            c = self._container()
            if c is None:
                return
            try:
                c.setUserData("motion_session", _json.dumps(self.session.to_dict(),
                                                            ensure_ascii=False))
                c.setComment(self.session.summary())
                c.setGenericFlag(hou.nodeFlag.DisplayComment, True)
            except Exception:
                pass

        def _load(self):
            c = self._container()
            if c is None:
                return
            try:
                raw = c.userData("motion_session")
                if raw:
                    self.session = MS.MotionSession.from_dict(_json.loads(raw))
            except Exception:
                pass

        def on_generate(self):
            self._refresh_nodes()
            c = self._container()
            if c is None:
                self.set_status("先选一个目标节点（或点「新建动作节点」）")
                return
            problems = self.session.validate()
            if problems:
                self.set_status("先修这些问题：" + problems[0])
                for p in problems:
                    self.log("⚠ " + p)
                return
            ok, why = motion_api.check_backend()
            if not ok:
                self.set_status(why)
                self.log(why)
                return

            out = os.path.join("D:/kimodo-motion/output",
                               "%s.npz" % _safe_name(c.name()))
            self._busy = True
            self._cancel = _threading.Event()
            self.progress.setVisible(True)
            self._refresh_status()
            self.set_status("准备中…")
            self.log("开始生成 → %s" % out)

            def worker():
                try:
                    payload = motion_api.build_payload(
                        self.session, self._read_pose_at_main, out)
                    res = motion_api.run_generation(
                        payload, on_event=self._on_event, cancel_event=self._cancel)
                    self._post(lambda: self._finish_ok(c, res))
                except Exception as exc:
                    self._post(lambda e=exc: self._finish_err(e))

            self._read_pose_at_main = lambda af: self._pose_via_main(af)
            t = _threading.Thread(target=worker)
            t.daemon = True
            t.start()

        def _pose_via_main(self, asset_frame):
            """工作线程要读场景 → 必须回主线程。用已有的 in_main 封装。"""
            return _runner.in_main(lambda: self._read_pose_at(asset_frame))

        def _post(self, fn):
            self._ui_queue.append(fn)

        def _drain_queue(self):
            while self._ui_queue:
                fn = self._ui_queue.pop(0)
                try:
                    fn()
                except Exception as exc:
                    self.log("回调出错：%r" % (exc,))

        def _on_event(self, ev):
            name = ev.get("name") or ev.get("event")
            if name:
                self._post(lambda n=name: (self.set_status(n), self.log(n)))

        def _finish_ok(self, container, res):
            self._busy = False
            self._cancel = None
            try:
                frames, a, b = motion_rig.apply_npz(container, res["npz"])
                self.set_status("完成：%d 关节 / %d 帧，视口帧范围 %d-%d"
                                % (res.get("joints", 0), frames, a, b))
                self.log("完成 → %s（%d 帧，%s 秒）"
                         % (res["npz"], frames, res.get("seconds")))
            except Exception as exc:
                self.set_status("落地失败：%s" % exc)
                self.log("落地失败：%r" % (exc,))
            self._refresh_status()

        def _finish_err(self, exc):
            self._busy = False
            self._cancel = None
            self.set_status("失败：%s" % str(exc)[:160])
            self.log("失败：%s" % str(exc)[:1500])
            self._refresh_status()

        def on_stop(self):
            if self._cancel is not None:
                self._cancel.set()
                self.set_status("已请求停止…")

    _WIDGET_CLASS = MotionPanelWidget
    return _WIDGET_CLASS


# 顶层一次性 import（避免在类体里反复 import）
import json as _json              # noqa: E402
import threading as _threading    # noqa: E402
from . import runner as _runner   # noqa: E402


def _safe_name(name):
    keep = "".join(ch if (ch.isalnum() or ch in "_-") else "_" for ch in str(name))
    return keep or "motion"


def panel_widget_class():
    return _widget_class()


# ===========================================================================
# 把面板开出来
# ===========================================================================
PANEL_INTERFACE = "HoudiniMotionGenerate"

_floating = []


def find_open_tab():
    """已经开着的动作面板标签（没有就返回 None）。"""
    import hou

    try:
        desktop = hou.ui.curDesktop()
    except Exception:
        return None
    for tab in desktop.paneTabs():
        try:
            if tab.type() != hou.paneTabType.PythonPanel:
                continue
            iface = tab.activeInterface()
            if iface is not None and iface.name() == PANEL_INTERFACE:
                return tab
        except Exception:
            continue
    return None


def open_pane_tab():
    """把动作面板作为一个 **PaneTab** 开出来。

    优先顺序：
      1. 已经开着 → 聚焦它（不重复开）
      2. **在当前 pane 里新开一个标签**（这样它就和视口并排，能拖去停靠）
      3. 兜底：浮动的 pane tab
      4. 再兜底：普通浮动窗口

    第 2 步是关键——用户要的就是「能跟视口并排摆的 pane tab」，
    而不是一个飘着的窗口。
    """
    import hou

    tab = find_open_tab()
    if tab is not None:
        try:
            tab.setIsCurrentTab()
        except Exception:
            pass
        return tab

    # 无头环境（hython）里 hou.ui 根本不存在，所以这几步都要包住
    try:
        desktop = hou.ui.curDesktop()
    except Exception:
        desktop = None

    if desktop is not None:
        # 2) 在当前 pane 里开一个新标签（这样它能跟视口并排、能拖去停靠）
        try:
            pane = None
            for t in desktop.currentPaneTabs():
                pane = t.pane()
                break
            if pane is None:
                panes = desktop.panes()
                pane = panes[0] if panes else None
            if pane is not None:
                return pane.createTab(hou.paneTabType.PythonPanel, PANEL_INTERFACE)
        except Exception:
            pass

        # 3) 浮动的 pane tab
        try:
            return desktop.createFloatingPaneTab(hou.paneTabType.PythonPanel, PANEL_INTERFACE)
        except Exception:
            pass

    # 4) 普通浮动窗口
    return show_floating()


def show_floating():
    """兜底：弹一个独立窗口（功能和停靠面板一样，只是不能停靠）。"""
    import hou

    if not hasattr(hou, "qt"):
        # 无头环境（hython）里根本没有主窗口。这里抛一个**说人话**的错，
        # 别让上层看到一个莫名其妙的 AttributeError。
        raise RuntimeError(
            "当前环境没有图形界面（hou.qt 不可用），开不了面板。"
            "这套面板只能在带界面的 Houdini 里用。")

    for window in _floating:
        try:
            window.show()
            window.raise_()
            return window
        except Exception:
            _floating.remove(window)

    QtCore, QtGui, QtWidgets = _qt()
    window = QtWidgets.QMainWindow(hou.qt.mainWindow())
    window.setWindowTitle("动作生成")
    window.setCentralWidget(panel_widget_class()())
    window.resize(980, 760)
    window.show()
    _floating.append(window)
    return window


def make_panel_class():
    """给 .pypanel 用的壳类（Houdini 按这个名字找）。"""
    Widget = _widget_class()

    class HoudiniMotionPanel(object):
        def __init__(self):
            self.widget = None

        def name(self):
            return "MotionGenerate"

        def buildInterface(self):
            import hou
            self.widget = Widget(parent=None)
            return self.widget

    return HoudiniMotionPanel
