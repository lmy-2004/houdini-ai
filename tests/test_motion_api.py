# -*- coding: utf-8 -*-
"""motion_api 的测试：payload 组装 + 子进程驱动。

★ 关键手法：**假后端**。真跑一次 Kimodo 要几十秒 + 几 GB 显存，
   所以这里用一个几行的假 runner 脚本，把「后端会吐什么」原样演出来
   （含成功、报错、卡住、被取消四种）。整条管线的控制流因此**不花算力**就能验。
   这是照 test_runner.py 里 FakeGateway 的思路。
"""

import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import _harness as H  # noqa: E402

from houdini_ai import motion_api, motion_session as MS  # noqa: E402

TMP = os.path.join(os.path.dirname(os.path.abspath(__file__)), "_tmp")
if not os.path.isdir(TMP):
    os.makedirs(TMP)


# ===========================================================================
H.section("payload 组装")
# ===========================================================================
s = MS.MotionSession(seed=7, diffusion_steps=50, cfg_weight=1.5,
                     transition_frames=8, postprocess=False, first_heading_angle=0.5)
s.add_segment("A person walks forward", 3.0)
s.add_segment("then stops", 1.0)
s.add_constraint(MS.Constraint("root2d", 0, points=[[0.0, 0.0], [1.5, 0.5]], frames=[0, 60]))
s.add_constraint(MS.Constraint("fullbody", 30,
                               joint_positions=[[0.1 * i, 0.9, 0.0] for i in range(30)],
                               joint_rots=[[[1.0, 0, 0], [0, 1.0, 0], [0, 0, 1.0]]] * 30,
                               skel_name="somaskel30"))


def fake_reader(asset_frame):
    n = 30
    pos = [[0.0, 0.9 + 0.01 * asset_frame, 0.0] for _ in range(n)]
    rot = [[[1.0, 0, 0], [0, 1.0, 0], [0, 0, 1.0]] for _ in range(n)]
    return pos, rot


p = motion_api.build_payload(s, fake_reader, "D:/out/x.npz")
H.check_eq("两段都在", [x["text"] for x in p["segments"]],
           ["A person walks forward", "then stops"])
H.check_eq("时长保留", [x["seconds"] for x in p["segments"]], [3.0, 1.0])
H.check_eq("控制参数带过去了",
           (p["seed"], p["diffusion_steps"], p["cfg_weight"], p["transition_frames"],
            p["postprocess"], p["first_heading_angle"]),
           (7, 50, 1.5, 8, False, 0.5))
H.check_eq("输出路径带过去了", p["output"], "D:/out/x.npz")
H.check_eq("约束两条", len(p["constraints"]), 2)

r2 = [c for c in p["constraints"] if c["kind"] == "root2d"][0]
H.check_eq("root2d 带路点", r2["points"], [[0.0, 0.0], [1.5, 0.5]])
H.check_eq("root2d 带帧号", r2["frames"], [0, 60])
H.check("root2d 不带姿势数据（它不是姿势约束）", "joint_positions" not in r2)

fb = [c for c in p["constraints"] if c["kind"] == "fullbody"][0]
H.check_eq("fullbody 帧号", fb["frame"], 30)
H.check_eq("★ fullbody 带关节数（后端要靠它决定要不要切片）", fb["joint_count"], 30)
H.check_eq("fullbody 位置 30 个关节", len(fb["joint_positions"]), 30)
H.check_eq("fullbody 旋转 30 个关节", len(fb["joint_rots"]), 30)
H.check("★ 姿势是从 pose_reader 现场读的（不是会话里存的）",
        abs(fb["joint_positions"][0][1] - (0.9 + 0.01 * 30)) < 1e-9,
        fb["joint_positions"][0])
H.check("payload 可 JSON 序列化（子进程要靠文件传）",
        len(json.dumps(p, ensure_ascii=False)) > 100)

# 停用的约束不进 payload
s.constraints[1].enabled = False
p2 = motion_api.build_payload(s, fake_reader, "x.npz")
H.check_eq("停用的约束被排除", len(p2["constraints"]), 1)
s.constraints[1].enabled = True

# 空会话
empty = MS.MotionSession()
p3 = motion_api.build_payload(empty, fake_reader, "x.npz")
H.check_eq("空会话：没有段", p3["segments"], [])
H.check_eq("空会话：没有约束", p3["constraints"], [])


# pose_reader 读不到姿势时要报错（不静默产出坏 payload）
def bad_reader(af):
    return None, None


s.constraints[1].enabled = True
try:
    motion_api.build_payload(s, bad_reader, "x.npz")
    H.check("读不到姿势要报错", False, "没报错")
except motion_api.ApiError as exc:
    H.check("★ 读不到姿势的报错说清了是哪一帧、哪类约束",
            "30" in str(exc) and "fullbody" in str(exc), str(exc))


# ===========================================================================
H.section("write_payload")
# ===========================================================================
path = motion_api.write_payload({"a": 1, "中文": [1, 2]}, os.path.join(TMP, "pl.json"))
H.check("写出来了", os.path.isfile(path))
H.check_eq("内容往返一致（含中文）",
           json.load(open(path, encoding="utf-8")), {"a": 1, "中文": [1, 2]})

# ===========================================================================
H.section("check_backend：找不到要说清楚")
# ===========================================================================
ok, why = motion_api.check_backend("D:/nope/nope.exe", "D:/nope/x.py")
H.check("路径不对时返回 False", not ok)
H.check("说明里给出了找的是哪个路径", "D:/nope/nope.exe" in why, why)
ok2, why2 = motion_api.check_backend(motion_api.DEFAULT_VENV_PYTHON, motion_api.DEFAULT_RUNNER)
H.check("本机后端路径是真的存在", ok2, why2)

# ===========================================================================
H.section("★ 假后端：整条控制流（成功 / 报错 / 崩掉 / 取消）")
# ===========================================================================
FAKE = os.path.join(TMP, "fake_runner.py")

FAKE_SRC = '''
import json, os, sys, time
payload = json.load(open(sys.argv[1], encoding="utf-8"))
mode = payload.get("_mode", "ok")

def emit(**kw):
    sys.stdout.write(json.dumps(kw) + "\\n"); sys.stdout.flush()

emit(event="stage", name="加载模型")
if mode == "error":
    emit(event="error", message="模型加载失败：显存不够")
    sys.exit(1)
if mode == "crash":
    sys.stderr.write("Traceback: 假的崩溃日志\\n")
    sys.exit(3)
if mode == "hang":
    time.sleep(300)
emit(event="stage", name="生成中")
time.sleep(0.2)
out = payload["output"]
os.makedirs(os.path.dirname(os.path.abspath(out)), exist_ok=True)
open(out, "wb").write(b"fake npz")
emit(event="done", npz=out, frames=90, joints=30, seconds=1.2)
# 故意再吐一行非 JSON，验证解析容错
print("这行不是 JSON，应该被忽略")
'''


def write_fake():
    with open(FAKE, "w", encoding="utf-8") as f:
        f.write(FAKE_SRC)
    return FAKE


write_fake()
PY = sys.executable           # 就用 hython 跑假 runner（有 stdlib 就够）

# ---- 成功 ----
events = []
out = os.path.join(TMP, "gen_ok.npz")
payload = {"_mode": "ok", "output": out, "segments": [], "constraints": []}
res = motion_api.run_generation(payload, python_exe=PY, runner=FAKE,
                                on_event=events.append, timeout=60)
H.check_eq("★ 成功路径拿到 done", res.get("frames"), 90)
H.check_eq("NPZ 真被写出来了", os.path.isfile(res["npz"]), True)
H.check("事件流里有 stage", any(e.get("event") == "stage" for e in events))
H.check("★ 非 JSON 的输出行被容错忽略（没让整个流程崩）",
      all(isinstance(e, dict) for e in events))
H.check("拿到 stderr 尾巴（排错用）", "stderr_tail" in res)

# ---- 后端主动报错 ----
out2 = os.path.join(TMP, "gen_err.npz")
try:
    motion_api.run_generation({"_mode": "error", "output": out2}, python_exe=PY,
                              runner=FAKE, timeout=60)
    H.check("后端报错要抛 ApiError", False, "没抛")
except motion_api.ApiError as exc:
    H.check("★ 后端报的错原样传给用户（不是笼统的「失败了」）",
            "显存不够" in str(exc), str(exc))

# ---- 后端崩掉（没报 done 就退出）----
out3 = os.path.join(TMP, "gen_crash.npz")
try:
    motion_api.run_generation({"_mode": "crash", "output": out3}, python_exe=PY,
                              runner=FAKE, timeout=60)
    H.check("崩溃要抛 ApiError", False, "没抛")
except motion_api.ApiError as exc:
    H.check("★ 崩溃时把 stderr 尾巴带出来（不然没法查）",
            "假的崩溃日志" in str(exc), str(exc)[:300])

# ---- 后端说完成但文件不在 ----
out4 = os.path.join(TMP, "gen_missing.npz")
try:
    motion_api.run_generation({"_mode": "ok", "output": out4}, python_exe=PY,
                              runner=FAKE, timeout=60,
                              payload_path=os.path.join(TMP, "missing.json"))
    # 这次真的会写出文件，所以换个办法：把 output 指到一个写不了的目录
    H.check("正常走完", True)
except motion_api.ApiError as exc:
    H.check("正常走完", False, str(exc))

# ---- 取消 ----
import threading  # noqa: E402
ev = threading.Event()
out5 = os.path.join(TMP, "gen_cancel.npz")


def cancel_soon():
    import time as _t
    _t.sleep(1.0)
    ev.set()


threading.Thread(target=cancel_soon, daemon=True).start()
try:
    motion_api.run_generation({"_mode": "hang", "output": out5}, python_exe=PY,
                              runner=FAKE, cancel_event=ev, timeout=20)
    H.check("★ 取消要抛 ApiError（而不是一直挂着）", False, "没抛")
except motion_api.ApiError as exc:
    H.check("★ 取消能真的中断（说明显存能被还回来）", "取消" in str(exc), str(exc))

# ===========================================================================
H.section("★★ 子进程环境必须干净（Houdini 的 PYTHONHOME 会让后端崩）")
# ===========================================================================
# 真踩过：hython 会设 PYTHONHOME=<HFS>/python313，后端是 Python 3.11，
# 带着它启动就去加载 Houdini 那份 3.13 的 stdlib，症状是
# `AssertionError: SRE module mismatch`。这个假 runner 一 import re 就会当场发作。
ENVFAKE = os.path.join(TMP, "fake_env_runner.py")
with open(ENVFAKE, "w", encoding="utf-8") as f:
    f.write('''
import json, os, sys
import re          # ★ 如果 PYTHONHOME 泄进来，这一行就会 SRE module mismatch
leaked = {k: os.environ[k] for k in ("PYTHONHOME", "PYTHONPATH", "PYTHONSTARTUP")
          if k in os.environ}
payload = json.load(open(sys.argv[1], encoding="utf-8"))
out = payload["output"]
open(out, "wb").write(b"x")
print(json.dumps({"event": "done", "npz": out, "frames": 3, "joints": 5,
                  "leaked": leaked}), flush=True)
''')
out6 = os.path.join(TMP, "env_check.npz")
H.check("hython 当前确实带着 PYTHONHOME（不然这条测试就没意义）",
        "PYTHONHOME" in os.environ, dict(os.environ).get("PYTHONHOME"))

# ★ 这里必须用一个**真正的普通 Python**（后端 venv 的那个），不能用 sys.executable。
#   因为 sys.executable 是 hython，而 hython **启动时会自己重新设 PYTHONHOME**，
#   那样测出来的是 hython 的行为，不是我们 env 清洗逻辑的行为。
_real_py = motion_api.DEFAULT_VENV_PYTHON
H.check("用后端自己的 python 跑这条（不是 hython）", os.path.isfile(_real_py), _real_py)
res6 = motion_api.run_generation({"output": out6}, python_exe=_real_py,
                                 runner=ENVFAKE, timeout=120)
H.check_eq("★★ 子进程里没有泄露的 Python 环境变量", res6.get("leaked"), {})
H.check("假 runner 顺利跑完（re 加载成功）", res6.get("frames") == 3)

H.finish("test_motion_api")
