# -*- coding: utf-8 -*-
"""热重载：改完代码不用重启 Houdini。

## 怎么用

**在 Houdini 的 Python Shell 里粘这一行**（Python Shell 是 Houdini 自带的：
任意面板 → 右键标签 → Python Shell，或者 New Pane Tab Type → Python Shell）：

    import sys; sys.path.insert(0, r"<工具根目录>"); import houdini_ai.reload as r; r.reload_all()

工具架上也有一键的「重载 AI 工具」按钮，点一下等价。

**更省事的办法**：工具架的每个按钮在跑之前都会自动做一次轻量重载，
所以你改完 `houdini_ai/` 里的代码，**直接点任意 AI 按钮就是新代码**。

## 涵盖范围

**不只是 AI 节点那套。** 这个包里的东西都归 `reload_all()` 管：

| 改了 | 会被重载 |
|---|---|
| `houdini_ai/*.py` 任何逻辑 | ✅（含动作线的 `motion_*` / `soma_skeleton`） |
| 面板界面代码 | ✅（还会 `reloadActiveInterface()` 刷新开着的面板） |
| `deploy/*.shelf` / `*.pypanel` 模板 | ✅（`install_runtime` 是**现场渲染模板**的） |
| `bridge.py`（命令桥自己） | ✅（特殊处理：会**停掉再重启**桥线程，见下） |
| 新增/删除模块文件 | ⚠️ 重启 Houdini 最稳（`sys.modules` 里的残留不好清干净） |

**包外的东西不归它管**（比如 `D:/FabLibrary/_tools/fab_library.py`）——
那些得自己 `importlib.reload` 一下，用命令桥送：
`importlib.reload(sys.modules['fab_library'])`。

## 一个安全阀

有任务在跑的时候默认**拒绝重载**。重载会换掉 `runner` 模块里的对象，
那个正在跑的工作线程手里还攥着旧模块的东西，重载会让它的状态写不回去。
要么等它跑完，要么 `force=True` 硬来（不推荐）。
"""

import importlib
import ast
import os
import sys
import traceback

PACKAGE = "houdini_ai"

# 依赖顺序：被依赖的排前面。反了的话，reload 之后旧模块会持有新模块的对象引用，
# 出现「一半新一半旧」的诡异状态。
MODULE_ORDER = [
    "prefdirs",
    "config",
    "gateway",
    "catalog",
    "session",
    # 跨节点共享记忆 + 向上读一级。没有依赖，谁都能 import。
    "memory",
    "context",
    "node",
    "builder",
    # 离屏出图。`schema._t_look` 里懒 import 它，所以排 schema 前面。
    "viewport",
    "schema",
    "prompt",
    "deploy",
    "runner",
    # 动作线：骨架表/数据 → 落地理 → 会话模型 → 后端接口 → 面板
    "soma_skeleton",
    "motion_npz",
    "motion_rig",
    "motion_session",
    "motion_api",
    "motion_panel",
    "panel",
    "shelf",
    "reload",
    # 注：`bridge` **故意不在这里**——它是个线程，光换模块代码没用，
    # 得停掉再重启线程新代码才跑得起来。单独由 restart_bridge() 处理。
]


def _queue_is_busy(bridge_mod):
    """队列里还有没有没跑完的命令。

    ★ 判据刻意用**文件系统**，不用线程或模块状态。
    前面两版修复都栽在「猜当前代码是哪一版、跑在哪个线程」上：

      * 第一版判「当前线程是不是桥线程」——错。命令是桥用 hdefereval
        **送回主线程**执行的，那一刻当前线程就是主线程，恒为假。
      * 第二版判「桥模块上有没有『正在执行』标记」——也错。用户重启桥时
        Python 的 import 是**带缓存**的，`sys.modules` 里可能还是旧模块，
        新起的线程跑旧代码，永远不会设那个标记，读不到就吞成 False。

    队列文件不一样：`_run_one` 是在**跑完之后**才删那个 `.py` 的，
    所以「还有 `.py` 躺着」= 有命令正在跑，跟模块和线程的新旧完全无关。

    ⚠️ **踩过的坑**：这个函数第一版忘了 `import os`，`os.listdir` 抛
    NameError 被下面那个 `except Exception` 吞掉、直接返回 False ——
    **判据从来没工作过**，帮手永远不等队列。（真机上它看起来"修好了"，
    纯靠时序侥幸：命令跑完后工作线程正好立刻看到停止信号退出，赶上了
    帮手那 5 秒的等待窗口。命令一长就不成立。）
    所以异常路径必须**当作忙**：等一等总是安全的，贸然停桥会把桥弄死。
    """
    try:
        names = os.listdir(bridge_mod.QUEUE_DIR)
    except FileNotFoundError:
        return False          # 目录都没有，肯定没有命令在跑
    except Exception:
        # 读不出来时保守处理：当作有命令在跑。宁可多等一会儿。
        return True
    return any(name.endswith(".py") for name in names)


RESTART_THREAD_NAME = "houdini-ai-bridge-restart"


def _restart_in_progress():
    import threading

    return any(t.name == RESTART_THREAD_NAME and t.is_alive()
               for t in threading.enumerate())


def _deferred_restart(bridge_mod):
    """等当前这条命令跑完，再把桥停掉重起（换上新代码）。

    **一律走这条路**，不做「能不能同步做」的判断——那个判断正是前两次出错
    的地方。代价只是重启晚半秒落地，换来的是不可能再猜错。

    ★ **互斥**：已经在重启了就直接返回。两个帮手会各自 `stop()` + `start()`，
    交错之下会**起出两个桥线程**（实测踩到过：连点两下 Reload 就能触发）。
    用线程名做互斥标记——线程对象活过 `importlib.reload`，而名字是常量字符串，
    两样都跟模块版本无关。
    """
    import threading
    import time

    if _restart_in_progress():
        return

    def _worker():
        try:
            # 等队列排空（含：这条命令就是从桥里送进来、正卡在主线程上执行我们）
            deadline = time.time() + 30.0
            while time.time() < deadline and _queue_is_busy(bridge_mod):
                time.sleep(0.05)

            bridge_mod.stop()
            deadline = time.time() + 5.0
            while time.time() < deadline and bridge_mod.is_running():
                time.sleep(0.05)

            # 重载之后再起：正在跑的那份可能是旧代码
            fresh = importlib.reload(bridge_mod)
            fresh.start()
        except Exception:
            pass

    threading.Thread(target=_worker, daemon=True,
                     name=RESTART_THREAD_NAME).start()


def restart_bridge(messages):
    """让命令桥跑起来，并且跑的是**新代码**。

    两件事：

    1. **桥自己改了要停掉重启才生效。** `importlib.reload` 只换模块里的代码，
       正在跑的那个线程手里还是旧代码；而 `is_running()` 是按线程名判断的，
       所以不重启的话 `start()` 会以为它还在跑、直接返回——**改了等于没改**。
       顺序：先 stop（按线程名找，跟模块新旧无关），等它真退出，再 reload，
       最后 start。

    2. **原本没在跑的话也把它拉起来。** 这是给「Houdini 是在
       `pythonrc.py` 装上之前启动的」这种情况留的逃生口——那时候桥压根没起来，
       而用户点一次 Reload 就该把它带起来，不该逼他重启 Houdini。
       `start()` 本身幂等，所以这里无条件调是安全的。
    """
    import time

    try:
        from . import bridge as bridge_mod
    except Exception as exc:
        messages.append("拿不到命令桥模块：%s" % exc)
        return

    # ★★ 重启一律交给后台线程做，**不在这里同步做**。
    #
    # 为什么不能同步：这条命令**本身**很可能就是从桥里送进来的（实际上
    # 绝大多数情况都是）。那样的话桥的工作线程正卡在主线程上执行我们这段代码，
    # 它根本退不出去——同步的 `stop()` + 等它退出会死等到超时，随后 `start()`
    # 又因为 `is_running()` 还是 True 而直接返回；等命令终于跑完，那个线程
    # 看到停止信号就退了。**净结果：桥被弄死而且没重启。**
    #
    # 2026-09-29 为此连栽两次，都是因为想「判断一下能不能同步做」：
    #   第一版判「当前线程是不是桥线程」——命令是送回主线程跑的，恒为假。
    #   第二版判「模块上的执行中标记」——用户重启桥时 import 走缓存，
    #        跑的是旧模块，那个标记根本不存在，读不到就吞成了 False。
    # 所以现在**不判断了**：一律延迟。代价是晚半秒落地，换来不可能再猜错。
    if bridge_mod.is_running():
        _deferred_restart(bridge_mod)
        messages.append("命令桥会在后台重启（约半秒后生效，不影响本次结果）")
        return

    # 本来就没在跑（比如 Houdini 是在 pythonrc.py 装上之前启动的）：
    # 直接拉起来，这也是那种情况下的逃生口。
    try:
        fresh = importlib.reload(bridge_mod)
        if fresh.start():
            messages.append("命令桥已启动（Claude Code 现在能把 Python 送进来了）")
    except Exception as exc:
        messages.append("启动命令桥失败：%s" % exc)


def busy_jobs():
    """现在有几个任务在跑。"""
    try:
        from . import runner

        with runner._STATE_LOCK:
            return dict(runner._running)
    except Exception:
        return {}


def _names_bound_by_source(module):
    """这个模块**当前源码**可能绑定的所有名字（刻意往多里收）。

    ★★ 为什么需要它：`importlib.reload` 是**在同一个字典里重新执行**模块体，
    它**不会删掉**已经消失的名字。所以删掉一个常量或函数之后，正在跑的
    Houdini 里它**还在**。实测踩到：删掉 `runner.REPEAT_STOP` / `_sig` 之后，
    现场 `hasattr(runner, "REPEAT_STOP")` 依然为真——**看起来像改动没生效**，
    我差点据此以为重载失败。这是**静默失效**的一种，本项目明令禁止。

    （危险方向也成立：改名之后旧名字还挂着，老代码路径照样能调到它。）

    返回 None 表示**判断不了**（读不到源码 / 有 `import *`），那时宁可不删。
    """
    path = getattr(module, "__file__", None)
    if not path or not os.path.exists(path):
        return None
    try:
        with open(path, "r", encoding="utf-8") as fh:
            tree = ast.parse(fh.read())
    except Exception:
        return None

    names = set()
    for node in ast.walk(tree):
        # `from x import *` 的话，源码上看不出会绑什么名字——不猜，直接放弃。
        if isinstance(node, ast.ImportFrom):
            if any(a.name == "*" for a in node.names):
                return None
        if isinstance(node, ast.alias):
            names.add((node.asname or node.name).split(".")[0])
        elif isinstance(node, ast.Name) and isinstance(node.ctx, ast.Store):
            # 赋值/for 目标/comprehension 变量，任意层级都算——**多收无害**
            # （多留一个名字不会出事，误删一个会当场炸）。
            names.add(node.id)
        elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef,
                               ast.ClassDef)):
            names.add(node.name)
        elif isinstance(node, (ast.Global, ast.Nonlocal)):
            names.update(node.names)
        elif isinstance(node, ast.ExceptHandler) and node.name:
            names.add(node.name)
    return names


def _drop_stale_names(module, keep):
    """把「上次还在、这次源码里已经没有了」的名字从模块字典里摘掉。"""
    if keep is None:
        return []
    dropped = []
    for key in list(vars(module)):
        # dunder 一律不动（`__builtins__`/`__name__`/`__loader__` 这些
        # 是 import 机制塞的，源码里根本不出现）。
        if key.startswith("__") or key in keep:
            continue
        try:
            delattr(module, key)
        except Exception:
            continue
        dropped.append(key)
    return dropped


def reload_modules(verbose=False):
    """按依赖顺序重载包内所有模块。

    返回 `(重载的模块名, 失败清单, 清掉的幽灵名字)`。
    第三项是 `{模块名: [名字, …]}`——重载真正删掉了东西的证据。
    """
    reloaded, failures, pruned = [], [], {}
    for name in MODULE_ORDER:
        full = "%s.%s" % (PACKAGE, name)
        module = sys.modules.get(full)
        if module is None:
            # 还没被 import 过（比如 deploy 只在安装时才用），跳过
            continue
        keep = _names_bound_by_source(module)
        try:
            importlib.reload(module)
            gone = _drop_stale_names(module, keep)
            if gone:
                pruned[name] = gone
            reloaded.append(name)
            if verbose:
                print("  重载 %s%s" % (full, "（清掉旧名字：%s）"
                                      % "、".join(gone) if gone else ""))
        except Exception as exc:
            failures.append((full, "%s: %s" % (type(exc).__name__, exc)))
            if verbose:
                print("  失败 %s：%s" % (full, exc))
    return reloaded, failures, pruned


def reload_all(force=False, verbose=False, refresh_panels=True):
    """完整重载：模块 + 工具架 + 面板定义 + 中文标签 + 刷新开着的面板。

    返回 (ok, 消息列表)。ok=False 时消息里说明了原因。
    """
    messages = []

    running = busy_jobs()
    if running and not force:
        names = "、".join(sorted(running))
        return False, ["有任务正在跑（%s），等它跑完再重载。"
                       "确实要硬来的话用 reload_all(force=True)。" % names]

    # 1) 模块
    try:
        reloaded, failures, pruned = reload_modules(verbose=verbose)
        messages.append("重载了 %d 个模块" % len(reloaded))
        for name, why in failures:
            messages.append("模块 %s 重载失败：%s" % (name, why))
        # ★ 重载**删掉了东西**这件事要说出来。不说的话，删掉一个函数之后
        # 现场 `hasattr` 还是真，你没法判断是新代码没生效还是我把名字清了。
        for name, gone in sorted(pruned.items()):
            messages.append("清掉了 %s 里已经删掉的旧名字：%s"
                            % (name, "、".join(gone)))
    except Exception as exc:
        return False, ["重载模块时崩了：%s\n%s" % (exc, traceback.format_exc())]

    # 注意：模块重载之后，本模块自己的全局函数也换了对象。
    # 后面的步骤要用**新的** deploy 模块，所以重新 import 一次。
    try:
        from . import deploy as deploy_module

        if refresh_panels:
            # 面板刷新的顺序要紧：先装定义（installFile），再刷开着的标签。
            # 反了的话 reloadActiveInterface 会去加载旧定义。
            fresh = importlib.reload(deploy_module) if deploy_module else None
            ok, deploy_messages = (fresh or deploy_module).install_runtime()
            messages.extend(deploy_messages)
        else:
            ok, deploy_messages = deploy_module.install_runtime()
            messages.extend([m for m in deploy_messages
                             if "面板" not in m])
    except Exception as exc:
        messages.append("重装工具架/面板时出错：%s" % exc)

    messages.extend(upgrade_existing_nodes())
    restart_bridge(messages)

    return True, messages


def upgrade_existing_nodes():
    """给场景里已有的 AI 节点补上新加的参数。

    界面上加了新参数（比如「修改意见」+「发送修改意见」）之后，**之前建的
    节点身上没有它们**——重载代码不会自动补。不补的话你打开老节点，
    会以为新功能没生效。
    """
    try:
        from . import node as node_mod

        upgraded = []
        for node in node_mod.all_ai_nodes():
            try:
                if node_mod.ensure_parms(node):
                    node_mod.repair_callback(node)
                    upgraded.append(node.name())
            except Exception:
                continue
        if upgraded:
            return ["给 %d 个已有的 AI 节点补上了新参数：%s"
                    % (len(upgraded), "、".join(upgraded[:6]))]
    except Exception as exc:
        return ["给已有节点补参数时出错：%s" % exc]
    return []


def reload_quietly():
    """工具架按钮每次点击前跑的轻量重载。

    只重载模块，不碰工具架和面板定义——那两个重装开销大，而且按钮自己
    正在被点击，重装工具架有点自己揪自己头发。**失败一律吞掉**：
    重载失败不该让原本的按钮功能也不能用。
    """
    try:
        if busy_jobs():
            return
        reload_modules(verbose=False)
    except Exception:
        pass


def paste_hint():
    """给用户复制粘贴用的一行命令。"""
    from . import deploy

    return ('import sys; sys.path.insert(0, r"%s"); '
            'import houdini_ai.reload as r; r.reload_all(verbose=True)'
            % deploy.TOOL_ROOT.replace("\\", "/"))


def print_report(ok, messages):
    print("=" * 58)
    if ok:
        print("重载完成")
    else:
        print("没有重载")
    for line in messages:
        print("  · %s" % line)
    print("=" * 58)
    if ok:
        print("\n改的是面板界面代码的话，开着的面板标签已经自动刷过了。")
        print("如果界面还是旧的，把那个面板标签关掉再开一次。")
