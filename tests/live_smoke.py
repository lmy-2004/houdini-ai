# -*- coding: utf-8 -*-
"""真实联网冒烟测试。**会花钱**，所以不放进 run_all.py，手动跑：

    "D:/ProgramFiles/Houdini22/bin/hython.exe" tests/live_smoke.py "你的中文描述"

不传描述就用默认那句。这个脚本的价值在于：假网关能验证「循环转得通」，
只有真网关能验证「模型到底建不建得对」。
"""

import sys
import threading
import time

import _harness  # 只为了把工具根目录塞进 sys.path

import hou
from houdini_ai import builder, config, node as node_mod, runner, session

# 用法：
#   hython tests/live_smoke.py "做一个矮胖的圆柱，顶面做出向内凹陷的效果"
#   hython tests/live_smoke.py "做一个盒子" "再高一点，并且加一点噪声"
#   hython tests/live_smoke.py "复杂的塔" --depth=deep
# 传两个位置参数时会跑第二轮（提修改意见），验证后续对话。

_DEPTH = "standard"
_args = []
for _a in sys.argv[1:]:
    if _a.startswith("--depth="):
        _DEPTH = _a.split("=", 1)[1]
    else:
        _args.append(_a)

DESCRIPTION = _args[0] if _args else "做一个矮胖的圆柱，顶面做出向内凹陷的效果"
FOLLOWUP = _args[1] if len(_args) > 1 else ""


def main():
    cfg = config.load()
    print("网关：%s" % config.describe(cfg))
    if not cfg.get("api_key"):
        print("\n没有读到密钥。检查 ~/.claude/settings.json 或环境变量。")
        return 2

    obj = hou.node("/obj")
    for old in list(obj.children()):
        if old.name().startswith("LIVE_"):
            old.destroy()
    geo = obj.createNode("geo", "LIVE_geo")
    ai = node_mod.make_ai_node(geo, "live1", prompt=DESCRIPTION)
    ai.parm(node_mod.DEPTH).set(_DEPTH)
    geo.setDisplayFlag(True)
    geo.layoutChildren()

    preset = config.depth_preset(_DEPTH)
    print("\n思考深度：%s（thinking=%s；步数不设限，等它自己收工）"
          % (preset["label"], preset["thinking"]))
    print("熔断：只有时间（%s 分钟；填 0 表示不管）"
          % config.load().get("runaway_minutes"))
    print("描述：%s" % DESCRIPTION)
    print("容器：%s" % ai.path())
    print("\n开始跑（实时状态会打在下面）…\n")

    def points_now():
        geometry = ai.geometry()
        return geometry.pointCount() if geometry is not None else 0

    start = time.time()
    cancel = threading.Event()
    runner._run_round(ai.path(), "build", DESCRIPTION, cancel)
    first_elapsed = time.time() - start
    first_points = points_now()
    first_nodes = sorted(c.name() for c in ai.children())
    print("\n第一轮完成：%.1f 秒，%d 点" % (first_elapsed, first_points))

    second_elapsed = 0.0
    second_points = None
    if FOLLOWUP:
        print("\n第二轮（修改意见）：%s\n" % FOLLOWUP)
        start = time.time()
        runner._run_round(ai.path(), "followup", FOLLOWUP, cancel)
        second_elapsed = time.time() - start
        second_points = points_now()
        print("\n第二轮完成：%.1f 秒，%d 点" % (second_elapsed, second_points))

    elapsed = first_elapsed + second_elapsed

    print("\n" + "=" * 62)
    print("耗时 %.1f 秒" % elapsed)
    if FOLLOWUP:
        print("第一轮：%.1f 秒 / %d 点 / 节点 %s"
              % (first_elapsed, first_points, first_nodes))
        print("第二轮：%.1f 秒 / %s 点" % (second_elapsed, second_points))
        print("第二轮网络变了：%s"
              % ("是" if sorted(c.name() for c in ai.children()) != first_nodes
                 else "节点名没变（只调了参数）"))
    print("状态：%s / %s" % (node_mod.current_state(ai),
                            node_mod.status_line(ai)))
    if node_mod.detail_of(ai):
        print("细节：%s" % node_mod.detail_of(ai))
    if node_mod.error_of(ai):
        print("错误：\n%s" % node_mod.error_of(ai))
    print("\n--- 节点注释（你在场景里看到的那段）---")
    print(ai.comment())

    print("\n--- 过程 ---")
    data = session.load(ai)
    for record in data.get("rounds", []):
        for line in record.get("trace", []):
            print("  · %s" % line)

    # ★ 账单。这是判断「改完到底变好没有」最直接的读数：
    #   * 输入里有多少走了缓存 —— 命中率高说明前缀稳定、便宜；
    #   * 输出总量 —— 它没被限制住就应该是个像样的数；
    #   * 有没有被截断 —— `max_tokens` 已经不发（见 gateway.py 顶部），
    #     这一项正常应该是 0。
    print("\n--- token 账单 ---")
    for record in data.get("rounds", []):
        usage = record.get("usage") or {}
        if not usage:
            print("  第 %s 轮：没记到用量（网关没回 usage？）" % record.get("n"))
            continue
        billed = usage.get("input_tokens", 0) + \
            usage.get("cache_creation_input_tokens", 0)
        cached = usage.get("cache_read_input_tokens", 0)
        total_in = billed + cached
        rate = (100.0 * cached / total_in) if total_in else 0.0
        print("  第 %s 轮：%s 步 | 输入 %d（其中缓存读 %d，命中 %.0f%%）"
              " | 输出 %d"
              % (record.get("n"), record.get("turns", 0),
                 total_in, cached, rate, usage.get("output_tokens", 0)))
        if record.get("truncated"):
            print("      ⚠ 输出被截断过 %d 次" % record["truncated"])

    print("\n--- 容器自己 ---")
    print("  名字：%s" % ai.name())
    promoted = builder.load_promotions(ai)
    if promoted:
        print("  提升到外面的参数（不用进来就能调）：")
        for entry in promoted:
            parm = ai.parm(entry.get("as"))
            value = parm.eval() if parm is not None else None
            print("     %-18s %-12s <- %s.%s   当前 %s"
                  % (entry.get("as"), entry.get("label") or "",
                     entry.get("node"), entry.get("parm"), value))
    else:
        print("  （没有提升任何参数）")
    print("  参数面板里用户能看到的：%s"
          % [p.name() for p in ai.parmTuples() if p.name().startswith("ai_")])

    print("\n--- 建出来的网络（自动排版后，从上到下）---")
    for child in sorted(ai.children(), key=lambda n: -n.position()[1]):
        ins = []
        for i in range(child.type().maxNumInputs()):
            upstream = child.input(i)
            if upstream is not None:
                ins.append("%s:%s" % (i, upstream.name()))
        pos = child.position()
        print("  %-14s %-22s x=%6.2f y=%6.2f 输入[%s]"
              % (child.name(), child.type().name(), pos[0], pos[1],
                 ", ".join(ins)))

    print("\n--- 在外面调一下试试 ---")
    changed = False
    for entry in promoted[:2]:
        parm = ai.parm(entry.get("as"))
        inner = ai.node(entry.get("node"))
        if parm is None or inner is None:
            continue
        before = parm.eval()
        try:
            new = float(before) * 1.5 + 1.0 if isinstance(before, float) else before
            parm.set(new)
            after = inner.parm(entry.get("parm")).eval()
            print("  %s: %s -> %s，内层 %s.%s 跟着变成 %s ✓"
                  % (entry.get("as"), before, new, entry.get("node"),
                     entry.get("parm"), after))
            parm.set(before)
            changed = True
        except Exception as exc:
            print("  %s 调整失败：%s" % (entry.get("as"), exc))
    if not changed:
        print("  （没有可调的提升参数）")
    summary = None
    for record in data.get("rounds", []):
        if record.get("summary"):
            summary = record["summary"]
    if summary:
        print("\n--- AI 的说明 ---\n%s" % summary)

    geometry = ai.geometry()
    points = geometry.pointCount() if geometry is not None else 0
    print("\n产出：%d 点" % points)
    print("cook 报错：%s" % (list(ai.errors()) or "无"))
    print("=" * 62)

    if points == 0:
        print("\n⚠ 产出为空——这是要重点看的情况")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
