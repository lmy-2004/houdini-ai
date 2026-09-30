# -*- coding: utf-8 -*-
"""出图（图像通道）的测试。

    "D:/ProgramFiles/Houdini22/bin/hython.exe" tests/test_viewport.py

★ 这个文件**只能真渲一次图**：实测 opengl ROP 在同一进程里渲第二次会崩，
所以 `viewport` 里有一道保险，默认拒掉第二张。文件里把唯一那次真渲染
放在最后，前面全是纯校验（不渲染）。

出图的端到端（渲染 → 图 → 模型能认出来）我在开发时手工验过：
渲一个细高的 tube，模型答「圆柱体 (Cylinder)」。
"""

import _harness as H

H.section("渲染预设")
from houdini_ai import viewport

H.check("预设不止一个", len(viewport.SHADING_PRESETS) >= 5,
        sorted(viewport.SHADING_PRESETS))
H.check("有线框", "wireframe" in viewport.SHADING_PRESETS)
H.check("有无光影", "flat" in viewport.SHADING_PRESETS)
H.check("有带光影", "shaded" in viewport.SHADING_PRESETS)
H.check("默认预设存在",
        viewport.DEFAULT_PRESET in viewport.SHADING_PRESETS,
        viewport.DEFAULT_PRESET)
H.check("preset_names 返回排序过的清单",
        viewport.preset_names() == sorted(viewport.SHADING_PRESETS))

H.section("搭场景")
import hou
from houdini_ai import builder, node as node_mod, schema

obj = hou.node("/obj")
for old in list(obj.children()):
    if old.name().startswith("VP_"):
        old.destroy()
geo = obj.createNode("geo", "VP_geo")
ai = node_mod.make_ai_node(geo, "VP_ai", prompt="x")
builder.apply(ai, {"nodes": [{"name": "t", "type": "tube",
                              "params": {"rad": [0.7, 0.7], "height": 7}}],
                   "output": "t"})
geo.setDisplayFlag(True)
geo.cook(force=True)
H.check("容器里有东西", len(ai.children()) > 0)

H.section("★ 属性名会被拼进 VEX，所以必须白名单校验")
for bad, why in ((u"密度", "中文名"),
                 ("nonexistent_attr", "不存在的属性"),
                 ("x; __import__('os')", "想注入 VEX"),
                 ("a-b", "带连字符"),
                 ("1abc", "数字开头"),
                 ("", "空名")):
    try:
        viewport._check_attribute(ai, bad)
        H.check("%s（%s）被拒" % (bad, why), False, "居然通过了")
    except viewport.ShotError:
        H.check("%s（%s）被拒" % (bad, why), True)
    except Exception as exc:
        H.check("%s（%s）被拒" % (bad, why), False,
                "抛的是 %s：%s" % (type(exc).__name__, exc))

H.section("★★ 只吃单值属性（向量会被静默取 x 分量，所以必须拒绝）")
# `_ATTR_VEX` 里是 `float v = point(...)`，向量喂进去会被 VEX 悄悄取 x 分量——
# 图渲得出来、看着正常、表达的却是错的东西。宁可不给看，也不能给错的。
H.check("拿得到的属性表里有 P", "P" in viewport._attribute_names(ai),
        viewport._attribute_names(ai))
H.check("★ P 是三分量的（真实几何上确实如此）",
        viewport._attribute_names(ai).get("P") == 3,
        viewport._attribute_names(ai).get("P"))
try:
    viewport._check_attribute(ai, "P")
    H.check("★★ 向量属性 P 被拒（不许静默取 x 分量）", False, "居然通过了")
except viewport.ShotError as exc:
    H.check("★★ 向量属性 P 被拒（不许静默取 x 分量）", True)
    H.check("  说清了是几分量", "3 分量" in str(exc), str(exc)[:80])
    H.check("  给了可操作的办法（拆成单值再来看）",
            "attribwrangle" in str(exc) and "look(attribute" in str(exc),
            str(exc)[-90:])


class _FakeAttrib(object):
    def __init__(self, name, size):
        self._name, self._size = name, size

    def name(self):
        return self._name

    def size(self):
        return self._size


class _FakeGeo(object):
    """造一个假的几何，好在 hython 里把「单值属性通过」这条正路走通。

    真几何上只有一个 P（三分量），走不出正路——只能造。
    """

    def __init__(self, attribs):
        self._attribs = attribs

    def pointAttribs(self):
        return self._attribs

    def primAttribs(self):
        return []

    def vertexAttribs(self):
        return []

    def globalAttribs(self):
        return []


class _FakeNode(object):
    def __init__(self, geo):
        self._geo = geo

    def geometry(self):
        return self._geo


fakes = _FakeNode(_FakeGeo([_FakeAttrib("P", 3),
                            _FakeAttrib("density", 1),
                            _FakeAttrib("height", 1),
                            _FakeAttrib("v", 3)]))
H.check("单值属性能通过", viewport._check_attribute(fakes, "density") is None)
H.check("另一个单值属性也通过",
        viewport._check_attribute(fakes, "height") is None)
try:
    viewport._check_attribute(fakes, "v")
    H.check("三分量 v 也被拒", False, "居然通过了")
except viewport.ShotError:
    H.check("三分量 v 也被拒", True)
try:
    viewport._check_attribute(fakes, "nope")
    H.check("不存在的名字被拒", False, "居然通过了")
except viewport.ShotError as exc:
    H.check("不存在的名字被拒", True)
    H.check("  列了存在的属性当参照",
            "density" in str(exc), str(exc)[:100])

H.section("非法预设被拒（在渲染之前就拦下）")
try:
    viewport.render(ai, preset="不存在的模式")
    H.check("非法预设被拒", False, "居然没抛")
except viewport.ShotError as exc:
    H.check("非法预设被拒", True)
    H.check("错误里列出了可选预设",
            "wireframe" in str(exc), str(exc)[:120])

H.section("占位几何/空容器不让渲（免得出一张空白图骗模型）")
empty_geo = obj.createNode("geo", "VP_empty_geo")
empty = node_mod.make_ai_node(empty_geo, "VP_empty", prompt="x")
try:
    viewport.render(empty)
    H.check("空容器被拒", False, "居然渲了")
except viewport.ShotError as exc:
    H.check("空容器被拒", True)
    H.check("说清楚了原因", "空" in str(exc), str(exc)[:100])

H.section("★★ 保险：一个会话只允许渲一次（防崩）")
viewport.reset_guard()
H.check("刚开始没渲过", viewport._rendered_in_process[0] == 0)
viewport._rendered_in_process[0] = 1          # 假装已经渲过一次
try:
    viewport._guard(allow_repeat=False)
    H.check("第二次被拒", False, "居然放行了")
except viewport.ShotError as exc:
    H.check("第二次被拒", True)
    H.check("说明里讲了为什么、以及怎么放开",
            "崩" in str(exc) and "screenshot_allow_repeat" in str(exc),
            str(exc)[:160])
H.check("显式 allow_repeat=True 就放行",
        viewport._guard(allow_repeat=True) is None)

H.section("★★ 计数不能被热重载清零（工具架按钮每点一下都会重载）")
# 工具架每个按钮点击前都跑 reload_quietly()，而 importlib.reload 是**在同一个
# 模块字典里重新执行模块体**——模块级的计数器每次都会被重新赋值成 0，
# 这道保险等于没设。真身挂在 hou.session 上（实测 reload 之后原封不动）。
import importlib

viewport.reset_guard()
H.check("计数器挂在 hou.session 上（重载碰不到的地方）",
        getattr(hou.session, "_ai_shot_counter", None)
        is viewport._rendered_in_process,
        type(getattr(hou.session, "_ai_shot_counter", None)))
viewport._rendered_in_process[0] = 1
importlib.reload(viewport)          # 按钮点一下干的就是这个
H.check("★ reload 之后计数还是 1（不是被清零）",
        viewport._rendered_in_process[0] == 1,
        viewport._rendered_in_process[0])
try:
    viewport._guard(allow_repeat=False)
    H.check("★ reload 之后保险照样拦得住", False, "居然放行了")
except viewport.ShotError:
    H.check("★ reload 之后保险照样拦得住", True)
viewport.reset_guard()
H.check("reset_guard 两边一起归零",
        viewport._rendered_in_process[0] == 0
        and hou.session._ai_shot_counter[0] == 0)

H.section("★★ 六个轴向的相机朝向（不需要渲染，读矩阵就能验）")
# ★ 实测约定：hou.Matrix4 是**行向量**——at(i,j) 的 i 是**行**，
#   第 0/1/2 行就是相机局部 X/Y/Z 轴在世界里的方向，**视线 = −第 2 行**。
#   这条要是搞错，六个轴会**集体反向**而断言照样可能通过。
_probe_cam = obj.createNode("cam", "VP_probe")


def _cam_axes(rot):
    _probe_cam.parmTuple("r").set(rot)
    m = _probe_cam.worldTransform()
    row = lambda i: hou.Vector3(m.at(i, 0), m.at(i, 1), m.at(i, 2))
    return row(0), row(1), row(2)


for _name in sorted(viewport.VIEWS):
    if _name == "six":
        continue
    _right, _up, _zloc = _cam_axes(viewport.VIEWS[_name]["rot"])
    _fwd = -_zloc
    _want = hou.Vector3(*viewport._axis_direction(_name)) * -1.0
    H.check("视角 %s 的视线对（照 %s 看）" % (_name, _name),
            (_fwd - _want).length() < 1e-9,
            (tuple(_fwd), tuple(_want)))
    H.check("  上方向不退化（lookat 在 ±Y 上会退化，写死 r 不会）",
            abs(_up.dot(_fwd)) < 1e-9 and abs(_up.length() - 1.0) < 1e-9)
    # ★ 这条最关键：行/列搞反时，纯轴对齐的 90° 旋转有可能碰巧还对，
    #   这条右手正交断言会立刻炸。
    H.check("  三根轴正交且右手（钉住行/列没搞反）",
            (_right.cross(_up) - _zloc).length() < 1e-9)
_probe_cam.destroy()

H.check("视角表里有六个轴 + 六视图", len(viewport.VIEWS) == 7,
        sorted(viewport.VIEWS))
H.check("默认值的那个视角也在表里",
        viewport.DEFAULT_VIEW in viewport.VIEWS)

H.section("★★ 六视图：六份的旋转 = 同名相机视角旋转的取负")
# 相机固定在 +z，把"要看的面"转到对着相机 —— 物体转 −R 等价于相机转 +R。
# ★ 判据是**模 360 相加为 0**：`-z` 的 180° 和 −180° 是**同一个旋转**，
#   直接比数值会误报（我自己第一版就这么误报过）。
H.check("六份", len(viewport.SIX_PIECES) == 6, len(viewport.SIX_PIECES))
for _name, _rot in viewport.SIX_PIECES:
    _cam = viewport.VIEWS[_name]["rot"]
    _sum = [(a + b) % 360 for a, b in zip(_cam, _rot)]
    H.check("  %s：相机%s + 物体%s ≡ 0（模 360）" % (_name, _cam, _rot),
            _sum == [0.0, 0.0, 0.0], _sum)
H.check("三列两行能装下六份",
        viewport.SIX_COLS * viewport.SIX_ROWS == len(viewport.SIX_PIECES))
H.check("★ 六份的方向两两不同（哪一份转重了就是 bug）",
        len(set(n for n, _ in viewport.SIX_PIECES)) == 6)

H.section("★★ 六视图：一张图里画六份（不渲染，只看它建了什么）")
# 「只渲一次」是硬契约——实测同一个进程渲第二次会崩，那道保险就是为它设的。
# 六视图必须靠**一次渲染里画六份几何**实现，不能渲六次。
_single = viewport._bbox(ai)
with viewport._Scratch(obj, ai, view="six") as _sc:
    _kids = [c.name() for c in _sc.render_target.children()]
    H.check("建了六个 piece",
            sum(1 for k in _kids if "piece" in k) == 6, _kids)
    H.check("汇到一个 merge", any(k.endswith("out") for k in _kids), _kids)
    H.check("★ 输出是**一个** SOP（一张图，不是六张）",
            _sc.output is not None and _sc.output.name().endswith("out"),
            _sc.output.name() if _sc.output else None)
    _sixbox = viewport._bbox(_sc.output)
    H.check("六份拼起来明显比容器大（确实散开了）",
            _sixbox is not None
            and _sixbox.sizevec()[0] > _single.sizevec()[0] * 2,
            (_sixbox.sizevec() if _sixbox else None, _single.sizevec()))
    H.check("★ 六份的格子**互不重叠**（并排摆开，不是叠在一起）",
            _sixbox is not None
            and _sixbox.sizevec()[1] > _single.sizevec()[1] * 1.5,
            _sixbox.sizevec() if _sixbox else None)

H.check("★ 用完临时节点一个不留（_Scratch 中途出错也要清）",
        not [c for c in obj.children()
             if c.name().startswith(viewport.TMP_PREFIX)],
        [c.name() for c in obj.children()
         if c.name().startswith(viewport.TMP_PREFIX)])

H.section("★★ _Scratch 建到一半炸了，也要把建出来的删掉")
# `with` 只在 `__enter__` **正常返回**之后才调 `__exit__`——中途抛异常的话
# 临时节点会**留在用户的场景里**。六视图把这条路的入口放大了六倍。
_before = len(obj.children())
try:
    with viewport._Scratch(obj, None, view="six"):   # source_node=None → 必炸
        H.check("不该走到这里", False, "居然没炸")
except Exception:
    pass
H.check("★ 建到一半炸了也没留残留",
        not [c for c in obj.children()
             if c.name().startswith(viewport.TMP_PREFIX)],
        [c.name() for c in obj.children()
         if c.name().startswith(viewport.TMP_PREFIX)])

H.section("工具层：开关和参数校验")
from houdini_ai import config

cfg = config.load()
H.check("配置里有图像通道的开关", "screenshot_enabled" in cfg, sorted(cfg)[:6])
H.check("默认分辨率是两分量", len(cfg.get("screenshot_size") or []) == 2,
        cfg.get("screenshot_size"))
H.check("★ 默认**不允许**重复渲染（保险默认开着）",
        cfg.get("screenshot_allow_repeat") is False,
        cfg.get("screenshot_allow_repeat"))

viewport.reset_guard()
result = schema.dispatch(ai, "look", {"view": "+z", "preset": "不存在"})
H.check("工具层把非法预设变成 error 而不是抛异常",
        "error" in result, result)
H.check("error 里带着可选的预设",
        "wireframe" in str(result.get("error")), result.get("error"))

H.section("★★ 视角是**必填**的（用户 2026-09-29 定）")
_missing = schema.dispatch(ai, "look", {})
H.check("★ 不给视角直接拒", "error" in _missing, _missing)
H.check("★ 错误里列了可选视角", "six" in str(_missing.get("error"))
        and "+y" in str(_missing.get("error")), _missing.get("error")[:120])
H.check("★ 而且**没**走到那道保险（说明参数校验排在 _guard 之前）",
        "已经出过一张图" not in str(_missing.get("error")),
        _missing.get("error")[:120])
_bad = schema.dispatch(ai, "look", {"view": "top"})
H.check("★ 不认识的视角也被拒，且列出可选值",
        "error" in _bad and "six" in str(_bad.get("error")),
        _bad.get("error")[:120])
H.check("返回里也给了机器可读的视角清单",
        "+z" in (_bad.get("views") or []), _bad.get("views"))

H.section("★★ 真的渲一张（本文件唯一一次，必须在最后）")
viewport.reset_guard()
result = schema.dispatch(ai, "look", {"view": "+z", "preset": "wireframe"})
H.check("出图成功", result.get("ok"), result)
images = result.get("__images__") or []
H.check("返回里带了一张图", len(images) == 1, len(images))
H.check("图不是空的", images and len(images[0]) > 1000,
        len(images[0]) if images else 0)
H.check("PNG 魔数对", images and images[0][:8] == b"\x89PNG\r\n\x1a\n",
        images[0][:8] if images else None)
H.check("说明里讲了预设和视角",
        "wireframe" in (result.get("note") or "")
        and "前视" in (result.get("note") or ""),
        result.get("note"))
# ★★ 2026-09-29 修的一个**一直都在**的 bug：opengl ROP 的 `res` 要生效，
#    必须先打开 `tres`（"Override Camera Resolution"，默认是关的）。
#    修之前 `screenshot_size` 是个**死旋钮**，每张图都是 1280×720。
def _png_size(blob):
    import struct
    return struct.unpack(">II", blob[16:24])

_want = cfg.get("screenshot_size")
H.check("★★ 渲出来的图**尺寸真的等于配置值**（修 tres 之前这条是死的）",
        _png_size(images[0]) == (int(_want[0]), int(_want[1])),
        (_png_size(images[0]), tuple(_want)))

H.check("渲完场景里没有临时节点残留",
        not [c for c in obj.children()
             if c.name().startswith(viewport.TMP_PREFIX)],
        [c.name() for c in obj.children()
         if c.name().startswith(viewport.TMP_PREFIX)])
out_root = hou.node("/out")
H.check("out 里也没有残留",
        not [c for c in (out_root.children() if out_root else [])
             if c.name().startswith(viewport.TMP_PREFIX)])

H.check("渲过一次之后保险计数是 1",
        viewport._rendered_in_process[0] == 1,
        viewport._rendered_in_process[0])
second = schema.dispatch(ai, "look", {"view": "+z"})
H.check("★ 第二张被保险拦下（不冒险崩进程）",
        "error" in second and "已经出过一张图" in str(second.get("error")),
        second.get("error"))

H.finish("test_viewport")
