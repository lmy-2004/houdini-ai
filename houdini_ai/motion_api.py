# -*- coding: utf-8 -*-
"""把会话变成后端要的 payload，并把后端当子进程拉起来。

这一层**不碰 hou**（除了调用方传进来的回调），所以能在工作线程里跑，
也能在 hython 里离线测（用一个假的「驱动器」就能验整套流程）。

## 为什么是「一次性子进程」而不是常驻服务

本机 12GB 显存，Houdini + 浏览器已经占掉 5.6GB。常驻服务会让模型一直占着
3~4GB 不放。一次性子进程跑完就退、显存立刻还回来；代价是每次重新加载模型（几十秒）。
"""

import json
import os
import subprocess
import sys
import tempfile
import threading

# 后端默认位置（可被 config / 面板覆盖）
DEFAULT_VENV_PYTHON = "D:/kimodo-motion/venv/Scripts/python.exe"
DEFAULT_RUNNER = "D:/kimodo-motion/kimodo_runner.py"
DEFAULT_MODEL = "kimodo-soma-rp"

# 素材帧率（Kimodo 固定 30）
KIMODO_FPS = 30.0


class ApiError(Exception):
    """给用户看的错误。"""


def check_backend(python_exe=None, runner=None):
    """看看后端在不在。返回 (能用吗, 说明)。"""
    py = python_exe or DEFAULT_VENV_PYTHON
    ru = runner or DEFAULT_RUNNER
    if not os.path.isfile(py):
        return False, "找不到后端的 Python：%s" % py
    if not os.path.isfile(ru):
        return False, "找不到 runner：%s" % ru
    return True, "后端就绪：%s" % py


def build_payload(session, pose_reader, output_path, skel_name=None,
                  model=None, device=None, total_frames=None):
    """会话 → 后端 payload。

    `pose_reader(asset_frame) -> (positions (J,3), rots (J,3,3))`：
    由调用方从场景里取姿势（**必须是 Kimodo 约定的全局旋转**，即
    `motion_npz.from_kinefx_transform` 处理过的）。位置是米、Y-up。

    `asset_frame` 是 **0-based 素材帧**——会话里存的就是这个单位。

    ★ 这里不做任何时间换算：会话与后端都用素材帧，换算只在面板那一处发生。
    """
    total = int(total_frames if total_frames is not None else session.total_frames())

    cons = []
    for c in session.active_constraints():
        if c.is_root2d:
            pts = c.points or []
            if not pts:
                continue
            cons.append({
                "kind": "root2d",
                "frames": [int(f) for f in (c.frames or list(range(len(pts))))],
                "points": [[float(p[0]), float(p[1])] for p in pts],
            })
            continue
        # 姿势类：现场从场景读（用户在面板上改了姿势也要跟上）
        pos, rot = pose_reader(c.frame)
        if pos is None:
            raise ApiError("第 %d 帧的 %s 约束读不到姿势——上游节点可能报错了"
                           % (c.frame, c.kind))
        cons.append({
            "kind": c.kind,
            "frame": int(c.frame),
            "joint_count": int(len(pos)),
            "joint_positions": [[float(v) for v in p] for p in pos],
            "joint_rots": [[[float(x) for x in row] for row in m] for m in rot],
        })

    segs = [{"text": s.text.strip(), "seconds": float(s.seconds)}
            for s in session.active_segments]
    return {
        "segments": segs,
        "constraints": cons,
        "seed": session.seed,
        "diffusion_steps": int(session.diffusion_steps),
        "cfg_weight": float(session.cfg_weight),
        "transition_frames": int(session.transition_frames),
        "postprocess": bool(session.postprocess),
        "first_heading_angle": float(session.first_heading_angle),
        "model": model or DEFAULT_MODEL,
        "device": device,
        "output": output_path,
    }


def child_env():
    """拉起后端子进程要用的环境。

    **所有**起后端的地方都必须用它——包括测试脚本里那些临时的 `subprocess.run`。
    自己拼一份 `os.environ` 就会踩下面这个坑：
    """
    env = dict(os.environ)
    # ★★ Houdini 的 hython 会设 `PYTHONHOME=<HFS>/python313`。后端是 **Python 3.11**，
    #    带着它启动就会去加载 Houdini 那份 3.13 的 stdlib，症状是
    #    `AssertionError: SRE module mismatch` 或 `_thread has no attribute ...`。
    #    这些变量是给 hython 自己用的，不该外传。
    for k in ("PYTHONHOME", "PYTHONPATH", "PYTHONSTARTUP", "PYTHONEXECUTABLE",
              "PYTHONUSERBASE", "PYTHONNOUSERSITE"):
        env.pop(k, None)
    env.setdefault("HF_ENDPOINT", "https://hf-mirror.com")
    env.setdefault("HF_HOME", "D:/kimodo-motion/hf")
    env.setdefault("TEXT_ENCODER_DEVICE", "cpu")     # ★ 官方：全 GPU 约 17GB，设了降到 <3GB
    # 默认的 auto 会先去连一个不存在的文本编码器 API 做健康检查再回落，白等一轮。
    env.setdefault("TEXT_ENCODER_MODE", "local")
    env.setdefault("HF_HUB_OFFLINE", "1")
    env.setdefault("HF_HUB_DISABLE_XET", "1")
    env.setdefault("HF_HUB_DISABLE_SYMLINKS_WARNING", "1")
    env.setdefault("PYTHONIOENCODING", "utf-8")
    env.setdefault("PYTHONUNBUFFERED", "1")
    if sys.platform == "win32":
        env.setdefault("KMP_DUPLICATE_LIB_OK", "TRUE")
    return env


def write_payload(payload, path=None):
    """写成一个临时 json。返回路径。"""
    if path is None:
        fd, path = tempfile.mkstemp(prefix="kimodo_payload_", suffix=".json")
        os.close(fd)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(payload, f, ensure_ascii=False, indent=1)
    return path


def run_generation(payload, python_exe=None, runner=None, on_event=None,
                   cancel_event=None, timeout=3600, payload_path=None):
    """拉起后端跑一次生成。**阻塞**，调用方负责放到工作线程里。

    `on_event(dict)` 会收到 runner 吐的每个 JSON 事件（stage / done / error）。
    `cancel_event`（threading.Event）一旦被 set，就杀掉子进程。

    返回最终的 `done` 事件 dict；失败抛 ApiError。
    """
    py = python_exe or DEFAULT_VENV_PYTHON
    ru = runner or DEFAULT_RUNNER
    ok, why = check_backend(py, ru)
    if not ok:
        raise ApiError(why)

    pp = write_payload(payload, payload_path)
    env = child_env()

    kwargs = {}
    if sys.platform == "win32":
        # 别让子进程弹黑窗
        kwargs["creationflags"] = getattr(subprocess, "CREATE_NO_WINDOW", 0)

    proc = subprocess.Popen([py, ru, pp], stdout=subprocess.PIPE,
                            stderr=subprocess.PIPE, env=env, text=True,
                            encoding="utf-8", errors="replace", bufsize=1, **kwargs)

    lines, errbuf = [], []

    def drain_err():
        try:
            for ln in proc.stderr:
                errbuf.append(ln.rstrip())
                if len(errbuf) > 400:
                    del errbuf[:200]
        except Exception:
            pass

    th = threading.Thread(target=drain_err)
    th.daemon = True
    th.start()

    done = None
    timed_out = False
    try:
        for line in proc.stdout:
            if cancel_event is not None and cancel_event.is_set():
                proc.kill()
                raise ApiError("已取消")
            line = line.strip()
            if not line:
                continue
            try:
                ev = json.loads(line)
            except ValueError:
                lines.append(line)
                continue
            if ev.get("event") == "done":
                done = ev
            elif ev.get("event") == "error":
                raise ApiError(ev.get("message") or "后端报错")
            if on_event is not None:
                try:
                    on_event(ev)
                except Exception:
                    pass
    finally:
        try:
            proc.wait(timeout=timeout)
        except Exception:
            proc.kill()
            timed_out = True
        th.join(timeout=2)

    if timed_out:
        raise ApiError("后端超时（%d 秒）" % timeout)
    if proc.returncode != 0 and done is None:
        tail = "\n".join(errbuf[-25:]) or "\n".join(lines[-15:])
        raise ApiError("后端退出码 %s。最后几行日志：\n%s" % (proc.returncode, tail))
    if done is None:
        tail = "\n".join(errbuf[-25:]) or "\n".join(lines[-15:])
        raise ApiError("后端没报完成就退出了。最后几行日志：\n%s" % tail)
    if not os.path.isfile(done.get("npz", "")):
        raise ApiError("后端说完成了，但 NPZ 不在：%s" % done.get("npz"))
    done["stderr_tail"] = "\n".join(errbuf[-25:])
    return done
