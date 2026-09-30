# -*- coding: utf-8 -*-
"""Kimodo 的 NPZ → KineFX 骨架。

这是动作线的落地端：后端（官方 Kimodo）吐一个 NPZ，这里把它变成 Houdini 里
能播的骨架。**纯逻辑与 hou 依赖分开**：`load` / `pose_at` / `contacts_at` /
`frame_index_for` 不碰 hou，可以离线测；只有 `build_geometry` 要 hou。

## 三个必须记住的事实（都有出处，不是猜的）

1. **KineFX 的 `transform` 是「世界」旋转，不是局部。**
   本机实测：hips→spine→head 链上把 spine 绕 X 转 45°，
   **spine 与 head 的 `transform` 完全相同**，且 head 的世界位置被正确带动。
   只有「世界变换」这一种解释自洽。

2. **Kimodo 用列向量/列主序，Houdini 用行向量/行主序 → 矩阵要转置。**
   所以 `transform_houdini = global_rot_mats.T`。

3. **关节数可能是 30，也可能是 77。**
   官方文档说 SOMA 的 NPZ 导出是 77；但官方 demo 里那批样例 NPZ **实测是 30**
   （`kimodo/assets/demo/examples/kimodo-soma-rp/*/motion.npz` → `(150, 30, 3)`）。
   两种都得吃，所以骨架表按关节数反查。
"""

import os

import numpy as np

from . import soma_skeleton

# NPZ 里我们认识的键。缺 posed_joints / global_rot_mats 就没法重建骨架。
REQUIRED_KEYS = ("posed_joints", "global_rot_mats")
OPTIONAL_KEYS = ("local_rot_mats", "root_positions", "smooth_root_pos",
                 "global_root_heading", "foot_contacts")

_DATA_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "data")
_TPOSE_CACHE = {}


def tpose_offsets(skel_name):
    """SOMA 的 T-pose 轴向偏移 `tp_i`，形状 (J,3,3)。取不到返回 None。

    这些数由 `scripts/extract_soma_tpose.py` 从 Kimodo 自己的
    `standard_t_pose_global_offsets_rots.p` 抽出来，并带一次轴向直方图自校验。
    """
    if skel_name in _TPOSE_CACHE:
        return _TPOSE_CACHE[skel_name]
    n = len(soma_skeleton.SKELETONS.get(skel_name, {}).get("joints", []))
    got = None
    if n:
        p = os.path.join(_DATA_DIR, "soma_tpose_offsets_%d.npy" % n)
        if os.path.isfile(p):
            try:
                got = np.load(p).astype(np.float64)
            except Exception:
                got = None
    _TPOSE_CACHE[skel_name] = got
    return got


def tpose_positions(skel_name):
    """SOMA 的 T-pose 关节世界位置，形状 (J,3)，**单位米**。取不到返回 None。

    这是 rest / capture 姿势的来源（KineFX 的 `rest_transform` 与绑定时要的 A/T-pose）。
    同样由 `scripts/extract_soma_tpose.py` 从 Kimodo 的
    `somaskel77_standard_tpose.bvh` 抽出（BVH 是厘米，脚本里除过 100）。
    """
    key = "pos:" + skel_name
    if key in _TPOSE_CACHE:
        return _TPOSE_CACHE[key]
    n = len(soma_skeleton.SKELETONS.get(skel_name, {}).get("joints", []))
    got = None
    if n:
        p = os.path.join(_DATA_DIR, "soma_tpose_joints_%d.npy" % n)
        if os.path.isfile(p):
            try:
                got = np.load(p).astype(np.float64)
            except Exception:
                got = None
    _TPOSE_CACHE[key] = got
    return got


def to_kinefx_transform(global_rot, tp):
    """Kimodo 的全局旋转 → KineFX 的 `transform`（float[9]，行主序）。

    两步，顺序不能反：
      ① **骨头对齐**：`R_bone = R_global @ tp`（**右乘**）。
         SOMA 的 rest 是「世界轴对齐」（global 全为 identity），而 KineFX 要
         「骨头对齐」（某个本地轴指向子节点）。不校正的话：本机实测轴向直方图
         会散在 ±X/±Y/±Z（主轴只占 72%），校正后压在一条轴上（97%）。
         ★ 写成 `tp @ R_global` 会得到一堆混乱轴向——这是那份约定文档专门警告过的。
      ② **转置**：Kimodo 列向量 → Houdini 行向量。
    """
    r = np.asarray(global_rot, dtype=np.float64)
    if tp is not None:
        r = r @ np.asarray(tp, dtype=np.float64)
    return r.T


def from_kinefx_transform(kinefx_transform, tp):
    """KineFX 的 `transform` 反推回 Kimodo 的全局旋转（发约束时用）。

    是 `to_kinefx_transform` 的逆：`R = T.T @ tp.T`（tp 是正交阵，所以逆 = 转置）。
    """
    t = np.asarray(kinefx_transform, dtype=np.float64).reshape(3, 3)
    r = t.T
    if tp is not None:
        r = r @ np.asarray(tp, dtype=np.float64).T
    return r


class NpzError(Exception):
    """NPZ 读不了 / 内容对不上。消息要能直接给用户看。"""


class NpzMotion(object):
    """一个 NPZ 里的动作。所有数组保持 **Kimodo 原始约定**（列向量）。"""

    def __init__(self, path, posed_joints, global_rot_mats, foot_contacts,
                 skel_name, joints, extra=None):
        self.path = path
        self.posed_joints = posed_joints          # (T, J, 3) 世界位置，米
        self.global_rot_mats = global_rot_mats    # (T, J, 3, 3) 世界旋转，Kimodo 约定
        self.foot_contacts = foot_contacts        # (T, K) bool 或 None
        self.skel_name = skel_name
        self.joints = joints                      # [(名, 父名), ...]
        self.extra = extra or {}
        self.warnings = []
        # T-pose 轴向偏移；缺了也能跑，只是骨头朝向会是「世界轴对齐」的弯的
        self.tp = tpose_offsets(skel_name)
        if self.tp is None:
            self.warnings.append(
                "没找到 %s 的 T-pose 轴向偏移表，骨架能显示但骨头朝向不会是 KineFX 期望的"
                "「骨头对齐」——绑定时可能行为异常。"
                "跑 scripts/extract_soma_tpose.py 生成它。" % skel_name)
        elif self.tp.shape[0] != self.joint_count:
            self.warnings.append("T-pose 偏移表有 %d 个关节，NPZ 有 %d 个，对不上，已忽略"
                                 % (self.tp.shape[0], self.joint_count))
            self.tp = None

    # ---- 基本量 ----------------------------------------------------------

    @property
    def frame_count(self):
        return int(self.posed_joints.shape[0])

    @property
    def joint_count(self):
        return int(self.posed_joints.shape[1])

    @property
    def joint_names(self):
        return [n for n, _ in self.joints]

    def index_of(self, name):
        for i, (n, _) in enumerate(self.joints):
            if n == name:
                return i
        return -1

    @property
    def root_index(self):
        for i, (_, p) in enumerate(self.joints):
            if p is None:
                return i
        return -1

    def parent_index(self, i):
        p = self.joints[i][1]
        return -1 if p is None else self.index_of(p)

    # ---- 取某一帧 --------------------------------------------------------

    def frame_clamped(self, frame_index):
        """把帧号夹到有效范围（宁可停在第一/最后一帧，也不要整个 cook 失败）。"""
        return max(0, min(int(frame_index), self.frame_count - 1))

    def pose_at(self, frame_index):
        """某一帧供 KineFX 用的 (世界位置 (J,3), `transform` (J,3,3))。

        `transform` 是**骨头对齐 + 行主序**的（见 `to_kinefx_transform`）。
        """
        i = self.frame_clamped(frame_index)
        pos = self.posed_joints[i]
        tp = self.tp
        if tp is None:
            rot = self.global_rot_mats[i].transpose(0, 2, 1)
        else:
            rot = np.stack([to_kinefx_transform(self.global_rot_mats[i][j], tp[j])
                            for j in range(self.joint_count)])
        return pos, rot

    def pose_at_kimodo(self, frame_index):
        """某一帧的**原始** Kimodo 约定数据 (位置 (J,3), 全局旋转 (J,3,3) 列主序)。

        发约束给后端时用这个——那边要的是它自己的约定，不能带 tp 校正。
        """
        i = self.frame_clamped(frame_index)
        return self.posed_joints[i], self.global_rot_mats[i]

    def contacts_at(self, frame_index):
        """某一帧每个关节的接触标志（int 列表，长度 = 关节数）。

        官方 NPZ 是 **4 通道** `[左heel, 左toe, 右heel, 右toe]`；
        SOMA77 展开版是 **6 通道**（多了两个 ToeEnd，值复制自 ToeBase）。
        这里**按骨架自己的脚链长度**去映射，不写死 4 或 6：
        左侧第 i 个通道 → `left_foot_joint_names[i]`，右侧同理。
        """
        n = self.joint_count
        out = [0] * n
        if self.foot_contacts is None or self.foot_contacts.shape[0] == 0:
            return out
        i = max(0, min(int(frame_index), self.foot_contacts.shape[0] - 1))
        row = self.foot_contacts[i]
        chains = soma_skeleton.SKELETONS[self.skel_name]["chains"]
        per_side = len(row) // 2
        for side, key in ((0, "left_foot_joint_names"), (1, "right_foot_joint_names")):
            names = chains.get(key, [])
            for c in range(per_side):
                j = self.index_of(names[c]) if c < len(names) else -1
                if j >= 0:
                    out[j] = 1 if row[side * per_side + c] else 0
        return out

    def contact_channel_names(self):
        """接触通道对应的关节名，只用于日志/提示。"""
        if self.foot_contacts is None:
            return []
        k = int(self.foot_contacts.shape[1]) if self.foot_contacts.ndim > 1 else 0
        chains = soma_skeleton.SKELETONS[self.skel_name]["chains"]
        per_side = k // 2
        names = []
        for side, key in ((0, "left_foot_joint_names"), (1, "right_foot_joint_names")):
            ch = chains.get(key, [])
            names += [ch[c] if c < len(ch) else "?" for c in range(per_side)]
        return names

    # ---- 帧率换算 --------------------------------------------------------

    def frame_index_for(self, scene_frame, first_scene_frame=1, scene_fps=24.0, kimodo_fps=30.0):
        """Houdini 场景帧 → NPZ 里的帧下标（**0-based**）。

        Kimodo 是 30fps，场景可能是 24fps。不换算的话时长直接是错的。
        """
        offset = (float(scene_frame) - float(first_scene_frame)) * (float(kimodo_fps) / float(scene_fps or kimodo_fps))
        return int(round(offset))


def load(path):
    """读一个 Kimodo NPZ。失败抛 `NpzError`，消息面向用户可读。"""
    if not path:
        raise NpzError("没有指定 NPZ 路径")
    if not os.path.isfile(path):
        raise NpzError("NPZ 文件不存在：%s" % path)
    try:
        z = np.load(path, allow_pickle=True)
        keys = set(z.files)
    except Exception as exc:
        raise NpzError("NPZ 读不了（%s）：%s" % (type(exc).__name__, exc))

    missing = [k for k in REQUIRED_KEYS if k not in keys]
    if missing:
        raise NpzError(
            "NPZ 里缺 %s。这个文件里有的键是：%s。\n"
            "（Kimodo 的 NPZ 至少要有 posed_joints 与 global_rot_mats）"
            % ("、".join(missing), "、".join(sorted(keys))))

    pj = np.asarray(z["posed_joints"], dtype=np.float64)
    gr = np.asarray(z["global_rot_mats"], dtype=np.float64)
    # ★ 模型原始的 NPZ 是 **(批量, 帧, 关节, 坐标)** 4 维；官方 CLI 在单样本时
    #   会把批量维挤掉存成 3 维（generate.py:345-352），我们的 runner 也这么做。
    #   但万一有人拿来一个没挤过的（批量=1），这里兜一层，免得把「批量」
    #   当成「帧数」——那会静默读成 1 帧，最难查。
    if pj.ndim == 4 and pj.shape[0] == 1:
        pj = pj[0]
    if gr.ndim == 5 and gr.shape[0] == 1:
        gr = gr[0]
    if pj.ndim != 3 or pj.shape[-1] != 3:
        raise NpzError("posed_joints 形状应为 (帧, 关节, 3)，实际 %s" % (pj.shape,))
    if gr.ndim != 4 or gr.shape[-2:] != (3, 3):
        raise NpzError("global_rot_mats 形状应为 (帧, 关节, 3, 3)，实际 %s" % (gr.shape,))
    if pj.shape[:2] != gr.shape[:2]:
        raise NpzError("posed_joints %s 与 global_rot_mats %s 的帧数/关节数对不上"
                       % (pj.shape, gr.shape))

    n_joints = int(pj.shape[1])
    skel_name, joints = soma_skeleton.joints_for_count(n_joints)
    if skel_name is None:
        known = "、".join("%s(%d)" % (k, len(v["joints"]))
                          for k, v in sorted(soma_skeleton.SKELETONS.items()))
        raise NpzError(
            "这个 NPZ 有 %d 个关节，我们不认识这个骨架。已知的：%s。\n"
            "（SOMA 只支持 30 与 77 两种）" % (n_joints, known))

    fc = None
    if "foot_contacts" in keys:
        fc = np.asarray(z["foot_contacts"])
        if fc.ndim == 3 and fc.shape[0] == 1:      # 同样是没挤过的批量维
            fc = fc[0]
        if fc.ndim != 2 or fc.shape[0] != pj.shape[0]:
            fc = None

    extra = {k: np.asarray(z[k]) for k in OPTIONAL_KEYS if k in keys and k != "foot_contacts"}

    m = NpzMotion(path, pj, gr, fc, skel_name, joints, extra)

    # 把不一致的地方记成警告，不阻断——用户需要知道但不必因此看不到动作
    if fc is not None:
        chains = soma_skeleton.SKELETONS[skel_name]["chains"]
        expect = len(chains.get("left_foot_joint_names", [])) + len(chains.get("right_foot_joint_names", []))
        if int(fc.shape[1]) != expect:
            m.warnings.append(
                "foot_contacts 有 %d 个通道，%s 的脚链期望 %d 个 —— 多出来的通道会被忽略"
                % (fc.shape[1], skel_name, expect))
    return m


# ===========================================================================
# 以下是 hou 相关的部分
# ===========================================================================

def _mat4_from_rot_pos(rot3, pos):
    """3x3 旋转 + 位置 → 4x4，**平移放最后一行**（Houdini 行向量约定）。"""
    m = np.eye(4, dtype=np.float64)
    m[:3, :3] = rot3
    m[3, :3] = pos
    return m


def rest_pose(skel_name):
    """rest(T-pose) 的 (位置 (J,3), KineFX `transform` (J,3,3))。

    ★ **rest 就是「全局旋转全取 identity」的那个姿势**，所以直接复用同一条
    `to_kinefx_transform` 路径：`transform = (I @ tp).T = tp.T`。这样动画输出与
    rest 输出天然同一套轴向约定——那份约定文档强调过，两者不一致会让绑定行为异常。
    """
    joints = soma_skeleton.SKELETONS.get(skel_name, {}).get("joints", [])
    pos = tpose_positions(skel_name)
    tp = tpose_offsets(skel_name)
    if pos is None:
        return None, None
    n = len(joints)
    ident = np.eye(3, dtype=np.float64)
    rot = np.stack([to_kinefx_transform(ident, None if tp is None else tp[j])
                    for j in range(n)])
    return pos, rot


def _skin_npz_path():
    """本机上 Kimodo 自带的蒙皮网格在哪。取不到返回 None。"""
    for p in (r"D:/kimodo-motion/src/kimodo/kimodo/assets/skeletons/somaskel77/skin_standard.npz",
              os.path.join(os.environ.get("KIMODO_ASSETS", ""),
                           "skeletons/somaskel77/skin_standard.npz")):
        if p and os.path.isfile(p):
            return p
    return None


def load_skin(path=None):
    """读 Kimodo 自带的 SOMA77 蒙皮网格。取不到返回 None。

    里面有：绑定姿势的顶点、三角面、77 个关节在绑定姿势下的世界变换、
    以及每顶点最多 8 根骨头的蒙皮权重（已归一化）。

    ★ 为什么**不能**直接把这份权重写成 KineFX 的 `boneCapture`：
      那个属性要挂一张 IndexPairPropertyTable，而 `hou.IndexPairPropertyTable`
      是抽象类（`No constructor defined - class is abstract`），hython 里建不出来。
      → 所以蒙皮改成让 Houdini 自己算（jointcaptureproximity）。
    """
    p = path or _skin_npz_path()
    if not p or not os.path.isfile(p):
        return None
    try:
        z = np.load(p, allow_pickle=True)
        out = {
            "path": p,
            "vertices": np.asarray(z["bind_vertices"], dtype=np.float64),
            "faces": np.asarray(z["faces"], dtype=np.int64),
            "bind_transform": np.asarray(z["bind_rig_transform"], dtype=np.float64),
            "names": [str(x) for x in z["rig_joint_names"]],
        }
    except Exception:
        return None
    return out


def capture_pose(skel_name="somaskel77", skin=None):
    """绑定（capture）姿势的 (位置 (J,3), KineFX `transform` (J,3,3))。

    位置直接取 `bind_rig_transform` 的平移列；旋转过**同一条** `to_kinefx_transform`，
    这样 capture 与动画输出天然同一套轴向。
    （顺带一个让人安心的性质：jointdeform 只用「动画 × capture 的逆」，
      而 `(R_anim tp)(R_capt tp)^-1 = R_anim R_capt^T` —— **tp 在相对量里自己约掉**，
      所以就算 tp 选错，蒙皮也不会歪。）
    """
    skin = skin or load_skin()
    if skin is None:
        return None, None
    bt = skin["bind_transform"]
    n = bt.shape[0]
    pos = bt[:, :3, 3].copy()
    tp = tpose_offsets(skel_name)
    if tp is None or tp.shape[0] != n:
        tp = None
    ident = np.eye(3, dtype=np.float64)
    rot = np.stack([to_kinefx_transform(bt[j, :3, :3], None if tp is None else tp[j])
                    for j in range(n)])
    return pos, rot


def build_skin_geometry(geo):
    """把绑定姿势的蒙皮网格写进一个 hou.Geometry。返回信息 dict。"""
    import hou

    skin = load_skin()
    if skin is None:
        raise NpzError("找不到 Kimodo 自带的蒙皮网格 skin_standard.npz")
    v, f = skin["vertices"], skin["faces"]
    geo.clear()
    pts = [geo.createPoint() for _ in range(len(v))]
    for i, pt in enumerate(pts):
        pt.setPosition(hou.Vector3(float(v[i][0]), float(v[i][1]), float(v[i][2])))
    for tri in f:
        poly = geo.createPolygon()
        poly.setIsClosed(True)
        for idx in tri:
            poly.addVertex(pts[int(idx)])
    return {"points": len(v), "prims": len(f), "path": skin["path"]}


def write_skeleton(geo, joints, pos, rot, contacts=None, with_local=True):
    """把一根骨架的**一帧**写进一个 hou.Geometry。动画与 rest 共用这一段。

    产出与那个桥的 output 0 同形：
      * 每个关节一个 point
      * 点属性：name / path / parent_id / transform(float[9] 世界旋转) / localtransform(float[16])
      * contact（int，可选）：写在**全部**关节上，非脚关节恒 0
      * 层级：每对父子之间一条**两点多边形**（KineFX 就是这个约定）

    返回一个 dict 供调用方写状态用。
    """
    import hou

    n = len(joints)
    names = [nm for nm, _ in joints]
    index = {nm: i for i, nm in enumerate(names)}
    parents = [(-1 if p is None else index.get(p, -1)) for _, p in joints]

    geo.clear()
    pts = [geo.createPoint() for _ in range(n)]

    a_name = geo.addAttrib(hou.attribType.Point, "name", "")
    a_path = geo.addAttrib(hou.attribType.Point, "path", "")
    a_pid = geo.addAttrib(hou.attribType.Point, "parent_id", -1)
    a_tr = geo.addAttrib(hou.attribType.Point, "transform", (0.0,) * 9)
    a_lt = geo.addAttrib(hou.attribType.Point, "localtransform", (0.0,) * 16) if with_local else None
    a_ct = geo.addAttrib(hou.attribType.Point, "contact", 0) if contacts is not None else None

    # 路径串：/根/子/孙
    paths = {}
    for i, nm in enumerate(names):
        pi = parents[i]
        paths[nm] = "/" + nm if pi < 0 else paths[names[pi]] + "/" + nm

    for i, nm in enumerate(names):
        pt = pts[i]
        pt.setPosition(hou.Vector3(float(pos[i][0]), float(pos[i][1]), float(pos[i][2])))
        pt.setAttribValue(a_name, nm)
        pt.setAttribValue(a_path, paths[nm])
        pi = parents[i]
        pt.setAttribValue(a_pid, pi)
        r = rot[i]
        pt.setAttribValue(a_tr, tuple(float(v) for v in r.reshape(-1)))
        if a_lt is not None:
            w = _mat4_from_rot_pos(r, pos[i])
            lt = w if pi < 0 else w.dot(np.linalg.inv(_mat4_from_rot_pos(rot[pi], pos[pi])))
            pt.setAttribValue(a_lt, tuple(float(v) for v in lt.reshape(-1)))
        if a_ct is not None:
            pt.setAttribValue(a_ct, int(contacts[i]))

    # 层级：父子之间一条两点线
    made = 0
    for i in range(n):
        pi = parents[i]
        if pi >= 0:
            poly = geo.createPolygon()
            poly.setIsClosed(False)
            poly.addVertex(pts[pi])
            poly.addVertex(pts[i])
            made += 1

    return {"points": n, "bones": made, "has_contact": contacts is not None}


def build_capture_geometry(geo, skel_name="somaskel77", with_local=True):
    """把**绑定（capture）姿势**的骨架写进一个 hou.Geometry。

    这是 jointdeform 的 input 1（网格绑在哪个姿势上）。和 rest(T-pose) 不是一回事：
    蒙皮网格是在 A-pose 下绑的，所以 capture 要用 skin_standard.npz 里的绑定变换。
    """
    joints = soma_skeleton.SKELETONS.get(skel_name, {}).get("joints")
    pos, rot = capture_pose(skel_name)
    if joints is None or pos is None:
        raise NpzError("没有 %s 的绑定姿势素材（skin_standard.npz）" % skel_name)
    info = write_skeleton(geo, joints, pos, rot, None, with_local)
    info["skel_name"] = skel_name
    info["kind"] = "capture"
    return info


def build_rest_geometry(geo, skel_name, with_local=True):
    """把 SOMA 的 rest(T-pose) 骨架写进一个 hou.Geometry。"""
    joints = soma_skeleton.SKELETONS.get(skel_name, {}).get("joints")
    pos, rot = rest_pose(skel_name)
    if joints is None or pos is None:
        raise NpzError("没有 %s 的 T-pose 素材，跑 scripts/extract_soma_tpose.py 生成" % skel_name)
    info = write_skeleton(geo, joints, pos, rot, None, with_local)
    info["skel_name"] = skel_name
    return info


def build_geometry(geo, motion, frame_index, with_local=True, with_contact=True):
    """把 NPZ 里某一帧的动画骨架写进一个 hou.Geometry。"""
    pos, rot = motion.pose_at(frame_index)
    contacts = motion.contacts_at(frame_index) if (with_contact and motion.foot_contacts is not None) else None
    return _legacy_build(geo, motion, pos, rot, contacts, frame_index, with_local)


def _legacy_build(geo, motion, pos, rot, contacts, frame_index, with_local):
    info = write_skeleton(geo, motion.joints, pos, rot, contacts, with_local)
    info.update({
        "frame_index": int(frame_index),
        "contacts": contacts,
        "skel_name": motion.skel_name,
    })
    return info


def describe(path):
    """读一个 NPZ 并给一段人能看的摘要（面板日志与节点注释用）。"""
    m = load(path)
    lines = [
        "%s · %d 关节 · %d 帧" % (m.skel_name, m.joint_count, m.frame_count),
        "30fps 下约 %.2f 秒" % (m.frame_count / 30.0),
    ]
    if m.foot_contacts is not None:
        names = m.contact_channel_names()
        planted = int(np.asarray(m.foot_contacts).sum())
        lines.append("脚部接触：%d 个通道（%s），共 %d 个着地标记"
                     % (m.foot_contacts.shape[1], " / ".join(names), planted))
    else:
        lines.append("脚部接触：这个 NPZ 没带 foot_contacts")
    for w in m.warnings:
        lines.append("⚠ " + w)
    return "\n".join(lines)
