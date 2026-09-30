# -*- coding: utf-8 -*-
"""离屏渲染：给 AI 出一张图，**不开窗口、不碰视口、不抢焦点**。

## 为什么用 opengl ROP，而不是去截视口

第一版设计是"开一个浮动的 SceneViewer，截它的视口"。那条路能走，但有两个问题：
创建浮动面板会**蹦一个窗口出来**（用户明确不要这个），而且我**没法在无界面环境里
验证它**——只能验证 API 存在。

`opengl` ROP 是另一条路，而且明显更好：

  * 它是**渲染 OP**，通过相机把场景渲到文件。**不看视口、不开窗口、不抢焦点。**
  * **在无界面 hython 里也能跑**（实测），所以整条链路可以离线测、不用求人在
    GUI 里试。这一条是决定性的——能测的东西才敢改。
  * `shadingmode` 正好覆盖线框/无光影/有光影这些需求（见 SHADING_PRESETS）。

## 实测验证过的端到端

    渲染一个细高的 tube → 400x300 PNG 11729 字节 → 发给模型
    → 模型答「圆柱体 (Cylinder)」  ✅

## 两个踩过的坑（都在这段代码里防住了）

1. **`res` / `rad` 这类参数是多分量的，`parm()` 取不到，要 `parmTuple()`。**
   不防的话 `parm('res')` 返回 None，一个 `if` 保护就会**静默用默认分辨率**，
   渲出空白图还以为是渲染坏了（实测踩了两次）。
2. **相机朝向**：以前用 `lookatpath` 指向一个临时空物体。2026-09-29 改成**直接
   写死旋转角**（视角枚举化之后，六个轴全是 90° 的倍数，精确可写）。两个理由：
   * `lookatpath` 在**视线平行于 Y 轴时退化**——俯视/仰视正是这个方向；
   * 设了 `lookatpath` 之后 `camera.worldTransform()` **仍然只反映 `r`**
     （实测），也就是说走 lookat 那条路**没法用矩阵验证朝向**，而走 `r` 可以。
   （当年弃用 `extractRotates()` 是因为它算出来 1650 度这种鬼值——那是**从矩阵反推**
   的坑；这里不反推，角度是已知的。）
"""

import math
import os
import sys
import tempfile

# 预设名 -> opengl ROP 的 shadingmode 取值。
# 这些取值是实测从 ROP 的菜单里读出来的，不是猜的。
SHADING_PRESETS = {
    "shaded": "smooth",        # 带光影（默认）
    "flat": "flat",            # 无光影，纯色，看剪影和结构
    "wireframe": "wire",       # 线框
    "wireframe_ghost": "wireghost",   # 线框 + 半透明面，看内部
    "hidden_line": "hidden",   # 隐藏线
    "shaded_wire": "smoothwire",      # 带光影 + 线框
}

DEFAULT_PRESET = "shaded"
DEFAULT_SIZE = (800, 600)

# 相机离包围盒的距离 = 半径 / tan(fov/2) * 这个系数。留点边距，别贴着图框。
FRAME_MARGIN = 1.35

# --------------------------------------------------------------------------
# 视角枚举（用户 2026-09-29 定：**必须强制选一个视角**，不给默认方向）
#
# 相机默认姿态：视线 −Z、上 +Y、右 +X。`rot` 是标准右手主动旋转（度）。
# 六个轴全是 90° 的倍数，所以可以**精确写死**，不用从矩阵反推。
#
# ★ 验证方法（不需要渲染）：建个临时相机、设完 `r` 之后读 `worldTransform()`，
#   **行向量约定**下第 0/1/2 行就是相机局部 X/Y/Z 轴在世界里的方向，视线 = −第 2 行。
#   （实测确认过约定；`tests/test_viewport.py` 里逐轴断言。）
# --------------------------------------------------------------------------
VIEWS = {
    "+z": {"rot": (0.0, 0.0, 0.0), "label": "前视",
           "help": "前视（相机站在 +Z 那一侧看过来）"},
    "-z": {"rot": (0.0, 180.0, 0.0), "label": "后视", "help": "后视"},
    "+x": {"rot": (0.0, 90.0, 0.0), "label": "右视", "help": "右视"},
    "-x": {"rot": (0.0, -90.0, 0.0), "label": "左视", "help": "左视"},
    "+y": {"rot": (-90.0, 0.0, 0.0), "label": "俯视",
           "help": "俯视（从上往下看）"},
    "-y": {"rot": (90.0, 0.0, 0.0), "label": "仰视", "help": "仰视（从下往上看）"},
    # 六视图：一张图里六个方向。**只渲一次**——把几何复制六份、各自转到对着
    # 相机、排成 3×2。为什么不是"渲六次"：实测同一个进程里渲第二次会崩进程，
    # 而那道保险就是为这个设的，绝不能为了多视角把它拆掉。
    "six": {"rot": (0.0, 0.0, 0.0), "label": "六视图",
            "help": "六视图：±X / ±Y / ±Z 六个方向排成 3×2 一张图"},
}
DEFAULT_VIEW = "+z"
# ★ `render()` 的 `view` 形参有默认值**只是为了不破坏直接调用它的测试**，
#   不是给模型的入口——工具层（JSON Schema 的 `required`）才是必填的地方。

# 六视图的画布：3 列 × 2 行。上行 前/右/俯，下行 后/左/仰——正好是三对正反。
SIX_COLS, SIX_ROWS = 3, 2
# 六个方向，按上面那个格子顺序。旋转角 = **同名相机视角旋转的取负**
# （物体转 −R 等价于把相机转 +R）。这个"取负"关系有测试逐对钉住。
SIX_PIECES = [
    ("+z", (0.0, 0.0, 0.0)),
    ("+x", (0.0, -90.0, 0.0)),
    ("+y", (90.0, 0.0, 0.0)),
    ("-z", (0.0, 180.0, 0.0)),
    ("-x", (0.0, 90.0, 0.0)),
    ("-y", (-90.0, 0.0, 0.0)),
]
# 格子之间留的空隙（按最大边长取比例）
PANEL_GAP = 0.12
# 六视图的**像素预算** = 单张的几倍。恒定上界——图会进模型上下文，而且历史里的
# 图在后续每次请求都会重发，所以这个数字直接等于每轮多付多少钱。
SIX_BUDGET = 3.0

# 临时节点的名字前缀。用完必删。
TMP_PREFIX = "__ai_shot_"


class ShotError(Exception):
    """出图失败。消息是给模型/用户看的人话。"""


# --------------------------------------------------------------------------
# ★ 一道保险：一个进程里只允许渲一次
#
# 实测（hython）：`opengl` ROP 在同一个进程里**只能渲一次**。
# 第二次调 `rop.render()` 会**直接杀死进程**——复用同一个 ROP 节点、
# 参数完全不变也一样。换输出格式（jpg/exr/tif/tga/bmp）都一样。
# 崩栈停在 `IlmThread::ThreadPool::setThreadProvider`（图像库的线程池）。
#
# **这几乎肯定是 hython 特有的**：hython 没有 GL 上下文，而 Houdini 用户
# 用 opengl ROP 渲多帧序列是常规操作，说明真实 GUI 里重复渲染是正常的。
#
# **但我证明不了**（无界面环境里没有 GUI 可测），而赌错的代价是**把用户的
# Houdini 搞崩**。所以默认只允许渲一次：第二次直接报错拒绝，不冒险。
# 用户在真实 Houdini 里确认过重复渲染没问题之后，把 config 里的
# `screenshot_allow_repeat` 打开即可。
# --------------------------------------------------------------------------

_rendered_in_process = [0]

# ★★ 计数器的**真身**挂在 `hou.session` 上，模块里这个名字只是指向它。
#
# 为什么：工具架每个按钮点击前都会跑 `reload_quietly()`，而 `importlib.reload`
# 是**在同一个模块字典里重新执行模块体**——模块级的 `[0]` 每次都被重新赋值成 0，
# 于是"这个会话已经渲过一张"每隔一次点击就忘一次，**这道保险等于没设**。
# （实测 hython 22.0.368：reload 之后 `hou.session` 上的东西原封不动。）
#
# 拿不到 hou / 拿不到 session（比如不在 Houdini 里跑）就退回模块级，
# 行为和以前一致——不会因为绑不上反而更糟。
_SESSION_COUNTER_KEY = "_ai_shot_counter"
_SESSION_PID_KEY = "_ai_shot_counter_pid"


def _bind_counter_to_session():
    global _rendered_in_process
    try:
        import hou

        store = getattr(hou.session, _SESSION_COUNTER_KEY, None)
        # pid 是为了兜住"hou.session 上的属性会不会随 .hip 存盘"这个不确定性：
        # 万一会存，换个进程打开 .hip 时不能把它误读成"我这个进程已经渲过"。
        same_process = getattr(hou.session, _SESSION_PID_KEY, None) == os.getpid()
        if isinstance(store, list) and len(store) == 1 and same_process:
            _rendered_in_process = store      # 重载之后走这里：接着数
            return
    except Exception:
        return
    try:
        setattr(hou.session, _SESSION_COUNTER_KEY, _rendered_in_process)
        setattr(hou.session, _SESSION_PID_KEY, os.getpid())
    except Exception:
        pass


# 模块体里执行 → 每次 `importlib.reload` 之后自动重新指向同一个 list。
_bind_counter_to_session()


def reset_guard():
    """给测试用：把保险复位。"""
    _rendered_in_process[0] = 0


def _guard(allow_repeat):
    if allow_repeat or _rendered_in_process[0] == 0:
        return
    raise ShotError(
        "这个 Houdini 会话里已经出过一张图了，为了安全不再出第二张——"
        "实测（无界面环境）opengl ROP 在同一进程里渲第二次会崩，"
        "虽然那大概率是无界面特有的，但我不能拿你的 Houdini 冒险。\n"
        "你在真实 Houdini 里确认重复截图没问题之后，把 config.json 里的 "
        "`screenshot_allow_repeat` 设成 true 就能放开。")


def preset_names():
    return sorted(SHADING_PRESETS)


def views_menu():
    """给模型/错误信息看的视角清单，每个带一句人话。"""
    return ["%s=%s" % (name, VIEWS[name]["help"]) for name in sorted(VIEWS)]


def view_names():
    return sorted(VIEWS)


def _obj_of(node):
    """从容器往上找到它所属的 Object 节点（ROP 渲染的是 OBJ 层的物体）。"""
    import hou

    current = node
    for _ in range(12):
        if current is None:
            return None
        if isinstance(current, hou.ObjNode):
            return current
        current = current.parent()
    return None


def _bbox(node):
    """容器产出的包围盒。拿不到就返回 None。

    ★ 从节点外部调 `geometry()` 是**只读**的：没 cook 过的节点会给一个
    1 点的占位几何，包围盒是空的。所以这里先试着 cook 一下；
    真拿不到就返回 None，让调用方退回用物体的整体包围盒。
    """
    try:
        node.cook(force=True)
    except Exception:
        pass
    try:
        geometry = node.geometry()
    except Exception:
        geometry = None
    if geometry is None:
        return None
    try:
        box = geometry.boundingBox()
        if box.sizevec().length() <= 0:
            return None
        return box
    except Exception:
        return None


def _axis_direction(view):
    """视角名 → 相机该站在哪个方向（单位向量）。

    ★ 六视图走的是**另一条路**：相机固定在 +z，靠把**几何**转六个方向来出六格
    （见 `SIX_PIECES`）。所以它的相机方向就是 +z。
    """
    return _AXIS_VECTORS.get(view) or _AXIS_VECTORS["+z"]


_AXIS_VECTORS = {
    "+x": (1.0, 0.0, 0.0), "-x": (-1.0, 0.0, 0.0),
    "+y": (0.0, 1.0, 0.0), "-y": (0.0, -1.0, 0.0),
    "+z": (0.0, 0.0, 1.0), "-z": (0.0, 0.0, -1.0),
}


def _frame_camera(camera, box, view, width, height):
    """把相机摆到能框住 box 的位置，并按视角设好朝向。返回距离。

    ★ 朝向**直接写死旋转角**，不用 `lookatpath`。原因见文件顶部坑 #2：
    lookat 在视线平行于 Y 轴（俯视/仰视）时退化，而且它不体现在
    `worldTransform()` 里、没法验证。

    ★ 取景**两个轴都要 fit**。老版本只按水平 FOV 算距离，隐含假设画面不比 4:3
    更宽——六视图的画幅宽得多，纵向 FOV 更小，细高的东西会被**切掉**
    （实测算例：高 7 的容器纵向只装得下 3.5）。代价是单个视角里细高的东西
    会比以前显得小一点（以前近端其实是切掉的，只是没人注意）。
    """
    import hou

    center = box.center()
    sizevec = box.sizevec()

    focal = camera.parm("focal").eval()
    aperture = camera.parm("aperture").eval()
    fov = 2.0 * math.atan(aperture / (2.0 * focal))        # 水平 FOV（实测）
    tan_h = math.tan(fov / 2.0)
    tan_v = tan_h / (float(width) / float(height))

    half_w = max(sizevec[0], 1e-6) * 0.5
    half_h = max(sizevec[1], 1e-6) * 0.5
    distance = max(half_w / tan_h, half_h / tan_v) * FRAME_MARGIN

    direction = hou.Vector3(*_axis_direction(view)).normalized()
    position = hou.Vector3(center) + direction * distance

    # 注意用 parmTuple —— t/r 都是三分量，parm() 会返回 None
    camera.parmTuple("t").set((position[0], position[1], position[2]))
    camera.parmTuple("r").set(VIEWS[view]["rot"])
    return distance


def _multiview_size(width, height, box):
    """六视图的画幅：**像素预算封顶，比例直接跟着拼装结果走**。

    盲目放大换不来清晰——真正决定能不能看清的是**每格分到多少像素**。
    这里固定总像素 = 单张的 `SIX_BUDGET` 倍（成本上界可预测）。

    ★ `box` 是**六份拼完之后**的包围盒（调用方量出来的），它的比例**已经**是
    3 列 2 行的形状了。第一版在这里又乘了一遍 `列/行`，等于乘两遍，画幅被拉成
    2.18、左右全是空白——真渲一张才看出来。
    """
    sizevec = box.sizevec()
    aspect = min(max(sizevec[0] / max(sizevec[1], 1e-6), 0.4), 4.0)
    budget = max(width * height * SIX_BUDGET, 1.0)
    out_h = math.sqrt(budget / aspect)
    return max(int(round(aspect * out_h)), 32), max(int(round(out_h)), 32)


_ATTR_NAME_OK = None

# 把某个属性映射成 Cd 的 VEX。
#
# ★ 范围（min/max）在 VEX 里自己算，**不从 Python 读**。这样做有两个好处：
#   1. 不用把几何读回 Python（无界面下节点外 `geometry()` 是只读的，
#      拿回来是 1 点占位几何，算出来全是错的）；
#   2. 任意浮点属性都成立，不用用户先告诉我们范围。
# 这是个 Detail Wrangle：跑一次，遍历所有点。
_ATTR_VEX = """// 把 %(attr)s 映射成 Cd，范围自动取该属性的实际最小/最大值
float mn = 1e30, mx = -1e30;
int n = npoints(0);
for (int i = 0; i < n; i++) {
    float v = point(0, "%(attr)s", i);
    if (v < mn) mn = v;
    if (v > mx) mx = v;
}
float rng = max(mx - mn, 1e-6);
for (int i = 0; i < n; i++) {
    float v = point(0, "%(attr)s", i);
    setpointattrib(0, "Cd", i, fit(v, mn, mx));
}
"""


def _attribute_names(node):
    """容器产出上真实存在的属性 → 分量数。拿不到就返回空字典。

    ★ 为什么带分量数：`_ATTR_VEX` 里是 `float v = point(...)`，**只吃单值**。
    向量属性（`N`/`v`/`up`）喂进去会被 VEX 静默取 x 分量——图渲得出来、
    看着挺正常、表达的却是错的东西。这是本项目最忌讳的失效方式
    （见 `_check_attribute` 的说明），所以分量数必须一路带到校验那一步。
    """
    try:
        geometry = node.geometry()
    except Exception:
        return {}
    if geometry is None:
        return {}
    found = {}
    for getter in ("pointAttribs", "primAttribs", "vertexAttribs",
                   "globalAttribs"):
        try:
            for attrib in getattr(geometry, getter)():
                try:
                    size = attrib.size()
                except Exception:
                    size = 1
                found.setdefault(attrib.name(), size)
        except Exception:
            continue
    return found


def _check_attribute(node, name):
    """★ 属性名会被拼进 VEX 源码，所以必须白名单校验。

    先查语法（只允许标识符），再查它在几何上真实存在，最后查它是单值。
    三步都不能省：

    * 只查语法的话，模型可以编一个不存在的名字，VEX 里读不到就全是 0，
      图看起来"渲染成功"但什么都没表达。
    * 只查存在性的话，名字可能带怪字符（`x; __import__('os')`）。
    * 不查分量数的话，`N` 这种向量会被静默取 x 分量——**看着对、实际错**，
      比前两种都坏。所以这里**拒绝**而不是猜一个意思。
    """
    global _ATTR_NAME_OK
    if _ATTR_NAME_OK is None:
        import re

        _ATTR_NAME_OK = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")

    if not _ATTR_NAME_OK.match(name or ""):
        raise ShotError("属性名 %r 不合法（只能是字母数字下划线，且不以数字开头）"
                        % name)

    available = _attribute_names(node)
    if not available:
        # 拿不到属性表（几何没 cook）——这时只能靠语法检查
        return
    if name not in available:
        near = sorted(n for n in available if name.lower() in n.lower())
        hint = ("，最接近的是：" + ", ".join(near[:5])) if near else ""
        raise ShotError("容器产出上没有 %r 这个属性。存在的有：%s%s"
                        % (name, ", ".join(sorted(available)[:15]), hint))

    size = available.get(name) or 1
    if size > 1:
        raise ShotError(
            "属性 %r 是 %d 分量的，看不了——这条通道只支持单值属性。"
            "想看它的某个分量的话：你在容器里加一个 attribwrangle，把它拆成一个"
            "新的单值属性（比如 `f@nx = v@N.x;`），再来调一次 "
            "look(attribute=\"nx\")。" % (name, size))


class _Scratch(object):
    """临时的相机 / 目标 / ROP / 渲染用物体。用完**一定**删掉。

    用 with 语句，异常路径也不会漏。
    """

    def __init__(self, obj_node, source_node, attribute=None, view=DEFAULT_VIEW):
        self.obj_node = obj_node
        self.source_node = source_node
        self.attribute = attribute
        self.view = view
        self.created = []
        self.rop = None
        self.camera = None
        # 真正被渲的那个 OBJ 节点，以及**用来算取景的那个 SOP**
        self.render_target = None
        self.output = None

    def __enter__(self):
        # ★★ `with` 只在 `__enter__` **正常返回**之后才会调 `__exit__`。
        # 中途抛异常（createNode 失败之类）临时节点就留在用户的场景里了。
        # 六视图把这条路的入口放大了好几倍，所以这里自己兜住。
        try:
            return self._build()
        except Exception:
            self.__exit__(*sys.exc_info())
            raise

    def _sop(self, parent, kind, name):
        node = parent.createNode(kind, TMP_PREFIX + name)
        self.created.append(node)
        return node

    def _build(self):
        import hou

        obj_root = hou.node("/obj")
        out_root = hou.node("/out") or hou.node("/").createNode("out")

        # ★★ 永远走临时支路（以前只有属性可视化走它）。
        # 老版本非属性路径渲的是 `self.obj_node` —— 那个 Object 节点上
        # **display flag 指着的东西**，未必是容器；而取景盒用的是容器的。
        # 两者不一致时画面和取景对不上，而且**没有任何报错**。
        # 统一之后：画面内容 == 容器产出 == 取景盒，而且只剩一条代码路径。
        scratch_geo = obj_root.createNode("geo", TMP_PREFIX + "view")
        self.created.append(scratch_geo)

        src = self._sop(scratch_geo, "object_merge", "src")
        try:
            src.parm("objpath1").set(self.source_node.path())
        except Exception:
            pass

        stem = src
        if self.attribute:
            # 属性可视化：只读引用容器的产出，烘完颜色再删。
            # ★ 烘焙放在**分份之前**，这样六份自动都带色。
            bake = self._sop(scratch_geo, "attribwrangle", "bake")
            bake.setInput(0, stem)
            try:
                # 跑在 Detail 上：整个属性只算一遍 min/max，再逐点写 Cd
                bake.parm("class").set("detail")
                bake.parm("snippet").set(_ATTR_VEX % {"attr": self.attribute})
            except Exception:
                pass
            stem = bake

        if self.view == "six":
            stem = self._build_six(scratch_geo, stem)

        try:
            stem.setDisplayFlag(True)
            stem.setRenderFlag(True)
        except Exception:
            pass
        self.output = stem
        self.render_target = scratch_geo

        self.camera = obj_root.createNode("cam", TMP_PREFIX + "cam")
        self.created.append(self.camera)
        self.rop = out_root.createNode("opengl", TMP_PREFIX + "rop")
        self.created.append(self.rop)
        return self

    def _build_six(self, parent, stem):
        """把几何复制六份、各自转到对着相机、排成 3×2。返回合并后的那个 SOP。

        ★ 相机固定在 +z（屏幕右 +X、上 +Y），所以"要看某个面"就是把**那个面
        转到对着 +Z**——`SIX_PIECES` 里那六个角就是干这个的，它们恰好是
        同名相机视角旋转的**取负**（物体转 −R ≡ 相机转 +R）。
        ★ 绕**自己的中心**转（pivot = 容器盒心）：转完每份的盒心仍在原处，
        所以 `t = 格心 − 盒心` 一步到位。绕世界原点转的话每份会甩到不同的
        位置，还要逐份补偿——没省任何事，只把错误留给调试。
        """
        box = _bbox(stem)          # 分份之前：这就是容器的产出
        if box is None or box.sizevec().length() <= 0:
            raise ShotError("六视图取不到容器的包围盒")
        center = box.center()

        pieces = []
        for i, (_name, rot) in enumerate(SIX_PIECES):
            piece = self._sop(parent, "xform", "piece%d" % i)
            piece.setInput(0, stem)
            try:
                piece.parmTuple("p").set((center[0], center[1], center[2]))
                piece.parmTuple("r").set(rot)
            except Exception:
                pass
            pieces.append(piece)

        # ★ 格子用**统一尺寸**（六份里最大的宽和高各取一遍），这样绝不会互相
        #   压住，也不用手推"转 90° 之后尺寸变成多少"。
        widths, heights = [], []
        for piece in pieces:
            pbox = _bbox(piece)
            if pbox is None:
                raise ShotError("六视图：有一份没取到几何（object_merge 没接上？）")
            sizevec = pbox.sizevec()
            widths.append(max(sizevec[0], 1e-6))
            heights.append(max(sizevec[1], 1e-6))
        cell_w, cell_h = max(widths), max(heights)
        gap = PANEL_GAP * max(cell_w, cell_h)

        for i, piece in enumerate(pieces):
            col = i % SIX_COLS
            row = i // SIX_COLS
            # 整排整列居中：格心相对原点对称分布
            cx = (col - (SIX_COLS - 1) / 2.0) * (cell_w + gap)
            cy = ((SIX_ROWS - 1) / 2.0 - row) * (cell_h + gap)
            try:
                piece.parmTuple("t").set((cx - center[0], cy - center[1], 0.0))
            except Exception:
                pass

        out = self._sop(parent, "merge", "out")
        for i, piece in enumerate(pieces):
            out.setInput(i, piece)
        return out

    def __exit__(self, exc_type, exc, tb):
        for node in reversed(self.created):
            try:
                node.destroy()
            except Exception:
                pass
        return False


def render(node, preset=DEFAULT_PRESET, size=None, attribute=None,
           view=DEFAULT_VIEW, extra_objects=(), allow_repeat=False):
    """把容器渲染成一张 PNG，返回 (png 字节, 说明文字)。

    node       容器（Subnet SOP）
    preset     见 SHADING_PRESETS
    size       (宽, 高)，默认 800x600
    attribute  可选。想看某个属性的分布时给属性名——会把它烘成颜色再渲
    view       视角，见 VIEWS。默认值只是为了不破坏直接调用，**工具层是必填的**
    extra_objects  额外要渲染的物体路径
    allow_repeat  见上面那段保险的说明。默认 False = 一个会话只出一张。
    """
    import hou

    _guard(allow_repeat)

    if view not in VIEWS:
        raise ShotError("没有 %r 这个视角。可选：%s"
                        % (view, ", ".join(views_menu())))
    if preset not in SHADING_PRESETS:
        raise ShotError("没有 %r 这个渲染预设。可选：%s"
                        % (preset, ", ".join(preset_names())))

    if attribute:
        _check_attribute(node, attribute)

    width, height = size or DEFAULT_SIZE
    obj_node = _obj_of(node)
    if obj_node is None:
        raise ShotError("容器 %s 不在任何 Object 里，渲不出来" % node.path())

    box = _bbox(node)
    if box is None:
        # 容器自己没算出包围盒（比如空网络），退回用所在物体的整体包围盒
        try:
            box = obj_node.boundingBox()
        except Exception:
            box = None
    if box is None or box.sizevec().length() <= 0:
        raise ShotError("容器现在是空的，没什么可看的（包围盒是零）")

    handle, path = tempfile.mkstemp(prefix="houdini_ai_shot_", suffix=".png")
    os.close(handle)

    try:
        with _Scratch(obj_node, node, attribute=attribute, view=view) as scratch:
            # ★★ 取景盒必须**重算**，而且要量**临时支路最后一节 SOP**。
            #   六视图把六份并排摆开之后，整体比容器大得多；不重算的话六份
            #   会横向溢出画面。⚠️ 绝不能对这个临时 geo（ObjNode）调 `_bbox`
            #   ——`hou.ObjNode` **没有 `.geometry()`**，会**静默返回 None**，
            #   于是"重算"变成"悄悄退回旧盒子"，症状是图溢出了却不报错。
            frame_box = _bbox(scratch.output)
            if frame_box is None or frame_box.sizevec().length() <= 0:
                raise ShotError(
                    "临时取景网络没取到几何（object_merge 可能没接上 %s）"
                    "——这是工具自己的问题，不是你网络的问题" % node.path())
            if view == "six":
                width, height = _multiview_size(width, height, frame_box)
            distance = _frame_camera(scratch.camera, frame_box, view,
                                     width, height)

            rop = scratch.rop
            rop.parm("picture").set(path)
            rop.parm("camera").set(scratch.camera.path())
            rop.parm("shadingmode").set(SHADING_PRESETS[preset])
            # ★ parmTuple：res 是两分量的，parm('res') 是 None，
            #   用 if 保护就会静默用默认分辨率渲出空白图（实测踩过）
            rop.parmTuple("res").set((int(width), int(height)))
            # ★★ 还得打开 `tres`（标签是 "Override Camera Resolution"）。
            #   它默认是 0——**关着的时候 res 根本不生效**，ROP 会用**相机自己的**
            #   分辨率渲染，而新建相机的默认是 1280×720。
            #   实测（hython 22.0.368）：设了 res=(800,600) 读回来也对，渲出来
            #   还是 1280×720；把 tres 设成 1 才真的是 800×600。
            #   → 也就是说在修这一行之前，`config.json` 的 `screenshot_size`
            #     **是个死旋钮**，每张图都是 1280×720（比文档写的贵 ~1.9 倍）。
            #   有测试盯这一条（断言渲出来的 PNG 尺寸等于要的尺寸）。
            rop.parm("tres").set(1)

            # 只渲染我们这个物体，别把用户场景里别的东西也渲进来。
            # ★ 这里**失败要报错**：老版本整段 `except: pass`，一旦设置失败就会
            #   把用户整个场景渲进去，而且一声不吭——正是这套东西最忌的静默失效。
            targets = [scratch.render_target.path()] + list(extra_objects)
            try:
                rop.parm("forceobjects").set(" ".join(targets))
            except Exception as exc:
                raise ShotError(
                    "没法限定只渲染这个容器（forceobjects 设置失败：%s）；"
                    "为了不把整个场景渲出来，这次出图中止" % exc)

            try:
                rop.render(verbose=False)
                _rendered_in_process[0] += 1
            except Exception as exc:
                raise ShotError("渲染失败：%s" % exc)

        if not os.path.isfile(path):
            raise ShotError("渲染没有产出文件")
        with open(path, "rb") as fh:
            data = fh.read()
        if not data:
            raise ShotError("渲染出来是空文件")

        note = ("渲染完成：%s 预设，%s 视角，%dx%d，相机自动框住内容"
                "（距离 %.2f）" % (preset, VIEWS[view]["label"],
                                   width, height, distance))
        if view == "six":
            note += "（六视图：一张图里六个方向，%d 列 %d 行）" % (
                SIX_COLS, SIX_ROWS)
        if attribute:
            note += "，按属性 %s 上的色" % attribute
        return data, note
    finally:
        try:
            os.remove(path)
        except Exception:
            pass
