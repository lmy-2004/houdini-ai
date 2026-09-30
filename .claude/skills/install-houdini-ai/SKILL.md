---
name: install-houdini-ai
description: Install the houdini-ai toolset into this machine's Houdini pref directory, or diagnose why the shelf/panel does not show up. Use when the user asks to install or set up houdini-ai on a machine, to make the AI node work here, or reports that the "AI" shelf or the AI panel is missing after Houdini starts.
---

# 安装 houdini-ai

完整说明在仓库根的 `INSTALL.md`。这里只留动手需要的部分。

## 先探明三件事，再动手

1. **hython 在哪。** 不要假设路径。找 `houdini` 安装目录下的 `bin/hython.exe`
   （Windows）或 `bin/hython`（Linux / macOS）。常见的本机位置是
   `D:/ProgramFiles/Houdini22/bin/hython.exe`。找不到就问用户，不要瞎猜一个。
2. **仓库在哪。** 桩文件里会烧进这个绝对路径，装完仓库不能挪。
3. **网关配了没。** 见下面第三步。

## 装

```bash
cd <仓库根目录>
"<hython>" scripts/install.py --check    # 先看现状
"<hython>" scripts/install.py            # 真装
```

装的就是 4 个桩文件：`toolbar/houdini_ai.shelf`、`python_panels/` 下两个 `.pypanel`、
`pref/pythonrc.py`。

装完**必须让 Houdini 重启一次**，工具架才会出现。这一步不能省。

## 装不上时按顺序查

**「找不到 Houdini pref 目录」**
`houdini_ai/prefdirs.py` 只扫 `~` 和 `~/Documents` 下以 `houdini` 开头的目录。
这台机器上 Houdini 从没启动过，或者 pref 在别处，就会报这个。
对策：先让用户用桌面图标启动一次 Houdini，然后用 `--pref` 明确指定：

```bash
"<hython>" scripts/install.py --pref "<pref 目录绝对路径>"
```

**改过 `HOUDINI_USER_PREF_DIR`**
两个独立的坑。Houdini 要求这个环境变量的值里含 `__HVER__` 占位符，不含就整条忽略、
静默退回默认位置（启动时会打印 `missing __HVER__, ignored.`）。另外本工具的
`prefdirs.py` 根本不读这个变量，所以挪过 pref 的机器必须用 `--pref`。

**工具架出现了但点了没反应**
桩文件里的路径指向了旧位置。重跑一次 `install.py`。

**面板报 `Start tag expected`**
`.pypanel` 必须是 XML，有人往里面写了 Python。

## 验证

```bash
cd <仓库根目录>
"<hython>" -c "from houdini_ai import config; print(config.describe(config.load()))"
```

必须在仓库根目录下跑，`hython -c` 才会把当前目录加进 `sys.path`。
输出里 `密钥=(未设置)` 说明配置链路一条都没命中，去 `INSTALL.md` 的「配网关」一节。

网关读取顺序（后者覆盖前者）：代码默认值 → `~/.claude/settings.json` 的 `env` 段
（`ANTHROPIC_BASE_URL` / `ANTHROPIC_AUTH_TOKEN` / `ANTHROPIC_MODEL`）→ 仓库根的
`config.json` → 环境变量 `HOUDINI_AI_*`。

## 边界

**不要装动作生成的后端。** 那一块写死了 `D:/kimodo-motion/` 下的路径，要 Python 3.11
的虚拟环境和模型权重，不在安装范围内。`motion_tools.pypanel` 会照装，但没后端就是空的，
AI 节点不受影响。

**仓库不能挪位置。** 挪了要重跑 `install.py`。

**不要改 `.shelf` / `.pypanel` 里的名字成中文。** 实测这两个格式的 XML 属性值只吃 ASCII，
中文会变成 `label=" AI "` 甚至双重编码的乱码。中文只出现在节点参数和面板内部。
