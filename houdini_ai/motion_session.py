# -*- coding: utf-8 -*-
"""动作会话的数据模型：文字段 + 约束。**纯逻辑，不依赖 hou，可离线测。**

设计上的一条硬规矩：**这里存的一律是后端单位**
（秒 / 素材帧、0-based、米、Y-up）。面板负责把场景帧换算过来显示。
这样「组装给后端的载荷」那一步就是直读，不会在两处各做一次换算然后对不上。

## 两种输入怎么统一

* **关键帧线**：用户在场景里摆姿势 → 记录成 `Constraint(kind=fullbody, ...)`，
  位置与旋转是**世界坐标、米**（KineFX 直接给的）
* **文字线**：`Segment(text="向前小心走", seconds=3.0)`，段首尾自动接上

多段时 Kimodo 原生支持：prompt 用句号分隔的多句 + 每段一个时长，
它在段边界自动加约束保证连续。所以**不需要我们手动拼接**，
只要把段按顺序排好、算出总帧数即可。
"""

import math

# Kimodo 恒定 30fps。这是后端单位，不要跟场景 fps 混。
KIMODO_FPS = 30.0

# 官方限制：每个 prompt 最长 10 秒（`Kimodo limitations` 页原文）
MAX_SEGMENT_SECONDS = 10.0
# 官方限制：每种约束的约束帧数应少于 20
MAX_FRAMES_PER_CONSTRAINT_KIND = 20

# 约束类型。前四种是 Kimodo 的「末端执行器」简写，第五个是通用形式。
KINDS = ("root2d", "fullbody", "left-hand", "right-hand", "left-foot", "right-foot")
EE_KINDS = ("left-hand", "right-hand", "left-foot", "right-foot")
# 面板上的中文名（只影响显示）
KIND_LABELS = {
    "root2d": "根路径",
    "fullbody": "全身姿势",
    "left-hand": "左手",
    "right-hand": "右手",
    "left-foot": "左脚",
    "right-foot": "右脚",
}


class SessionError(Exception):
    """给用户看的错误。"""


class Segment(object):
    """一段文字描述。时长用**秒**（后端单位）。"""

    def __init__(self, text="", seconds=3.0, seed=None, enabled=True):
        self.text = str(text)
        self.seconds = float(seconds)
        self.seed = seed
        self.enabled = bool(enabled)

    def frames(self, fps=KIMODO_FPS):
        return int(round(self.seconds * float(fps)))

    def to_dict(self):
        d = {"text": self.text, "seconds": self.seconds, "enabled": self.enabled}
        if self.seed is not None:
            d["seed"] = int(self.seed)
        return d

    @classmethod
    def from_dict(cls, d):
        return cls(d.get("text", ""), d.get("seconds", 3.0),
                   d.get("seed"), d.get("enabled", True))

    def __repr__(self):
        return "Segment(%r, %.2fs, enabled=%s)" % (self.text[:18], self.seconds, self.enabled)


class Constraint(object):
    """一条约束。`frame` 是**素材帧、0-based**。

    * `root2d`：`points` 是 [[x,z], ...]（米，相对规范原点）；配 `frames` 是每个路点的帧号
    * 其余（fullbody / 手脚）：`joint_positions` (J,3) 世界位置（米）、
      `joint_rots` (J,3,3) 全局旋转（Kimodo 列主序约定）
      —— 这两个直接从 KineFX 的 `P` 与 `transform` 来，不做局部转换
    """

    def __init__(self, kind, frame, joint_positions=None, joint_rots=None,
                 points=None, frames=None, enabled=True, skel_name=None,
                 joint_names=None):
        if kind not in KINDS:
            raise SessionError("不认识的约束类型 %r，只能是 %s" % (kind, "、".join(KINDS)))
        self.kind = kind
        self.frame = int(frame)
        self.joint_positions = joint_positions
        self.joint_rots = joint_rots
        self.points = points                    # 仅 root2d
        self.frames = frames                    # 仅 root2d：每个路点的帧
        self.enabled = bool(enabled)
        self.skel_name = skel_name
        self.joint_names = joint_names or []    # 仅通用 end-effector

    @property
    def is_root2d(self):
        return self.kind == "root2d"

    def frame_list(self):
        """这条约束覆盖哪些帧（0-based）。"""
        if self.is_root2d and self.frames:
            return [int(f) for f in self.frames]
        return [self.frame]

    def to_dict(self):
        d = {"kind": self.kind, "frame": self.frame, "enabled": self.enabled}
        if self.skel_name:
            d["skel_name"] = self.skel_name
        if self.joint_names:
            d["joint_names"] = list(self.joint_names)
        # 数组不存进 node userData（太大），只存"有"这个事实——
        # 真正的数组在生成时从场景现场读（姿势可能已经被改过）
        d["has_pose"] = self.joint_positions is not None
        if self.is_root2d:
            d["points"] = [list(map(float, p)) for p in (self.points or [])]
            d["frames"] = [int(f) for f in (self.frames or [])]
        return d

    @classmethod
    def from_dict(cls, d):
        return cls(d.get("kind", "fullbody"), d.get("frame", 0),
                   points=d.get("points"), frames=d.get("frames"),
                   enabled=d.get("enabled", True), skel_name=d.get("skel_name"),
                   joint_names=d.get("joint_names"))

    def __repr__(self):
        return "Constraint(%s, frame=%d, enabled=%s)" % (self.kind, self.frame, self.enabled)


class MotionSession(object):
    """一个动作节点的会话：段 + 约束。可以整体存进 node userData。"""

    def __init__(self, segments=None, constraints=None, seed=None,
                 diffusion_steps=100, cfg_weight=2.0, transition_frames=5,
                 postprocess=True, first_heading_angle=0.0, notes=""):
        self.segments = list(segments or [])
        self.constraints = list(constraints or [])
        self.seed = seed
        self.diffusion_steps = int(diffusion_steps)
        self.cfg_weight = float(cfg_weight)
        self.transition_frames = int(transition_frames)
        self.postprocess = bool(postprocess)
        self.first_heading_angle = float(first_heading_angle)
        self.notes = notes

    # ---- 段 --------------------------------------------------------------

    @property
    def active_segments(self):
        return [s for s in self.segments if s.enabled and s.text.strip()]

    def total_frames(self, fps=KIMODO_FPS):
        return sum(s.frames(fps) for s in self.active_segments)

    def total_seconds(self, fps=KIMODO_FPS):
        return self.total_frames(fps) / float(fps)

    def segment_frame_ranges(self, fps=KIMODO_FPS):
        """每个启用段的 (起, 止) **素材帧**，左闭右开。"""
        out, cur = [], 0
        for s in self.active_segments:
            n = s.frames(fps)
            out.append((cur, cur + n))
            cur += n
        return out

    def prompt_text(self, fps=KIMODO_FPS):
        """拼成 Kimodo 要的 prompt：多句用句号分隔。"""
        parts = [s.text.strip().rstrip(".") for s in self.active_segments]
        return ". ".join(p for p in parts if p)

    def durations(self, fps=KIMODO_FPS):
        """每段时长（秒），与 prompt 的句子一一对应。"""
        return [s.frames(fps) / float(fps) for s in self.active_segments]

    def add_segment(self, text="", seconds=3.0, seed=None):
        self.segments.append(Segment(text, seconds, seed))
        return self.segments[-1]

    def remove_segment(self, index):
        if not (0 <= index < len(self.segments)):
            raise SessionError("段的序号 %d 越界（共 %d 段）" % (index, len(self.segments)))
        return self.segments.pop(index)

    def move_segment(self, index, delta):
        j = index + int(delta)
        if not (0 <= index < len(self.segments)) or not (0 <= j < len(self.segments)):
            return False
        self.segments[index], self.segments[j] = self.segments[j], self.segments[index]
        return True

    # ---- 约束 ------------------------------------------------------------

    def add_constraint(self, c):
        self.constraints.append(c)
        self.constraints.sort(key=lambda x: (min(x.frame_list()), x.kind))
        return c

    def remove_constraint(self, index):
        if not (0 <= index < len(self.constraints)):
            raise SessionError("约束的序号 %d 越界（共 %d 条）" % (index, len(self.constraints)))
        return self.constraints.pop(index)

    def clear_constraints(self, kind=None):
        if kind is None:
            n = len(self.constraints)
            self.constraints = []
            return n
        before = len(self.constraints)
        self.constraints = [c for c in self.constraints if c.kind != kind]
        return before - len(self.constraints)

    def active_constraints(self):
        return [c for c in self.constraints if c.enabled]

    def constraints_by_kind(self):
        out = {}
        for c in self.active_constraints():
            out.setdefault(c.kind, []).append(c)
        return out

    # ---- 规范化（★ 后端要求根在第 0 帧位于 XZ 原点）------------------------

    def canonical_offset(self):
        """要整体平移多少，才能让**最早的根路点**落在 XZ 原点。

        Kimodo 的 canonical 约定：根在第 0 帧位于 XZ=(0,0)。
        用户是在场景里随手画的路径，所以要整体挪一次。
        返回 (dx, dz)；没有任何根路点时返回 (0,0)。
        """
        best = None
        for c in self.active_constraints():
            if not c.is_root2d or not c.points:
                continue
            fs = c.frames or list(range(len(c.points)))
            first_f = min(fs)
            i = fs.index(first_f)
            if best is None or first_f < best[0]:
                best = (first_f, c.points[i])
        if best is None:
            return 0.0, 0.0
        px, pz = float(best[1][0]), float(best[1][1])
        return -px, -pz

    def apply_canonical(self):
        """就地平移所有根路点，让最早的落回原点。返回 (dx, dz)。"""
        dx, dz = self.canonical_offset()
        if dx == 0.0 and dz == 0.0:
            return dx, dz
        for c in self.constraints:
            if c.is_root2d and c.points:
                c.points = [[float(p[0]) + dx, float(p[1]) + dz] for p in c.points]
            if c.joint_positions is not None:
                c.joint_positions = [[float(p[0]) + dx, float(p[1]), float(p[2]) + dz]
                                     for p in c.joint_positions]
        return dx, dz

    # ---- 校验 ------------------------------------------------------------

    def validate(self):
        """返回一串**面向用户**的问题描述。空列表 = 没问题。"""
        out = []
        segs = self.active_segments
        if not segs:
            out.append("还没有任何一段文字描述（至少要一段）")
        for i, s in enumerate(segs, 1):
            if s.seconds <= 0:
                out.append("第 %d 段时长是 %.2f 秒，要大于 0" % (i, s.seconds))
            elif s.seconds > MAX_SEGMENT_SECONDS:
                out.append("第 %d 段 %.1f 秒超过了 Kimodo 的上限 %.0f 秒，会被截断"
                           % (i, s.seconds, MAX_SEGMENT_SECONDS))
        total = self.total_frames()
        if total <= 0:
            out.append("总时长为 0，生成不出东西")
        # 约束帧号要落在总帧数内
        for c in self.active_constraints():
            for f in c.frame_list():
                if f < 0:
                    out.append("%s 约束的帧号 %d 是负数" % (KIND_LABELS.get(c.kind, c.kind), f))
                elif total and f >= total:
                    out.append("%s 约束在第 %d 帧，但总长只有 %d 帧（会被忽略）"
                               % (KIND_LABELS.get(c.kind, c.kind), f, total))
        # 每种约束的帧数上限
        for kind, cs in self.constraints_by_kind().items():
            n = sum(len(c.frame_list()) for c in cs)
            if n > MAX_FRAMES_PER_CONSTRAINT_KIND:
                out.append("%s 约束有 %d 个约束帧，官方建议每种少于 %d 个，可能会糊"
                           % (KIND_LABELS.get(kind, kind), n, MAX_FRAMES_PER_CONSTRAINT_KIND))
        # 姿势类的约束要真带数据
        for c in self.active_constraints():
            if not c.is_root2d and c.joint_positions is None:
                out.append("%s 约束在第 %d 帧没有姿势数据（要先「记录当前姿势」）"
                           % (KIND_LABELS.get(c.kind, c.kind), c.frame))
        return out

    # ---- 存取 ------------------------------------------------------------

    def to_dict(self):
        d = {
            "segments": [s.to_dict() for s in self.segments],
            "constraints": [c.to_dict() for c in self.constraints],
            "seed": self.seed,
            "diffusion_steps": self.diffusion_steps,
            "cfg_weight": self.cfg_weight,
            "transition_frames": self.transition_frames,
            "postprocess": self.postprocess,
            "first_heading_angle": self.first_heading_angle,
            "notes": self.notes,
        }
        return d

    @classmethod
    def from_dict(cls, d):
        if not d:
            return cls()
        return cls(
            segments=[Segment.from_dict(x) for x in d.get("segments", [])],
            constraints=[Constraint.from_dict(x) for x in d.get("constraints", [])],
            seed=d.get("seed"),
            diffusion_steps=d.get("diffusion_steps", 100),
            cfg_weight=d.get("cfg_weight", 2.0),
            transition_frames=d.get("transition_frames", 5),
            postprocess=d.get("postprocess", True),
            first_heading_angle=d.get("first_heading_angle", 0.0),
            notes=d.get("notes", ""),
        )

    def summary(self):
        """一行摘要，给节点注释用。"""
        segs = self.active_segments
        cs = self.active_constraints()
        return "%d 段 · 共 %.1f 秒 / %d 帧 · %d 条约束" % (
            len(segs), self.total_seconds(), self.total_frames(), len(cs))


# ===========================================================================
# 场景帧 ↔ 素材帧（面板用；只在这一处换算）
# ===========================================================================

def scene_to_asset(scene_frame, first_scene_frame=1, scene_fps=24.0, kimodo_fps=KIMODO_FPS):
    return int(round((float(scene_frame) - float(first_scene_frame))
                     * float(kimodo_fps) / float(scene_fps or kimodo_fps)))


def asset_to_scene(asset_frame, first_scene_frame=1, scene_fps=24.0, kimodo_fps=KIMODO_FPS):
    return int(round(float(asset_frame) * float(scene_fps or kimodo_fps)
                     / float(kimodo_fps) + float(first_scene_frame)))


def scene_range_for_assets(frame_count, first_scene_frame=1, scene_fps=24.0,
                           kimodo_fps=KIMODO_FPS):
    """素材帧数 → 场景帧范围（含首尾）。按「时长不变」换算。"""
    n = max(1, int(frame_count))
    span = int(round((n - 1) * float(scene_fps or kimodo_fps) / float(kimodo_fps)))
    return int(first_scene_frame), int(first_scene_frame) + span


def nice_duration(seconds):
    """给面板显示用：3.0 → '3 秒'，3.5 → '3.5 秒'。"""
    if abs(seconds - round(seconds)) < 1e-6:
        return "%d 秒" % int(round(seconds))
    return "%.1f 秒" % seconds
