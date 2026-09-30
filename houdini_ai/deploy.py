# -*- coding: utf-8 -*-
"""部署：把工具架和面板装进 Houdini。

分两层：
  * **写文件**（`render_*` + `scripts/install.py`）—— 装到 pref 目录，供下次启动加载
  * **装进当前会话**（`install_runtime`）—— 调 `hou.shelves.loadFile` 和
    `hou.pypanel.installFile`，**不用重启 Houdini** 就生效

第二层是「改完不想重开 Houdini」那条需求的核心。三个 API 让它成立：
    hou.shelves.loadFile(path)               重新加载工具架
    hou.pypanel.installFile(path)            重新注册面板定义
    hou.PythonPanel.reloadActiveInterface()  刷新已经开着的面板标签

★ 关于中文：`.shelf` / `.pypanel` 的 XML **属性值只能 ASCII**
（实测 `label="新建 AI 节点"` 变成 `" AI "`，数字字符引用也一样被吃——
它们都不是真正的 XML 解析器）。所以工具架和面板的名字一律用英文。
中文界面在**节点参数**和**面板内部**，那些是 Qt / HOM 渲染的，没有限制。

★ 曾经为了给工具架打中文标签，这里有过一套 `setLabel()` + 文件只读 + pythonrc
的机制。后来用户明确要英文名，整套拆掉了。**代价是记住这个坑**：
一旦对 `Tool` 调 `setLabel()`，Houdini 会立刻把它加载的那个 .shelf 文件整个
写回磁盘，中文写成双重编码乱码（`label="æ°å»º AI èç¹"`），下次启动读到乱码，
比英文还糟。真要做中文标签，得配合把文件设成只读。
"""

import os

TOOL_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DEPLOY_DIR = os.path.join(TOOL_ROOT, "deploy")

# deploy/ 里的模板名 -> 装到 pref 目录里的文件名
SHELF_TEMPLATE = "ai.shelf"
SHELF_NAME = "houdini_ai.shelf"

# ★ 每个 Python Panel 是**独立的 PaneTabType**（`hou.paneTabType.PythonPanel` 的一个接口），
#   各自是一个可以单独停靠的面板标签——不是谁里面的子标签。
#   加面板就在这里加一条；**工具架按钮是另一回事**，不受这个影响
#   （工具架已经定死三个按钮）。
PANELS = [
    {"template": "ai_tools.pypanel", "name": "ai_tools.pypanel",
     "interface": "HoudiniAINodes", "label": "AI 节点"},
    {"template": "motion_tools.pypanel", "name": "motion_tools.pypanel",
     "interface": "HoudiniMotionGenerate", "label": "动作生成"},
]

# AI 面板的老名字（测试与旧代码在用），保留成别名
PANEL_TEMPLATE = PANELS[0]["template"]
PANEL_NAME = PANELS[0]["name"]
PANEL_INTERFACE = PANELS[0]["interface"]

SHELF_SUBDIR = "toolbar"
PANEL_SUBDIR = "python_panels"

PYTHONRC_NAME = "pythonrc.py"
PYTHONRC_MARKER = "# houdini-ai-toolset-pythonrc"

# 早期版本装过 pythonrc 用来给工具架打中文标签，后来拆掉了；
# 现在是**命令桥**需要它（Houdini 启动时把桥拉起来，好让 Claude Code
# 直接把 Python 送进来执行、改完自动热重载）。
PYTHONRC_TEMPLATE = '''# -*- coding: utf-8 -*-
%s
# 由「Houdini AI 工具集」的 scripts/install.py 写入。
#
# 唯一作用：Houdini 启动时把**命令桥**拉起来，这样 Claude Code 那边可以
# 把一小段 Python 直接送进来执行（改完代码自动热重载，不用你点按钮）。
#
# 桥**不监听任何端口**——它只是往 ~/.houdini-ai-bridge/queue 读写文件，
# 由 Houdini 自己的后台线程捡起来在主线程上执行。
# 详见 houdini_ai/bridge.py 顶部的说明。
#
# 不想要了：跑 scripts/install.py --uninstall，或者直接删掉这个文件。

import sys

_p = r"%s"
if _p not in sys.path:
    sys.path.insert(0, _p)

try:
    from houdini_ai import bridge as _bridge
    _bridge.autostart()
except Exception:
    # ★ 绝不静默。以前这里是个光秃秃的 `pass`，结果桥没起来的时候现场
    #   什么线索都没留下（2026-09-29 真踩到：新开的 Houdini 里桥不见了，
    #   而 --list 只报一句"找不到活着的实例"，无从查起）。
    #   现在把原因写进 ~/.houdini-ai-bridge/boot.log。
    try:
        import os as _os, time as _time, traceback as _tb
        _d = _os.path.expanduser("~/.houdini-ai-bridge")
        _os.makedirs(_d, exist_ok=True)
        with open(_os.path.join(_d, "boot.log"), "a", encoding="utf-8") as _fh:
            _fh.write("\\n===== %%s  pythonrc 启动桥失败 =====\\n"
                      %% _time.strftime("%%Y-%%m-%%d %%H:%%M:%%S"))
            _fh.write("python: %%s\\n" %% sys.version.split()[0])
            _fh.write("sys.path 里有工具目录: %%s\\n" %% (_p in sys.path))
            _fh.write(_tb.format_exc())
    except Exception:
        pass
'''


def render(template_name):
    """把模板里的 __TOOL_ROOT__ 换成真实路径。

    用正斜杠：Windows 认，而且塞进 Python 的 raw 字符串里不用转义。
    """
    path = os.path.join(DEPLOY_DIR, template_name)
    with open(path, "r", encoding="utf-8") as fh:
        text = fh.read()
    return text.replace("__TOOL_ROOT__", TOOL_ROOT.replace("\\", "/"))


def render_shelf():
    return render(SHELF_TEMPLATE)


def render_panel(template=None):
    """渲染一个面板模板。不给就用 AI 面板那个（向后兼容）。"""
    return render(template or PANEL_TEMPLATE)


def render_panels():
    """所有面板：[(接口名, 文件名, 内容文本), ...]。"""
    return [(p["interface"], p["name"], render(p["template"])) for p in PANELS]


def panel_interfaces():
    return [p["interface"] for p in PANELS]


# --------------------------------------------------------------------------
# 装进当前会话（不重启）
# --------------------------------------------------------------------------

def install_runtime(verbose=False):
    """把工具架和面板装进**正在运行的** Houdini。

    返回 (ok, 消息列表)。没有任何一步是致命的——某一步失败只记一笔，
    因为工具架和面板是独立的。
    """
    import hou

    messages = []

    try:
        shelf_path = _write_temp(render_shelf(), "shelf")
        hou.shelves.loadFile(shelf_path)
        messages.append("工具架已重载")
    except Exception as exc:
        messages.append("工具架重载失败：%s" % exc)

    for interface, name, text in render_panels():
        try:
            panel_path = _write_temp(text, "pypanel")
            hou.pypanel.installFile(panel_path)
            messages.append("面板定义已重载：%s" % name)
        except Exception as exc:
            messages.append("面板 %s 重载失败：%s" % (name, exc))

    # 顺序要紧：先装定义，再刷开着的标签。反了的话 reloadActiveInterface
    # 会去加载旧定义。
    messages.extend(refresh_open_panels())

    if verbose:
        for line in messages:
            print("  " + line)
    return True, messages


def _write_temp(text, suffix):
    """写到临时文件供 Houdini 加载。

    直接加载 pref 目录里那份也行，但那样就得依赖「已经装过」；
    写临时文件让这个函数在任何时候都能独立工作。
    """
    import tempfile

    handle, path = tempfile.mkstemp(prefix="houdini_ai_", suffix="." + suffix)
    os.close(handle)
    with open(path, "w", encoding="utf-8") as fh:
        fh.write(text)
    return path


def refresh_open_panels():
    """让已经开着的 AI 面板标签重新加载界面。

    没有 `reloadActiveInterface()` 的话，改完界面代码得手动关掉面板标签
    再开一次——比重启 Houdini 轻，但还是很烦。
    """
    import hou

    messages = []
    wanted = panel_interfaces()
    try:
        desktop = hou.ui.curDesktop()
        for pane_tab in desktop.paneTabs():
            if pane_tab.type() != hou.paneTabType.PythonPanel:
                continue
            try:
                interface = pane_tab.activeInterface()
            except Exception:
                continue
            if interface is None:
                continue
            name = getattr(interface, "name", lambda: "")()
            if name not in wanted:
                continue
            try:
                pane_tab.setActiveInterface(name)
                pane_tab.reloadActiveInterface()
                messages.append("已刷新打开着的面板：%s" % pane_tab.name())
            except Exception as exc:
                messages.append("刷新面板 %s 失败：%s" % (pane_tab.name(), exc))
    except Exception:
        # 没有桌面（hython）时会走到这
        pass
    return messages


# --------------------------------------------------------------------------
# 写文件到 pref 目录（scripts/install.py 调这个）
# --------------------------------------------------------------------------

def deploy_to_prefs(extra_prefs=None, version=None, every_version=False,
                    check_only=False, uninstall=False):
    from . import prefdirs

    prefs = extra_prefs or prefdirs.candidate_pref_dirs(
        version=version, every_version=every_version)

    shelf_text = render_shelf()
    panel_text = render_panel()
    report = []

    if uninstall:
        for pref in prefs:
            for sub, name in ((SHELF_SUBDIR, SHELF_NAME),
                              (PANEL_SUBDIR, PANEL_NAME)):
                dest = os.path.join(pref, sub, name)
                if os.path.isfile(dest):
                    os.remove(dest)
                    report.append(("删除", dest, name))
            for _iface, pname, _text in render_panels():
                dest = os.path.join(pref, PANEL_SUBDIR, pname)
                if os.path.isfile(dest):
                    os.remove(dest)
                    report.append(("删除", dest, pname))
        return prefs, report

    for pref in prefs:
        todo = [(SHELF_SUBDIR, SHELF_NAME, shelf_text),
                (PANEL_SUBDIR, PANEL_NAME, panel_text)]
        # 其余面板（动作生成等）——AI 面板已经在上面那条里了
        for _iface, pname, text in render_panels():
            if pname != PANEL_NAME:
                todo.append((PANEL_SUBDIR, pname, text))
        for sub, name, text in todo:
            dest = os.path.join(pref, sub, name)
            if check_only:
                if not os.path.isfile(dest):
                    state = "没装"
                else:
                    try:
                        with open(dest, "r", encoding="utf-8") as fh:
                            same = fh.read() == text
                    except Exception:
                        same = False
                    state = "已是最新" if same else "内容不同（需要重装）"
                report.append((state, dest, name))
                continue
            os.makedirs(os.path.dirname(dest), exist_ok=True)
            try:
                # 老版本把 shelf 设成过只读（那是打中文标签的配套措施），
                # 重装前先解开，否则写不进去
                mode = os.stat(dest).st_mode
                os.chmod(dest, mode | 0o200)
            except Exception:
                pass
            with open(dest, "w", encoding="utf-8") as fh:
                fh.write(text)
            report.append(("已写入", dest, name))

    return prefs, report


def pythonrc_body():
    return PYTHONRC_TEMPLATE % (PYTHONRC_MARKER,
                                TOOL_ROOT.replace("\\", "/"))


def pythonrc_path(pref):
    return os.path.join(pref, PYTHONRC_NAME)


def pythonrc_state(pref):
    """返回 (路径, 状态)。状态是给人看的一句话。"""
    path = pythonrc_path(pref)
    if not os.path.isfile(path):
        return path, "没装"
    try:
        with open(path, "r", encoding="utf-8") as fh:
            text = fh.read()
    except Exception:
        return path, "读不了"
    if PYTHONRC_MARKER not in text:
        # ★ 用户自己的 pythonrc 绝对不碰
        return path, "★ 已存在但不是我们写的，不会动它"
    if text == pythonrc_body():
        return path, "已是最新"
    return path, "是我们写的旧版本（需要更新）"


def write_pythonrc(pref):
    path, state = pythonrc_state(pref)
    if state.startswith("★"):
        return state
    try:
        with open(path, "w", encoding="utf-8") as fh:
            fh.write(pythonrc_body())
        return "已写入"
    except Exception as exc:
        return "写入失败：%s" % exc


def remove_pythonrc(pref):
    path, state = pythonrc_state(pref)
    if not os.path.isfile(path):
        return None
    if state.startswith("★"):
        return "保留（不是我们写的）"
    try:
        os.remove(path)
        return "已删除"
    except Exception as exc:
        return "删除失败：%s" % exc
