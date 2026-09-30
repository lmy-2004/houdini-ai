# -*- coding: utf-8 -*-
"""动作线的 Houdini 侧测试：rest 骨架、读姿势、建网络。

★ 本文件最有力的一条断言是「**读 T-pose 骨架应当得到全局旋转 = identity**」。
  理由：SOMA 的 rest 姿势按定义就是 `global_rot_mats = I`。
  所以「建 rest 骨架 → 读回来」必须原样还原成 I。
  这一条同时验了 tp 校正的正向与逆向（哪怕两边的 tp 用法一起写反，这条也会红）。
"""

import os
import sys

import numpy as np

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import _harness as H  # noqa: E402

from houdini_ai import motion_npz, motion_rig, soma_skeleton  # noqa: E402
import hou  # noqa: E402

TOOL_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
TMP = os.path.join(os.path.dirname(os.path.abspath(__file__)), "_tmp")
if not os.path.isdir(TMP):
    os.makedirs(TMP)


def fwd(p):
    return p.replace("\\", "/")


# 把一段代码塞进 Python SOP 并 cook，返回几何（节点外拿到的是只读几何，必须这样测）
def cook(code):
    geo = hou.node("/obj").createNode("geo", "__test_rig_geo__")
    sop = geo.createNode("python", "__cook__")
    sop.parm("python").set(code)
    try:
        sop.cook(force=True)
    except hou.OperationFailed:
        # cook 抛的异常本身没有信息，真报错在 errors() 里
        H.check("Python SOP cook 成功", False,
                " | ".join(sop.errors()) or "（errors() 是空的）")
        geo.destroy()
        raise
    if sop.errors():
        H.check("Python SOP 无报错", False, "; ".join(sop.errors()))
    return sop.geometry(), sop


REST_CODE = """
import sys
sys.path.insert(0, %(root)r)
from houdini_ai import motion_npz
motion_npz.build_rest_geometry(hou.pwd().geometry(), %(skel)r)
"""

# 直接把某个关节的 transform 换成一个已知旋转，模拟「用户摆了姿势」
POSE_CODE = """
import sys
import numpy as np
sys.path.insert(0, %(root)r)
from houdini_ai import motion_npz, motion_rig, soma_skeleton

geo = hou.pwd().geometry()
motion_npz.build_rest_geometry(geo, %(skel)r)
tp = motion_npz.tpose_offsets(%(skel)r)
joints = [n for n, _ in soma_skeleton.SKELETONS[%(skel)r]["joints"]]
j = joints.index(%(joint)r)

# 用一个明确不为 identity 的旋转，走**正向**转换（和后端→场景同一条路）
R = np.array([[0.0, -1.0, 0.0], [1.0, 0.0, 0.0], [0.0, 0.0, 1.0]])   # 绕 Z 转 90 度
T = motion_npz.to_kinefx_transform(R, tp[j])
geo.point(j).setAttribValue("transform", tuple(float(v) for v in T.reshape(-1)))
"""

# 空几何
EMPTY_CODE = "hou.pwd().geometry().clear()"


# ===========================================================================
H.section("rest 骨架：形状与 KineFX 恒等式")
# ===========================================================================
for skel in ("somaskel30", "somaskel77"):
    g, s = cook(REST_CODE % {"root": fwd(TOOL_ROOT), "skel": skel})
    J = len(soma_skeleton.SKELETONS[skel]["joints"])
    H.check_eq("%s：点数 = 关节数" % skel, len(g.points()), J)
    H.check_eq("%s：骨头段数 = 关节数-1" % skel, len(g.prims()), J - 1)
    names = [p.attribValue("name") for p in g.iterPoints()]
    H.check_eq("%s：name 顺序与骨架表一致" % skel, names,
               [n for n, _ in soma_skeleton.SKELETONS[skel]["joints"]])
    H.check("%s：有 transform / localtransform / parent_id / path" % skel,
            all(g.findPointAttrib(a) is not None
                for a in ("transform", "localtransform", "parent_id", "path")))
    # rest 骨架不该有 contact（那是动画帧才有的）
    H.check("%s：rest 骨架没有 contact 属性" % skel, g.findPointAttrib("contact") is None)

    # KineFX 恒等式
    worst = 0.0
    for i in range(J):
        pi = g.point(i).attribValue("parent_id")
        if pi < 0:
            continue
        ti = np.array(g.point(i).attribValue("transform")).reshape(3, 3)
        li = np.array(g.point(i).attribValue("localtransform")).reshape(4, 4)[:3, :3]
        tp_ = np.array(g.point(pi).attribValue("transform")).reshape(3, 3)
        worst = max(worst, float(np.abs(ti - li @ tp_).max()))
    H.check("%s：KineFX 恒等式 transform_i == local_i @ transform_parent" % skel,
            worst < 1e-5, "最大误差 %.2e" % worst)

    # ★★ 核心不变量：T-pose 的全局旋转应当是 identity
    pos, rot = motion_rig.read_pose(g, skel)
    dev = float(np.abs(rot - np.eye(3)).max())
    H.check("★★ %s：读 T-pose 得到全局旋转 = identity（往返自洽）" % skel,
            dev < 1e-5, "与单位阵的最大偏差 %.2e" % dev)

    # 位置应当与素材表一致
    tp_pos = motion_npz.tpose_positions(skel)
    H.check("%s：读回的位置与 T-pose 素材一致" % skel,
            float(np.abs(pos - tp_pos).max()) < 1e-5,
            "最大偏差 %.2e" % float(np.abs(pos - tp_pos).max()))
    s.destroy()

# ===========================================================================
H.section("read_pose：关节顺序按骨架表，不按点序")
# ===========================================================================
g, s = cook(REST_CODE % {"root": fwd(TOOL_ROOT), "skel": "somaskel30"})
skel30 = [n for n, _ in soma_skeleton.SKELETONS["somaskel30"]["joints"]]
pos, rot = motion_rig.read_pose(g)
H.check_eq("不给 skel_name 也能按点数反推", len(pos), 30)
idx_hips = skel30.index("Hips")
H.check("第 0 个读回来的就是 Hips（按骨架表排序）",
        abs(float(pos[0][1]) - float(motion_npz.tpose_positions("somaskel30")[0][1])) < 1e-6)
H.check("Hips 的高度是 1.0 米（确认单位是米）",
        0.9 < float(pos[idx_hips][1]) < 1.1, float(pos[idx_hips][1]))
s.destroy()

# ===========================================================================
H.section("read_pose：摆过的姿势能读回来（往返）")
# ===========================================================================
g, s = cook(POSE_CODE % {"root": fwd(TOOL_ROOT), "skel": "somaskel30",
                         "joint": "LeftHand"})
pos2, rot2 = motion_rig.read_pose(g, "somaskel30")
j = skel30.index("LeftHand")
Rz90 = np.array([[0.0, -1.0, 0.0], [1.0, 0.0, 0.0], [0.0, 0.0, 1.0]])
H.check("★ 摆的姿势被原样读回（不是 identity）",
        float(np.abs(rot2[j] - np.eye(3)).max()) > 0.9,
        "与单位阵偏差 %.3f" % float(np.abs(rot2[j] - np.eye(3)).max()))
H.check("★ 读回的就是我们放进去的那个旋转（往返一致）",
        float(np.abs(rot2[j] - Rz90).max()) < 1e-5,
        "最大偏差 %.2e" % float(np.abs(rot2[j] - Rz90).max()))
H.check("没动过的关节仍是 identity",
        float(np.abs(rot2[skel30.index("RightHand")] - np.eye(3)).max()) < 1e-5)
s.destroy()

# ===========================================================================
H.section("read_pose：错误信息要能直接给用户看")
# ===========================================================================
g, s = cook(EMPTY_CODE)
try:
    motion_rig.read_pose(g)
    H.check("空几何要报错", False, "没报错")
except motion_rig.RigError as exc:
    H.check("空几何的报错说得清（提示去看上游报错）",
            "空" in str(exc) and "报错" in str(exc), str(exc))
s.destroy()

bad_code = """
import sys
sys.path.insert(0, %(root)r)
from houdini_ai import motion_npz
g = hou.pwd().geometry()
motion_npz.build_rest_geometry(g, "somaskel30")
g.point(0).setAttribValue("name", "NotAJoint")
""" % {"root": fwd(TOOL_ROOT)}
g, s = cook(bad_code)
try:
    motion_rig.read_pose(g, "somaskel30")
    H.check("关节名对不上要报错", False, "没报错")
except motion_rig.RigError as exc:
    H.check("关节名对不上时报错里列出缺哪个、多哪个",
            "缺" in str(exc) and "NotAJoint" in str(exc) and "Hips" in str(exc), str(exc))
s.destroy()

# ===========================================================================
H.section("建网络：rest 骨架 + Rig Pose + 动作节点 + out")
# ===========================================================================
obj = hou.node("/obj")
holder = obj.createNode("geo", "__test_net_holder__")
net = motion_rig.build_motion_network(holder, "somaskel30")
H.check("容器建出来了", net["container"] is not None)
H.check_eq("四个主节点都在（名字要能自解释）",
           sorted(n.name() for n in net["container"].children()
                  if n.name() in ("rest_skeleton", "pose_rig", "motion", "out")),
           sorted(["rest_skeleton", "pose_rig", "motion", "out"]))
H.check_eq("Rig Pose 的输入是 rest 骨架",
           net["rigpose"].input(0).path(), net["rest"].path())
H.check_eq("动作节点的输入 1 是 Rig Pose",
           net["motion"].input(1).path(), net["rigpose"].path())
H.check_eq("out 的输入是动作节点",
           net["out"].input(0).path(), net["motion"].path())
H.check("out 有显示标志", net["out"].isDisplayFlagSet())
H.check("节点有中文注释（状态露在外面）",
        bool(net["motion"].comment()) and bool(net["rigpose"].comment()))
H.check("注释在网络里可见",
        net["rigpose"].isGenericFlagSet(hou.nodeFlag.DisplayComment))

# rest 骨架节点真能 cook 出东西
net["rest"].cook(force=True)
H.check_eq("rest 骨架节点 cook 出 30 个点", len(net["rest"].geometry().points()), 30)

# 再建一个不会撞名
net2 = motion_rig.build_motion_network(holder, "somaskel30")
H.check("再建一个会自动改名不撞", net2["container"].name() != net["container"].name())
holder.destroy()

# ===========================================================================
H.section("★ 蒙皮支链：网格真的能跟着骨架动")
# ===========================================================================
holder = obj.createNode("geo", "__test_skin_holder__")
net = motion_rig.build_motion_network(holder, "somaskel30")
skin = net.get("skin")
H.check("★ 建出了蒙皮支链（素材在盘上）", skin is not None)
if skin is not None:
    for k in ("skin_mesh", "capture_pose", "capture", "deform"):
        H.check("有 %s 节点" % k, skin[k] is not None and skin[k].parent() is not None)

    mesh = skin["skin_mesh"]
    mesh.cook(force=True)
    H.check_eq("★ 网格点数（Kimodo 自带的 SOMA77 网格）", len(mesh.geometry().points()), 18056)
    H.check_eq("★ 网格面数", len(mesh.geometry().prims()), 36108)
    H.check("网格在合理尺度（A-pose 身高约 1.76 米）",
            1.5 < max(p.position()[1] for p in mesh.geometry().points()) < 2.0)

    skin["capture_pose"].cook(force=True)
    H.check_eq("绑定姿势骨架 77 个点", len(skin["capture_pose"].geometry().points()), 77)

    grab = skin["capture"]
    grab.cook(force=True)
    H.check_eq("抓取无报错", grab.errors(), ())
    H.check("★★ Houdini 自己算出了 boneCapture（手搓不出来的那个属性）",
            grab.geometry().findPointAttrib("boneCapture") is not None,
            [a.name() for a in grab.geometry().pointAttribs()])
    H.check_eq("★ maxinfluences 设成了 8（默认 2 会明显生硬）",
               grab.parm("maxinfluences").eval(), 8)

    # ★ 恒等对照：动画骨架 == 绑定骨架 → 网格必须纹丝不动
    #   用一个新的 deform 把 input2 也接成 capture_pose
    ident = net["container"].createNode("kinefx::jointdeform", "__ident_deform__")
    ident.setInput(0, grab)
    ident.setInput(1, skin["capture_pose"])
    ident.setInput(2, skin["capture_pose"])
    ident.cook(force=True)
    H.check_eq("恒等 deform 无报错", ident.errors(), ())
    d = max(np.linalg.norm(
        np.array(list(a.position())) - np.array(list(b.position())))
        for a, b in zip(grab.geometry().points(), ident.geometry().points()))
    H.check("★★ 恒等对照：动画==绑定时网格位移 ≈ 0（证明这条链自洽）",
            d < 1e-5, "最大位移 %.3e 米" % d)
    ident.destroy()

    # 真驱动：deform 的 input2 接的是动作节点
    d2 = skin["deform"]
    H.check_eq("deform 的 input 0 是抓取结果", d2.input(0).path(), grab.path())
    H.check_eq("deform 的 input 1 是绑定骨架",
               d2.input(1).path(), skin["capture_pose"].path())
    H.check_eq("★ deform 的 input 2 是动画骨架", d2.input(2).path(),
               net["motion"].path())
holder.destroy()

# ===========================================================================
H.section("apply_npz：接上生成的 NPZ")
# ===========================================================================
p = os.path.join(TMP, "rig_fixture.npz")
base = motion_npz.tpose_positions("somaskel30")
nf, J = 30, 30
pos3 = np.zeros((nf, J, 3), dtype=np.float32)
for f in range(nf):
    pos3[f] = base + [0.0, 0.0, 0.02 * f]
np.savez(p, posed_joints=pos3, global_rot_mats=np.tile(np.eye(3, dtype=np.float32), (nf, J, 1, 1)))

# ★ 帧率重映射：素材 30 帧@30fps = 1 秒。
#   场景什么帧率，就铺多少帧——按「时长不变」换算，不是照搬帧数。
H.check_eq("30fps 场景：30 帧素材铺 1..30", motion_rig.scene_frame_range(30, 1, 30.0, 30.0), (1, 30))
H.check_eq("★ 24fps 场景：30 帧素材铺 1..24（时长不变，1 秒）",
           motion_rig.scene_frame_range(30, 1, 24.0, 30.0), (1, 24))
H.check_eq("起始帧不是 1 时要平移", motion_rig.scene_frame_range(30, 101, 30.0, 30.0), (101, 130))
H.check_eq("单帧素材不炸", motion_rig.scene_frame_range(1, 1, 24.0, 30.0), (1, 1))

holder = obj.createNode("geo", "__test_apply_holder__")
net = motion_rig.build_motion_network(holder, "somaskel30")
fps_before = hou.fps()
try:
    frames, a, b = motion_rig.apply_npz(net["container"], p, kimodo_fps=30.0, first_frame=1)
    H.check_eq("报告了素材帧数", frames, nf)
    fr = hou.playbar.frameRange()
    pr = hou.playbar.playbackRange()
    expect_b = motion_rig.scene_frame_range(nf, 1, hou.fps(), 30.0)[1]
    H.check_eq("★ 帧范围按当前场景帧率换算（不是照搬 30）", (int(fr[0]), int(fr[1])), (1, expect_b))
    H.check_eq("★ 播放范围也设了（两个是独立的东西，必须都设）",
               (int(pr[0]), int(pr[1])), (1, expect_b))
    H.check_eq("返回值与 playbar 一致", (a, b), (1, expect_b))
    H.check("NPZ 路径写进了参数",
            net["motion"].parm("npzpath").eval().replace("\\", "/").endswith("rig_fixture.npz"))
    H.check_eq("素材帧率写进了参数", net["motion"].parm("kimodo_fps").eval(), 30.0)
    # cook 一下，几何应当有 30 点
    net["motion"].cook(force=True)
    H.check_eq("动作节点 cook 出 30 个点", len(net["motion"].geometry().points()), 30)
    H.check("节点注释里写了状态", "关节" in net["motion"].comment(), net["motion"].comment())
    # 换帧后几何要变（证明「能播」不是假的）
    hou.setFrame(int(fr[0]))
    net["motion"].cook(force=True)
    y0 = float(net["motion"].geometry().point(0).attribValue("P")[2])
    hou.setFrame(int(fr[1]))
    net["motion"].cook(force=True)
    y1 = float(net["motion"].geometry().point(0).attribValue("P")[2])
    H.check("★ scrub 到末帧，几何确实变了（能播）", abs(y1 - y0) > 1e-6,
            "首帧 z=%.4f 末帧 z=%.4f" % (y0, y1))
finally:
    if abs(hou.fps() - fps_before) > 1e-9:
        hou.fps(fps_before)
holder.destroy()

H.finish("test_motion_rig")
