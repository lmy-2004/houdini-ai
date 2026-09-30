# -*- coding: utf-8 -*-
"""跑全部离线测试。用 hython：

    "D:/ProgramFiles/Houdini22/bin/hython.exe" tests/run_all.py

真联网的冒烟测试（live_smoke.py）**不在这里**，因为它会花钱，
而且它验证的是「模型建得对不对」这种主观的东西，不适合当回归测试。
"""

import os
import subprocess
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
SUITES = ["test_catalog.py", "test_builder.py", "test_runner.py",
          "test_deploy.py", "test_panel.py",
          "test_context.py", "test_memory.py",
          # 出图（离屏渲染）。★ 它必须在自己的进程里——opengl ROP 实测
          # 一个进程只能渲一次，第二次会崩。这里每个文件本来就是独立进程。
          "test_viewport.py",
          # 命令桥（Claude Code 把 Python 送进跑着的 Houdini）。
          # 用临时目录，不碰真正那份桥目录。
          "test_bridge.py",
          # 桥的 shell 前端。单独一个文件是因为它跑外部进程，
          # 塞在 test_bridge 里会把那个文件的时序搞飘（实测过）。
          "test_hcmd.py",
          # 动作线（NPZ → KineFX）。不需要后端、不花钱。
          "test_motion_npz.py", "test_motion_rig.py", "test_motion_session.py",
          "test_motion_api.py", "test_motion_panel.py"]


def main():
    python = sys.executable
    failures = []
    for suite in SUITES:
        print("\n" + "#" * 66)
        print("# %s" % suite)
        print("#" * 66)
        proc = subprocess.run([python, os.path.join(HERE, suite)])
        if proc.returncode != 0:
            failures.append(suite)

    print("\n" + "=" * 66)
    if failures:
        print("失败：%s" % ", ".join(failures))
        return 1
    print("全部通过：%s" % ", ".join(SUITES))
    return 0


if __name__ == "__main__":
    sys.exit(main())
