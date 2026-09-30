# houdini-ai

Houdini 的 AI 工具集。**不走 MCP**——不监听任何端口，只有向网关的出站 HTTPS；
会话和状态都在 Houdini 里，AI 在后台线程干活，不挡你的前台操作。

第一个功能是「AI 节点」：放一个 Subnet SOP，用中文写描述，AI 在里面把节点网络建出来。

## 环境

Houdini 20 以上、Python 3（Houdini 自带），没有第三方依赖，不用 pip 装任何东西。
开发时用的是 Houdini 22.0.368 + Python 3.13.10。Windows 上验证过，Mac / Linux 没测。

## 快速开始

```bash
git clone <这个仓库>
cd houdini-ai
"D:/ProgramFiles/Houdini22/bin/hython.exe" scripts/install.py --check   # 看部署状态
"D:/ProgramFiles/Houdini22/bin/hython.exe" scripts/install.py           # 装进去
```

把 hython 的路径换成你自己的 Houdini 安装位置。装的动作是往两个 pref 目录
（`$HOUDINI_USER_PREF_DIR` 和家目录那份）写工具架和面板的桩文件，桩文件只做
`sys.path.insert` 然后 import 这个仓库里的真代码，所以之后改代码不用重装。

**重启 Houdini** 之后：

1. SOP 网络里右键 → `AI` → `New AI Node`
2. 选中它，参数面板「描述」里写中文，点「开始构建」
3. 看节点颜色和下方的字，或者 Houdini 状态栏的通知
4. 提修改意见：工具架 `AI` → `AI Panel`，下半部的输入框

网关地址和密钥从 `~/.claude/settings.json` 自动捡（协议是 Anthropic Messages API，
官方 Anthropic 和任何兼容网关都能直接换）。要覆盖就复制 `config.json.example`
成 `config.json` 改——那个文件不进版本库，因为里面可能有明文密钥。

**完整说明见 [docs/AI节点.md](docs/AI节点.md)**（含已知限制和实测质量记录）。

## ★ 命令桥：把 Python 送进跑着的 Houdini

不监听任何端口，走本地文件队列（`~/.houdini-ai-bridge/`）：

```bash
bash scripts/hcmd.sh --list        # 有哪些 Houdini 开着
printf '%s\n' '...代码...' | bash scripts/hcmd.sh
bash scripts/hcmd.sh -f cmd.py     # 从文件投，不怕引号
```

由 `pythonrc.py` 在 Houdini 启动时自动拉起。客户端是纯 bash，不起 Python。

> 没用 Houdini 自带的 `hrpyc`，因为 `start_server()` **绑 `0.0.0.0`** 且没有
> host 参数——那等于把 Python 执行权开放给整个局域网。

## ★ 改完代码不用重启 Houdini

| 改了什么 | 怎么做 |
|---|---|
| `houdini_ai/*.py` 的逻辑 | 直接点任意 AI 按钮（每个按钮会自动轻量重载） |
| 面板界面 / `deploy/*.shelf` / `*.pypanel` | 点工具架的「重载 AI 工具」 |

手动方式（Houdini 的 Python Shell 里粘一行）：

```python
import sys; sys.path.insert(0, r"<工具根目录>"); import houdini_ai.reload as r; r.reload_all(verbose=True)
```

`houdini_ai.reload.paste_hint()` 会打印这行、路径自动填好。
有任务在跑时会拒绝重载（会搞丢工作线程的状态）。

## 命令

```bash
H="D:/ProgramFiles/Houdini22/bin/hython.exe"   # 换成你自己的 hython

$H tests/run_all.py            # 全部离线测试，不花钱
$H scripts/install.py --check  # 看部署状态
$H scripts/install.py          # 重新部署
$H scripts/install.py --uninstall

$H tests/live_smoke.py "做一个高塔"              # 真实联网，花钱
$H tests/live_smoke.py "做一个高塔" "再高一点"   # 第二轮提修改意见
```

## 结构

```
houdini_ai/      真正的代码（改这里不用重新部署）
deploy/          ★ 部署模板（改了要重跑 install，或点重载按钮）
scripts/         安装脚本
tests/           测试（用 hython 跑，不需要 pytest）
docs/AI节点.md   使用说明
```

装在 pref 目录里的是**两个**「桩文件」（工具架、面板），
它们只做 `sys.path.insert` 然后 import 真正的代码。

## 工具架只有三个按钮

**NewAiSub**（建节点）/ **AiSubPanel**（对话与修改意见）/ **AiSubReload**（改完代码不重启生效）。
名字一律带 `AiSub` 前缀，离开子菜单也知道是谁家的。

曾经有六个——`Build Selected` 和节点自己的按钮重复，`Dive Into` 是 Subnet 双击就有的
原生功能，`Convert` 用不到。加按钮前先问：Houdini 原生做不到吗？节点自己已经有吗？

## 三条硬规矩

1. **碰不到 Subnet 外面**——描述格式里没有「路径」这个概念，模型没法表达「去改外面那个节点」。
2. **编不出节点和参数**——所有类型和参数名都从正在运行的 Houdini 里查出来，编造会被当场拒绝。
3. **没有危险能力**——不能执行任意 Python、不能读写文件、不能调 shell、不能渲染导出。

每轮修改包在一个 `hou.undos.group()` 里，所以一次 Ctrl+Z 退掉整轮。

## 许可证与出处

本仓库的代码用 MIT，见 [LICENSE](LICENSE)。

有一处是别人家的东西，写在下面：

`houdini_ai/soma_skeleton.py` 和 `houdini_ai/data/soma_tpose_*.npy` 里的关节点名、
父子关系、T-pose 轴向偏移，是从 NVIDIA 的
[Kimodo](https://research.nvidia.com/labs/sil/projects/kimodo/) 动作生成项目里抽出来的，
抽的对象是它 `src/kimodo/skeleton/definitions.py` 的骨架定义类和它自带的一份 pickle。
Kimodo 声明的是 Apache-2.0（`LICENSE` 正文 + `pyproject.toml` 里
`license = {text = "Apache-2.0"}`）。骨架本身来自同属 NVIDIA 的
[SOMA-X](https://github.com/NVlabs/SOMA-X)。

抽的时候没抄任何代码，是把骨架表解析成纯数据（关节名、父节点下标、每根骨的
轴向偏移矩阵），抽的程序就在本仓库的 `scripts/gen_soma_tables.py` 和
`scripts/extract_soma_tpose.py` 里，你可以自己跑一遍复现。按 Apache-2.0 的要求
在这里说明出处和改动。Kimodo 自己的 `ATTRIBUTIONS.MD` 里还挂着 LLM2Vec（MIT）和
Unitree mujoco（BSD-3），那些是它的依赖，没进本仓库。

模型权重一个都没进本仓库；本仓库只跟本地跑着的推理后端说话。
