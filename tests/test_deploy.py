# -*- coding: utf-8 -*-
"""部署与热重载的测试。

    "D:/ProgramFiles/Houdini22/bin/hython.exe" tests/test_deploy.py

锁住的东西：
  1. 模板里的 __TOOL_ROOT__ 被替换了
  2. ★ .pypanel 真的是 XML（早期版本按纯 Python 写，Houdini 报
     "Start tag expected, '<' not found"）——而且真的能被 installFile 装上
  3. ★ 工具架**只有三个按钮**，名字是短英文
  4. ★ 所有 XML 属性值是 ASCII（中文会被解析器吃掉）
  5. 热重载能跑通，且不重启 Houdini 就把工具架和面板装进当前会话
"""

import os
import sys
import tempfile
import warnings

# ★ 必须在 import houdini_ai **之前**设：reload_all() 现在会顺手把命令桥
# 拉起来，不隔离的话测试进程会在真正的桥目录里留下一个死不掉的心跳文件。
os.environ["HOUDINI_AI_BRIDGE_DIR"] = tempfile.mkdtemp(
    prefix="houdini_ai_deploy_bridge_")

import _harness as H

H.section("pref 目录选择")
from houdini_ai import deploy, prefdirs

names = [os.path.basename(p) for p in prefdirs.all_pref_dirs()]
H.check("找得到 pref 目录", len(names) >= 1, names)
H.check("新版本排在前面",
        names == sorted(names, key=prefdirs.version_key, reverse=True), names)

picked = [os.path.basename(p) for p in prefdirs.candidate_pref_dirs()]
H.check("只挑当前版本（22.0）",
        all(n == "houdini22.0" for n in picked), picked)
H.check("旧版本目录不装",
        not any(n in ("houdini20.5", "houdini21.0") for n in picked), picked)
H.check("同版本的两个 pref 目录都选中（GUI 与 Git Bash 启动各用一个）",
        len(picked) >= 2, picked)

H.section("模板渲染")
shelf_text = deploy.render_shelf()
panel_text = deploy.render_panel()
root_forward = deploy.TOOL_ROOT.replace("\\", "/")
H.check("shelf 里没有残留占位符", "__TOOL_ROOT__" not in shelf_text)
H.check("panel 里没有残留占位符", "__TOOL_ROOT__" not in panel_text)
H.check("shelf 写进了真实工具根目录", root_forward in shelf_text)
H.check("panel 写进了真实工具根目录", root_forward in panel_text)

H.section("★ 所有 XML 属性值必须是纯 ASCII")
import re


def _attrs(text):
    body = re.sub(r"<!--.*?-->", "", text, flags=re.S)
    body = re.sub(r"<!\[CDATA\[.*?\]\]>", "", body, flags=re.S)
    return re.findall(r'\b(?:label|name|icon|help_url)="([^"]*)"', body)


for label, text in (("shelf", shelf_text), ("pypanel", panel_text)):
    attrs = _attrs(text)
    bad = [a for a in attrs if any(ord(ch) > 127 for ch in a)]
    H.check("%s 的属性值都是 ASCII" % label, not bad, bad)
    H.check("%s 有属性可查" % label, len(attrs) >= 3, len(attrs))

H.section("★ 工具架只有三个按钮，名字是短英文")
hou_shelf_tools = re.findall(r'<tool name="([^"]+)" label="([^"]+)"',
                             shelf_text)
H.check("★ 只有 3 个工具（曾经有 6 个，太臃肿被砍）",
        len(hou_shelf_tools) == 3, hou_shelf_tools)
names_only = [n for n, _ in hou_shelf_tools]
H.check("就是 New Node / Panel / Reload",
        sorted(names_only) == ["ai_new_node", "ai_open_panel", "ai_reload"],
        names_only)
for name, label in hou_shelf_tools:
    H.check("  %s 的标签是英文" % name,
            all(ord(c) < 128 for c in label), label)
    # ★ 用户定的命名规则：一律带 "AiSub" 前缀。光叫 "New Node" / "Panel" /
    # "Reload" 太泛，离开这个子菜单就不知道是干什么的。
    H.check("  %s 的标签带 AiSub 前缀" % name, "AiSub" in label, label)
    H.check("  %s 的标签不太长（<= 12 字符）" % name,
            len(label) <= 12, label)
H.check("没有中文标签（用户要英文名）",
        not any(ord(ch) > 127 for ch in "".join(
            lbl for _, lbl in hou_shelf_tools)))

# 砍掉的那些，名字里不该再出现
for gone in ("ai_convert_subnet", "ai_build_selected", "ai_dive_in"):
    H.check("已删除的 %s 不该回来" % gone, gone not in shelf_text)

H.section("★ .pypanel 必须是 XML 而不是 Python")
H.check("以 XML 声明开头", panel_text.lstrip().startswith("<?xml"))
H.check("有 pythonPanelDocument 根标签",
        "<pythonPanelDocument>" in panel_text)
H.check("有 interface 标签", "<interface " in panel_text)
H.check("有 onCreateInterface（Houdini 按这个函数拿控件）",
        "def onCreateInterface" in panel_text)
H.check("有 includeInToolbarMenu（否则不出现在标签菜单里）",
        "includeInToolbarMenu" in panel_text)
H.check("面板名对得上 deploy.PANEL_INTERFACE",
        'name="%s"' % deploy.PANEL_INTERFACE in panel_text)

H.section("真的能被 hou.pypanel 装进当前会话")
import hou

panel_path = deploy._write_temp(panel_text, "pypanel")
try:
    hou.pypanel.installFile(panel_path)
    H.check("installFile 没抛异常", True)
except Exception as exc:
    H.check("installFile 没抛异常", False, exc)

interface = hou.pypanel.interfaceByName(deploy.PANEL_INTERFACE)
H.check("面板接口注册上了", interface is not None)
if interface is not None:
    H.check("接口的 name 对", interface.name() == deploy.PANEL_INTERFACE)
    H.check("脚本非空", bool((interface.script() or "").strip()))
    H.check("脚本里有 panel_widget_class",
            "panel_widget_class" in (interface.script() or ""))
    H.check("面板标签是英文", interface.label() == "AI Nodes",
            interface.label())

H.section("shelf 真的能被加载，而且就是那三个")
shelf_path = deploy._write_temp(shelf_text, "shelf")
hou.shelves.loadFile(shelf_path)
for name, label in hou_shelf_tools:
    tool = hou.shelves.tool(name)
    H.check("工具 %s 加载成功" % name, tool is not None)
    if tool is None:
        continue
    H.check("  标签是 %r" % label, tool.label() == label, tool.label())
    H.check("  脚本非空", bool((tool.script() or "").strip()))
    H.check("  脚本会先做轻量重载（点按钮就是新代码）",
            "reload_quietly" in (tool.script() or "")
            or name == "ai_reload")

# ★ 这里**不能**用 hou.shelves.tool(name) is None 判断「已删除」——
# pref 目录里旧版部署的 shelf 文件会被 Houdini 在启动时自动加载，
# 那些老工具名仍然查得到。真正的保证是**部署出去的模板文件**里没有它们，
# 那条断言在上面（"已删除的 xxx 不该回来"）已经做了。
#
# 这里补一条：模板里出现过的三个工具，加载之后标签都对、脚本都能重载。
H.check("加载后三个工具的标签都还是英文名",
        all(hou.shelves.tool(n).label() == lbl
            for n, lbl in hou_shelf_tools
            if hou.shelves.tool(n) is not None),
        [(n, hou.shelves.tool(n).label() if hou.shelves.tool(n) else None)
         for n, lbl in hou_shelf_tools])
H.check("三个工具的脚本都指向 houdini_ai（不是老版本的残留）",
        all("houdini_ai" in (hou.shelves.tool(n).script() or "")
            for n, _ in hou_shelf_tools
            if hou.shelves.tool(n) is not None))

H.section("★ 记录一个陷阱：别给工具架打中文标签")
# 这条测试是**记录性**的，不是在用这个行为。
# 实测：一旦对 Tool 调 setLabel()，Houdini 会立刻把它加载的那个 .shelf 文件
# 整个写回磁盘，中文写成双重编码乱码（label="新建 AI 节点" -> "æ°å»º AI èç¹"），
# 下次启动读到乱码。所以工具架名字一律用英文。
# 哪天 Houdini 修了这个行为，这条会变红，那时可以重新考虑中文标签。
import tempfile

probe_dir = tempfile.mkdtemp(prefix="houdini_ai_trap_")
probe_path = os.path.join(probe_dir, "probe.shelf")
with open(probe_path, "w", encoding="utf-8") as fh:
    fh.write(shelf_text)
before = open(probe_path, "rb").read()

hou.shelves.loadFile(probe_path)
hou.shelves.tool("ai_new_node").setLabel("新建节点")
after = open(probe_path, "rb").read()
H.check("★ 确认陷阱仍在：setLabel 会立刻改写 .shelf 文件", after != before)

# 别拿「非 ASCII 字节数」当判据——Houdini 重写时会把中文**注释**全删掉，
# 所以字节数反而可能变少（实测 864 -> 24）。要看的是**属性值**变成了乱码。
after_attrs = _attrs(after.decode("utf-8", "replace"))
mangled = [a for a in after_attrs if any(ord(c) > 127 for c in a)]
H.check("★ 而且写进去的是乱码属性值（比英文标签还糟）",
        bool(mangled), after_attrs)
H.check("中文没被原样保住（就是那个双重编码问题）",
        "新建节点" not in after.decode("utf-8", "replace"))

H.section("★ install_runtime 一次装完（不重启 Houdini）")
ok, msgs = deploy.install_runtime()
H.check("install_runtime 返回成功", ok, msgs)
blob = " ".join(msgs)
H.check("它报告了工具架重载", "工具架" in blob, msgs)
H.check("它报告了面板定义重载", "面板定义" in blob, msgs)

H.section("热重载模块")
from houdini_ai import reload as reload_mod

H.check("paste_hint 给出一行可粘贴的命令",
        "reload_all" in reload_mod.paste_hint()
        and "sys.path.insert" in reload_mod.paste_hint(),
        reload_mod.paste_hint())

# reload_modules 只重载**已经 import 过**的模块，所以测试要先把它们 import 一遍
import houdini_ai.builder  # noqa: F401
import houdini_ai.catalog  # noqa: F401
import houdini_ai.config  # noqa: F401
import houdini_ai.gateway  # noqa: F401
import houdini_ai.node  # noqa: F401
import houdini_ai.panel  # noqa: F401
import houdini_ai.prompt  # noqa: F401
import houdini_ai.runner  # noqa: F401
import houdini_ai.schema  # noqa: F401
import houdini_ai.session  # noqa: F401
import houdini_ai.shelf  # noqa: F401

reloaded, failures, pruned = reload_mod.reload_modules()
H.check("重载了一批模块", len(reloaded) >= 10, reloaded)
H.check("没有模块重载失败", not failures, failures)
for name in ("config", "catalog", "builder", "runner", "panel", "session"):
    H.check("  重载了 %s" % name, name in reloaded, reloaded)

H.section("★★ 重载要清掉「源码里已经删了」的幽灵名字")
# `importlib.reload` 是在**同一个字典里重新执行**模块体，它不删已经消失的名字。
# 实测：删掉 runner.REPEAT_STOP / _sig 之后，跑着的 Houdini 里 hasattr 依然为真，
# 看起来像改动没生效——这类静默失效正是本项目最忌讳的东西。


class _FakeModule(object):
    """假的模块对象：只要能当字典用、能读 `__file__` 就够了。"""


_PROBE_DIR = tempfile.mkdtemp(prefix="houdini_ai_prune_probe_")


def _write_probe(name, source):
    path = os.path.join(_PROBE_DIR, name)
    with open(path, "w", encoding="utf-8") as fh:
        fh.write(source)
    return path


_probe_src = _write_probe("probe_a.py",
                          "import os\n"
                          "\n"
                          "KEPT = 1\n"
                          "ALSO = {'a': 1}\n"
                          "\n"
                          "def kept_fn():\n"
                          "    return 1\n"
                          "\n"
                          "class KeptClass(object):\n"
                          "    pass\n"
                          "\n"
                          "for _i in (1, 2):\n"
                          "    pass\n"
                          "\n"
                          "try:\n"
                          "    pass\n"
                          "except Exception as _exc:\n"
                          "    pass\n")
_probe = _FakeModule()
_probe.__file__ = _probe_src
_probe.KEPT = 1
_probe.ALSO = {"a": 1}
_probe.kept_fn = lambda: 1
_probe.KeptClass = object
_probe.os = os
_probe._i = 2            # for 循环变量，源码里有
_probe._exc = None       # except 名，源码里有
_probe.GONE = 2          # 源码里没有 → 该清掉
_probe._also_gone = 3    # 也没有 → 也该清掉
_probe.__doc__ = "内置的，不能动"

_keep = reload_mod._names_bound_by_source(_probe)
H.check("源码分析认得赋值/def/class/import/for/except 这几类名字",
        {"KEPT", "ALSO", "kept_fn", "KeptClass", "os", "_i", "_exc"} <= _keep,
        sorted(_keep))
_dropped = reload_mod._drop_stale_names(_probe, _keep)
H.check("★ 源码里已经没有的名字被清掉了",
        sorted(_dropped) == ["GONE", "_also_gone"], _dropped)
H.check("源码里有的名字一个都没动",
        _probe.KEPT == 1 and _probe.kept_fn() == 1 and _probe.os is os)
H.check("dunder 一个都不许动", _probe.__doc__ == "内置的，不能动")

# 读不到源码（编译进 exe、纯内存模块）时**宁可不删**——猜错就是删掉活着的
# 名字，那比留个幽灵名字糟得多。
_no_src = _FakeModule()
_no_src.whatever = 1
_dropped2 = reload_mod._drop_stale_names(
    _no_src, reload_mod._names_bound_by_source(_no_src))
H.check("★ 判断不了就不删（读不到源码时一个都不动）",
        _dropped2 == [] and _no_src.whatever == 1, _dropped2)

# `import *` 也是"判断不了"：源码上看不出会绑哪些名字。
_star = _FakeModule()
_star.__file__ = _write_probe("probe_b.py", "from os import *\n")
H.check("★ `import *` 时放弃判断（返回 None，不猜）",
        reload_mod._names_bound_by_source(_star) is None)

# 真实模块上跑一遍：源码里定义的东西必须**一个都不能少**。
_real = sys.modules["houdini_ai.runner"]
_real_keep = reload_mod._names_bound_by_source(_real)
H.check("真实模块也能分析出名字来（不是空集）",
        bool(_real_keep), _real_keep)
H.check("★ 对真实模块跑一遍，一个活着的名字都不许误删",
        reload_mod._drop_stale_names(_real, _real_keep) == [],
        sorted(_real_keep)[:6])

# ★ 端到端：手工塞一个源码里没有的名字进去，重载之后它必须消失。
# 这就是「删掉 runner.REPEAT_STOP 之后现场 hasattr 还是真」那件事的复现与验收。
_real.GHOST_PROBE = 1
reload_mod.reload_modules()
H.check("★★ 真实模块上的幽灵名字被重载清掉了（端到端）",
        not hasattr(_real, "GHOST_PROBE"),
        getattr(_real, "GHOST_PROBE", "<已清掉>"))

H.section("★ 源码体检：自己编译一遍，不许有无效转义这类警告")
# 起因：上面这个 ast 分析一加进来，跑测试就冒出
#   `SyntaxWarning: invalid escape sequence '\F'`
# ——我在 reload.py 的文档字符串里顺手写了个 Windows 路径 `D:\FabLibrary`。
# 这类东西**平时不报错**（Python 会保留原字符），但 `\U` / `\N` 开头的是真的
# 会被吃掉（本项目就踩过 `\U` 吃掉路径）。顺手体检一遍，免得下次又埋一颗。
import ast as _ast
import glob

_src_warnings = []
for _p in sorted(glob.glob(os.path.join(deploy.TOOL_ROOT, "houdini_ai",
                                        "*.py"))):
    try:
        _text = open(_p, encoding="utf-8").read()
    except Exception as exc:
        _src_warnings.append("%s: 读不了 %s" % (os.path.basename(_p), exc))
        continue
    with warnings.catch_warnings(record=True) as _caught:
        warnings.simplefilter("always")
        try:
            compile(_text, _p, "exec", _ast.PyCF_ONLY_AST)
        except SyntaxError as exc:
            _src_warnings.append("%s: 语法错误 %s"
                                 % (os.path.basename(_p), exc))
            continue
    for _item in _caught:
        _src_warnings.append("%s: %s"
                             % (os.path.basename(_p), _item.message))
H.check("★ 我们自己的源码编译一遍是干净的（无效转义 / 语法错误）",
        not _src_warnings, _src_warnings)

ok, msgs = reload_mod.reload_all()
H.check("reload_all 成功", ok, msgs)

# ★ 重装面板定义会让之前抓到的 interface 对象失效（"Interface not installed"）。
# 这正是 install_runtime 里要把 refresh_open_panels() 排在 installFile 之后的原因。
stale_error = None
try:
    interface.script()
except Exception as exc:
    stale_error = exc
H.check("重装定义后旧 interface 对象确实会失效（所以必须重新绑定）",
        stale_error is not None, stale_error)
fresh = hou.pypanel.interfaceByName(deploy.PANEL_INTERFACE)
H.check("重装后能重新拿到接口", fresh is not None)
if fresh is not None:
    H.check("新接口的脚本可用", bool((fresh.script() or "").strip()))

H.section("★ 重载时给已有的老节点补上新参数")
from houdini_ai import node as upgrade_node

geo_old = hou.node("/obj").createNode("geo", "R_upgrade_geo")
old = upgrade_node.make_ai_node(geo_old, "aiOld", prompt="x")
# 模拟「老节点」：把它后来才有的参数摘掉
hook = {"before": sorted(t.name() for t in old.parmTemplateGroup().parmTemplates()
                         if t.name().startswith("ai_"))}
grp = old.parmTemplateGroup()
for gone in (upgrade_node.FEEDBACK, upgrade_node.SEND):
    tpl = grp.find(gone)
    if tpl is not None:
        grp.remove(tpl)
old.setParmTemplateGroup(grp)
H.check("老节点确实少了新参数",
        old.parm(upgrade_node.FEEDBACK) is None
        and old.parm(upgrade_node.SEND) is None)

msgs = reload_mod.upgrade_existing_nodes()
H.check("升级过程有报告", any("补上" in m for m in msgs), msgs)
H.check("★ 老节点被补回了「修改意见」",
        old.parm(upgrade_node.FEEDBACK) is not None)
H.check("★ 老节点被补回了「发送修改意见」",
        old.parm(upgrade_node.SEND) is not None)
H.check("补回来之后参数齐全",
        sorted(t.name() for t in old.parmTemplateGroup().parmTemplates()
               if t.name().startswith("ai_")) ==
        sorted(upgrade_node.VISIBLE_PARMS),
        sorted(t.name() for t in old.parmTemplateGroup().parmTemplates()
               if t.name().startswith("ai_")))
H.check("二次升级是幂等的（不会重复报）",
        not any("补上" in m for m in reload_mod.upgrade_existing_nodes()))

H.section("有任务在跑时拒绝重载（否则会把工作线程的状态搞丢）")
from houdini_ai import runner

fake_path = "/obj/__fake_running__"
with runner._STATE_LOCK:
    runner._running[fake_path] = {"cancel": None, "thread": None}
try:
    ok, msgs = reload_mod.reload_all()
    H.check("busy 时拒绝重载", ok is False, msgs)
    H.check("说明了原因", any("跑" in m for m in msgs), msgs)
    ok2, _ = reload_mod.reload_all(force=True)
    H.check("force=True 能硬来", ok2 is True)
    reload_mod.reload_quietly()
    H.check("reload_quietly 有任务时不崩", True)
finally:
    with runner._STATE_LOCK:
        runner._running.pop(fake_path, None)

H.section("写文件到 pref 目录")
tmp_pref = tempfile.mkdtemp(prefix="houdini_ai_pref_")
deploy.deploy_to_prefs(extra_prefs=[tmp_pref])
shelf_dest = os.path.join(tmp_pref, deploy.SHELF_SUBDIR, deploy.SHELF_NAME)
panel_dest = os.path.join(tmp_pref, deploy.PANEL_SUBDIR, deploy.PANEL_NAME)
H.check("shelf 写出来了", os.path.isfile(shelf_dest))
H.check("panel 写出来了", os.path.isfile(panel_dest))
H.check("写出来的 shelf 和模板一致",
        open(shelf_dest, "r", encoding="utf-8").read() == shelf_text)

_, check_report = deploy.deploy_to_prefs(extra_prefs=[tmp_pref], check_only=True)
states = [r[0] for r in check_report]
H.check("--check 报告已是最新", all(s == "已是最新" for s in states), states)

deploy.deploy_to_prefs(extra_prefs=[tmp_pref])
H.check("能重复写（不报 PermissionError）", os.path.isfile(shelf_dest))
deploy.deploy_to_prefs(extra_prefs=[tmp_pref], uninstall=True)
H.check("能删掉", not os.path.isfile(shelf_dest))

H.section("★ pythonrc.py：启动时拉起命令桥，但不能碰用户自己的")
mine = deploy.pythonrc_path(tmp_pref)
H.check("目标就是 pref 目录下的 pythonrc.py",
        mine == os.path.join(tmp_pref, "pythonrc.py"), mine)

path, state = deploy.pythonrc_state(tmp_pref)
H.check("一开始没装", state == "没装", state)

H.check("写入成功", deploy.write_pythonrc(tmp_pref) == "已写入")
body = open(mine, "r", encoding="utf-8").read()
H.check("写进去的是拉桥的代码", "bridge" in body and "autostart" in body, body)
H.check("带我们的标记（好识别是不是我们写的）",
        deploy.PYTHONRC_MARKER in body)
H.check("路径指向工具根目录",
        deploy.TOOL_ROOT.replace("\\", "/") in body)
H.check("再写一次报「已是最新」",
        deploy.pythonrc_state(tmp_pref)[1] == "已是最新")

H.check("卸载时删得掉", deploy.remove_pythonrc(tmp_pref) == "已删除")
H.check("确实删了", not os.path.isfile(mine))

# ★ 用户自己的 pythonrc 绝对不碰
with open(mine, "w", encoding="utf-8") as fh:
    fh.write("# 用户自己的 pythonrc\nprint('用户的东西')\n")
H.check("认出这不是我们的",
        deploy.pythonrc_state(tmp_pref)[1].startswith("★"))
H.check("不覆盖它", deploy.write_pythonrc(tmp_pref).startswith("★"))
H.check("内容没被动",
        "用户的东西" in open(mine, "r", encoding="utf-8").read())
H.check("卸载时也保留",
        deploy.remove_pythonrc(tmp_pref) == "保留（不是我们写的）")
H.check("文件还在", os.path.isfile(mine))
os.remove(mine)

H.section("panel 桩里的函数名和实现对得上")
current = hou.pypanel.interfaceByName(deploy.PANEL_INTERFACE)
panel_script = current.script() if current is not None else ""
H.check("桩里定义了 onCreateInterface",
        "def onCreateInterface" in panel_script)
H.check("桩里定义了 onNodePathChanged",
        "def onNodePathChanged" in panel_script)
H.check("onNodePathChanged 会调 select_path",
        "select_path" in panel_script)

H.section("shelf.py 里的入口和按钮一一对应")
import houdini_ai.shelf as shelf_mod
from houdini_ai import node as node_mod

for fn in ("new_node_ui", "open_panel_ui", "convert_ui"):
    H.check("shelf.%s 存在" % fn, callable(getattr(shelf_mod, fn, None)))
for gone in ("build_selected_ui", "open_selected_network_ui"):
    H.check("已删除的 %s 不该回来" % gone,
            getattr(shelf_mod, gone, None) is None)

H.section("★★ 建节点必须走 Houdini 自己的放置交互（虚影 + 点击落位）")
# 裸调 parent.createNode() 会跳过整套放置交互：没有跟着鼠标的虚影、
# 没法自己选落点、也不会自动接到你选中的节点上。
# Houdini 自己的建节点工具全走 soptoolutils.genericTool，它内部会
#     pane.pushEventContext('nodegraphselectpos', context_data)
# 等用户点完再把 'pos' 塞回来。工具架工具完全能拿到这套交互。
import inspect

src = inspect.getsource(shelf_mod._create_with_placement)
H.check("用的是 soptoolutils.genericTool（Houdini 自己的工具助手）",
        "soptoolutils.genericTool" in src)
H.check("在 SOP 网络里才走它", 'category.name() == SOP' in src
        or "in_sop" in src)
H.check("处理了用户按 Esc 取消（OperationInterrupted）",
        "OperationInterrupted" in src)

# 放置交互本身在无界面环境测不了（isinstance(pane, hou.NetworkEditor)
# 造不出来）。能测的是：没有面板上下文时，兜底路径要正常。
plain = shelf_mod.new_node_ui({})
H.check("没有 pane 上下文也能建出 AI 节点",
        plain is not None and node_mod.is_ai_node(plain))
if plain is not None:
    H.check("  全套参数都在",
            all(plain.parm(p) is not None for p in node_mod.VISIBLE_PARMS))
    H.check("  回调脚本指向工具根目录",
            "houdini_ai" in (
                plain.parm(node_mod.BUILD).parmTemplate().scriptCallback()
                or ""))
    H.check("  节点注释里说明了下一步",
            "描述" in plain.comment(), plain.comment())


class _NotANetworkEditor(object):
    pass


other = shelf_mod.new_node_ui({"pane": _NotANetworkEditor()})
H.check("pane 不是 NetworkEditor 时也能兜住",
        other is not None and node_mod.is_ai_node(other))

H.check("kwargs 传 None 也不崩",
        shelf_mod.new_node_ui(None) is not None)

H.section("Esc 取消放置时安静收场，不报错")
# 直接测 genericTool 抛 OperationInterrupted 时的分支：把返回值显式当 False 处理。
H.check("_create_with_placement 的取消语义是 False（调用方据此静默返回）",
        "return False" in src)

script_blob = " ".join((hou.shelves.tool(n).script() or "")
                       for n, _ in hou_shelf_tools
                       if hou.shelves.tool(n) is not None)
H.check("按钮调 new_node_ui", "new_node_ui" in script_blob)
H.check("按钮调 open_panel_ui", "open_panel_ui" in script_blob)
H.check("convert_ui 不在任何按钮上（低频操作，不占菜单）",
        "convert_ui" not in script_blob)

H.finish("test_deploy")
