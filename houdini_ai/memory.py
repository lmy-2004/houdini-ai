# -*- coding: utf-8 -*-
"""跨节点的共享记忆。

## 为什么要有它

每个 AI 节点的对话历史存在自己身上（`session.py`），所以**节点之间互相看不见**——
同一个 .hip 里的第二个 AI 节点不知道第一个建了什么、用了什么约定、
这个项目有什么坑。它是一座孤岛。

这里放的是**能跨节点复用的经验和教训**，由 AI 自己判断值不值得记。

## 存哪、为什么存在那儿

存在 `/obj` 的 userData 上，键 `houdini_ai_memory`。

选 `/obj` 是因为它一定在场景里、比根节点常规。存 userData 是因为官方文档明确
`setUserData` / `userData` / `userDataDict` 属于 **"Saved with scene file"**
（只支持字符串），而 `cachedUserData` 才是**不**存的。
来源：https://www.sidefx.com/docs/houdini/hom/nodeuserdata.md

所以这些记忆**跟着 .hip 走**：你把文件拷到别的机器，经验还在。

## 为什么要设上限

不设上限的话它迟早变成一堆没人读的日志，而且每轮都要读给模型看、白烧 token。
所以**条数封顶、满了丢最旧的**。

★★ 但**上限只有这一个**（`MAX_NOTES`），而且它是**明示的存储上限**——
不在读取的时候再偷偷砍一刀（2026-09-29 用户定：不要截断任何内容）。
以前这里还有 `BRIEF_MAX_NOTES=20` / `BRIEF_MAX_CHARS=3000`，每次注入只给
最近的 20 条——那是"存了 40 条、只让看 20 条"，模型看到的世界是残缺的。
现在**存多少就给看多少**。要调节开销就调 `MAX_NOTES`，那是显式的。

## 什么时候注入（2026-09-29 起）

不是每轮都注入，而是**变了才注入**。理由：这个节点自己写的条目本来就在
它自己的对话历史里，每轮重发一遍是纯冗余；真正需要注入的是**别的节点写的**
和你**在面板里手改的**。所以按指纹比对（`digest()`），没变就不发。
细节见 `runner._run_round` 里那段。
"""

import hashlib
import json
import time

# 记忆挂在哪个节点上。选 /obj 是因为它一定在场景里。
ANCHOR_PATH = "/obj"
KEY = "houdini_ai_memory"

# 条数上限。满了丢最旧的。★ 这是**唯一**的上限，而且是明示的。
MAX_NOTES = 40
# 单条长度上限。模型写超了会被**拒绝**（告诉它拆成两条），
# 不是被偷偷砍短——静默改它写的东西比拒绝坏得多。
MAX_NOTE_CHARS = 500

# 注入时的抬头。runner 把它拼在本轮**第一条 user 消息**的开头
# （不是系统提示词——系统提示词是缓存前缀的头部，放会变的东西会让整条前缀失效）。
HEADER = ("# 这个 .hip 里的共享经验\n"
          "前面几轮（可能是别的 AI 节点）总结下来的，供参考。"
          "如果你发现了新的、别的节点也用得上的经验，用 remember 记下来。")

VERSION = 1


class MemoryError_(Exception):
    pass


def _anchor():
    """记忆挂靠的节点。"""
    import hou

    node = hou.node(ANCHOR_PATH)
    if node is None:
        raise MemoryError_("找不到 %s，没法存取共享记忆" % ANCHOR_PATH)
    return node


def load():
    """读全部记忆。坏数据一律当空处理，绝不因为记忆读不出来就把构建搞挂。"""
    try:
        raw = _anchor().userData(KEY)
    except Exception:
        return []
    if not raw:
        return []
    try:
        data = json.loads(raw)
    except ValueError:
        return []
    if not isinstance(data, dict):
        return []
    notes = data.get("notes")
    if not isinstance(notes, list):
        return []
    # 只留下结构对得上的
    clean = []
    for item in notes:
        if isinstance(item, dict) and item.get("text"):
            clean.append({
                "text": str(item.get("text")),
                "from": str(item.get("from") or ""),
                "time": str(item.get("time") or ""),
            })
    return clean


def save(notes):
    try:
        _anchor().setUserData(
            KEY, json.dumps({"version": VERSION, "notes": notes},
                            ensure_ascii=False))
        return True
    except Exception:
        return False


def add(text, source=""):
    """记一条。返回 (ok, 全部记忆, 说明)。

    重复的（文本一模一样）不重复记，只回一句说明——省得模型每轮都记一遍同样的话。
    """
    text = (text or "").strip()
    if not text:
        return False, load(), "内容是空的"
    if len(text) > MAX_NOTE_CHARS:
        # ★ **拒绝，不砍短**。静默改它写的东西，它下轮读回来会发现自己的话
        # 变了样——那比直接告诉它"太长"坏得多。和 build 的 SpecError 一个套路。
        return False, load(), (
            "这条太长了（%d 字，上限 %d）。共享经验要的是**一句能独立读懂的话**，"
            "请拆成两条更短的。" % (len(text), MAX_NOTE_CHARS))

    notes = load()
    for existing in notes:
        if existing["text"] == text:
            return False, notes, "这条已经记过了"

    notes.append({
        "text": text,
        "from": source or "",
        "time": time.strftime("%Y-%m-%d %H:%M"),
    })

    dropped = 0
    if len(notes) > MAX_NOTES:
        dropped = len(notes) - MAX_NOTES
        notes = notes[dropped:]

    if not save(notes):
        return False, load(), "写入失败"
    return True, notes, ("记下了" if not dropped
                         else "记下了（挤掉了 %d 条最旧的）" % dropped)


def clear():
    try:
        _anchor().destroyUserData(KEY)
        return True
    except Exception:
        return False


def count():
    return len(load())


def digest():
    """当前全部记忆的指纹。**用它决定这一轮要不要把记忆注入给模型。**

    返回空串 = 没有记忆。见文件顶部「什么时候注入」那段。
    """
    notes = load()
    if not notes:
        return ""
    blob = "\n".join("%s\x1f%s" % (note.get("from") or "", note["text"])
                     for note in notes)
    return hashlib.sha1(blob.encode("utf-8")).hexdigest()[:16]


def brief():
    """这一轮要注入给模型的共享经验全文。

    ★ **不再限量**（2026-09-29 用户定：不要截断任何内容）。以前这里砍到
    最近 20 条 / 3000 字符。真正的界是存储上限 `MAX_NOTES`——要调开销就调它，
    那是个**明示的**数字，不是注入时偷偷砍的一刀。
    """
    notes = load()
    if not notes:
        return ""
    lines = ["· [%s] %s" % (note.get("from") or "（不知道来源）", note["text"])
             for note in reversed(notes)]        # 新的排在前面
    return HEADER + "\n" + "\n".join(lines)


def to_text():
    """给**编辑框**用的纯文本：一行一条，不带元数据。

    为什么不带 `from` / `time`：那是给人看的注解，混在可编辑文本里只会碍事。
    元数据在 `from_text` 里按「这行跟原来哪条一字不差」认回去，所以你没改过
    的行，来源和时间都保得住。
    """
    return "\n".join(note["text"] for note in load())


def from_text(text, source="手动编辑"):
    """把编辑框里的文本写回记忆。

    - 一行一条，空行忽略
    - **跟原来某条一字不差的行**：保留它原来的来源和时间（你只是没动它）
    - 新写的或改过的行：来源标成 `source`

    ★ 这里**不套用 `MAX_NOTE_CHARS`**：那是给模型写的条目设的闸，不是给
    你自己设的。你在编辑框里打多长就存多长——静默砍掉你打进去的字比什么都糟。
    （条数上限 `MAX_NOTES` 照旧生效，那是防 .hip 无限长大的。）
    """
    old = load()
    by_text = {}
    for note in old:
        by_text.setdefault(note["text"], note)

    notes = []
    for line in (text or "").splitlines():
        line = line.strip()
        if not line:
            continue
        if line in by_text:
            notes.append(dict(by_text[line]))       # 没动过，元数据留着
        else:
            notes.append({"text": line, "from": source,
                          "time": time.strftime("%Y-%m-%d %H:%M")})

    if len(notes) > MAX_NOTES:
        notes = notes[-MAX_NOTES:]
    return save(notes)


def dump():
    """给人看的一行行文本。面板和 Python Shell 都用它。"""
    notes = load()
    if not notes:
        return "（共享记忆是空的）"
    out = []
    for i, note in enumerate(notes, 1):
        source = note.get("from") or "?"
        out.append("%2d. [%s] %s\n    %s"
                   % (i, note.get("time") or "?", source, note["text"]))
    return "\n".join(out)
