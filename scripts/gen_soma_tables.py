# -*- coding: utf-8 -*-
"""从 Kimodo 源码生成 SOMA 骨架表（关节名 + 父节点）。

**为什么不手抄**：SOMA77 有 77 条、SOMA30 有 30 条，手抄必错且没人能校对。
这里用 `ast` 直接解析 Kimodo 自己的 `skeleton/definitions.py`，
真值只有一处（Kimodo 那边），Kimodo 升级时重跑这个脚本即可。

用法：
    hython scripts/gen_soma_tables.py [kimodo 仓库根目录]

默认找 `D:/kimodo-motion/src/kimodo`。产出 `houdini_ai/soma_skeleton.py`。
"""

import ast
import os
import sys

DEFAULT_KIMODO = "D:/kimodo-motion/src/kimodo"
OUT = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
    "houdini_ai", "soma_skeleton.py",
)

# 我们需要的两个类，以及「末端执行器」那些链（做约束时要按名字选关节）
WANTED = ["SOMASkeleton77", "SOMASkeleton30"]
CHAIN_ATTRS = [
    "right_foot_joint_names", "left_foot_joint_names",
    "right_hand_joint_names", "left_hand_joint_names",
]


def _literal(node):
    """把 AST 节点还原成 Python 值；失败返回 None。"""
    try:
        return ast.literal_eval(node)
    except Exception:
        return None


def parse(defs_path):
    tree = ast.parse(open(defs_path, "r", encoding="utf-8").read())
    out = {}
    for cls in [n for n in tree.body if isinstance(n, ast.ClassDef)]:
        if cls.name not in WANTED:
            continue
        rec = {"chains": {}}
        for item in cls.body:
            # 形如  bone_order_names_with_parents = [...]
            if isinstance(item, ast.Assign) and len(item.targets) == 1:
                tgt = item.targets[0]
                if not isinstance(tgt, ast.Name):
                    continue
                if tgt.id == "bone_order_names_with_parents":
                    val = _literal(item.value)
                    if val is None:
                        raise SystemExit("!! bone_order_names_with_parents 解析失败：它可能不再是纯字面量")
                    rec["joints"] = val
                elif tgt.id in CHAIN_ATTRS:
                    val = _literal(item.value)
                    if val is not None:
                        rec["chains"][tgt.id] = val
            # 形如  name = "somaskel77"
            elif isinstance(item, ast.Assign):
                pass
        for item in cls.body:
            if isinstance(item, ast.Assign) and len(item.targets) == 1 \
                    and isinstance(item.targets[0], ast.Name) and item.targets[0].id == "name":
                v = _literal(item.value)
                if isinstance(v, str):
                    rec["skel_name"] = v
        if "joints" not in rec:
            raise SystemExit("!! %s 里没找到 bone_order_names_with_parents" % cls.name)
        out[cls.name] = rec
    missing = [c for c in WANTED if c not in out]
    if missing:
        raise SystemExit("!! 源码里没有这些类：%s" % missing)
    return out


def emit(data, src_path):
    L = []
    L.append("# -*- coding: utf-8 -*-")
    L.append('"""SOMA 骨架表（关节名 + 父节点）—— **自动生成，不要手改**。')
    L.append("")
    L.append("生成器：`scripts/gen_soma_tables.py`")
    # 路径里的反斜杠会让生成的模块触发 invalid escape 警告，统一成正斜杠
    L.append("真值来源：%s" % src_path.replace("\\", "/"))
    L.append("")
    L.append("Kimodo 升级后重跑生成器即可。手抄 77 条关节名是必错的，所以走生成。")
    L.append('"""')
    L.append("")
    L.append("# (%s)" % "关节名, 父节点名（根为 None）")
    for cls_name in WANTED:
        rec = data[cls_name]
        joints = [(str(n), (None if p is None else str(p))) for n, p in rec["joints"]]
        L.append("")
        L.append("# %s（%s）—— %d 个关节" % (cls_name, rec.get("skel_name", "?"), len(joints)))
        L.append("%s_JOINTS = [" % cls_name)
        for n, p in joints:
            L.append("    (%r, %r)," % (n, p))
        L.append("]")
    L.append("")
    L.append("")
    L.append("SKELETONS = {")
    for cls_name in WANTED:
        rec = data[cls_name]
        L.append("    %r: {" % rec.get("skel_name", cls_name))
        L.append("        'class': %r," % cls_name)
        L.append("        'joints': %s_JOINTS," % cls_name)
        L.append("        'chains': {")
        for k in CHAIN_ATTRS:
            v = rec["chains"].get(k, [])
            L.append("            %r: %r," % (k, v))
        L.append("        },")
        L.append("    },")
    L.append("}")
    L.append("")
    L.append("")
    L.append("def joint_count(skel_name):")
    L.append('    """按 NPZ 的关节数反推骨架名（30 或 77）。"""')
    L.append("    for name, rec in SKELETONS.items():")
    L.append("        if len(rec['joints']) == skel_name:")
    L.append("            return name")
    L.append("    return None")
    L.append("")
    L.append("")
    L.append("def joints_for_count(n):")
    L.append('    """给关节数（30 / 77），返回 (骨架名, [(关节名, 父节点名), ...])。"""')
    L.append("    for name, rec in SKELETONS.items():")
    L.append("        if len(rec['joints']) == n:")
    L.append("            return name, list(rec['joints'])")
    L.append("    return None, None")
    L.append("")
    L.append("")
    L.append("def root_joint(skel_name):")
    L.append('    """根关节名（唯一 parent 为 None 的那个）。"""')
    L.append("    for n, p in SKELETONS[skel_name]['joints']:")
    L.append("        if p is None:")
    L.append("            return n")
    L.append("    return None")
    L.append("")
    return "\n".join(L) + "\n"


def main():
    root = sys.argv[1] if len(sys.argv) > 1 else DEFAULT_KIMODO
    defs = os.path.join(root, "kimodo", "skeleton", "definitions.py")
    if not os.path.isfile(defs):
        raise SystemExit("!! 找不到 %s\n   用法：hython scripts/gen_soma_tables.py <kimodo 仓库根>" % defs)
    data = parse(defs)
    text = emit(data, defs)
    with open(OUT, "w", encoding="utf-8", newline="\n") as f:
        f.write(text)
    for cls_name in WANTED:
        n = len(data[cls_name]["joints"])
        print("  %-16s %d 个关节" % (cls_name, n))
    print("已写入 %s（%d 字节）" % (OUT, len(text.encode("utf-8"))))


if __name__ == "__main__":
    main()
