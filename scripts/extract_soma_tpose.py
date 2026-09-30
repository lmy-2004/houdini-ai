# -*- coding: utf-8 -*-
"""把 SOMA 的 T-pose 轴向偏移（`tp_i`）从 Kimodo 的 pickle 抽成 .npy。

**为什么需要它**：SOMA 模型的 `global_rot_mats` 在 rest 时是 identity，
意思是「关节坐标系与世界轴对齐」；而 KineFX 希望「骨头对齐」（某个本地轴指向子节点）。
两者差一个右乘偏移：`R_bone_i = R_global_i @ tp_i`。

**怎么验证它是对的**（本脚本会自己跑一遍）：把骨头方向转进关节本地坐标做直方图。
  * 做校正 → 集中在 ±X（本机实测 61 个关节里 59 个落在 ±X）
  * 不校正 → 散开在 ±X/±Y/±Z
这正是那份转换约定文档给的判据。

Kimodo 的 pickle 是 torch 存的，所以这个脚本要 torch（用 ComfyUI 那个 venv 跑）。
产出的 .npy 是纯 numpy，Houdini 侧不需要 torch。

用法：
    <有 torch 的 python> scripts/extract_soma_tpose.py [kimodo 仓库根]
"""

import ast
import os
import re
import sys

import numpy as np
import torch

HERE = os.path.dirname(os.path.abspath(__file__))
TOOL_ROOT = os.path.dirname(HERE)
DEFAULT_KIMODO = "D:/kimodo-motion/src/kimodo"
OUT_DIR = os.path.join(TOOL_ROOT, "houdini_ai", "data")


def parse_bvh(path):
    """解析 BVH 的 HIERARCHY：返回 (关节名列表, 父字典, 偏移字典)。去掉 Root 包装层。"""
    names, parents, offsets, cur = [], [], [], None
    for ln in open(path, "r", encoding="utf-8"):
        s = ln.strip()
        if s.startswith("MOTION"):
            break
        m = re.match(r"^(ROOT|JOINT)\s+(\w+)", s)
        if m:
            names.append(m.group(2))
            parents.append(cur)
            offsets.append(None)
            cur = len(names) - 1
            continue
        m = re.match(r"^OFFSET\s+([-\d.eE+]+)\s+([-\d.eE+]+)\s+([-\d.eE+]+)", s)
        if m and offsets[cur] is None:
            offsets[cur] = np.array([float(m.group(i)) for i in (1, 2, 3)])
            continue
        if s == "}":
            cur = parents[cur]

    joints = [n for n in names if n != "Root"]
    par = {}
    for i, n in enumerate(names):
        if n == "Root":
            continue
        p = parents[i]
        par[n] = None if (p is None or names[p] == "Root") else names[p]
    off = {n: offsets[names.index(n)] for n in joints}
    return joints, par, off


def fk_positions(joints, par, off):
    """T-pose 局部旋转全为 0 ⇒ FK 就是偏移的累加。"""
    pos = {}

    def walk(n, base):
        pos[n] = base + off[n]
        for c in joints:
            if par[c] == n:
                walk(c, pos[n])

    root = [n for n in joints if par[n] is None][0]
    walk(root, np.zeros(3))
    return pos


def axis_hist(joints, par, pos, rots):
    """把骨头方向转进关节本地坐标，统计落在哪个轴。"""
    ax = ["+X", "+Y", "+Z", "-X", "-Y", "-Z"]
    hist = {}
    for i, n in enumerate(joints):
        kids = [x for x in joints if par[x] == n]
        if not kids:
            continue
        wd = pos[kids[0]] - pos[n]
        nrm = np.linalg.norm(wd)
        if nrm < 1e-12:
            continue
        wd = wd / nrm
        ld = wd @ rots[i]
        a = int(np.argmax(np.abs(ld)))
        key = ax[a + (0 if ld[a] >= 0 else 3)]
        hist[key] = hist.get(key, 0) + 1
    return hist


def main():
    # 控制台可能是 GBK，别让一个对钩字符把整个脚本崩掉
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass
    root = sys.argv[1] if len(sys.argv) > 1 else DEFAULT_KIMODO
    skel_dir = os.path.join(root, "kimodo", "assets", "skeletons", "somaskel77")
    bvh = os.path.join(skel_dir, "somaskel77_standard_tpose.bvh")
    pkl = os.path.join(skel_dir, "standard_t_pose_global_offsets_rots.p")
    defs = os.path.join(root, "kimodo", "skeleton", "definitions.py")
    for p in (bvh, pkl, defs):
        if not os.path.isfile(p):
            raise SystemExit("!! 找不到 %s" % p)

    joints, par, off = parse_bvh(bvh)
    pos = fk_positions(joints, par, off)
    tp77 = np.asarray(torch.load(pkl, map_location="cpu", weights_only=False), dtype=np.float32)
    if tp77.shape != (len(joints), 3, 3):
        raise SystemExit("!! pickle 形状 %s 与 BVH 关节数 %d 对不上" % (tp77.shape, len(joints)))

    # --- 自校验：轴向直方图 ---
    # 判据用「同一条轴（± 合并）上的占比」：骨对齐时该压到 90% 以上，不校正则明显更低。
    # 只比 max/spread 太弱——本机实测那是 54% vs 36%，区分度不够。
    def dominant_axis(hist):
        pair = {}
        for k, v in hist.items():
            pair[k[-1]] = pair.get(k[-1], 0) + v
        top = max(pair.values())
        return top / max(1, sum(hist.values())), pair

    good = axis_hist(joints, par, pos, tp77)
    bad = axis_hist(joints, par, pos, np.tile(np.eye(3, dtype=np.float32), (len(joints), 1, 1)))
    gfrac, gpair = dominant_axis(good)
    bfrac, bpair = dominant_axis(bad)
    print("做 tp 校正后的轴向直方图：%s  （主轴 %s 占 %.0f%%）" % (good, max(gpair, key=gpair.get), gfrac * 100))
    print("不校正的轴向直方图：      %s  （主轴 %s 占 %.0f%%）" % (bad, max(bpair, key=bpair.get), bfrac * 100))
    if gfrac < 0.90:
        raise SystemExit("!! 自校验没过：校正后主轴只占 %.0f%%，没有压到 90%% 以上，说明 tp 用法不对" % (gfrac * 100))
    if bfrac >= 0.90:
        raise SystemExit("!! 自校验没过：不校正居然也集中在一条轴上（%.0f%%），判据失去意义" % (bfrac * 100))
    print("[OK] 自校验通过：校正后压在一条轴上，不校正则散开")

    # --- 算 30 关节的 tp（按名字从 77 里取子集）---
    tree = ast.parse(open(defs, "r", encoding="utf-8").read())
    idx77 = {n: i for i, n in enumerate(joints)}
    tp30_names, missing = [], []
    for cls in [n for n in tree.body if isinstance(n, ast.ClassDef) and n.name == "SOMASkeleton30"]:
        for item in cls.body:
            if isinstance(item, ast.Assign) and isinstance(item.targets[0], ast.Name) \
                    and item.targets[0].id == "bone_order_names_with_parents":
                for nm, _p in ast.literal_eval(item.value):
                    tp30_names.append(nm)
                    if nm not in idx77:
                        missing.append(nm)
    if not tp30_names:
        raise SystemExit("!! 没能从 definitions.py 里取到 SOMA30 的关节表")
    if missing:
        raise SystemExit("!! 这些 30 关节名在 77 里找不到：%s" % missing)
    tp30 = np.stack([tp77[idx77[n]] for n in tp30_names])
    print("SOMA30 的 tp 子集：%d 个关节，全部在 77 里按名字匹配到" % len(tp30))

    # --- T-pose 的关节世界位置（**米**）。BVH 是厘米，要除 100。
    #     这是 rest / capture 姿势的来源，也是「轴向直方图」判据唯一有意义的所在
    #     （动画帧上骨头的弯曲由姿势决定，直方图本来就会散，不能拿来当判据）。
    pos_m = {n: pos[n] / 100.0 for n in joints}
    pj77 = np.stack([pos_m[n] for n in joints]).astype(np.float32)
    pj30 = np.stack([pos_m[n] for n in tp30_names]).astype(np.float32)
    hip = float(pos_m["Hips"][1])
    print("T-pose 髋高 %.3f 米（确认 BVH 是厘米、除以 100 后是米）" % hip)
    if not 0.9 < hip < 1.1:
        raise SystemExit("!! T-pose 髋高 %.3f 米不像人类尺度，单位换算可能错了" % hip)

    if not os.path.isdir(OUT_DIR):
        os.makedirs(OUT_DIR)
    outs = [
        ("soma_tpose_offsets_77.npy", tp77),
        ("soma_tpose_offsets_30.npy", tp30),
        ("soma_tpose_joints_77.npy", pj77),
        ("soma_tpose_joints_30.npy", pj30),
    ]
    print("已写入：")
    for fn, arr in outs:
        p = os.path.join(OUT_DIR, fn)
        np.save(p, arr)
        print("  %-28s %s" % (fn, arr.shape))


if __name__ == "__main__":
    main()
