# -*- coding: utf-8 -*-
"""动作线的 Houdini 侧：搭网络、读姿势、把 NPZ 落到场景里。

`motion_npz.py` 管「NPZ ↔ 骨架几何」，这一层管「Houdini 里怎么用」。
所有函数都是普通 hou 调用，不含 UI（UI 在 motion_panel.py）。

## 两条数据流（方向相反，别混）

    后端 → 场景：  NPZ --motion_npz.build_geometry--> 骨架几何（视口里能播）
    场景 → 后端：  摆好的骨架 --read_pose--> 世界位置 + 全局旋转 --> 约束

第二条是 `from_kinefx_transform` 的逆：KineFX 的 `transform` 是**骨头对齐**的，
Kimodo 要的是**世界轴对齐**的原始全局旋转，所以要乘回 tp。
"""

import os

import numpy as np

from . import motion_npz, soma_skeleton

# 动作节点的容器名（沿用 AI 节点那套命名习惯）
CONTAINER_CANDIDATES = ("motion_ai", "motion_ai1", "subnet_motion")
POSE_RIG_NAME = "pose_rig"
SKEL_NAME = "rest_skeleton"
OUT_NAME = "out"


class RigError(Exception):
    """给用户看的错误。"""


# ===========================================================================
# 读姿势（场景 → 后端）
# ===========================================================================

def read_pose(geo, skel_name=None, tp=None):
    """读一根**摆好的骨架**，返回 (位置 (J,3), Kimodo 全局旋转 (J,3,3))。

    * 位置直接取点的 `P`（KineFX 的 `P` 就是世界位置、米制）
    * 旋转取点的 `transform`（**世界旋转、骨头对齐、行主序**），
      再用 `from_kinefx_transform` 乘回 tp → Kimodo 要的「世界轴对齐」全局旋转
    * 关节顺序**按骨架表**排，不按几何里的点序——这样后端拿到的顺序是确定的

    `skel_name` 不给就按点数反推。点数不认识就抛 RigError。
    """
    import hou

    pts = list(geo.points())
    if not pts:
        raise RigError("输入骨架是空的。检查上游节点有没有报错（节点上按 Ctrl+E 看）？")

    n = len(pts)
    if skel_name is None:
        skel_name, _ = soma_skeleton.joints_for_count(n)
        if skel_name is None:
            known = "、".join("%s(%d)" % (k, len(v["joints"]))
                              for k, v in sorted(soma_skeleton.SKELETONS.items()))
            raise RigError("输入骨架有 %d 个点，不是我们认识的 SOMA 骨架（%s）" % (n, known))
    joints = soma_skeleton.SKELETONS[skel_name]["joints"]
    expect = [nm for nm, _ in joints]

    # 按名字对齐（不靠点序）——用户可能在中途插过删除关节之类的节点
    by_name = {}
    for pt in pts:
        nm = pt.attribValue("name")
        if nm:
            by_name.setdefault(nm, pt)
    if len(by_name) != n or any(nm not in by_name for nm in expect):
        missing = [nm for nm in expect if nm not in by_name][:6]
        extra = [nm for nm in by_name if nm not in expect][:6]
        raise RigError(
            "输入骨架的关节名与 %s 对不上（%d 个点）。\n缺：%s\n多出来：%s\n"
            "（这一般是上游不是 SOMA 骨架，或用错了输出）" % (skel_name, n, missing, extra))

    if tp is None:
        tp = motion_npz.tpose_offsets(skel_name)

    pos = np.zeros((n, 3), dtype=np.float64)
    rot = np.zeros((n, 3, 3), dtype=np.float64)
    for i, nm in enumerate(expect):
        pt = by_name[nm]
        p = pt.attribValue("P")
        pos[i] = (float(p[0]), float(p[1]), float(p[2]))
        t = pt.attribValue("transform")
        T = np.array(t, dtype=np.float64).reshape(3, 3)
        rot[i] = motion_npz.from_kinefx_transform(T, None if tp is None else tp[i])
    return pos, rot


def read_pose_geo(path_or_geo):
    """便利用：从路径或几何读。给面板/测试用。"""
    import hou
    if hasattr(path_or_geo, "points"):
        return read_pose(path_or_geo)
    node = hou.node(path_or_geo)
    if node is None:
        raise RigError("找不到节点 %s" % path_or_geo)
    return read_pose(node.geometry())


# ===========================================================================
# 建网络（场景侧）
# ===========================================================================

def _unique_name(parent, base):
    if parent.node(base) is None:
        return base
    for i in range(1, 100):
        cand = "%s%d" % (base, i)
        if parent.node(cand) is None:
            return cand
    raise RigError("名字被占满了：%s" % base)


def build_pose_rig(parent, skel_name="somaskel30", skel_node_name=SKEL_NAME):
    """建一根「可摆的 rest 骨架」：rest 骨架 SOP + Rig Pose。

    这是关键件——用户要在这上面摆关键帧。
    **必须用 SOMA 自己的 T-pose**：姿势→约束的转换要反演节点自己的前向变换，
    换成任意一根骨架，转换就错了（这条是那个桥的 README 专门警告过的）。

    返回 (骨架节点, RigPose 节点)。
    """
    import hou

    # 用 Python SOP 吐 rest 骨架，这样不依赖任何外部素材文件
    skel = parent.createNode("python", _unique_name(parent, skel_node_name))
    skel.parm("python").set(_fill(_REST_SOP_CODE, tool_root=_tool_root().replace("\\", "/"),
                                  skel=skel_name))
    skel.setColor(hou.Color((0.35, 0.5, 0.75)))
    skel.setComment("SOMA %s 的 T-pose 骨架（动作线的起点）" % skel_name)
    skel.setGenericFlag(hou.nodeFlag.DisplayComment, True)

    rig = parent.createNode("kinefx::rigpose", _unique_name(parent, POSE_RIG_NAME))
    rig.setInput(0, skel)
    rig.setColor(hou.Color((0.55, 0.75, 0.45)))
    rig.setComment("在这上面摆姿势：拖关节，然后回面板点「记录当前姿势」")
    rig.setGenericFlag(hou.nodeFlag.DisplayComment, True)
    return skel, rig


def build_motion_network(parent, skel_name="somaskel30"):
    """建完整动作网络：rest 骨架 → Rig Pose →【动作节点】→ out。

    返回一个 dict：容器、各节点、以及动作节点。
    """
    import hou

    cname = _unique_name(parent, "motion_ai")
    container = hou.node(parent.path()).createNode("subnet", cname)

    skel, rig = build_pose_rig(container, skel_name)
    motion = container.createNode("python", _unique_name(container, "motion"))
    motion.parm("python").set(_fill(_MOTION_SOP_CODE, tool_root=_tool_root().replace("\\", "/")))
    motion.setInput(1, rig)          # 输入 1 = 摆好的骨架（发约束用）
    _add_motion_parms(motion)
    motion.setColor(hou.Color((0.75, 0.45, 0.45)))
    motion.setComment("动作落地节点：读 NPZ 吐骨架。\\n"
                      "路径与帧率由面板或下面的参数控制；\\n"
                      "操作（生成/停止/记录姿势）都在面板上。")
    motion.setGenericFlag(hou.nodeFlag.DisplayComment, True)
    out = container.createNode("null", OUT_NAME)
    out.setInput(0, motion)
    out.setDisplayFlag(True)
    out.setRenderFlag(True)

    # 蒙皮支链（加分项；素材缺失时安静跳过，不挡骨架）
    skin = build_skin_chain(container, motion)

    container.layoutChildren()
    result = {"container": container, "rest": skel, "rigpose": rig,
              "motion": motion, "out": out, "skin": skin}
    return result


_SKIN_MESH_CODE = '''
import sys
sys.path.insert(0, "__TOOL_ROOT__")
from houdini_ai import motion_npz
motion_npz.build_skin_geometry(hou.pwd().geometry())
'''

_CAPTURE_CODE = '''
import sys
sys.path.insert(0, "__TOOL_ROOT__")
from houdini_ai import motion_npz
motion_npz.build_capture_geometry(hou.pwd().geometry(), "somaskel77")
'''


def build_skin_chain(container, motion_node=None, skel_name="somaskel77"):
    """建「蒙皮网格跟着动」那条支链。

        skin_mesh ─┐
                   ├→ capture(抓取权重) ─→ deform(被 motion 驱动)
        capture_pose ┘                    ↑
                                    motion（NPZ 动画骨架）

    ★ 为什么蒙皮权重让 Houdini 现算，而不是用 Kimodo 自带的：
      Kimodo 的 skin_standard.npz 里确实有现成的 lbs 权重，但要写成 KineFX 的
      `boneCapture` 得挂一张 IndexPairPropertyTable，而
      `hou.IndexPairPropertyTable` 是**抽象类**（No constructor defined），
      hython 里建不出来。所以改成让 jointcaptureproximity 自己算——
      本机实测：18056 点 / 36108 面，权重算得出来，恒等对照位移 2.4e-7 米。

    ★ `maxinfluences` 要设 8：默认只有 2，实测参与变形的点从 3961 掉到 2027，
      形变会明显生硬。

    ⚠ 需要 **77 关节**的动画骨架（网格是按 77 绑的，按名字匹配关节）。
      真实 CLI 输出的就是 77；官方 demo 里那批样例 NPZ 是切片过的 30，配不上。

    返回 dict；素材缺失时返回 None（不报错——蒙皮是加分项，不该挡住骨架）。
    """
    import hou

    if motion_npz.load_skin() is None:
        return None

    root = _tool_root().replace("\\", "/")
    mesh = container.createNode("python", _unique_name(container, "skin_mesh"))
    mesh.parm("python").set(_fill(_SKIN_MESH_CODE, tool_root=root))
    mesh.setColor(hou.Color((0.6, 0.55, 0.4)))
    mesh.setComment("Kimodo 自带的 SOMA77 蒙皮网格（绑定姿势，A-pose）")
    mesh.setGenericFlag(hou.nodeFlag.DisplayComment, True)

    cap = container.createNode("python", _unique_name(container, "capture_pose"))
    cap.parm("python").set(_fill(_CAPTURE_CODE, tool_root=root))
    cap.setColor(hou.Color((0.6, 0.55, 0.4)))
    cap.setComment("绑定姿势的骨架（jointdeform 的 input 1）")
    cap.setGenericFlag(hou.nodeFlag.DisplayComment, True)

    grab = container.createNode("kinefx::jointcaptureproximity",
                                _unique_name(container, "capture"))
    grab.setInput(0, mesh)
    grab.setInput(1, cap)
    try:
        grab.parm("maxinfluences").set(8)     # ★ 默认 2 会明显生硬
    except Exception:
        pass
    grab.setColor(hou.Color((0.6, 0.55, 0.4)))

    deform = container.createNode("kinefx::jointdeform", _unique_name(container, "deform"))
    deform.setInput(0, grab)
    deform.setInput(1, cap)
    deform.setInput(2, motion_node if motion_node is not None else container.node("motion"))
    deform.setColor(hou.Color((0.72, 0.5, 0.72)))
    deform.setComment("网格跟着生成的动画动。\\n"
                      "⚠ 需要 77 关节的 NPZ（网格按 77 绑的）。\\n"
                      "想看它就点这个节点再按视图里的显示标志。")
    deform.setGenericFlag(hou.nodeFlag.DisplayComment, True)
    return {"skin_mesh": mesh, "capture_pose": cap, "capture": grab, "deform": deform}


def find_motion_containers():
    """找出场景里所有「动作容器」（含 pose_rig + motion 的 Subnet），返回路径列表。

    判据用结构而不是名字——用户可能把容器改过名。
    """
    import hou

    out = []
    for node in hou.node("/obj").allSubChildren():
        try:
            if node.type().name() != "subnet":
                continue
            if node.node("pose_rig") is not None and node.node("motion") is not None:
                out.append(node.path())
        except Exception:
            continue
    return sorted(out)


def create_motion_container(skel_name="somaskel30", parent=None):
    """在 /obj 下建一个动作容器。返回容器节点。

    ★ 走 Houdini 自己的放置交互（虚影 + 左键落位），不是裸 createNode——
      这样用户能自己挑位置，和 Tab 菜单建节点的手感一致。
      用户按 Esc 取消时抛 hou.OperationInterrupted，由调用方安静处理。
    """
    import hou

    obj = parent or hou.node("/obj")
    net = build_motion_network(obj, skel_name)
    try:
        net["container"].setPosition(net["container"].position())
    except Exception:
        pass
    return net["container"]


def _add_motion_parms(node):
    """动作节点的参数——**只留三个**（用户明确要求参数面板要干净）。

    状态、进度、报错一律走节点注释（setComment），不往参数面板塞。
    其余操作（生成/停止/记录姿势）都在 Python 面板上。
    """
    import hou

    group = node.parmTemplateGroup()
    group.append(hou.StringParmTemplate(
        "npzpath", "NPZ 文件", 1, default_value=("",),
        string_type=hou.stringParmType.FileReference,
        tags={"filechooser_mode": "read", "filechooser_pattern": "*.npz"}))
    group.append(hou.FloatParmTemplate("kimodo_fps", "素材帧率", 1, default_value=(30.0,),
                                       min=1.0, max=240.0))
    group.append(hou.IntParmTemplate("first_frame", "起始帧", 1, default_value=(1,),
                                     min=-10000, max=100000))
    node.setParmTemplateGroup(group)


def scene_frame_range(frame_count, first_frame=1, scene_fps=None, kimodo_fps=30.0):
    """素材帧数 → 场景帧范围（含首尾）。

    ★ Kimodo 是 30fps，场景可能是 24fps。**按「时长不变」重映射**：
      素材第 0 帧 → 场景首帧；素材最后一帧 → 场景 `first + round((n-1)*scene/kimodo)`。
    不做这一步，动作时长直接是错的。
    """
    import hou
    if scene_fps is None:
        scene_fps = hou.fps() or 24.0
    n = max(1, int(frame_count))
    span = int(round((n - 1) * float(scene_fps) / float(kimodo_fps or 30.0)))
    return int(first_frame), int(first_frame) + span


def apply_npz(container, npz_path, kimodo_fps=30.0, first_frame=1):
    """把生成的 NPZ 接到场景里：设路径 + 设帧范围（**两个 playbar API 都要设**）。

    返回 (素材帧数, 场景首帧, 场景末帧)。
    """
    import hou

    m = motion_npz.load(npz_path)
    node = container.node("motion") if hasattr(container, "node") else container
    if node is None:
        raise RigError("容器里没有 motion 节点")
    node.parm("npzpath").set(npz_path)
    if node.parm("kimodo_fps") is not None:
        node.parm("kimodo_fps").set(float(kimodo_fps or 30.0))
    if node.parm("first_frame") is not None:
        node.parm("first_frame").set(int(first_frame))
    a, b = scene_frame_range(m.frame_count, first_frame, None, kimodo_fps)
    hou.playbar.setFrameRange(a, b)
    hou.playbar.setPlaybackRange(a, b)          # ★ 与 frameRange 是独立的两套，都要设
    node.cook(force=True)
    return m.frame_count, a, b


# ===========================================================================
# 内嵌的 SOP 代码
# ===========================================================================
# ★ 两个坑，都踩过：
#   1. 路径要转正斜杠——Windows 路径里的 \U 会被 Python 当成 unicode 转义 → SyntaxError，
#      而报错藏在 SOP 里、外面只看到 "Error while cooking"，极难查。
#   2. **不能用 % 格式化这些模板**——模板里的 SOP 代码自己就含 %s/%d，
#      会和外层格式化打架（TypeError: not enough arguments）。
#      用占位符 + replace。

_REST_SOP_CODE = '''
import sys
sys.path.insert(0, "__TOOL_ROOT__")
from houdini_ai import motion_npz
motion_npz.build_rest_geometry(hou.pwd().geometry(), "__SKEL__")
'''

_MOTION_SOP_CODE = '''
import sys
sys.path.insert(0, "__TOOL_ROOT__")
from houdini_ai import motion_npz

node = hou.pwd()
geo = node.geometry()
path = node.parm("npzpath").eval()

if not path:
    node.setComment("还没有生成结果。回面板点「生成动作」，或是手动指定一个 NPZ 路径。")
else:
    try:
        m = motion_npz.load(path)
    except motion_npz.NpzError as exc:
        node.setComment("读不了这个 NPZ：" + str(exc)[:300])
    else:
        kfps = node.parm("kimodo_fps").eval() or 30.0
        scene_fps = hou.fps() or 24.0
        idx = m.frame_index_for(hou.frame(), node.parm("first_frame").eval() or 1,
                                scene_fps, kfps)
        motion_npz.build_geometry(geo, m, idx)
        node.setComment("%s · %d 关节 · 共 %d 帧 · 30fps 下 %.2f 秒   当前场景第 %d 帧 = 素材第 %d 帧"
                        % (m.skel_name, m.joint_count, m.frame_count,
                           m.frame_count / 30.0, hou.frame(), idx))
'''


def _fill(template, **kw):
    """占位符替换。不用 % 也不用 format——模板里的代码自带花括号与百分号。"""
    out = template
    for k, v in kw.items():
        out = out.replace("__%s__" % k.upper(), str(v))
    return out


def _tool_root():
    return os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
