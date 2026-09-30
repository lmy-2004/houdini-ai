# -*- coding: utf-8 -*-
"""命令桥的测试。

    "D:/ProgramFiles/Houdini22/bin/hython.exe" tests/test_bridge.py

桥的作用：让 Claude Code 把一小段 Python 送进**正在运行的** Houdini 里执行，
这样改完代码能自动热重载，不用人去点按钮。

★ 环境变量要在 import bridge **之前**设好——目录是在模块导入时读的。
"""

import os
import tempfile
import time

_TMP = tempfile.mkdtemp(prefix="houdini_ai_bridge_test_")
os.environ["HOUDINI_AI_BRIDGE_DIR"] = _TMP

import _harness as H

H.section("搭一个隔离的桥目录")
from houdini_ai import bridge

H.check_eq("桥目录用的是环境变量给的临时目录", bridge.BRIDGE_DIR, _TMP)
H.check("临时目录跟真正那份不是一回事",
        _TMP != os.path.join(os.path.expanduser("~"), ".houdini-ai-bridge"))

H.section("execute：执行、收输出、抓异常")
r = bridge.execute("print('你好')")
H.check("stdout 收到了", "你好" in r["stdout"], r)
H.check("没有报错", not r["error"], r)
H.check("stderr 是空的", not r["stderr"], r)

r = bridge.execute("x = 1 + 1\nprint(x)")
H.check("算术和变量都对", r["stdout"].strip() == "2", r)

r = bridge.execute("raise ValueError('故意炸的')")
H.check("★ 异常被抓住而不是抛出去", "ValueError" in r["error"], r)
H.check("异常里有原始信息", "故意炸的" in r["error"], r)

r = bridge.execute("import sys; print('err', file=sys.stderr)")
H.check("stderr 也收得到", "err" in r["stderr"], r)

r = bridge.execute("print(hou.applicationVersionString())")
H.check("★ hou 被自动注入（不用自己 import）",
        not r["error"] and r["stdout"].strip(), r)

r = bridge.execute("for i in range(20000): print('x' * 10)")
H.check("超长输出会被截断（别把结果文件撑爆）",
        "已截断" in r["stdout"], len(r["stdout"]))

H.section("★ start：幂等，重复调不会起第二个线程")
import threading

H.check("一开始没在跑", not bridge.is_running())
started = bridge.start()
H.check("start() 返回 True（这次真起了）", started is True)
H.check("is_running() 为真", bridge.is_running())
count = sum(1 for t in threading.enumerate() if t.name == bridge.THREAD_NAME)
H.check_eq("只有一个桥线程", count, 1)
H.check("再调一次返回 False（不会起第二个）", bridge.start() is False)
count = sum(1 for t in threading.enumerate() if t.name == bridge.THREAD_NAME)
H.check_eq("线程数还是 1", count, 1)

H.section("心跳：客户端靠它发现实例")
deadline = time.time() + 5
beat = None
while time.time() < deadline:
    found = bridge.instances()
    if any(i.get("pid") == os.getpid() for i in found):
        beat = [i for i in found if i.get("pid") == os.getpid()][0]
        break
    time.sleep(0.1)
H.check("★ 心跳文件出现了", beat is not None, os.listdir(_TMP))
if beat:
    H.check("  心跳里有 pid", beat.get("pid") == os.getpid(), beat)
    H.check("  心跳里有场景名", "scene" in beat, beat)
    H.check("  心跳有 started 时间戳", bool(beat.get("started")), beat)

H.section("★★ 完整往返：投递一个 .py，拿回 .out")
pid = os.getpid()
req = "%d__%d" % (pid, int(time.time() * 1000))
in_path = os.path.join(bridge.QUEUE_DIR, req + ".py")
out_path = os.path.join(bridge.QUEUE_DIR, req + ".out")
with open(in_path, "w", encoding="utf-8") as fh:
    fh.write("import hou\n"
             "obj = hou.node('/obj')\n"
             "print('容器数=%d' % len(obj.children()))\n")

deadline = time.time() + 10
while time.time() < deadline and not os.path.isfile(out_path):
    time.sleep(0.05)

H.check("★ 结果文件回来了", os.path.isfile(out_path))
if os.path.isfile(out_path):
    import json

    with open(out_path, "r", encoding="utf-8") as fh:
        result = json.load(fh)
    H.check("  执行成功", result.get("ok") is True, result)
    H.check("  拿得到 Houdini 里的东西",
            "容器数=" in (result.get("stdout") or ""), result)
    H.check("  request id 对得上", result.get("request") == req, result)
H.check("★ 投递进去的 .py 被执行完就删了（不堆积）",
        not os.path.isfile(in_path))

H.section("★ 多实例隔离：别人的命令我不碰")
other = "%d__1" % (pid + 99999)
other_path = os.path.join(bridge.QUEUE_DIR, other + ".py")
with open(other_path, "w", encoding="utf-8") as fh:
    fh.write("print('这是别的实例的')\n")
time.sleep(0.6)
H.check("别的实例的 .py 还在（本实例没去执行它）",
        os.path.isfile(other_path))
H.check("也没有给它写结果",
        not os.path.isfile(os.path.join(bridge.QUEUE_DIR, other + ".out")))
os.remove(other_path)

H.section("★ 命令里报错时，客户端能看出来")
req2 = "%d__%d" % (pid, int(time.time() * 1000) + 1)
in2 = os.path.join(bridge.QUEUE_DIR, req2 + ".py")
out2 = os.path.join(bridge.QUEUE_DIR, req2 + ".out")
with open(in2, "w", encoding="utf-8") as fh:
    fh.write("raise RuntimeError('这轮要失败')\n")
deadline = time.time() + 10
while time.time() < deadline and not os.path.isfile(out2):
    time.sleep(0.05)
if os.path.isfile(out2):
    import json

    with open(out2, "r", encoding="utf-8") as fh:
        failed = json.load(fh)
    H.check("  ok 是 False", failed.get("ok") is False, failed)
    H.check("  error 里有原因", "这轮要失败" in (failed.get("error") or ""),
            failed)

H.section("★ stop：停得掉，而且停完还能重新起")
H.check("stop 返回 True", bridge.stop() is True)
deadline = time.time() + 5
while time.time() < deadline and bridge.is_running():
    time.sleep(0.05)
H.check("停掉了", not bridge.is_running())
H.check("心跳文件也被清掉了",
        not os.path.isfile(os.path.join(bridge.INSTANCE_DIR,
                                        "%d.json" % os.getpid())))
H.check("还能重新起来", bridge.start() is True)
bridge.stop()

H.section("status 是给人看的一句话")
H.check("status 返回字符串", isinstance(bridge.status(), str), bridge.status())

H.section("★★ 热重载要把桥线程也重启（否则 bridge.py 改了等于没改）")
# 桥是线程：importlib.reload 只换模块代码，正在跑的线程手里还是旧的；
# 而 is_running() 按线程名判断，不重启的话 start() 会以为它还在跑直接返回。
#
# ★ 这里必须先确定桥真的起来了。上一节结尾调过 stop()，而 stop 是异步的
# （线程最多 0.25 秒后才退），紧接着判断 is_running() 会得到误导性的 True
# ——测试一开始就是被这个坑到，表现为「桥被弄死了没重启」。
# 先停干净（stop 是异步的），再起——不然 start() 撞上还在退的线程会直接返回 False
bridge.stop()
_deadline = time.time() + 3
while time.time() < _deadline and bridge.is_running():
    time.sleep(0.05)
H.check("前一次的桥已停干净", not bridge.is_running())

bridge.start()
_deadline = time.time() + 3
while time.time() < _deadline and not bridge.is_running():
    time.sleep(0.05)
H.check("先确认桥真的在跑", bridge.is_running())
before_thread = [t for t in threading.enumerate()
                 if t.name == bridge.THREAD_NAME]
H.check("  只有一个桥线程", len(before_thread) == 1, len(before_thread))

from houdini_ai import reload as reload_mod


def settle(timeout=20):
    """等异步重启落地干净。

    重启是**故意异步**的（`_deferred_restart` 交给帮手线程）。测试如果不等它
    落地就操作桥的生命周期，就会出现「上一节的帮手还在跑、这一节又 start()
    一次」的交叉，表现为多出一个桥线程——**是测试在跟产品的异步打架，
    不是产品的问题**。每个碰生命周期的段落前后都调它。
    """
    deadline = time.time() + timeout
    while time.time() < deadline:
        helpers = [t for t in threading.enumerate()
                   if t.name == reload_mod.RESTART_THREAD_NAME
                   and t.is_alive()]
        if not helpers:
            return True
        time.sleep(0.05)
    return False


H.check("队列空闲时 _queue_is_busy() 是假",
        not reload_mod._queue_is_busy(bridge))

ok, msgs = reload_mod.reload_all(verbose=False)
H.check("reload_all 成功", ok, msgs)
H.check("★ 报告里说桥会在后台重启",
        any("后台重启" in m for m in msgs), msgs)

# ★ 重启是**异步**的：不判断「能不能同步做」，一律交给后台线程。
# 那个判断正是前两次把桥弄死的原因（详见 reload.restart_bridge 的注释）。
H.check("reload_all 当场返回时桥还没被重启（异步的）",
        "命令桥已启动" not in " ".join(msgs))

_deadline = time.time() + 12
after_thread = []
while time.time() < _deadline:
    after_thread = [t for t in threading.enumerate()
                    if t.name == bridge.THREAD_NAME]
    if after_thread and after_thread[0] is not before_thread[0]:
        break
    time.sleep(0.05)

_diag = "队列=%s 线程=%s" % (
    sorted(os.listdir(bridge.QUEUE_DIR))
    if os.path.isdir(bridge.QUEUE_DIR) else "?", after_thread)
H.check("★ 后台重启落地了，桥在跑（没被弄死）", bridge.is_running(), _diag)
settle()
H.check("★ 仍然只有一个桥线程（没有起两个）",
        len(after_thread) == 1, (len(after_thread), _diag))
H.check("★ 确实是新线程（旧的那个退了）",
        bool(after_thread) and after_thread[0] is not before_thread[0], _diag)

# 重启之后还得能干活
req3 = "%d__%d" % (os.getpid(), int(time.time() * 1000) + 2)
in3 = os.path.join(bridge.QUEUE_DIR, req3 + ".py")
out3 = os.path.join(bridge.QUEUE_DIR, req3 + ".out")
with open(in3, "w", encoding="utf-8") as fh:
    fh.write("print('重启之后还能干活')\n")
deadline = time.time() + 10
while time.time() < deadline and not os.path.isfile(out3):
    time.sleep(0.05)
H.check("★ 重启之后仍然能执行命令", os.path.isfile(out3))
if os.path.isfile(out3):
    import json

    with open(out3, "r", encoding="utf-8") as fh:
        r3 = json.load(fh)
    H.check("  输出对", "重启之后还能干活" in (r3.get("stdout") or ""), r3)

# 这一节把桥留在跑着的状态了，收尾前停掉，免得后面的清理等不到它退出
bridge.stop()

H.section("★ 重载要能把「原本没在跑」的桥拉起来")
# 逃生口：Houdini 是在 pythonrc.py 装上之前启动的，桥压根没起来。
# 那时候用户点一次 Reload（= reload_all）就该把它带起来，
# 不该逼他重启 Houdini。
bridge.stop()
_deadline = time.time() + 3
while time.time() < _deadline and bridge.is_running():
    time.sleep(0.05)
H.check("先确认桥是停着的", not bridge.is_running())

msgs2 = []
reload_mod.restart_bridge(msgs2)
H.check("★ 没在跑时走的是同步启动（不需要等队列）",
        bridge.is_running())
H.check("★ 报告里说了「已启动」",
        any("命令桥已启动" in m for m in msgs2), msgs2)

# 已经在跑的时候交给后台，不当场重启
msgs3 = []
reload_mod.restart_bridge(msgs3)
H.check("已经在跑时报的是「会在后台重启」",
        any("后台重启" in m for m in msgs3), msgs3)

_deadline = time.time() + 10
while time.time() < _deadline and not bridge.is_running():
    time.sleep(0.05)
settle()
H.check("后台重启之后仍然只有一个桥线程",
        sum(1 for t in threading.enumerate()
            if t.name == bridge.THREAD_NAME) == 1,
        [t.name for t in threading.enumerate()])
bridge.stop()

H.section("★★ 队列里有命令在跑时，重启必须走延迟路径")
# 这条替代了原来那个「让桥去执行 reload_all」的自指测试——那个在进程内
# 会重载**测试进程自己正在跑的模块**，本质上是自己改自己，偶发把整个
# 测试进程弄死（退出码 1、没有 traceback、没有汇总）。hython 里没有真正的
# 事件循环，那条线程路径本来也模拟不忠实。
#
# 所以直接测**判据本身**：队列里还有 .py 躺着 = 有命令在跑 = 必须延迟。
# 判据用的就是文件系统，跟线程和模块版本无关（这是第三次修复换来的教训）。
bridge.stop()
_deadline = time.time() + 3
while time.time() < _deadline and bridge.is_running():
    time.sleep(0.05)
bridge.start()
_deadline = time.time() + 3
while time.time() < _deadline and not bridge.is_running():
    time.sleep(0.05)
H.check("桥起来了", bridge.is_running())

# 手动放一个「正在跑」的命令文件进去（没有工作线程会消费它，它会一直留着）
busy_req = "%d__%d" % (os.getpid() + 12345, int(time.time() * 1000))
busy_path = os.path.join(bridge.QUEUE_DIR, busy_req + ".py")
with open(busy_path, "w", encoding="utf-8") as fh:
    fh.write("print('假装这条命令正在跑')\n")

H.check("★ 队列被判为忙", reload_mod._queue_is_busy(bridge),
        "队列=%s 文件名=%s" % (sorted(os.listdir(bridge.QUEUE_DIR)),
                            os.path.basename(busy_path)))
msgs_busy = []
reload_mod.restart_bridge(msgs_busy)
H.check("★★ 忙的时候走延迟路径（不当场重启）",
        any("后台重启" in m for m in msgs_busy), msgs_busy)
H.check("  而且当场没有启动新桥", "命令桥已启动" not in " ".join(msgs_busy),
        msgs_busy)

# 忙的时候后台帮手应该一直在等：给 3 秒，它不该动
time.sleep(3)
H.check("★ 队列还堵着时，后台帮手不会硬来（桥还是原来那个）",
        bridge.is_running())

# 把「正在跑」的命令挪走 —— 帮手应该立刻完成重启
os.remove(busy_path)
H.check("队列不再忙", not reload_mod._queue_is_busy(bridge))
_deadline = time.time() + 10
while time.time() < _deadline and not bridge.is_running():
    time.sleep(0.05)
H.check("★★ 队列空了之后桥被重启回来，还活着", bridge.is_running())
settle()
H.check("  仍然只有一个桥线程",
        sum(1 for t in threading.enumerate()
            if t.name == bridge.THREAD_NAME) == 1,
        [t.name for t in threading.enumerate()])

# 重启之后还得能干活
req5 = "%d__%d" % (os.getpid(), int(time.time() * 1000) + 4)
in5 = os.path.join(bridge.QUEUE_DIR, req5 + ".py")
out5 = os.path.join(bridge.QUEUE_DIR, req5 + ".out")
with open(in5, "w", encoding="utf-8") as fh:
    fh.write("print('重启之后还能干活')\n")
deadline = time.time() + 10
while time.time() < deadline and not os.path.isfile(out5):
    time.sleep(0.05)
H.check("★ 重启之后仍然能执行命令", os.path.isfile(out5))
if os.path.isfile(out5):
    with open(out5, "r", encoding="utf-8") as fh:
        r5 = json.load(fh)
    H.check("  输出对", "重启之后还能干活" in (r5.get("stdout") or ""), r5)
bridge.stop()

H.section("★ 清理死掉的实例心跳（被强杀的 Houdini 自己清不掉）")
# 心跳是线程正常退出时删的；Houdini 被强杀就永远留着，--list 会一直列出
# 几个早就没了的实例。所以让任何活着的 Houdini 顺手把过期的清掉。
import json as _json

dead = os.path.join(bridge.INSTANCE_DIR, "999999.json")
with open(dead, "w", encoding="utf-8") as fh:
    _json.dump({"pid": 999999, "seen": time.time() - 9999,
                "started": time.time() - 9999}, fh)
alive = os.path.join(bridge.INSTANCE_DIR, "888888.json")
with open(alive, "w", encoding="utf-8") as fh:
    _json.dump({"pid": 888888, "seen": time.time(),
                "started": time.time()}, fh)

bridge._prune()
H.check("★ 过期的心跳被清掉了", not os.path.isfile(dead))
H.check("★ 活着的不动", os.path.isfile(alive))
H.check("instances() 也不会把过期的算进来",
        all(i.get("pid") != 999999 for i in bridge.instances()))
os.remove(alive)

H.section("★ 桥这一层也拒绝空命令（任何入口都堵住）")
# 客户端那道拦截只管 hcmd.sh 一个入口。万一有别的路径（未来的脚本、手写、
# 别的工具）往队列里丢了个空 .py，桥必须自己拒绝，而不是 exec("") 然后
# 返回 ok:true —— 那是最难查的一类失败。
empty_req = "%d__%d" % (os.getpid(), int(time.time() * 1000) + 7)
empty_in = os.path.join(bridge.QUEUE_DIR, empty_req + ".py")
empty_out = os.path.join(bridge.QUEUE_DIR, empty_req + ".out")
with open(empty_in, "w", encoding="utf-8") as fh:
    fh.write("   \n\n\t\n")
bridge.start()
_deadline = time.time() + 5
while time.time() < _deadline and not os.path.isfile(empty_out):
    time.sleep(0.05)
H.check("★ 空命令产生了结果文件", os.path.isfile(empty_out))
if os.path.isfile(empty_out):
    with open(empty_out, "r", encoding="utf-8") as fh:
        r_empty = _json.load(fh)
    H.check("★★ 而且 ok 是 False（不再假装成功）",
            r_empty.get("ok") is False, r_empty)
    H.check("  说明了原因", "空" in (r_empty.get("error") or ""), r_empty)
bridge.stop()

H.section("把临时目录清干净")
# 先把线程等干净——Windows 上文件可能还被占着，直接 rmtree 会静默失败。
import shutil

bridge.stop()
deadline = time.time() + 5
while time.time() < deadline and bridge.is_running():
    time.sleep(0.05)
_threads = [t for t in threading.enumerate()
            if t.name == bridge.THREAD_NAME]
H.check("桥线程已退出", not bridge.is_running(),
        "还剩 %d 个桥线程：%s" % (len(_threads), _threads))

H.section("★★ 清扫死进程留下的心跳文件")
# 实测踩到：Houdini 被强杀后，NTFS 留下一个**全是 NUL** 的心跳文件
# （长度对、内容是零）。它读不出 pid，客户端只能莫名跳过，越积越多。
import json as _json

_inst_dir = bridge.INSTANCE_DIR
os.makedirs(_inst_dir, exist_ok=True)
for _old in os.listdir(_inst_dir):
    os.remove(os.path.join(_inst_dir, _old))

_limit = bridge.STALE_SECONDS * 10


def _write_instance(name, text, age):
    path = os.path.join(_inst_dir, name)
    with open(path, "w", encoding="utf-8") as fh:
        fh.write(text)
    when = time.time() - age
    os.utime(path, (when, when))
    return path


_good = _json.dumps({"pid": 111, "seen": time.time(), "started": 1.0})
# 陈旧的那份：`seen` 也必须是旧的——判活看的是 seen，不是文件时间
_stale = _json.dumps({"pid": 998, "seen": time.time() - (_limit + 60),
                      "started": 1.0})
_dead_corrupt = _write_instance("999.json", "\0" * 185, _limit + 60)
_dead_stale = _write_instance("998.json", _stale, _limit + 60)
_fresh_corrupt = _write_instance("997.json", "\0" * 40, 1)
_fresh_good = _write_instance("996.json", _good, 1)

bridge._sweep_dead_instances()

H.check("★ 坏掉的旧心跳文件被清掉（就是它让 --list 打出怪行）",
        not os.path.exists(_dead_corrupt))
H.check("旧但不坏的也清掉（进程肯定没了）",
        not os.path.exists(_dead_stale))
H.check("★ 刚写的坏文件不动它（可能是另一个实例正在写）",
        os.path.exists(_fresh_corrupt))
H.check("活着的那份绝不碰", os.path.exists(_fresh_good))

bridge._sweep_dead_instances()
H.check("反复扫也不会误删（幂等）", os.path.exists(_fresh_good))

for _p in (_fresh_corrupt, _fresh_good):
    try:
        os.remove(_p)
    except Exception:
        pass

for _ in range(20):
    shutil.rmtree(_TMP, ignore_errors=True)
    if not os.path.isdir(_TMP):
        break
    time.sleep(0.1)
H.check("临时桥目录已删", not os.path.isdir(_TMP),
        os.listdir(_TMP) if os.path.isdir(_TMP) else "")

H.finish("test_bridge")
