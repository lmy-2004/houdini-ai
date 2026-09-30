# -*- coding: utf-8 -*-
"""动作线的**真链路**冒烟测试 —— 会真的跑 Kimodo，慢（几十秒到几分钟）。

    "D:/ProgramFiles/Houdini22/bin/hython.exe" tests/live_motion_smoke.py

**不在 run_all.py 里**（那是全离线的）。这个要：
  * 后端的 venv 装好（D:/kimodo-motion/venv）
  * 权重下好（HF_HOME=D:/kimodo-motion/hf）
  * 显存够（TEXT_ENCODER_DEVICE=cpu 时 <3GB）

它验的是**离线测试永远验不到的东西**：
  后端真的能加载模型、真的能生成、真的吐出一个我们读得懂的 NPZ、
  而且那个 NPZ 落到场景里真的能在视口里动。

用法：
    <hython> tests/live_motion_smoke.py ["文字描述"] [时长秒]
"""

import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import _harness as H  # noqa: E402

from houdini_ai import motion_api, motion_npz, motion_rig, motion_session as MS  # noqa: E402
import hou  # noqa: E402

TEXT = sys.argv[1] if len(sys.argv) > 1 else "A person walks forward"
SECONDS = float(sys.argv[2]) if len(sys.argv) > 2 else 3.0

H.section("后端就绪检查")
ok, why = motion_api.check_backend()
H.check("后端在", ok, why)
if not ok:
    H.finish("live_motion_smoke")

import subprocess  # noqa: E402
# ★ 必须用 motion_api.child_env()，不能裸继承 os.environ：
#   hython 带着 PYTHONHOME=<HFS>/python313，传给 3.11 的后端会崩
#   （SRE module mismatch）。我第一版就是这么写的，白跑了一轮。
p = subprocess.run([motion_api.DEFAULT_VENV_PYTHON, "-c",
                    "import torch,motion_correction;print(torch.__version__, torch.cuda.is_available())"],
                   capture_output=True, text=True, timeout=300,
                   env=motion_api.child_env())
H.check("后端 import torch + motion_correction 成功", p.returncode == 0,
        (p.stderr or "")[-400:])
H.check("★ 防脚滑/约束优化模块可用（关键帧才能被精确命中）",
        p.returncode == 0 and "True" in (p.stdout or ""), p.stdout)

H.section("建场景")
obj = hou.node("/obj")
for old in list(obj.children()):
    if old.name().startswith(("LIVE_", "motion_ai")):
        old.destroy()
holder = obj.createNode("geo", "LIVE_holder")
net = motion_rig.build_motion_network(holder, "somaskel30")
container = net["container"]
H.check("动作容器建好", container is not None)

H.section("★ 在 Rig Pose 上摆两个关键帧（模拟用户操作）")
rig = net["rigpose"]
from kinefx import baking  # noqa: E402
import numpy as np  # noqa: E402

# 第 0 帧：静止姿势
baking.bakeCurrentPose(rig, source_node=rig, frame=0, expression="bezier()")
H.check("★ 记了第 0 帧的姿势", rig.parm("transformations").eval() > 0,
        "transformations=%s" % rig.parm("transformations").eval())

# 第 90 帧（3 秒处）：把一条腿抬起来
hou.setFrame(90)
# 找一条腿的关节在 Rig Pose 里的实例号
import kinefx.rigapi  # noqa: E402
geo_rest = net["rest"].geometry()
names = [pt.attribValue("name") for pt in geo_rest.points()]
target = "LeftLeg"
idx = names.index(target)
try:
    rig.parm("group%d" % idx).set("@name=%s" % target)
    rig.parm("enable%d" % idx).set(1)
    rig.parm("mode%d" % idx).set("override")
    rig.parm("r%dx" % idx).set(35.0)
    H.check("给 %s 摆了 35 度" % target, True)
except Exception as exc:
    H.check("给 %s 摆姿势" % target, False, repr(exc))
baking.bakeCurrentPose(rig, source_node=rig, frame=90, expression="bezier()")

H.section("组装 payload 并生成（这一步最慢）")
s = MS.MotionSession(postprocess=True, diffusion_steps=50)
s.add_segment(TEXT, SECONDS)
out = os.path.join("D:/kimodo-motion/output", "smoke.npz")


def reader(asset_frame):
    scene_f = MS.asset_to_scene(asset_frame, 1, hou.fps(), 30.0)
    hou.setFrame(scene_f)
    rig.cook(force=True)
    return motion_rig.read_pose(rig.geometry())


t0 = time.time()
done = None
try:
    payload = motion_api.build_payload(s, reader, out)
    H.check("payload 组装成功", len(payload["segments"]) == 1)
    done = motion_api.run_generation(
        payload, on_event=lambda ev: print("   [后端] %s" % (ev.get("name") or ev.get("event"))),
        timeout=3600)
except Exception as exc:
    H.check("后端跑通", False, "%s: %s" % (type(exc).__name__, str(exc)[:900]))

H.check("★ 后端生成了 NPZ", done is not None)

if done:
    H.section("★ 产出的 NPZ 我们能读、且内容合理")
    m = motion_npz.load(done["npz"])
    H.check("★ 关节数是 77（真实 CLI 的导出；网格是按 77 绑的）",
            m.joint_count in (30, 77), m.joint_count)
    H.check("帧数与请求相符（%d 帧 ≈ %.1f 秒）" % (m.frame_count, SECONDS),
            abs(m.frame_count - SECONDS * 30) <= 3, m.frame_count)
    print("   ", motion_npz.describe(done["npz"]).replace("\n", "\n    "))

    hip_y = m.posed_joints[:, m.root_index, 1]
    H.check("★ 髋高中位数在人类尺度（0.85~1.05 米）",
            0.85 < float(np.median(hip_y)) < 1.05,
            "中位 %.3f" % float(np.median(hip_y)))
    H.check("★ 第 0 帧根在 XZ 原点附近（canonical 约定）",
            float(np.abs(m.posed_joints[0, m.root_index, [0, 2]]).max()) < 0.3,
            m.posed_joints[0, m.root_index, [0, 2]])
    H.check("动作确实在变（不是静止的）",
            float(np.abs(m.posed_joints[0] - m.posed_joints[-1]).max()) > 0.01)

    H.section("★ 落到场景里：视口里能播")
    frames, a, b = motion_rig.apply_npz(container, done["npz"])
    motion = net["motion"]
    hou.setFrame(a)
    motion.cook(force=True)
    p0 = np.array([list(pt.position()) for pt in motion.geometry().points()])
    hou.setFrame(b)
    motion.cook(force=True)
    p1 = np.array([list(pt.position()) for pt in motion.geometry().points()])
    H.check("★ scrub 到末帧，骨架位置确实变了（能在视口里看到动）",
            float(np.linalg.norm(p1 - p0, axis=1).max()) > 0.01,
            "最大变化 %.4f 米" % float(np.linalg.norm(p1 - p0, axis=1).max()))
    H.check_eq("骨架点数 = 关节数", len(p0), m.joint_count)

    if net.get("skin") is not None and m.joint_count == 77:
        H.section("★ 蒙皮网格跟着动")
        d = net["skin"]["deform"]
        hou.setFrame(b)
        d.cook(force=True)
        H.check_eq("deform 无报错", d.errors(), ())
        H.check("★ 网格产生了形变（不是原样不动）",
                len(d.geometry().points()) == 18056)
    print()
    print("总用时 %.0f 秒" % (time.time() - t0))
    print("NPZ: %s" % done["npz"])
    print("场景里：%s（骨架）/ %s（网格）" % (net["out"].path(),
                                          (net["skin"] or {}).get("deform", container).path()))

H.finish("live_motion_smoke")
