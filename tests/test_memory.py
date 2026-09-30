# -*- coding: utf-8 -*-
"""共享记忆的测试。

    "D:/ProgramFiles/Houdini22/bin/hython.exe" tests/test_memory.py

★ 最要紧的一条在最后：**存一次 .hip 再读回来，记忆还在不在**。
「跟着 .hip 文件走」是这个需求的地基，不能靠推断，必须真存真读。
（所以那一段放在文件末尾——`hipFile.load` 会把整个场景换掉。）
"""

import _harness as H

H.section("存取的基本行为")
import hou
from houdini_ai import memory

obj = hou.node("/obj")
H.check("记忆挂在 /obj 上", obj is not None)
H.check("锚点就是 /obj", memory.ANCHOR_PATH == "/obj")

memory.clear()
H.check("清空之后是空的", memory.load() == [])
H.check("空的时候 count 是 0", memory.count() == 0)
H.check("空的时候 dump 给一句人话", "空" in memory.dump(), memory.dump())
H.check("空的时候 brief 是空串（不往提示词里塞空段落）",
        memory.brief() == "", repr(memory.brief()))

ok, notes, why = memory.add("这个项目里 gltf 导入的 uv 是 3 分量",
                            source="/obj/geo1/AI1")
H.check("记一条成功", ok, why)
H.check("读得回来", len(memory.load()) == 1, memory.load())
H.check("内容对", memory.load()[0]["text"].startswith("这个项目里"), memory.load())
H.check("记了来源（用户看得出是谁总结的）",
        memory.load()[0]["from"] == "/obj/geo1/AI1", memory.load()[0])
H.check("记了时间", bool(memory.load()[0]["time"]), memory.load()[0])
H.check("count 跟着涨", memory.count() == 1)

H.section("重复的不重复记（省得模型每轮记一遍一样的话）")
ok, notes, why = memory.add("这个项目里 gltf 导入的 uv 是 3 分量")
H.check("同一条再记会被挡下", not ok, why)
H.check("说明里讲了原因", "记过" in why, why)
H.check("条数没变", memory.count() == 1)

H.section("空内容不记")
ok, _notes, why = memory.add("   ")
H.check("空白被拒", not ok, why)

H.section("★ 单条太长：**拒绝**，不是偷偷砍短（2026-09-29 定）")
# 以前写超了会被砍到 500 字加个「…」再存进去。那等于**静默改了模型写的
# 东西**——它下轮读回来会发现自己的话变了样。现在直接拒绝并让它拆开。
before = len(memory.load())
long_text = "很长的经验" * 200
ok, _notes, why = memory.add(long_text)
H.check("★ 超长被拒了", not ok, why)
H.check("★ 拒绝的理由说清了「太长」和上限",
        "太长" in why and str(memory.MAX_NOTE_CHARS) in why, why)
H.check("★ 还给了出路（让它拆成两条）", "拆" in why, why)
H.check("★ 被拒之后记忆条数没变（没偷偷塞一条砍过的进去）",
        len(memory.load()) == before, (before, len(memory.load())))
H.check("★ 记忆里没有任何一条以「…」结尾（那就是被砍过的痕迹）",
        not any(n["text"].endswith("…") for n in memory.load()))

H.section("★ 条数封顶：满了丢最旧的，不是无限长")
memory.clear()
for i in range(memory.MAX_NOTES + 5):
    memory.add("经验编号 %03d" % i)
notes = memory.load()
H.check("条数正好卡在上限", len(notes) == memory.MAX_NOTES, len(notes))
H.check("★ 丢掉的是最旧的（第 0 条不在了）",
        not any("编号 000" in n["text"] for n in notes))
H.check("最新那条留着", "编号 %03d" % (memory.MAX_NOTES + 4) in notes[-1]["text"],
        notes[-1])
H.check("说明里报告了挤掉几条",
        "挤掉" in memory.add("再来一条")[2], memory.add("再来一条")[2])

H.section("★ brief **全量给**，不再砍到 20 条 / 3000 字符（2026-09-29 定）")
# 以前是"存 40 条、只让模型看 20 条"——模型看到的世界是残缺的。
# 这里把断言**反转**成完整性：存多少就给看多少。
memory.clear()
COUNT = 30
for i in range(COUNT):
    memory.add("经验 %02d" % i, source="/obj/g/AI%d" % i)
brief = memory.brief()
H.check("brief 非空", bool(brief))
H.check("brief 里有标题说明这是共享经验", "共享经验" in brief, brief[:120])
H.check("★ 条数**全部给出**（%d 条一条不少）" % COUNT,
        brief.count("· [") == COUNT, brief.count("· ["))
H.check("★ 没有「还有 N 条没显示」这种话（不再有隐藏的部分）",
        "没显示" not in brief)
H.check("★ 最早那条也在（被砍的话最先丢的就是它）", "经验 00" in brief)
H.check("★ 最新那条也在", "经验 29" in brief)
H.check("新的排在前面", brief.index("经验 29") < brief.index("经验 00"))
H.check("带上了来源", "/obj/g/AI" in brief)
H.check("★ 那两个限量常量已经不存在了",
        not hasattr(memory, "BRIEF_MAX_NOTES")
        and not hasattr(memory, "BRIEF_MAX_CHARS"))

H.section("★ digest：用指纹决定要不要注入")
memory.clear()
H.check("没有记忆时指纹是空的（= 不用注入）", memory.digest() == "")
memory.add("第一条", source="/obj/g/AI0")
d1 = memory.digest()
H.check("有记忆就有指纹", bool(d1) and len(d1) >= 8, d1)
H.check("同样的内容 → 同样的指纹", memory.digest() == d1)
memory.add("第二条", source="/obj/g/AI1")
H.check("★ 内容变了 → 指纹就变（这就是「变了才注入」的判据）",
        memory.digest() != d1)
H.check("重复 add 同一条不会改指纹",
        memory.digest() == memory.digest())

H.section("dump 给人看的格式")
text = memory.dump()
H.check("dump 里有编号", " 1." in text or "1." in text, text[:80])
H.check("dump 里有来源", "/obj/g/" in text)

H.section("派发成工具")
from houdini_ai import schema

memory.clear()
geo = obj.createNode("geo", "M_geo")
ai = hou.node("/obj").createNode("subnet", "M_ai") if False else None
from houdini_ai import node as node_mod

ai = node_mod.make_ai_node(geo, "M_ai", prompt="x")
result = schema.dispatch(ai, "remember", {"text": "polyextrude 的 translate 是 3 分量"})
H.check("工具返回成功", result.get("ok"), result)
H.check("工具回传了全部记忆（模型看得到全貌）",
        len(result.get("all_notes") or []) == 1, result)
H.check("★ 来源自动记成了容器路径",
        memory.load()[0]["from"] == ai.path(),
        memory.load()[0])

H.section("坏数据不影响开工")
try:
    obj.setUserData(memory.KEY, "{这不是合法 JSON")
except Exception:
    pass
H.check("坏 JSON 读成空列表而不是抛异常", memory.load() == [], memory.load())
obj.destroyUserData(memory.KEY)

H.section("★★ 编辑框的往返：to_text / from_text")
# 用户要求共享记忆**可编辑**（「我得可以编辑它呀」）。编辑框里是纯文本
# （一行一条，不带元数据），写回时按「这行跟原来哪条一字不差」认回元数据。
memory.clear()
memory.add("polyextrude 的 translate 是 3 分量", source="/obj/g/AI1")
memory.add("这个项目的单位是米", source="/obj/g/AI2")

text = memory.to_text()
H.check("一行一条", len(text.splitlines()) == 2, repr(text))
H.check("★ 不带元数据（from/time 不混进可编辑文本）",
        "/obj/g/AI1" not in text and ":" not in text.replace("：", ""),
        repr(text))
H.check("顺序跟存的顺序一致",
        text.splitlines()[0] == "polyextrude 的 translate 是 3 分量", repr(text))

times = [n["time"] for n in memory.load()]
H.check("★ 一字不改地写回去，条数不变", memory.from_text(text) is True)
after = memory.load()
H.check_eq("还是两条", len(after), 2)
H.check_eq("★ 来源全保住了",
           [n["from"] for n in after], ["/obj/g/AI1", "/obj/g/AI2"])
H.check_eq("★ 时间也全保住了", [n["time"] for n in after], times)

H.section("★ 编辑：改一条、删一条、加一条")
memory.from_text("改过的第一条\n新加的一条")
notes = memory.load()
H.check_eq("删掉的那条没了", len(notes), 2)
H.check_eq("改过的那条在", notes[0]["text"], "改过的第一条")
H.check("★ 改过的内容来源标成「手动编辑」",
        notes[0]["from"] == "手动编辑", notes[0]["from"])
H.check("新加的也是手动编辑", notes[1]["from"] == "手动编辑")
H.check("★ 时间戳是新的（不是继承来的）",
        notes[0]["time"] >= times[0], (times[0], notes[0]["time"]))

H.section("编辑时的边角")
memory.clear()
memory.from_text("第一条\n\n\n  第二条  \n")
notes = memory.load()
H.check_eq("★ 空行忽略、首尾空格去掉", len(notes), 2)
H.check_eq("内容被 strip 过", notes[1]["text"], "第二条")

memory.from_text("\n\n  \n")
H.check_eq("★ 全是空行 = 清空（编辑框里全删掉就真的没了）",
           memory.count(), 0)

memory.clear()
long_line = "长" * (memory.MAX_NOTE_CHARS + 50)
memory.from_text(long_line)
kept_line = memory.load()[0]["text"]
# ★ 反转：`MAX_NOTE_CHARS` 是给**模型**写的条目设的闸，不是给**你**设的。
# 编辑框里打多长就存多长——静默砍掉你打进去的字比什么都糟。
H.check("★ 你在编辑框里打的字**一个字都不砍**（那是给模型设的闸，不是给你的）",
        kept_line == long_line, (len(kept_line), len(long_line)))
H.check("★ 也没给它加「…」尾巴", not kept_line.endswith("…"))

memory.clear()
memory.from_text("\n".join("第 %d 条" % i for i in range(memory.MAX_NOTES + 10)))
kept = memory.load()
H.check_eq("★ 手写也受条数上限约束", len(kept), memory.MAX_NOTES)
H.check_eq("★ 丢的是最旧的（留最新的）", kept[-1]["text"],
           "第 %d 条" % (memory.MAX_NOTES + 9))

memory.clear()

H.section("★★★ 记忆真的跟着 .hip 文件走（真存真读）")
import os
import tempfile

memory.clear()
memory.add("这条要活过一次存读往返", source="/obj/geo1/AI1")
H.check("存之前内存里是这条", memory.count() == 1)

hip_path = os.path.join(tempfile.mkdtemp(prefix="houdini_ai_hip_"),
                        "memory_test.hip")
hou.hipFile.save(file_name=hip_path)
H.check("场景存下来了", os.path.isfile(hip_path), hip_path)

# 存完再往内存里加一条——这样如果 load 之后只看到第一条，
# 就证明它**确实是从文件里读回来的**，不是内存里残留的
memory.add("这条是存完之后才加的，不该活下来", source="/obj/geo1/AI2")
H.check("现在内存里有两条", memory.count() == 2, memory.count())

hou.hipFile.load(hip_path, suppress_save_prompt=True,
                 ignore_load_warnings=True)

reloaded = memory.load()
H.check("★ 读回来还有记忆", len(reloaded) >= 1, reloaded)
H.check("★ 活下来的是存盘前那条",
        any("活过一次存读往返" in n["text"] for n in reloaded), reloaded)
H.check("★ 存盘之后加的那条**没有**活下来（证明是从文件读的，不是内存残留）",
        not any("存完之后才加" in n["text"] for n in reloaded), reloaded)
H.check("来源也跟着活下来了",
        any(n["from"] == "/obj/geo1/AI1" for n in reloaded), reloaded)

H.finish("test_memory")
