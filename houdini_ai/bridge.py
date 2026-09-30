# -*- coding: utf-8 -*-
"""命令桥：让 Claude Code 把一小段 Python 送进**正在运行的** Houdini 里执行。

## 为什么是文件队列，不是远程 Python

Houdini 自带 `hrpyc`（rpyc 远程调用），`hrpyc.start_server()` 一行就能开。
**但它绑的是 `0.0.0.0`**——`start_server(port, use_thread, quiet)` 根本没有
host 参数，解析器里 `--host` 的默认值就是 `0.0.0.0`。那等于把你 Houdini 里的
Python 执行权开放给整个局域网，而且没有鉴权。

用户自己的调研里就把「绑 0.0.0.0」列成过「别碰某个工具」的理由。所以这里
**不监听任何端口**，走本地文件队列：

    写一个 .py 到 queue/  ->  Houdini 后台线程发现它  ->  主线程执行  ->  写 .out

代价是延迟约 0.25 秒，换来的是网络面上零暴露。而且客户端**不需要 Python**，
bash 直接写文件读文件就行（token 也最省）。

## 布局

    ~/.houdini-ai-bridge/
      instances/<pid>.json   每个活着的 Houdini 一份心跳，含场景路径和时间
      queue/<pid>__<req>.py  投递的命令（执行完就删）
      queue/<pid>__<req>.out 执行结果（JSON：stdout / stderr / error）

多个 Houdini 同时开着时靠文件名里的 pid 区分，互不干扰。
"""

import json
import os
import sys
import threading
import time
import traceback

# 目录可以用环境变量覆盖——测试用临时目录，免得污染真正的那份
# （scripts/hcmd.sh 认同一个环境变量）。
BRIDGE_DIR = os.environ.get("HOUDINI_AI_BRIDGE_DIR") or os.path.join(
    os.path.expanduser("~"), ".houdini-ai-bridge")
INSTANCE_DIR = os.path.join(BRIDGE_DIR, "instances")
QUEUE_DIR = os.path.join(BRIDGE_DIR, "queue")

THREAD_NAME = "houdini-ai-bridge"
POLL_SECONDS = 0.25
HEARTBEAT_SECONDS = 4.0
# 超过这么久没心跳就当这个实例没了（客户端据此挑活着的实例）
STALE_SECONDS = 20.0
# 结果文件留这么久，之后清掉
RESULT_TTL = 3600.0

MAX_OUTPUT = 20000


def _ensure_dirs():
    for path in (BRIDGE_DIR, INSTANCE_DIR, QUEUE_DIR):
        try:
            os.makedirs(path, exist_ok=True)
        except Exception:
            pass


# --------------------------------------------------------------------------
# 主线程调度（自己实现一份，不依赖 runner —— 桥要保持独立）
# --------------------------------------------------------------------------

_MAIN_THREAD_ID = None


def _note_main_thread():
    global _MAIN_THREAD_ID
    try:
        _MAIN_THREAD_ID = threading.current_thread().ident
    except Exception:
        pass


def _in_main(fn, *args, **kwargs):
    """在主线程上执行。已经主线程就直接跑。

    从主线程调 `hdefereval.executeInMainThreadWithResult` 会**死锁**
    （它阻塞等主线程事件循环来处理，而主线程正堵在这儿），所以必须判断。
    """
    if _MAIN_THREAD_ID is not None and \
            threading.current_thread().ident == _MAIN_THREAD_ID:
        return fn(*args, **kwargs)
    try:
        import hdefereval
    except ImportError:
        return fn(*args, **kwargs)
    return hdefereval.executeInMainThreadWithResult(fn, *args, **kwargs)


# --------------------------------------------------------------------------
# 执行
# --------------------------------------------------------------------------

def execute(code, filename="<bridge>"):
    """在主线程上执行一段代码，把输出和异常都收下来。

    注：这段代码是**给用户自己用**的逃生舱——你（或者 Claude）想干什么就
    干什么，包括动场景。桥不限制它能做什么，因为这是你自己的机器、
    你自己的 Houdini。它只是把「执行」这件事变得不用切窗口而已。
    """
    import io
    import contextlib

    buf_out, buf_err = io.StringIO(), io.StringIO()
    namespace = {"__name__": "__houdini_ai_bridge__", "__file__": filename}
    error = ""
    try:
        import hou

        namespace["hou"] = hou
    except Exception:
        pass

    try:
        compiled = compile(code, filename, "exec")
        with contextlib.redirect_stdout(buf_out), \
                contextlib.redirect_stderr(buf_err):
            exec(compiled, namespace)
    except Exception:
        error = traceback.format_exc()
    except SystemExit:
        error = "（代码里调了 SystemExit，已忽略）"

    def _clip(text):
        text = text or ""
        if len(text) > MAX_OUTPUT:
            return text[:MAX_OUTPUT] + "\n…（已截断）"
        return text

    return {
        "stdout": _clip(buf_out.getvalue()),
        "stderr": _clip(buf_err.getvalue()),
        "error": _clip(error),
    }


# --------------------------------------------------------------------------
# 实例心跳
# --------------------------------------------------------------------------

def _instance_path(pid):
    return os.path.join(INSTANCE_DIR, "%d.json" % pid)


def _write_heartbeat(pid):
    import hou

    info = {
        "pid": pid,
        "seen": time.time(),
        "started": _STARTED,
    }
    try:
        scene = hou.hipFile.path()
        info["scene"] = os.path.basename(scene) if scene else "(未保存)"
    except Exception:
        info["scene"] = "?"
    try:
        info["houdini"] = hou.applicationVersionString()
    except Exception:
        pass
    try:
        # 顺便报一下当前在哪个网络里，多实例时好认
        desktop = hou.ui.curDesktop()
        editor = desktop.paneTabOfType(hou.paneTabType.NetworkEditor)
        if editor is not None:
            info["pwd"] = editor.pwd().path()
    except Exception:
        pass

    try:
        tmp = _instance_path(pid) + ".tmp"
        with open(tmp, "w", encoding="utf-8") as fh:
            json.dump(info, fh, ensure_ascii=False)
        os.replace(tmp, _instance_path(pid))
    except Exception:
        pass


def _remove_heartbeat(pid):
    try:
        os.remove(_instance_path(pid))
    except Exception:
        pass


# --------------------------------------------------------------------------
# 队列处理
# --------------------------------------------------------------------------

def _pending(pid):
    """这个实例收到的、还没执行的命令，按文件名排序（= 投递顺序）。"""
    prefix = "%d__" % pid
    try:
        names = sorted(os.listdir(QUEUE_DIR))
    except Exception:
        return []
    out = []
    for name in names:
        if not name.startswith(prefix) or not name.endswith(".py"):
            continue
        path = os.path.join(QUEUE_DIR, name)
        try:
            # 投递方可能还在写，等到文件大小稳定再取。简单做法：
            # 看修改时间是不是已经过去一小会儿了。
            if time.time() - os.path.getmtime(path) < 0.05:
                continue
        except Exception:
            continue
        out.append(path)
    return out


def _run_one(path):
    name = os.path.basename(path)
    request_id = name[:-3]          # 去掉 .py
    result = {
        "request": request_id,
        "pid": os.getpid(),
        "time": time.time(),
    }
    try:
        with open(path, "r", encoding="utf-8") as fh:
            code = fh.read()
    except Exception as exc:
        result["error"] = "读不到命令文件：%s" % exc
        code = None

    if code is not None and not code.strip():
        # ★ 空命令一律拒绝，**不要执行**。
        # `exec("")` 不报错、什么都不做，返回的还是 ok:true、stdout 空——
        # 看起来完全正常，其实什么都没发生。这条路上摔过：hcmd.sh 的 `-f`
        # 分支漏了输出文件内容，一连推了两次「成功」的重载，其实什么都没做。
        # 客户端那道拦截只管 CLI 一个入口；这里堵住之后，**任何**往队列写空
        # 文件的路径（未来的脚本、手写、别的工具）都不会再静默成功。
        result["error"] = ("命令是空的，没有执行。"
                           "（exec 空脚本不会报错，只会安静地什么都不做）")
        code = None

    if code is not None:
        # 注：命令文件是在**跑完之后**才删的（见本函数末尾），
        # `reload._queue_is_busy()` 就靠「队列里还有没有 .py」判断
        # 「有没有命令正在跑」——那个判据跟线程/模块新旧无关，比往模块上
        # 挂标记可靠得多。别在这里提前删文件，会把那个判据弄坏。
        try:
            result.update(_in_main(execute, code, name))
        except Exception:
            result["error"] = traceback.format_exc()

    result["ok"] = not result.get("error")

    out_path = os.path.join(QUEUE_DIR, request_id + ".out")
    try:
        tmp = out_path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as fh:
            json.dump(result, fh, ensure_ascii=False, indent=1)
        os.replace(tmp, out_path)
    except Exception:
        pass

    try:
        os.remove(path)
    except Exception:
        pass


def _prune():
    """清掉过期的结果文件和**死掉的实例心跳**。

    心跳文件是在线程正常退出时删的。但如果 Houdini 被强杀（任务管理器、
    kill），那个线程根本没机会执行清理，文件就永远留着——`--list` 会一直
    列出几个早就没了的实例，看着很乱。所以让**任何活着的** Houdini 顺手
    把过期的清掉。
    """
    now = time.time()
    try:
        names = os.listdir(QUEUE_DIR)
    except Exception:
        names = []
    for name in names:
        if not name.endswith(".out"):
            continue
        path = os.path.join(QUEUE_DIR, name)
        try:
            if now - os.path.getmtime(path) > RESULT_TTL:
                os.remove(path)
        except Exception:
            continue

    try:
        beats = os.listdir(INSTANCE_DIR)
    except Exception:
        return
    for name in beats:
        if not name.endswith(".json"):
            continue
        path = os.path.join(INSTANCE_DIR, name)
        try:
            with open(path, "r", encoding="utf-8") as fh:
                info = json.load(fh)
            seen = float(info.get("seen") or 0)
        except Exception:
            seen = 0
        try:
            # 太久没心跳 = 那个进程没了（被强杀的话它自己清不掉）
            if now - seen > STALE_SECONDS:
                os.remove(path)
        except Exception:
            continue


# --------------------------------------------------------------------------
# 线程
# --------------------------------------------------------------------------

_STARTED = time.time()

# 注：这里曾经有过一个 `_EXECUTING` 模块级标记 + `is_executing()`，用来让
# reload 判断「有没有命令正在跑」。**删掉了**——模块级状态会被
# `importlib.reload` 重置，而且不能可靠地反映正在跑的那个线程用的是哪一版代码。
# 现在 `reload._queue_is_busy()` 直接看队列里还有没有 `.py` 文件，
# 用文件系统判断，跟模块和线程的新旧完全无关。


def is_running():
    """桥是不是已经在跑了。

    用线程名判断而不是模块全局变量——`reload_all()` 会重载本模块，
    模块级的标志位会被重置，但线程还在跑。不这么判的话每次重载都会
    再起一个线程。
    """
    return any(t.name == THREAD_NAME and t.is_alive()
               for t in threading.enumerate())


def _loop(stop_event, pid):
    last_beat = 0.0
    last_prune = 0.0
    while not stop_event.is_set():
        now = time.time()
        if now - last_beat >= HEARTBEAT_SECONDS:
            last_beat = now
            try:
                _in_main(_write_heartbeat, pid)
            except Exception:
                pass
        try:
            for path in _pending(pid):
                if stop_event.is_set():
                    break
                _run_one(path)
        except Exception:
            pass
        if now - last_prune >= 60.0:
            last_prune = now
            _prune()
        stop_event.wait(POLL_SECONDS)
    _remove_heartbeat(pid)


BOOT_LOG = os.path.join(BRIDGE_DIR, "boot.log")


def log_boot_failure(why):
    """把"桥没起来"的原因写进 boot.log。

    ★ 为什么不 print：pythonrc 跑的时候 Houdini 的界面还没出来，stdout
    不一定落到用户看得到的地方；而且这个工具集的一条硬纪律就是
    **不许静默失效**——2026-09-29 桥没起来时现场什么线索都没有，
    只能靠猜。留个文件，下次直接读。
    """
    try:
        _ensure_dirs()
        with open(BOOT_LOG, "a", encoding="utf-8") as fh:
            fh.write("\n===== %s  启动桥失败：%s =====\n"
                     % (time.strftime("%Y-%m-%d %H:%M:%S"), why))
            fh.write("pid=%d  python=%s\n"
                     % (os.getpid(), sys.version.split()[0]))
            fh.write(traceback.format_exc())
    except Exception:
        pass


def _sweep_dead_instances():
    """清掉死掉进程留下的心跳文件。

    ★ 为什么要清：Houdini 被**强杀**时来不及调 `_remove_heartbeat`，文件会留下。
    更糟的是实测过一种情况——NTFS 在被强杀时留下一个**全是 NUL 的文件**
    （长度对、内容是零，数据没来得及落盘）。那种文件读不出 pid，
    客户端只会莫名其妙地跳过它，越积越多。

    只在**自己启动时**扫一次，而且阈值给得很宽（`STALE_SECONDS` 的 10 倍）：
    别的活着的实例心跳慢一点也绝不会被误删。
    """
    limit = STALE_SECONDS * 10
    now = time.time()
    try:
        names = os.listdir(INSTANCE_DIR)
    except Exception:
        return
    for name in names:
        if not name.endswith(".json"):
            continue
        path = os.path.join(INSTANCE_DIR, name)
        try:
            with open(path, "r", encoding="utf-8") as fh:
                info = json.load(fh)
            seen = float(info.get("seen") or 0)
            if now - seen <= limit:
                continue
        except Exception:
            # 读不出来就是坏的——但**不能立刻删**：可能是另一个正在启动的
            # 实例刚 open() 还没写完。用文件修改时间兜一下。
            try:
                if now - os.path.getmtime(path) <= limit:
                    continue
            except Exception:
                continue
        try:
            os.remove(path)
        except Exception:
            pass


def start(verbose=False):
    """在当前 Houdini 里启动命令桥。幂等——重复调不会起第二个线程。"""
    _note_main_thread()
    _ensure_dirs()
    _sweep_dead_instances()

    if is_running():
        if verbose:
            print("命令桥已经在跑了，pid=%d" % os.getpid())
        return False

    stop_event = threading.Event()
    pid = os.getpid()
    thread = threading.Thread(target=_loop, args=(stop_event, pid),
                              name=THREAD_NAME, daemon=True)
    # 把停止开关挂在**线程对象**上，这样 reload 之后 stop() 还能找到它
    thread._bridge_stop = stop_event
    thread.start()

    if verbose:
        print("命令桥已启动：pid=%d，目录 %s" % (pid, QUEUE_DIR))
    return True


def stop():
    for thread in threading.enumerate():
        if thread.name != THREAD_NAME:
            continue
        event = getattr(thread, "_bridge_stop", None)
        if event is not None:
            event.set()
            return True
    return False


def autostart():
    """给 pythonrc.py 用：等界面真的起来了再启动桥。

    ★ 为什么不直接在 pythonrc 里调 start()：pythonrc 跑得比界面事件循环还早。
    桥的第一次心跳要**回主线程**（`hdefereval.executeInMainThreadWithResult`），
    而那个函数是阻塞等事件循环来处理任务的——事件循环还没起来的话，
    桥的线程会一直卡在第一次心跳上，等于没启动。

    挂个事件循环回调，等它真的开始跑了再启动，就稳了。
    """
    try:
        import hou
    except Exception:
        return False

    def _kick():
        try:
            hou.ui.removeEventLoopCallback(_kick)
        except Exception:
            pass
        try:
            start()
        except Exception:
            # ★ 这个盲区 2026-09-29 才发现：异常发生在**事件循环回调里**时，
            #   `autostart()` 早就返回 True 了，pythonrc 那层的 try/except
            #   根本看不到——于是桥没起来，而 boot.log 一片空白。
            #   所以这里必须自己记一笔。
            log_boot_failure("事件循环回调里启动桥失败")

    try:
        hou.ui.addEventLoopCallback(_kick)
        return True
    except Exception:
        # 没有界面（hython）或者 hou.ui 还没就绪——直接起，
        # 反正线程会自己等事件循环
        try:
            return start()
        except Exception:
            return False


def status():
    """给自己看的一句话状态。"""
    if not is_running():
        return "命令桥：没在跑"
    return "命令桥：在跑（pid=%d），队列目录 %s" % (os.getpid(), QUEUE_DIR)


# --------------------------------------------------------------------------
# 客户端用得到的小工具（Houdini 侧也能调）
# --------------------------------------------------------------------------

def instances():
    """所有活着的 Houdini 实例。客户端用它挑目标。"""
    now = time.time()
    found = []
    try:
        names = os.listdir(INSTANCE_DIR)
    except Exception:
        return found
    for name in names:
        if not name.endswith(".json"):
            continue
        path = os.path.join(INSTANCE_DIR, name)
        try:
            with open(path, "r", encoding="utf-8") as fh:
                info = json.load(fh)
        except Exception:
            continue
        if now - float(info.get("seen") or 0) > STALE_SECONDS:
            continue
        found.append(info)
    found.sort(key=lambda i: i.get("started") or 0, reverse=True)
    return found
