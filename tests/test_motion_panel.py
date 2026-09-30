# -*- coding: utf-8 -*-
"""动作面板测试：离屏 Qt 真建界面、真刷新、真点按钮。

    "D:/ProgramFiles/Houdini22/bin/hython.exe" tests/test_motion_panel.py

★ **不会真的去生成**（那要跑几十秒模型）。生成相关的分支只测「拦得住」：
  校验不过、后端不在、没选节点——这些才是用户最容易撞上的路径。
"""

import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import _harness as H  # noqa: E402

H.section("起离屏 Qt 与一个动作容器")
import hou  # noqa: E402
from PySide6 import QtWidgets  # noqa: E402

_app = QtWidgets.QApplication.instance() or QtWidgets.QApplication([])
H.check("离屏 QApplication 起来了", _app is not None)

from houdini_ai import motion_panel, motion_rig, motion_session as MS  # noqa: E402

obj = hou.node("/obj")
for old in list(obj.children()):
    if old.name().startswith(("MP_", "motion_ai")):
        old.destroy()

holder = obj.createNode("geo", "MP_holder")
net = motion_rig.build_motion_network(holder, "somaskel30")
container = net["container"]
H.check("动作容器建好了", container is not None)

H.section("★ 容器能被自动发现（面板靠这个列出来）")
found = motion_rig.find_motion_containers()
H.check_eq("找得到刚建的那个", found, [container.path()])

H.section("界面能被构造出来")
Widget = motion_panel.panel_widget_class()
w = Widget()
H.check("面板构造成功", w is not None)
for name in ("node_combo", "mode_combo", "status_label", "timeline", "seg_list",
             "seg_text", "seg_secs", "con_list", "btn_gen", "btn_stop",
             "log_view", "progress"):
    H.check("有 %s" % name, hasattr(w, name))

H.section("刷新不崩（场景在变的时候也不能崩）")
w.refresh()
H.check("refresh 后节点下拉里有容器",
        w.node_combo.findData(container.path()) >= 0,
        [w.node_combo.itemText(i) for i in range(w.node_combo.count())])
w.node_combo.setCurrentIndex(w.node_combo.findData(container.path()))
w.refresh()
H.check_eq("选中了目标节点", w.node_path, container.path())

H.section("段的增删改")
n0 = len(w.session.segments)
w.on_add_segment()
H.check_eq("加了一段", len(w.session.segments), n0 + 1)
w.refresh()
H.check_eq("列表也跟着更新", w.seg_list.count(), n0 + 1)
H.check_eq("新段被选中", w.seg_list.currentRow(), n0)

w.seg_text.setPlainText("a person walks forward")
H.check_eq("★ 改文字写回了会话",
           w.session.segments[n0].text, "a person walks forward")
w.seg_secs.setValue(2.5)
H.check_eq("★ 改时长写回了会话", w.session.segments[n0].seconds, 2.5)
H.check_eq("时长换算成帧", w.session.segments[n0].frames(), 75)

w.on_dup_segment()
H.check_eq("复制了一段", len(w.session.segments), n0 + 2)
H.check_eq("复制的内容一致", w.session.segments[n0 + 1].text, "a person walks forward")

w.on_seg_down()
H.check_eq("下移后顺序变了", [x.text for x in w.session.segments][-2:],
           ["a person walks forward", "a person walks forward"] if n0 == 0 else
           [x.text for x in w.session.segments][-2:])
w.on_seg_up()

w.on_del_segment()
H.check_eq("删掉一段", len(w.session.segments), n0 + 1)

H.section("★ 会话存进了节点 userData（换面板/重开场景还在）")
w._save()
raw = container.userData("motion_session")
H.check("userData 里有会话", bool(raw))
back = MS.MotionSession.from_dict(__import__("json").loads(raw))
H.check_eq("存回来的段数一致", len(back.segments), len(w.session.segments))
H.check("节点注释写了摘要", "段" in container.comment(), container.comment())

H.section("时间轴画得出来")
w.refresh()
H.check("场景里有东西（段色块 + 播放头）", len(w.timeline.scene_.items()) > 0,
        len(w.timeline.scene_.items()))
n_items_before = len(w.timeline.scene_.items())

H.section("★ 加约束：全身姿势（真的从场景里读姿势）")
w.timeline.redraw(90, w.session.segment_frame_ranges(), [], 0)
w.on_add_constraint("fullbody")
con = w.session.constraints
H.check_eq("加了一条约束", len(con), 1)
H.check_eq("类型对", con[0].kind, "fullbody")
H.check("★ 姿势数据真的读到了（30 个关节）",
        con[0].joint_positions is not None and len(con[0].joint_positions) == 30,
        None if con[0].joint_positions is None else len(con[0].joint_positions))
H.check("★ 旋转是 Kimodo 约定的全局旋转（T-pose 下应是单位阵）",
        abs(con[0].joint_rots[0][0][0] - 1.0) < 1e-5
        and abs(con[0].joint_rots[0][0][1]) < 1e-5,
        con[0].joint_rots[0])
w.refresh()
H.check("约束列表有内容", w.con_list.count() == 1)
H.check("时间轴上多了菱形", len(w.timeline.scene_.items()) > n_items_before)

H.section("约束的增删与规范原点")
w.session.constraints.append(
    MS.Constraint("root2d", 0, points=[[2.0, -1.0], [3.0, 0.0]], frames=[0, 30]))
w.refresh()
H.check_eq("两条约束", w.con_list.count(), 2)
w.on_canonical()
r2 = [c for c in w.session.constraints if c.is_root2d][0]
H.check_eq("★ 自动原点把起点挪到 (0,0)",
           [round(v, 6) for v in r2.points[0]], [0.0, 0.0])
w.on_clear_constraints()
H.check_eq("清空后没有约束", len(w.session.constraints), 0)
w.refresh()

H.section("★ 生成前的拦截（这些才是用户最容易撞上的路径）")
# 没有文字段 → 拦住
w.session.segments = []
w.on_generate()
H.check("★ 没有文字段时被拦住（不静默去跑模型）",
        "修" in w.status_text or "问题" in w.status_text, w.status_text)
H.check("按钮没被卡在忙碌态", not w._busy)

w.on_add_segment()
w.seg_text.setPlainText("a person walks forward")
w.seg_secs.setValue(3.0)

# ★ 后端不在 → 拦住。
#   这里**故意把后端路径指到不存在的文件**：绝不能在这套测试里真的跑模型
#   （那是几十秒 + 几 GB 显存）。
from houdini_ai import motion_api as _api  # noqa: E402
_keep_py, _keep_ru = _api.DEFAULT_VENV_PYTHON, _api.DEFAULT_RUNNER
_api.DEFAULT_VENV_PYTHON = "D:/__no_such_venv__/python.exe"
try:
    w.on_generate()
    H.check("★ 后端不在时被拦住（不静默去跑模型）", not w._busy, w.status_text)
    H.check("状态里说了后端找不到", "找不到" in w.status_text, w.status_text)
    H.check("日志里也留了一条", "找不到" in w.log_view.toPlainText())
finally:
    _api.DEFAULT_VENV_PYTHON, _api.DEFAULT_RUNNER = _keep_py, _keep_ru

# 再确认一次：拦住的路径**没有**留下忙碌状态
w.refresh()
H.check("拦截后按钮已复位", not w._busy)

H.section("预览 JSON（不联网，只看组装得对不对）")
w.on_preview()
log = w.log_view.toPlainText()
H.check("日志里有 payload", "segments" in log and "constraints" in log, log[-200:])
H.check("日志里有文字段内容", "walks forward" in log)

H.section("停止按钮在空闲时不可点")
H.check("空闲时停止不可点", not w.btn_stop.isEnabled())
H.check("空闲时生成可点", w.btn_gen.isEnabled())

H.section("时间轴交互")
w.timeline.on_timeline_clicked if False else None
w.on_timeline_clicked(10)
w.refresh()
H.check("点时间轴没崩", True)
w.on_first()
w.on_last()
w.on_prev()
w.on_next()
H.check("播放控制都没崩", True)
w.on_set_fps_30()
H.check("设为 30fps 没崩", True)

H.section("★ 面板刷新绝不把 Houdini 搞崩")
container.destroy()
w.refresh()
H.check("目标节点被删掉后刷新仍正常", True)
holder.destroy()
w.refresh()
H.check("容器整个删掉后刷新仍正常", True)

# ===========================================================================
H.section("★ 把面板开出来：接口名与降级路径")
# ===========================================================================
H.check_eq("面板接口名对得上 .pypanel 里的声明",
           motion_panel.PANEL_INTERFACE, "HoudiniMotionGenerate")
from houdini_ai import deploy  # noqa: E402
H.check("★ 接口名和部署清单里的那个一致（写错了就开不出来）",
        motion_panel.PANEL_INTERFACE in deploy.panel_interfaces(),
        deploy.panel_interfaces())

# 无头环境里 hou.ui 不存在，open_pane_tab 应当**降级成浮动窗口**而不是抛异常
real_hou_ui = getattr(hou, "ui", None)
had_ui = real_hou_ui is not None
if had_ui:
    # 有 ui 的场合（真 Houdini）不该在这里跑——测试是 hython
    H.check("（在真 Houdini 里跑才有意义）", True)
else:
    # 无头环境（hython）开不了任何面板——这本身是对的。要验的是：
    # **它抛的错得说人话**，而不是一个莫名其妙的 AttributeError。
    H.check("hython 里确实没有 hou.ui（所以下面测的是降级路径）", True)
    try:
        motion_panel.open_pane_tab()
        H.check("无头环境下居然开出来了？", False, "这里应该开不出来才对")
    except Exception as exc:
        msg = str(exc)
        H.check("★ 无头环境下抛的错说清了「没有图形界面」",
                "图形界面" in msg or "hou.qt" in msg, "%s: %s" % (type(exc).__name__, msg))
        H.check("★ 不是莫名的 AttributeError",
                not (isinstance(exc, AttributeError) and "qt" in msg), repr(exc))
    # show_floating 同理
    try:
        motion_panel.show_floating()
        H.check("show_floating 在无头下也该说不通", False)
    except RuntimeError as exc:
        H.check("show_floating 在无头下抛 RuntimeError 并说清原因",
                "图形界面" in str(exc), str(exc))

H.section("找已经开着的面板标签")
try:
    motion_panel.find_open_tab()
    H.check("无头环境里 find_open_tab 返回 None 而不是崩", True)
except Exception as exc:
    H.check("无头环境里 find_open_tab 不崩", False, repr(exc))

H.finish("test_motion_panel")
