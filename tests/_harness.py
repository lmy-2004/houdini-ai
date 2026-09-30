# -*- coding: utf-8 -*-
"""极简测试脚手架。

不用 pytest：Houdini 自带的 Python 没装它，而为了跑测试去动 Houdini 的解释器
环境不值得。这个够用了——每个测试文件末尾调 finish()，失败就非零退出。
"""

import os
import sys
import traceback

_TOOL_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _TOOL_ROOT not in sys.path:
    sys.path.insert(0, _TOOL_ROOT)

_RESULTS = []


def check(label, condition, detail=""):
    ok = bool(condition)
    _RESULTS.append((label, ok, detail))
    print("  %s %s%s" % ("PASS" if ok else "FAIL", label,
                         ("" if ok else "   <- " + str(detail))))
    return ok


def check_eq(label, actual, expected):
    return check(label, actual == expected, "得到 %r，期望 %r" % (actual, expected))


def check_raises(label, exc_type, fn, *args, **kwargs):
    try:
        fn(*args, **kwargs)
    except exc_type as exc:
        return check(label, True, "")
    except Exception as exc:
        return check(label, False, "抛的是 %s 而不是 %s：%s"
                     % (type(exc).__name__, exc_type.__name__, exc))
    return check(label, False, "没抛异常（期望 %s）" % exc_type.__name__)


def section(title):
    print("\n== %s ==" % title)


def finish(name):
    failed = [r for r in _RESULTS if not r[1]]
    print("\n%s: %d 项，%d 通过，%d 失败"
          % (name, len(_RESULTS), len(_RESULTS) - len(failed), len(failed)))
    if failed:
        for label, _, detail in failed:
            print("   失败：%s  %s" % (label, detail))
        sys.exit(1)
    sys.exit(0)


def guard(fn):
    """包住一个测试段落，异常转成失败而不是直接崩。"""
    try:
        fn()
    except Exception:
        _RESULTS.append(("未捕获异常", False, traceback.format_exc()))
        print(traceback.format_exc())
