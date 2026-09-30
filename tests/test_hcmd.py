# -*- coding: utf-8 -*-
"""hcmd.sh（命令桥的 shell 前端）的测试。

    "D:/ProgramFiles/Houdini22/bin/hython.exe" tests/test_hcmd.py

**为什么单独一个文件**：这测的是 shell 脚本，不是桥的 Python 模块。
一度把它塞在 `test_bridge.py` 里，结果它**把那个文件的时序搞飘了**
（实测：带它 3 次里挂 2 次，剥掉它 3 次全过）——桥的测试里有等线程退出的
时限，多一段跑外部进程的代码就会踩线。分开之后两边都稳定。

不需要活的 Houdini：伪造一个心跳新鲜的 instance 文件，命令就会被写进队列；
再让脚本等 1 秒超时退出，队列里那个 .py 留着给我们检查。
"""

import glob
import json
import os
import shutil
import subprocess
import tempfile
import time

import _harness as H

H.section("定位脚本")
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
HCMD = os.path.join(ROOT, "scripts", "hcmd.sh")
H.check("hcmd.sh 在", os.path.isfile(HCMD), HCMD)

H.section("★★ -f 模式真的把文件内容送出去了")
# 实测踩过（2026-09-29）：-f 分支**只检查文件存在、没把内容输出出去**
# （另外两个分支都有 printf / cat，就它漏了）。于是 `-f 文件` 一直在
# **静默发送空脚本**：Houdini 那边 exec("") 什么都不做，返回 ok:true、
# stdout 空、什么也没发生。看起来一切正常——最难查的那种失败。
work = tempfile.mkdtemp(prefix="houdini_ai_hcmd_")
bridge_dir = os.path.join(work, "bridge")
os.makedirs(os.path.join(bridge_dir, "instances"))
os.makedirs(os.path.join(bridge_dir, "queue"))

now = time.time()
with open(os.path.join(bridge_dir, "instances", "fake.json"), "w",
          encoding="utf-8") as fh:
    json.dump({"pid": 999999, "seen": now, "started": now,
               "scene": "test.hip", "pwd": "/obj"}, fh)

MARKER = "PROBE_BODY_MARKER_98765"
body = os.path.join(work, "cmd.py")
with open(body, "w", encoding="utf-8") as fh:
    fh.write("print('%s')\n" % MARKER)

env = dict(os.environ, HOUDINI_AI_BRIDGE_DIR=bridge_dir)
try:
    proc = subprocess.run(["bash", HCMD, "-f", body, "-t", "1"],
                          capture_output=True, timeout=60, env=env)
    H.check("跑得起来（等到超时退出是预期的）", proc.returncode in (0, 3),
            (proc.returncode, proc.stderr[:200]))
except Exception as exc:
    H.check("跑得起来", False, exc)

queued = glob.glob(os.path.join(bridge_dir, "queue", "*.py"))
H.check("命令被写进了队列", bool(queued),
        os.listdir(os.path.join(bridge_dir, "queue")))
if queued:
    content = open(queued[0], encoding="utf-8", errors="replace").read()
    H.check("★★ 队列里的脚本**有内容**（不是空文件）",
            MARKER in content, repr(content[:200]))
    H.check("内容原样送达（换行也在）",
            content.strip() == "print('%s')" % MARKER, repr(content))

shutil.rmtree(work, ignore_errors=True)

H.section("直接传一段代码也走同一条路")
work2 = tempfile.mkdtemp(prefix="houdini_ai_hcmd2_")
bridge2 = os.path.join(work2, "bridge")
os.makedirs(os.path.join(bridge2, "instances"))
os.makedirs(os.path.join(bridge2, "queue"))
now2 = time.time()
with open(os.path.join(bridge2, "instances", "fake.json"), "w",
          encoding="utf-8") as fh:
    json.dump({"pid": 888888, "seen": now2, "started": now2,
               "scene": "t.hip", "pwd": "/obj"}, fh)
try:
    subprocess.run(["bash", HCMD, "print('INLINE_%s')" % MARKER, "-t", "1"],
                   capture_output=True, timeout=60,
                   env=dict(os.environ, HOUDINI_AI_BRIDGE_DIR=bridge2))
except Exception:
    pass
queued2 = glob.glob(os.path.join(bridge2, "queue", "*.py"))
H.check("内联模式也写进了队列", bool(queued2))
if queued2:
    content2 = open(queued2[0], encoding="utf-8", errors="replace").read()
    H.check("内联内容也对", MARKER in content2, repr(content2))
shutil.rmtree(work2, ignore_errors=True)

def make_bridge_dir(with_instance=True):
    """造一个像样的桥目录。

    ★ 两个实测踩到的前提：
      * 桥目录**不存在**时 hcmd.sh 会直接退出 2 并提示「Houdini 那边还没启动过
        命令桥」——那是好行为，但测试得先建目录。
      * hcmd.sh 的顺序是**先找实例、再取代码**，所以想测「取代码」那一段，
        必须伪造一个心跳新鲜的 instance 文件。
    """
    path = tempfile.mkdtemp(prefix="houdini_ai_hcmd_")
    os.makedirs(os.path.join(path, "instances"))
    os.makedirs(os.path.join(path, "queue"))
    if with_instance:
        stamp = time.time()
        with open(os.path.join(path, "instances", "fake.json"), "w",
                  encoding="utf-8") as fh:
            json.dump({"pid": 777777, "seen": stamp, "started": stamp,
                       "scene": "t.hip", "pwd": "/obj"}, fh)
    return path


def run_hcmd(args, bridge_dir, **kwargs):
    return subprocess.run(["bash", HCMD] + args, capture_output=True,
                          timeout=60, stdin=subprocess.DEVNULL,
                          env=dict(os.environ, HOUDINI_AI_BRIDGE_DIR=bridge_dir),
                          **kwargs)


H.section("★ 空命令一律拦住，不静默发送")
# exec("") 不会报错，只会安静地什么都不做——这正是 -f 漏 cat 时的症状。
# 所以空 body 必须在发出去之前就报错。
work3 = make_bridge_dir()
try:
    proc3 = run_hcmd([], work3)
    H.check("空命令非零退出", proc3.returncode == 2, proc3.returncode)
    err3 = proc3.stderr.decode("utf-8", "replace")
    H.check("★ 明确说了「命令是空的」（不是安静地排队一个空脚本）",
            "命令是空的" in err3, err3[:300])
    H.check("★ 队列里没有被塞进空脚本",
            not glob.glob(os.path.join(work3, "queue", "*.py")),
            os.listdir(os.path.join(work3, "queue")))
except Exception as exc:
    H.check("空命令时不崩", False, exc)
shutil.rmtree(work3, ignore_errors=True)

H.section("★ 纯空白也算空（拿文件大小当判据会漏）")
# ⚠️ 这条是补测出来的**真 bug**：空 body 的判据原本是 `[ ! -s "$BODY" ]`
# （**文件大小**），而纯空白 "   \n\t\n" 大小非零，照样被排进队列——
# 于是又绕回「exec 空脚本、静默成功」那条老路。
# 判据必须是「有没有非空白字符」（`grep -q '[^[:space:]]'`）。
work3b = make_bridge_dir()
try:
    proc3b = subprocess.run(
        ["bash", HCMD], capture_output=True, timeout=60,
        input=b"   \n\t\n",
        env=dict(os.environ, HOUDINI_AI_BRIDGE_DIR=work3b))
    H.check("纯空白非零退出", proc3b.returncode == 2, proc3b.returncode)
    err3b = proc3b.stderr.decode("utf-8", "replace")
    H.check("★ 说了「命令是空的」", "命令是空的" in err3b, err3b[:300])
    H.check("★ 队列里没有被塞进空脚本",
            not glob.glob(os.path.join(work3b, "queue", "*.py")),
            os.listdir(os.path.join(work3b, "queue")))
except Exception as exc:
    H.check("纯空白时不崩", False, exc)
shutil.rmtree(work3b, ignore_errors=True)

H.section("读不到的文件要明确报错")
work4 = make_bridge_dir()
try:
    proc4 = run_hcmd(["-f", "/definitely/not/here.py"], work4)
    H.check("读不到文件时非零退出", proc4.returncode != 0, proc4.returncode)
    H.check("说了读不到哪个文件",
            "读不到" in proc4.stderr.decode("utf-8", "replace"),
            proc4.stderr[:300])
except Exception as exc:
    H.check("读不到文件时不崩", False, exc)
shutil.rmtree(work4, ignore_errors=True)

H.section("--list：有实例、没实例、桥没起来，三种都要给句人话")
work5 = make_bridge_dir()
try:
    proc5 = run_hcmd(["--list"], work5)
    out5 = proc5.stdout.decode("utf-8", "replace")
    H.check("--list 正常退出", proc5.returncode == 0, proc5.returncode)
    H.check("列出了那个实例", "777777" in out5, out5[:200])

    empty = make_bridge_dir(with_instance=False)
    proc6 = run_hcmd(["--list"], empty)
    H.check("一个实例都没有时也正常退出", proc6.returncode == 0,
            proc6.returncode)
    H.check("并且说明白了",
            "一个活着的实例都没有" in proc6.stdout.decode("utf-8", "replace"),
            proc6.stdout[:200])
    shutil.rmtree(empty, ignore_errors=True)
except Exception as exc:
    H.check("--list 不崩", False, exc)
shutil.rmtree(work5, ignore_errors=True)

H.section("★ 桥目录不存在时给的是人话，不是栈")
# 这是实测确认过的好行为：目录不在 = Houdini 没起过桥，直接告诉人怎么办。
gone = os.path.join(tempfile.mkdtemp(prefix="houdini_ai_hcmd_"), "nope")
try:
    proc7 = run_hcmd(["--list"], gone)
    H.check("桥没起来时非零退出", proc7.returncode == 2, proc7.returncode)
    err7 = proc7.stderr.decode("utf-8", "replace")
    H.check("说了是怎么回事", "还没启动过命令桥" in err7, err7[:250])
    H.check("还给了怎么办（pythonrc / Python Shell）",
            "pythonrc" in err7 or "Python Shell" in err7, err7[:300])
except Exception as exc:
    H.check("桥没起来时不崩", False, exc)
shutil.rmtree(os.path.dirname(gone), ignore_errors=True)

H.finish("test_hcmd")
