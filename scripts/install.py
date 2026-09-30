# -*- coding: utf-8 -*-
"""把工具架和面板装到 Houdini 的 pref 目录。

    hython scripts/install.py            # 装到当前版本的两个 pref 目录
    hython scripts/install.py --check    # 只检查现状，不动文件
    hython scripts/install.py --uninstall

**装的是「桩文件」**：本机实测 `pref/python` 不在 sys.path 上（Fab 工具架已经
踩过这个坑）。所以桩文件只做两件事——把工具根目录插进 sys.path，然后把真正的
实现 import 回来。真正的代码留在 .tools/houdini-ai/ 里，**改代码不用重新部署**。

**为什么装两份**：本机有两个同版本的 pref 目录，取决于 Houdini 怎么启动——
从图标启动用 Documents 那份，从 Git Bash 启动用家目录那份。Fab 工具架也是两份。

★ 装完之后其实**不用重启 Houdini**：在 Houdini 里点工具架的「Reload」按钮
（或跑 houdini_ai.reload.reload_all()）就会把这些装进当前会话。
重启只在第一次装的时候需要。
"""

import argparse
import sys
import os

HERE = os.path.dirname(os.path.abspath(__file__))
TOOL_ROOT = os.path.dirname(HERE)
if TOOL_ROOT not in sys.path:
    sys.path.insert(0, TOOL_ROOT)

from houdini_ai import deploy, prefdirs  # noqa: E402


def main():
    parser = argparse.ArgumentParser(description="安装 Houdini AI 工具集")
    parser.add_argument("--pref", action="append", default=[],
                        help="手动指定 pref 目录，可重复")
    parser.add_argument("--check", action="store_true", help="只报告现状")
    parser.add_argument("--uninstall", action="store_true", help="卸载")
    parser.add_argument("--version", default=None,
                        help="目标版本目录名，比如 houdini22.0（默认自动认）")
    parser.add_argument("--all-versions", action="store_true",
                        help="装给所有找到的版本目录（一般不需要）")
    args = parser.parse_args()

    prefs = args.pref or prefdirs.candidate_pref_dirs(
        version=args.version, every_version=args.all_versions)

    if not prefs:
        print("找不到 Houdini pref 目录。用 --pref 手动指定。")
        return 1

    print("工具根目录：%s" % TOOL_ROOT)
    print("目标版本：%s" % prefdirs.target_version())
    print("找到 %d 个 pref 目录\n" % len(prefs))

    if args.uninstall:
        return _uninstall(prefs)
    if args.check:
        return _check(prefs)
    return _install(prefs)


def _check(prefs):
    for pref in prefs:
        print("[%s]" % pref)
        _, report = deploy.deploy_to_prefs(extra_prefs=[pref], check_only=True)
        for state, dest, _name in report:
            print("   %-22s %s" % (state, dest))
        path, state = deploy.pythonrc_state(pref)
        print("   %-22s %s" % (state, path))
        print("")
    print("在 Houdini 里点工具架的「Reload」就能让改动立刻生效，不用重启。")
    return 0


def _install(prefs):
    touched = 0
    for pref in prefs:
        print("[%s]" % pref)
        _, report = deploy.deploy_to_prefs(extra_prefs=[pref])
        for state, dest, _name in report:
            print("   %s %s" % (state, dest))
            touched += 1

        state = deploy.write_pythonrc(pref)
        print("   %s %s" % (state, deploy.pythonrc_path(pref)))
        touched += 1
        print("")

    print("装好了，共 %d 个文件。\n" % touched)
    print("现在有两种方式让它生效：")
    print("  A) **不用重启**：在 Houdini 里点工具架上的「Reload」")
    print("  B) 重启 Houdini（第一次装的时候最省事）")
    print("")
    print("pythonrc.py 会在每次 Houdini 启动时把**命令桥**拉起来，")
    print("之后 Claude Code 可以这样把 Python 送进来跑（不重启、不点按钮）：")
    print("  bash scripts/hcmd.sh --list")
    print("  bash scripts/hcmd.sh 'import houdini_ai.reload as r; r.reload_all()'")
    print("用 install.py --check 能看实例和命令的详细状态（离线也能看）。")
    print("")
    print("先确认网关能被读到：")
    print('  hython -c "from houdini_ai import config; '
          'print(config.describe(config.load()))"')
    return 0


def _uninstall(prefs):
    removed = 0
    for pref in prefs:
        _, report = deploy.deploy_to_prefs(extra_prefs=[pref], uninstall=True)
        for state, dest, _name in report:
            print("%s %s" % (state, dest))
            removed += 1
        state = deploy.remove_pythonrc(pref)
        if state:
            print("pythonrc.py：%s" % state)
            if state == "已删除":
                removed += 1

    print("\n删了 %d 个文件。重启 Houdini 生效。" % removed)
    print("（节点上的对话历史还在 .hip 里，不受影响）")
    return 0


if __name__ == "__main__":
    sys.exit(main())
