# -*- coding: utf-8 -*-
"""找 Houdini 的 pref 目录。

★ 本机有两个同版本的 pref 目录，**取决于 Houdini 是怎么启动的**：
  * 从开始菜单 / 桌面图标启动 -> `%USERPROFILE%\\Documents\\houdini22.0`   ← GUI 常态
  * 从 Git Bash 启动          -> `%USERPROFILE%\\houdini22.0`（HOME 被改写过）

两份都要装，否则换个方式启动工具就没了。Fab 工具架也是这么做的。

另外本机还留着 houdini20.5 / houdini21.0 的 pref 目录——那是旧版本的，
装进去纯属污染，所以默认只装当前版本。
"""

import os


def target_version():
    """要装给哪个版本。能 import hou 就用它的版本号。"""
    try:
        import hou

        major, minor = hou.applicationVersion()[:2]
        return "houdini%d.%d" % (major, minor)
    except Exception:
        return "houdini22.0"


def version_key(name):
    try:
        return tuple(int(part) for part in
                     name.replace("houdini", "").split(".") if part.isdigit())
    except Exception:
        return (0,)


def all_pref_dirs():
    """所有存在的 Houdini pref 目录，版本新的在前。"""
    home = os.path.expanduser("~")
    found = []
    for parent in (os.path.join(home, "Documents"), home):
        if not os.path.isdir(parent):
            continue
        try:
            entries = sorted(os.listdir(parent))
        except Exception:
            continue
        for entry in entries:
            if not entry.startswith("houdini"):
                continue
            path = os.path.join(parent, entry)
            if os.path.isdir(path):
                found.append(path)

    seen, ordered = set(), []
    for path in found:
        if path not in seen:
            seen.add(path)
            ordered.append(path)
    ordered.sort(key=lambda p: version_key(os.path.basename(p)), reverse=True)
    return ordered


def candidate_pref_dirs(version=None, every_version=False):
    """要装的 pref 目录。默认只装当前版本，但同版本的两个目录都装。"""
    dirs = all_pref_dirs()
    if every_version:
        return dirs
    want = version or target_version()
    matching = [p for p in dirs if os.path.basename(p) == want]
    if matching:
        return matching
    # 没有正好匹配的，退回同主版本号
    major = want.replace("houdini", "").split(".")[0]
    return [p for p in dirs if os.path.basename(p).startswith("houdini%s." % major)]
