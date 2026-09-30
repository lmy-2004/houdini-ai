# -*- coding: utf-8 -*-
"""NPZ → KineFX 骨架的测试。

分三层：
  ① 合成 NPZ —— 旋转矩阵是我们自己放的，能**精确**验证转置与 tp 校正的数学
  ② 官方真实 NPZ —— 金标准。如果本机没有 Kimodo 检出就响亮地跳过（不静默）
  ③ hou 相关 —— 在 hython 里真建几何，并验证 KineFX 自己的恒等式

★ 本文件里最重要的一条断言是**轴向直方图**（§4.3）：它不看数值对不对，
  只看骨头朝向是不是「骨头对齐」。这是唯一能客观判断坐标转换有没有做错的判据，
  也是唯一能挡住「数值看着像对的、其实轴全歪了」这种错的判据。
"""

import os
import sys

import numpy as np

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import _harness as H  # noqa: E402

from houdini_ai import motion_npz, soma_skeleton  # noqa: E402
import hou  # noqa: E402

# 官方样例 NPZ（可选依赖：没有就跳过，但要响亮地说出来）
OFFICIAL = ("D:/kimodo-motion/src/kimodo/kimodo/assets/demo/examples/"
            "kimodo-soma-rp/01_single_text_prompt/motion.npz")
OFFICIAL_KEYS = ("posed_joints", "global_rot_mats", "foot_contacts")


def make_synthetic(path, n_frames=4, joints=30, rot=None, contacts=None):
    """造一个形状与官方一致的 NPZ。

    ★ 位置必须用**真实的 T-pose 关节位置**。第一版我把所有关节都放在同一点，
      结果夹具退化了：相对位移全是 0，于是「平移落在最后一行」这类断言
      测的是夹具而不是代码（当时真就误报了一次失败）。
    """
    skel = soma_skeleton.SKELETONS["somaskel30"]
    J = len(skel["joints"])
    assert joints == J, "合成夹具目前只做 30 关节"
    base = motion_npz.tpose_positions("somaskel30")
    assert base is not None, "缺 T-pose 位置表，跑 scripts/extract_soma_tpose.py"
    pos = np.zeros((n_frames, J, 3), dtype=np.float32)
    for f in range(n_frames):
        pos[f] = base
        pos[f, :, 1] += 0.01 * f               # 逐帧抬一点点，让帧与帧之间可区分
        pos[f, :, 2] += 0.02 * f               # 也带点前进，模拟位移
    gr = np.tile(np.eye(3, dtype=np.float32), (n_frames, J, 1, 1))
    if rot is not None:
        for f, per_joint in enumerate(rot):
            for j, R in per_joint.items():
                gr[f, j] = R
    kw = {}
    if contacts is not None:
        kw["foot_contacts"] = np.asarray(contacts, dtype=bool)
    np.savez(path, posed_joints=pos, global_rot_mats=gr, **kw)
    return pos, gr


TMP = os.path.join(os.path.dirname(os.path.abspath(__file__)), "_tmp")
if not os.path.isdir(TMP):
    os.makedirs(TMP)


def rot_x(a):
    c, s = np.cos(a), np.sin(a)
    return np.array([[1, 0, 0], [0, c, -s], [0, s, c]], dtype=np.float32)


# ===========================================================================
H.section("转置与 tp 校正的数学（合成数据，可精确验证）")
# ===========================================================================
J30 = len(soma_skeleton.SKELETONS["somaskel30"]["joints"])
tp = motion_npz.tpose_offsets("somaskel30")
H.check("30 关节的 tp 表存在", tp is not None and tp.shape == (J30, 3, 3),
        "得到 %r" % (None if tp is None else tp.shape,))
H.check("77 关节的 tp 表存在",
        motion_npz.tpose_offsets("somaskel77") is not None
        and motion_npz.tpose_offsets("somaskel77").shape == (77, 3, 3))

# 取一个明确不为 identity 的旋转
R = rot_x(0.7).astype(np.float64)
T_out = motion_npz.to_kinefx_transform(R, tp[3])
H.check("to_kinefx：等于 (R @ tp).T",
        np.allclose(T_out, (R @ tp[3]).T, atol=1e-10))
H.check("to_kinefx：不含 tp 时就是纯转置",
        np.allclose(motion_npz.to_kinefx_transform(R, None), R.T, atol=1e-10))

# 往返
H.check("往返一致：from(to(R)) == R",
        np.allclose(motion_npz.from_kinefx_transform(T_out, tp[3]), R, atol=1e-9))

# 顺序陷阱：右乘 vs 左乘
H.check("★ 右乘才对：`tp @ R` 与 `R @ tp` 结果不同（写反了会得到混乱轴向）",
        not np.allclose(R @ tp[3], tp[3] @ R, atol=1e-6))

H.check("tp 都是合法旋转（正交、det=1）",
        np.allclose(np.linalg.det(tp), 1.0, atol=1e-5))

# ===========================================================================
H.section("load：合成 NPZ")
# ===========================================================================
p1 = os.path.join(TMP, "synth_plain.npz")
make_synthetic(p1)
m = motion_npz.load(p1)
H.check_eq("骨架识别为 30", m.joint_count, 30)
H.check_eq("骨架名", m.skel_name, "somaskel30")
H.check_eq("帧数", m.frame_count, 4)
H.check_eq("根关节是 Hips", m.joint_names[m.root_index], "Hips")
H.check_eq("Hips 的父索引是 -1", m.parent_index(m.root_index), -1)
H.check_eq("Spine1 的父是 Hips", m.parent_index(m.index_of("Spine1")), m.root_index)
H.check("没有 foot_contacts 时不报错", m.foot_contacts is None)
H.check_eq("contacts_at 全 0", set(m.contacts_at(0)), {0})

# ===========================================================================
H.section("★ 轴向直方图：骨头对齐 vs 世界轴对齐")
# ===========================================================================
# ★ 这个判据**只在 rest(T-pose) 上有意义**。
#   实测教训：我一开始拿动画帧去算，带 tp 71%、不带 tp 也差不多，两条都「不通过」。
#   原因是动画帧里骨头本来就是弯的，骨头方向落在哪个本地轴由姿势决定，自然会散。
#   T-pose 里所有长骨都伸直，才会集中——那份转换约定文档给的也正是 T-pose 的数字
#   （`{+X:33, -X:26}`），本机复现出来一模一样。
TOOL_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def axis_hist_T_pose(skel_name, use_tp):
    """T-pose 下的轴向直方图。T-pose 的 global 旋转恒为 identity，
    所以骨对齐帧就是 tp 本身（use_tp=False 时用 identity，模拟「不校正」）。"""
    skel = soma_skeleton.SKELETONS[skel_name]
    joints = [n for n, _ in skel["joints"]]
    par = {n: p for n, p in skel["joints"]}
    pos = motion_npz.tpose_positions(skel_name)
    tp = motion_npz.tpose_offsets(skel_name) if use_tp else None
    hist = {}
    ax = ["+X", "+Y", "+Z", "-X", "-Y", "-Z"]
    for i, n in enumerate(joints):
        kids = [k for k in joints if par[k] == n]
        if not kids:
            continue
        wd = pos[joints.index(kids[0])] - pos[i]
        nrm = np.linalg.norm(wd)
        if nrm < 1e-9:
            continue
        wd = wd / nrm
        # 与 to_kinefx_transform(R_global, tp) 等价，只是 R_global = I 且不转置
        R = tp[i] if tp is not None else np.eye(3)
        ld = wd @ R
        a = int(np.argmax(np.abs(ld)))
        k = ax[a + (0 if ld[a] >= 0 else 3)]
        hist[k] = hist.get(k, 0) + 1
    return hist


def dominant(h):
    pair = {}
    for k, v in h.items():
        pair[k[-1]] = pair.get(k[-1], 0) + v
    return max(pair.values()) / max(1, sum(h.values())) if h else 0.0


for skel_name in ("somaskel77", "somaskel30"):
    ha = axis_hist_T_pose(skel_name, True)
    hb = axis_hist_T_pose(skel_name, False)
    H.check("★ %s：做 tp 校正后轴向压在一条轴上（>=90%%）" % skel_name,
            dominant(ha) >= 0.90, "直方图 %s，主轴 %.0f%%" % (ha, dominant(ha) * 100))
    H.check("★ %s：不校正则散开（<90%%）—— 反证上面的断言有意义" % skel_name,
            dominant(hb) < 0.90, "直方图 %s，主轴 %.0f%%" % (hb, dominant(hb) * 100))

H.check("T-pose 位置表存在且是米制（髋高约 1.0）",
        motion_npz.tpose_positions("somaskel30") is not None
        and 0.9 < float(motion_npz.tpose_positions("somaskel30")[0][1]) < 1.1,
        None if motion_npz.tpose_positions("somaskel30") is None
        else "髋高 %r" % (float(motion_npz.tpose_positions("somaskel30")[0][1]),))

# ===========================================================================
H.section("脚部接触：4 通道 → 按骨架脚链映射到关节")
# ===========================================================================
# 官方顺序是 [左heel, 左toe, 右heel, 右toe]
contacts = [[1, 0, 0, 1], [0, 0, 0, 0], [1, 1, 0, 0], [0, 1, 1, 0]]
p2 = os.path.join(TMP, "synth_contacts.npz")
make_synthetic(p2, n_frames=4, contacts=contacts)
m2 = motion_npz.load(p2)
H.check_eq("通道名映射", m2.contact_channel_names(),
           ["LeftFoot", "LeftToeBase", "RightFoot", "RightToeBase"])
c0 = m2.contacts_at(0)
H.check_eq("第 0 帧：左 Foot 着地", c0[m2.index_of("LeftFoot")], 1)
H.check_eq("第 0 帧：左 ToeBase 没着地", c0[m2.index_of("LeftToeBase")], 0)
H.check_eq("第 0 帧：右 Foot 没着地", c0[m2.index_of("RightFoot")], 0)
H.check_eq("第 0 帧：右 ToeBase 着地", c0[m2.index_of("RightToeBase")], 1)
H.check("★ 接触只落在脚上：其余关节恒 0",
        all(v == 0 for i, v in enumerate(m2.contacts_at(0))
            if m2.joint_names[i] not in ("LeftFoot", "LeftToeBase", "RightFoot", "RightToeBase")))

# 通道数比骨架脚链多时应给警告而不是崩
p3 = os.path.join(TMP, "synth_wide.npz")
make_synthetic(p3, n_frames=2, contacts=[[1, 1, 1, 1, 0, 0], [0, 0, 0, 0, 0, 0]])
m3 = motion_npz.load(p3)
H.check("6 通道的 NPZ：给警告但不是错误",
        len(m3.warnings) == 1 and "脚链期望" in m3.warnings[0], m3.warnings)
H.check_eq("多出来的通道被忽略，前 4 个照常映射", m3.contacts_at(0)[m3.index_of("LeftFoot")], 1)

# ===========================================================================
H.section("帧率换算：Kimodo 30fps vs 场景 24fps")
# ===========================================================================
p4 = os.path.join(TMP, "synth_fps.npz")
make_synthetic(p4, n_frames=100)
m4 = motion_npz.load(p4)
H.check_eq("同帧率：场景第 1 帧 → 下标 0", m4.frame_index_for(1, 1, 24, 24), 0)
H.check_eq("同帧率：场景第 25 帧 → 下标 24", m4.frame_index_for(25, 1, 24, 24), 24)
# 24fps 场景放 30fps 素材：场景过 24 帧 = 1 秒 = 素材 30 帧
H.check_eq("★ 24fps 场景走过 1 秒 → 素材第 30 帧", m4.frame_index_for(25, 1, 24, 30), 30)
H.check_eq("★ 30fps 场景走过 1 秒 → 素材第 30 帧", m4.frame_index_for(31, 1, 30, 30), 30)
H.check_eq("第一帧就是第一帧", m4.frame_index_for(1, 1, 24, 30), 0)
H.check_eq("帧号越界会被夹住（不抛异常）", m4.pose_at(9999)[0].shape, (30, 3))
H.check_eq("负数帧号被夹到 0", m4.frame_clamped(-5), 0)

# ===========================================================================
H.section("错误信息要能直接给用户看")
# ===========================================================================
H.check_raises("空路径", motion_npz.NpzError, motion_npz.load, "")
H.check_raises("文件不存在", motion_npz.NpzError, motion_npz.load,
               os.path.join(TMP, "nope.npz"))
bad1 = os.path.join(TMP, "bad_missing_key.npz")
np.savez(bad1, posed_joints=np.zeros((2, 30, 3)))
try:
    motion_npz.load(bad1)
    H.check("缺键要报错", False, "没报错")
except motion_npz.NpzError as exc:
    H.check("缺键的报错里说了缺哪个、以及文件里有什么",
            "global_rot_mats" in str(exc) and "posed_joints" in str(exc), str(exc))
# ★ 批量维：模型原始输出是 (批量, 帧, 关节, 坐标) 4 维。
#   官方 CLI 单样本时会把批量维挤掉（generate.py:345-352），但没挤过的也得认——
#   不认的话会把「批量=1」当成「帧数=1」，1 秒的动作静默读成 1 帧（真踩过）。
H.section("★ 批量维：4 维 (1, T, J, 3) 要能正确读成 T 帧")
b1 = os.path.join(TMP, "batched.npz")
T, J = 12, 30
base = motion_npz.tpose_positions("somaskel30")
pos4 = np.zeros((1, T, J, 3), dtype=np.float32)
for f in range(T):
    pos4[0, f] = base + [0.0, 0.0, 0.01 * f]
np.savez(b1, posed_joints=pos4,
         global_rot_mats=np.tile(np.eye(3, dtype=np.float32), (1, T, J, 1, 1)),
         foot_contacts=np.zeros((1, T, 4), dtype=bool))
mb = motion_npz.load(b1)
H.check_eq("★ 4 维读成 12 帧（不是 1 帧）", mb.frame_count, T)
H.check_eq("关节数不受影响", mb.joint_count, J)
H.check("位置也对（不是只改了形状）",
        abs(float(mb.posed_joints[5][0][1]) - float(base[0][1])) < 1e-5)
H.check("脚部接触也跟着挤掉了批量维", mb.foot_contacts is not None
        and mb.foot_contacts.shape == (T, 4),
        None if mb.foot_contacts is None else mb.foot_contacts.shape)
# 3 维的仍然照常
H.check_eq("3 维的照常（12 帧）", motion_npz.load(p4).frame_count, 100)

# 批量 > 1 的要明确拒绝，不能瞎猜
b2 = os.path.join(TMP, "batch3.npz")
np.savez(b2, posed_joints=np.zeros((3, 5, 30, 3), dtype=np.float32),
         global_rot_mats=np.zeros((3, 5, 30, 3, 3), dtype=np.float32))
try:
    motion_npz.load(b2)
    H.check("批量 >1 要报错（不能瞎猜取哪一个）", False, "没报错")
except motion_npz.NpzError as exc:
    H.check("★ 批量 >1 时报错说清了形状", "3" in str(exc) and "形状" in str(exc), str(exc))

bad2 = os.path.join(TMP, "bad_jointcount.npz")
np.savez(bad2, posed_joints=np.zeros((2, 41, 3)), global_rot_mats=np.zeros((2, 41, 3, 3)))
try:
    motion_npz.load(bad2)
    H.check("关节数不认识要报错", False, "没报错")
except motion_npz.NpzError as exc:
    H.check("关节数不认识时报错里给出已知的骨架与关节数",
            "41" in str(exc) and "30" in str(exc) and "77" in str(exc), str(exc))

# ===========================================================================
H.section("官方真实 NPZ（金标准；没有就响亮跳过）")
# ===========================================================================
if not os.path.isfile(OFFICIAL):
    print("  SKIP 本机没有官方样例 NPZ：%s" % OFFICIAL)
else:
    mo = motion_npz.load(OFFICIAL)
    H.check("官方 NPZ 读得进来", mo.frame_count > 0)
    H.check_eq("官方样例是 30 关节（不是文档说的 77）", mo.joint_count, 30)
    H.check("官方 NPZ 带 foot_contacts", mo.foot_contacts is not None)
    H.check_eq("foot_contacts 是 4 通道（官方原版）", mo.foot_contacts.shape[1], 4)
    # ★ 单位判据：用**中位数**而不是极值。
    #   这段动作是「走过去捡东西」，第 75~80 帧髋高 1.379（起身/跳），
    #   拿 min/max 卡区间会把正常数据判错。中位数才反映「这个人站着有多高」。
    pos_all = mo.posed_joints
    hip_y = pos_all[:, mo.root_index, 1]
    H.check("★ 髋高中位数在 0.85~1.05 米（确认单位是米，不是厘米）",
            0.85 < float(np.median(hip_y)) < 1.05,
            "中位 %.3f，范围 %.3f ~ %.3f" % (np.median(hip_y), hip_y.min(), hip_y.max()))
    H.check("髋高绝不会超出人类尺度（0.4~1.8 米）",
            float(hip_y.min()) > 0.4 and float(hip_y.max()) < 1.8,
            "范围 %.3f ~ %.3f" % (hip_y.min(), hip_y.max()))
    H.check("★ 第 0 帧根在 XZ 原点附近（Kimodo 的 canonical 约定）",
            float(np.abs(pos_all[0, mo.root_index, [0, 2]]).max()) < 0.3,
            "实际 %s（注意：canonical 只保证「平滑后」的根在原点，"
            "而 posed_joints 是真实根，所以给的是宽松界）"
            % (pos_all[0, mo.root_index, [0, 2]],))
    H.check("接触确实有变化（不是恒 0 或恒 1）",
            0 < int(mo.foot_contacts.sum()) < mo.foot_contacts.size)
    # 逐帧都能建几何（不是只有第 0 帧能用）
    ok_all, err = True, ""
    try:
        for f in (0, 1, mo.frame_count // 2, mo.frame_count - 1):
            mo.pose_at(f)
            mo.contacts_at(f)
    except Exception as exc:
        ok_all, err = False, repr(exc)
    H.check("首帧/中间帧/末帧都能取姿势", ok_all, err)

# ===========================================================================
H.section("hou 侧：真建几何 + KineFX 自己的恒等式")
# ===========================================================================
# ★ 必须在真正的 Python SOP 里 cook：节点外拿到的 geometry() 是**只读**的
#   （hou.GeometryPermissionError）。这样测的也正是真实用法。
# ★ 路径一律转成正斜杠再塞进代码字符串：
#   Windows 路径里 `\U` 会被 Python 当成 unicode 转义 → SyntaxError，
#   而报错发生在 SOP 里、外面只看到「Error while cooking」，很难查。
SOP_CODE = """
import sys
sys.path.insert(0, %(root)r)
from houdini_ai import motion_npz
_m = motion_npz.load(%(npz)r)
_info = motion_npz.build_geometry(hou.pwd().geometry(), _m, %(frame)d)
hou.pwd().setUserData("info", repr(_info))
"""


def fwd(p):
    return p.replace("\\", "/")


def cook_geo(path, frame):
    geo = hou.node("/obj").createNode("geo", "__test_motion_geo__")
    sop = geo.createNode("python", "__emit__")
    sop.parm("python").set(SOP_CODE % {"root": fwd(TOOL_ROOT), "npz": fwd(path), "frame": frame})
    sop.cook(force=True)
    if sop.errors():
        H.check("Python SOP cook 无报错", False, "; ".join(sop.errors()))
    return geo, sop, sop.geometry()


geo, sop, g = cook_geo(p2, 0)
info = eval(sop.userData("info"), {"__builtins__": {}}) if sop.userData("info") else {}
H.check_eq("点数 = 关节数", info.get("points"), m2.joint_count)
H.check_eq("骨头段数 = 关节数 - 1", info.get("bones"), m2.joint_count - 1)
H.check("有 contact 属性", info.get("has_contact"))

pt = g.iterPoints()
names = [p.attribValue("name") for p in pt]
H.check_eq("name 顺序与骨架表一致", names, m2.joint_names)
H.check("path 是层级路径", g.point(0).attribValue("path") == "/Hips")
H.check("子关节的 path 带上了父",
        g.point(m2.index_of("Spine1")).attribValue("path") == "/Hips/Spine1")
H.check("parent_id 指向父点", g.point(m2.index_of("Spine1")).attribValue("parent_id") == 0)

# P 必须与 NPZ 一致（位置不由旋转决定）
p0 = g.point(0).attribValue("P")
H.check("P 与 NPZ 的 posed_joints 一致",
        abs(p0[1] - float(m2.posed_joints[0][0][1])) < 1e-5, "P=%s" % (p0,))

# 换一帧，几何要跟着变（否则「能播」就是假的）
# ★ 换一帧重 cook，几何必须跟着变（否则「能在视口里播」就是假的）
sop.parm("python").set(SOP_CODE % {"root": fwd(TOOL_ROOT), "npz": fwd(p2), "frame": 2})
sop.cook(force=True)
g3 = sop.geometry()
info3 = eval(sop.userData("info"), {"__builtins__": {}})
H.check("换一帧后 P 确实变了",
        abs(float(g3.point(0).attribValue("P")[1]) - p0[1]) > 1e-6,
        "第 0 帧 y=%s，第 2 帧 y=%s" % (p0[1], g3.point(0).attribValue("P")[1]))
H.check_eq("换帧后 contact 也跟着变（第 2 帧左 Foot 着地）",
           info3["contacts"][m2.index_of("LeftFoot")], 1)
H.check("第 1 帧全场不着地 → 全 0", set(m2.contacts_at(1)) == {0})

# ★ KineFX 恒等式（那份约定文档给的交叉验证）：
#   transform_i == localtransform_3x3_i @ transform_parent   （row-vector）
ok_id, worst = True, 0.0
for i in range(m2.joint_count):
    pi = m2.parent_index(i)
    if pi < 0:
        continue
    ti = np.array(g.point(i).attribValue("transform")).reshape(3, 3)
    li = np.array(g.point(i).attribValue("localtransform")).reshape(4, 4)[:3, :3]
    tp_ = np.array(g.point(pi).attribValue("transform")).reshape(3, 3)
    err = float(np.abs(ti - li @ tp_).max())
    worst = max(worst, err)
    if err > 1e-5:
        ok_id = False
        break
H.check("★ KineFX 恒等式成立：transform_i == localtransform_3x3_i @ transform_parent",
        ok_id, "最大误差 %.2e" % worst)

# ★ 约定文档给的第二条交叉验证：由 localtransform 重建位置，应回到 P。
#   这条比「平移在最后一行」强得多——它同时验了平移放在哪、以及局部/世界的关系对不对。
#   行向量约定：localtransform_i[3,:3] @ transform_parent + P_parent == P_i
ok_pos, worst_pos, bad_at = True, 0.0, ""
for i in range(m2.joint_count):
    pi = m2.parent_index(i)
    if pi < 0:
        continue
    lm = np.array(g.point(i).attribValue("localtransform")).reshape(4, 4)
    tp_ = np.array(g.point(pi).attribValue("transform")).reshape(3, 3)
    pp = np.array(g.point(pi).attribValue("P"))
    rebuilt = lm[3, :3] @ tp_ + pp
    err = float(np.abs(rebuilt - np.array(g.point(i).attribValue("P"))).max())
    if err > worst_pos:
        worst_pos, bad_at = err, names[i]
    if err > 1e-5:
        ok_pos = False
        break
H.check("★ 由 localtransform 能重建回 P（localtransform[3,:3] @ parent_rot + parent_P == P）",
        ok_pos, "最大误差 %.2e（在 %s）" % (worst_pos, bad_at))

H.check("localtransform 的第 4 列（除 [3][3]）为 0 —— 平移在最后一行而不是最后一列",
        all(abs(float(np.array(g.point(i).attribValue("localtransform")).reshape(4, 4)[r, 3])) < 1e-9
            for i in range(m2.joint_count) for r in range(3)))

# 所有 transform 都是合法旋转
dets = [float(np.linalg.det(np.array(g.point(i).attribValue("transform")).reshape(3, 3)))
        for i in range(m2.joint_count)]
H.check("所有 transform 的 det 都接近 1（合法旋转）",
        max(abs(d - 1.0) for d in dets) < 1e-5, "det 范围 %.6f ~ %.6f" % (min(dets), max(dets)))

# 层级是用两点多边形表示的
polys = [pr for pr in g.prims() if pr.type() == hou.primType.Polygon]
H.check("层级是多边形（两点线）", len(polys) == m2.joint_count - 1, len(polys))
H.check("都是两点、且不闭合",
        all(len(pr.vertices()) == 2 and not pr.isClosed() for pr in polys))

# contact 写在全部点上，非脚关节为 0
ct = [p.attribValue("contact") for p in g.iterPoints()]
H.check_eq("contact 属性长度 = 关节数", len(ct), m2.joint_count)
H.check("contact 只落在脚上",
        all(v == 0 for i, v in enumerate(ct) if names[i] not in
            ("LeftFoot", "LeftToeBase", "RightFoot", "RightToeBase")))

# 没有 foot_contacts 的 NPZ 不应该有 contact 属性
geo2, sop2, g2 = cook_geo(p1, 0)
H.check("NPZ 不带 foot_contacts 时就不写 contact 属性",
        g2.findPointAttrib("contact") is None,
        "属性列表：%s" % [a.name() for a in g2.pointAttribs()])
H.check("不带 contact 时仍有点与骨头",
        len(g2.points()) == m.joint_count and len(g2.prims()) == m.joint_count - 1)
geo2.destroy()

geo.destroy()

# ===========================================================================
H.section("describe：给人看的摘要")
# ===========================================================================
d = motion_npz.describe(p2)
H.check("摘要里有骨架名与关节数", "somaskel30" in d and "30 关节" in d, d)
H.check("摘要里有脚部接触的信息", "脚部接触" in d, d)

H.finish("test_motion_npz")
