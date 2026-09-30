# 装到另一台电脑

三步，不用 pip，不用 conda，不依赖 Houdini 安装目录里的任何文件。
唯一要你自己准备的是：一个 Anthropic Messages API 兼容的网关地址和密钥。

装的动作是往 Houdini 的 pref 目录写 4 个文件，所以想撤销就是删掉那 4 个。

## 你需要什么

- Houdini。开发和在用的是 22.0.368（自带 Python 3.13.10）。更早的版本没测过，
  代码里用到的 `hou.pypanel.installFile`、`PythonPanel.reloadActiveInterface`
  这几个 API 要求哪个最低版本，我没有查证。
- 一个网关。官方 `https://api.anthropic.com` 可以，任何兼容 Anthropic
  Messages API 的第三方网关也可以，只要它能被 `urllib` 直接 POST 到。
- Windows 上验证过。macOS 和 Linux 没测，而且找 pref 目录的代码是按 Windows
  的形状写的（细节见下面「装不上怎么办」）。

不需要装的东西：PySide6 和 numpy 都是 Houdini 自带的，核心功能一个第三方包都不用。
只有动作生成那一块要 numpy 和 torch，而那份文档不覆盖它（见最后一节）。

## 安装

```bash
git clone https://github.com/lmy-2004/houdini-ai.git
cd houdini-ai

# 先看现状，不动任何文件
"D:/ProgramFiles/Houdini22/bin/hython.exe" scripts/install.py --check

# 真装
"D:/ProgramFiles/Houdini22/bin/hython.exe" scripts/install.py

# 或者卸载
"D:/ProgramFiles/Houdini22/bin/hython.exe" scripts/install.py --uninstall
```

把 `D:/ProgramFiles/Houdini22/bin/hython.exe` 换成你自己的 hython。
Linux / macOS 上是 `$HFS/bin/hython`。装完**重启 Houdini**，工具架才会出现。

装进去的是**桩文件**，一共 4 个：

| 文件 | 装到哪 |
|---|---|
| `houdini_ai.shelf` | `<pref>/toolbar/` |
| `ai_tools.pypanel` | `<pref>/python_panels/` |
| `motion_tools.pypanel` | `<pref>/python_panels/` |
| `pythonrc.py` | `<pref>/`（Houdini 每次启动都会执行它） |

桩文件只做两件事：把仓库根目录插进 `sys.path`，然后 import 真正的代码。
真正的代码一直留在你 clone 的那份仓库里，所以以后改代码不用重新部署。

**这就是为什么仓库不能随便挪位置。** 桩文件里烧的是你 clone 时的绝对路径。
挪了目录就重跑一次 `install.py`，它会用新路径覆盖。

装两份的理由：某些配置下 Houdini 会有两个同版本的 pref 目录，取决于它是怎么启动的
（从桌面图标启动读 `~/Documents/houdini22.0`，从 Git Bash 启动读 `~/houdini22.0`）。
`install.py` 会把找到的同版本目录都装上。

## 配网关

工具不硬编码任何人的密钥。读取顺序是后者覆盖前者：

1. 代码里的默认值：`https://api.deepseek.com/anthropic`，模型 `deepseek-flash`，密钥为空
2. `~/.claude/settings.json` 的 `env` 段，认这三个键：
   `ANTHROPIC_BASE_URL`、`ANTHROPIC_AUTH_TOKEN`（或 `ANTHROPIC_API_KEY`）、`ANTHROPIC_MODEL`
3. 仓库根目录的 `config.json`
4. 环境变量：`HOUDINI_AI_BASE_URL`、`HOUDINI_AI_API_KEY`、`HOUDINI_AI_MODEL`

第 2 条是给「这台机器上本来就在用 Claude Code」的情况准备的，你什么都不用写，
它会把你 Claude Code 那套网关原样捡起来用。

别的机器上就这样做：把 `config.json.example` 复制成 `config.json`，改三行。

```json
{
  "base_url": "https://api.anthropic.com",
  "api_key": "sk-ant-你的密钥",
  "model": "claude-sonnet-5-5"
}
```

`config.json` 已经在 `.gitignore` 里，不会被提交。

不配会怎样：默认那套指向 DeepSeek 的端点而且密钥是空的，模型一调用就是 401。
这是故意的，代码里不写死任何人的密钥。

## 验证装好了

第一步，确认网关读到了：

```bash
cd houdini-ai
"D:/ProgramFiles/Houdini22/bin/hython.exe" -c "from houdini_ai import config; print(config.describe(config.load()))"
```

必须在这个仓库目录下跑（`hython -c` 会把当前目录加进 `sys.path`）。
正常的话打印一行：

```
网关=https://api.anthropic.com | 模型=claude-sonnet-5-5 | 密钥=sk-ant***abcd
```

密钥是脱敏的，只显示前 6 位和后 4 位。看到 `密钥=(未设置)` 就是第 2、3、4 条一条都没命中，
回去看上一节。

第二步，在 Houdini 里试：

1. 进 SOP 网络，空白处右键 → `AI` → `New AI Node`
2. 选中新节点，参数面板的「描述」里写一句中文，比如「一排渐变大小的球」
3. 点「开始构建」
4. 看节点的颜色和下方那行字，或者 Houdini 状态栏的通知

跑得动就说明网关通了。跑不动、节点变红、注释里写了报错，报错文本就在节点上。

## 装不上怎么办

**报「找不到 Houdini pref 目录。用 --pref 手动指定。」**

两种情况：Houdini 从来没在这台机器上启动过，pref 目录还不存在；或者 pref 目录不在
`~` 和 `~/Documents` 底下。`install.py` 只扫这两个地方。

对策是先用桌面图标启动一次 Houdini 再装，或者直接告诉它装哪：

```bash
hython scripts/install.py --pref "C:/Users/你的用户名/Documents/houdini22.0"
```

**你改过 `HOUDINI_USER_PREF_DIR`**

两个坑叠在一起，都得知道。

第一个是 Houdini 自己的：这个环境变量的值里**必须含 `__HVER__` 占位符**，
写成 `C:/somewhere/houdini__HVER__`，Houdini 会把它换成版本号。不含占位符的话
Houdini 会打印一行 `HOUDINI_USER_PREF_DIR missing __HVER__, ignored.` 然后**整条忽略**，
悄悄退回默认位置。

第二个是本工具现在的短板：`houdini_ai/prefdirs.py` **不读这个环境变量**，
只扫 `~` 和 `~/Documents`。所以你把 pref 挪到别处之后，`install.py` 会装到旧位置，
而 Houdini 从新位置读，两边对不上，工具架就是不出现。这种情况要用 `--pref` 明确指定。

**工具架没出现**

第一次装完必须重启 Houdini。之后改代码就不用重启了，点工具架上的 `AiSubReload` 即可。

**面板打开是空白或者报 `Start tag expected`**

`.pypanel` 文件必须是 XML。如果你手改过它，检查是不是把 Python 写进去了。

**macOS / Linux**

没测过，很可能要手动指定 `--pref`。原因是 `prefdirs.py` 要求目录名以 `houdini` 开头，
而 macOS 的默认位置是 `~/Library/Preferences/houdini/22.0`，最末一级是 `22.0`
不满足这个条件，扫不到。

## 工具架上的三个按钮

- `NewAiSub`：建一个新的 AI 节点
- `AiSubPanel`：对话与修改意见
- `AiSubReload`：改完代码不重启 Houdini 生效

三个按钮每次点击前都会自动做一次轻量热重载，所以改了 `houdini_ai/*.py` 直接点按钮
就生效。只有改工具架和面板本身才需要点 `AiSubReload`。

## 还有一条路：命令桥

装 `pythonrc.py` 的时候顺带装起来的。它让 Claude Code 往里跑着的 Houdini 送一小段
Python 执行，不用你点按钮、不用重启。

```bash
bash scripts/hcmd.sh --list        # 有哪些 Houdini 开着
bash scripts/hcmd.sh -f cmd.py     # 从文件投一段代码进去
```

它**不监听任何端口**。走的是 `~/.houdini-ai-bridge/` 下面的文件队列，Houdini 自己的
后台线程捡起来在主线程上执行。没用 Houdini 自带的 `hrpyc`，因为那个的
`start_server()` 绑 `0.0.0.0` 而且没有 host 参数，等于把 Python 执行权开放给整个局域网。

客户端 `scripts/hcmd.sh` 是纯 bash 的，不需要 Python。

## 这份文档不管什么

**动作生成（motion）那一整块不在覆盖范围内。** 它要另外一整套后端：代码里写死了
`D:/kimodo-motion/` 下的三个路径（`venv/Scripts/python.exe`、`kimodo_runner.py`、
`src/kimodo/.../skin_standard.npz`），还要 Python 3.11 的虚拟环境和几百 MB 的模型权重。

没有那套东西，`motion_tools.pypanel` 装上去了也是空的。AI 节点那一整套和它完全独立，
不装后端照样能用。

## 为什么是这么设计的

**为什么不走 MCP。** 三个理由：Houdini 的会话应该待在 Houdini 里，不该侵占编辑器；
AI 要在后台干活，不能挡住前台操作；大方向人定，小方向让 AI 填。所以这里没有 MCP server，
没有监听端口，只有向网关的出站 HTTPS。

**为什么装进 pref 目录而不是打成插件包。** 桩文件模式让「改代码」和「部署」分开：
真代码在仓库里，改了立刻生效，不用重新部署。代价是仓库不能挪位置。

**为什么 `.shelf` 和 `.pypanel` 里只能写英文。** 实测这两个格式的 XML 属性值只接受
ASCII，`label="新建 AI 节点"` 会变成 `label=" AI "`，数字字符引用也一样被吃掉
（它们不是真正的 XML 解析器）。所以工具架和面板的名字一律用英文。中文界面在节点参数
和面板内部，那些是 Qt 渲染的，没有限制。
