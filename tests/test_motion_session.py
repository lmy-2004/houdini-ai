# -*- coding: utf-8 -*-
"""动作会话的数据模型测试（纯逻辑，不需要 hou 除了脚手架）。

重点在两条：
  * **多段怎么拼**（prompt 用句号分隔 + 每段一个时长，帧号要连起来）
  * **规范化平移**（Kimodo 要求根在第 0 帧位于 XZ 原点；用户是随手画的路径）
"""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import _harness as H  # noqa: E402

from houdini_ai import motion_session as MS  # noqa: E402


# ===========================================================================
H.section("段：帧数与时长")
# ===========================================================================
s = MS.Segment("向前小心走", 3.0)
H.check_eq("3 秒 @30fps = 90 帧", s.frames(), 90)
H.check_eq("1 秒 = 30 帧", MS.Segment("x", 1.0).frames(), 30)
H.check_eq("0.5 秒 = 15 帧", MS.Segment("x", 0.5).frames(), 15)
H.check_eq("约等于：3.333 秒 → 100 帧", MS.Segment("x", 100 / 30.0).frames(), 100)

# ===========================================================================
H.section("多段：总帧数、段区间、prompt 拼接")
# ===========================================================================
m = MS.MotionSession()
m.add_segment("向前小心走", 3.0)
m.add_segment("然后向左看", 2.0)
m.add_segment("再停下", 1.0)
H.check_eq("三段总帧数 = 90+60+30", m.total_frames(), 180)
H.check_eq("总时长 6 秒", round(m.total_seconds(), 6), 6.0)
H.check_eq("★ 段区间要连起来不留缝", m.segment_frame_ranges(), [(0, 90), (90, 150), (150, 180)])
H.check_eq("★ prompt 用句号分隔（Kimodo 的多段格式）",
           m.prompt_text(), "向前小心走. 然后向左看. 再停下")
H.check_eq("★ 每段一个时长，与句子一一对应", m.durations(), [3.0, 2.0, 1.0])
H.check_eq("摘要", m.summary(), "3 段 · 共 6.0 秒 / 180 帧 · 0 条约束")

# 段的结尾句号不该产生空句
m2 = MS.MotionSession()
m2.add_segment("走。", 1.0)
m2.add_segment("停", 1.0)
H.check("段尾的中文句号不会串味", m2.prompt_text().count("。") <= 1, m2.prompt_text())

# 停用的段不参与
m.segments[1].enabled = False
H.check_eq("★ 停用的段不计入总帧数", m.total_frames(), 120)
H.check_eq("停用的段不出现在 prompt 里", m.prompt_text(), "向前小心走. 再停下")
H.check_eq("停用后段区间重算", m.segment_frame_ranges(), [(0, 90), (90, 120)])
m.segments[1].enabled = True

# 空白文本的段忽略
m3 = MS.MotionSession()
m3.add_segment("真的", 1.0)
m3.add_segment("   ", 5.0)
H.check_eq("全是空白的段不算数", m3.total_frames(), 30)
H.check_eq("空白段不进 prompt", m3.prompt_text(), "真的")

# ===========================================================================
H.section("段的增删与排序")
# ===========================================================================
m4 = MS.MotionSession()
m4.add_segment("A", 1.0)
m4.add_segment("B", 1.0)
m4.add_segment("C", 1.0)
H.check("下移一段", m4.move_segment(0, 1))
H.check_eq("顺序变了", [s.text for s in m4.segments], ["B", "A", "C"])
H.check("上移回来", m4.move_segment(1, -1))
H.check_eq("顺序恢复", [s.text for s in m4.segments], ["A", "B", "C"])
H.check("越界的移动返回 False 而不是崩", not m4.move_segment(0, -1))
H.check("末尾再下移也返回 False", not m4.move_segment(2, 1))
removed = m4.remove_segment(1)
H.check_eq("删掉的是 B", removed.text, "B")
H.check_eq("剩两段", len(m4.segments), 2)
H.check_raises("删越界的段要报错", MS.SessionError, m4.remove_segment, 99)

# ===========================================================================
H.section("约束：类型、帧号、启停")
# ===========================================================================
H.check_raises("不认识的类型要报错", MS.SessionError, MS.Constraint, "nonsense", 0)

cs = MS.MotionSession()
c_pose = MS.Constraint("fullbody", 45, joint_positions=[[0, 1, 0]] * 30,
                       joint_rots=[[[1, 0, 0], [0, 1, 0], [0, 0, 1]]] * 30, skel_name="somaskel30")
c_path = MS.Constraint("root2d", 0, points=[[1.0, 2.0], [3.0, 4.0]], frames=[0, 90])
cs.add_constraint(c_path)
cs.add_constraint(c_pose)
H.check_eq("按帧号排序（0 在前）", [c.kind for c in cs.constraints], ["root2d", "fullbody"])
H.check_eq("root2d 的帧列表", c_path.frame_list(), [0, 90])
H.check_eq("姿势约束的帧列表只有一个", c_pose.frame_list(), [45])
H.check("root2d 判定", c_path.is_root2d and not c_pose.is_root2d)

cs.constraints[1].enabled = False
H.check_eq("停用的约束不算数", len(cs.active_constraints()), 1)
H.check_eq("按类型分组只含启用的", {k: len(v) for k, v in cs.constraints_by_kind().items()},
           {"root2d": 1})
cs.constraints[1].enabled = True
H.check_eq("清掉某类型", cs.clear_constraints("root2d"), 1)
H.check_eq("剩下全身约束", [c.kind for c in cs.constraints], ["fullbody"])

# ===========================================================================
H.section("★ 规范化：把最早的根路点挪到 XZ 原点")
# ===========================================================================
can = MS.MotionSession()
# 用户随手画的路径：从 (2.5, -1.2) 出发
can.add_constraint(MS.Constraint("root2d", 0, points=[[2.5, -1.2], [4.0, -0.5], [6.0, 0.3]],
                                 frames=[0, 45, 90]))
dx, dz = can.canonical_offset()
H.check_eq("★ 算出的平移量正好抵消起点", (round(dx, 6), round(dz, 6)), (-2.5, 1.2))
applied = can.apply_canonical()
H.check_eq("应用后返回同样的量", (round(applied[0], 6), round(applied[1], 6)), (-2.5, 1.2))
H.check_eq("★ 起点落到原点", [round(v, 6) for v in can.constraints[0].points[0]], [0.0, 0.0])
H.check_eq("其余路点整体平移（形状不变）",
           [[round(v, 6) for v in p] for p in can.constraints[0].points],
           [[0.0, 0.0], [1.5, 0.7], [3.5, 1.5]])
dx2, dz2 = can.canonical_offset()
H.check_eq("再算一次已经是 0（幂等）", (dx2, dz2), (0.0, 0.0))

# 多个 root2d 时按**帧号最早**的那个定基准
can2 = MS.MotionSession()
can2.add_constraint(MS.Constraint("root2d", 100, points=[[9.0, 9.0]], frames=[100]))
can2.add_constraint(MS.Constraint("root2d", 0, points=[[5.0, 5.0]], frames=[0]))
H.check_eq("★ 基准取帧号最早的，不是列表里第一个",
           tuple(round(v, 6) for v in can2.canonical_offset()), (-5.0, -5.0))

# 姿势类约束的位置也要跟着平移（否则人和路径会脱开）
can3 = MS.MotionSession()
can3.add_constraint(MS.Constraint("root2d", 0, points=[[1.0, 0.0]], frames=[0]))
can3.add_constraint(MS.Constraint("fullbody", 30,
                                  joint_positions=[[3.0, 1.0, 4.0]], skel_name="somaskel30"))
can3.apply_canonical()
H.check_eq("★ 姿势约束的位置也整体平移（人跟路径不脱开）",
           [round(v, 6) for v in can3.constraints[1].joint_positions[0]], [2.0, 1.0, 4.0])

# 没有根路点时不动
can4 = MS.MotionSession()
can4.add_constraint(MS.Constraint("fullbody", 0, joint_positions=[[1.0, 1.0, 1.0]]))
H.check_eq("没有根路点时平移量是 0", can4.canonical_offset(), (0.0, 0.0))

# ===========================================================================
H.section("校验：问题要说人话")
# ===========================================================================
v = MS.MotionSession()
problems = v.validate()
H.check("没有段时要报", any("至少要一段" in p for p in problems), problems)

v.add_segment("走", 3.0)
H.check_eq("一段文字、没有约束 → 没问题", v.validate(), [])

v.segments[0].seconds = 0.0
H.check("时长为 0 要报", any("大于 0" in p for p in v.validate()), v.validate())
v.segments[0].seconds = -1.0
H.check("负时长要报", any("大于 0" in p for p in v.validate()))
v.segments[0].seconds = 15.0
H.check("★ 超过官方 10 秒上限要报", any("上限" in p for p in v.validate()), v.validate())
v.segments[0].seconds = 3.0

v.add_constraint(MS.Constraint("fullbody", 500, joint_positions=[[0, 0, 0]]))
H.check("★ 约束帧号超出总长要报", any("总长只有" in p for p in v.validate()), v.validate())
v.clear_constraints()

v.add_constraint(MS.Constraint("fullbody", 10))
H.check("★ 姿势约束没数据要报", any("没有姿势数据" in p for p in v.validate()), v.validate())
v.clear_constraints()

# 每种约束的帧数上限（官方：少于 20）
many = MS.Constraint("root2d", 0, points=[[0, 0]] * 25, frames=list(range(25)))
v.add_constraint(many)
H.check("★ 单种约束超过 20 帧要提醒", any("少于 20" in p for p in v.validate()), v.validate())

# 负数帧号
v.clear_constraints()
v.add_constraint(MS.Constraint("root2d", 0, points=[[0, 0]], frames=[-5]))
H.check("负帧号要报", any("负数" in p for p in v.validate()), v.validate())

# ===========================================================================
H.section("存取：存进 node userData 再读回来")
# ===========================================================================
orig = MS.MotionSession(seed=42, diffusion_steps=50, cfg_weight=1.5,
                        transition_frames=8, postprocess=False,
                        first_heading_angle=1.2, notes="测试")
orig.add_segment("走", 3.0, seed=7)
orig.add_segment("停", 1.0)
orig.add_constraint(MS.Constraint("root2d", 0, points=[[0.0, 0.0], [1.0, 1.0]], frames=[0, 30]))
orig.add_constraint(MS.Constraint("left-foot", 15, joint_positions=[[1, 2, 3]],
                                   skel_name="somaskel30", joint_names=["LeftFoot"]))

# 模拟 userData 往返（必须是 JSON 可序列化的）
import json  # noqa: E402
blob = json.dumps(orig.to_dict())
back = MS.MotionSession.from_dict(json.loads(blob))
H.check_eq("段数一样", len(back.segments), 2)
H.check_eq("段内容一样", [s.text for s in back.segments], ["走", "停"])
H.check_eq("段的 seed 保住了", back.segments[0].seed, 7)
H.check_eq("约束数一样", len(back.constraints), 2)
H.check_eq("控制参数一样",
           (back.seed, back.diffusion_steps, back.cfg_weight, back.transition_frames,
            back.postprocess, back.first_heading_angle, back.notes),
           (42, 50, 1.5, 8, False, 1.2, "测试"))
H.check_eq("root2d 的路点保住了", back.constraints[0].points, [[0.0, 0.0], [1.0, 1.0]])
H.check_eq("root2d 的帧保住了", back.constraints[0].frames, [0, 30])
H.check_eq("★ 姿势数组不塞进 userData（太大），只记『有』",
           back.constraints[1].joint_positions, None)

H.check("空字典也能还原成空会话", MS.MotionSession.from_dict({}).total_frames() == 0)
H.check("None 也能还原", MS.MotionSession.from_dict(None).total_frames() == 0)

# ===========================================================================
H.section("场景帧 ↔ 素材帧（只在这一处换算）")
# ===========================================================================
H.check_eq("同帧率：场景 1 → 素材 0", MS.scene_to_asset(1, 1, 24, 24), 0)
H.check_eq("同帧率：场景 31 → 素材 30", MS.scene_to_asset(31, 1, 24, 24), 30)
H.check_eq("★ 24fps 场景：场景 25（过 1 秒）→ 素材 30", MS.scene_to_asset(25, 1, 24, 30), 30)
H.check_eq("★ 30fps 场景：场景 31（过 1 秒）→ 素材 30", MS.scene_to_asset(31, 1, 30, 30), 30)
H.check_eq("起始帧不是 1 时能对上", MS.scene_to_asset(101, 101, 24, 24), 0)

H.check_eq("素材 0 → 场景首帧", MS.asset_to_scene(0, 1, 24, 30), 1)
H.check_eq("★ 素材 30（1 秒）→ 场景 25（24fps 也过 1 秒）", MS.asset_to_scene(30, 1, 24, 30), 25)
# 往返
for f in (0, 1, 15, 30, 89):
    sc = MS.asset_to_scene(f, 1, 24, 30)
    H.check("往返一致：素材 %d → 场景 %d → 素材" % (f, sc),
            abs(MS.scene_to_asset(sc, 1, 24, 30) - f) <= 1, MS.scene_to_asset(sc, 1, 24, 30))

H.check_eq("★ 30 帧素材在 30fps 场景铺 1..30", MS.scene_range_for_assets(30, 1, 30, 30), (1, 30))
H.check_eq("★ 30 帧素材在 24fps 场景铺 1..24（时长不变）",
           MS.scene_range_for_assets(30, 1, 24, 30), (1, 24))
H.check_eq("单帧不炸", MS.scene_range_for_assets(1, 1, 24, 30), (1, 1))
H.check_eq("起始帧能平移", MS.scene_range_for_assets(30, 101, 30, 30), (101, 130))

H.check_eq("整秒显示成「3 秒」", MS.nice_duration(3.0), "3 秒")
H.check_eq("半秒显示成「3.5 秒」", MS.nice_duration(3.5), "3.5 秒")

H.finish("test_motion_session")
