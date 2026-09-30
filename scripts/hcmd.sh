#!/usr/bin/env bash
# 把一小段 Python 送进**正在运行的** Houdini 里执行，把结果拿回来。
#
#   bash scripts/hcmd.sh -f cmd.py        # 从文件读（推荐，不怕引号）
#   bash scripts/hcmd.sh 'print(hou.node("/obj"))'
#   echo 'print(1)' | bash scripts/hcmd.sh
#   bash scripts/hcmd.sh --list           # 看有哪些 Houdini 开着
#   bash scripts/hcmd.sh -p 12345 -f c.py # 指定实例（pid）
#
# 刻意**不监听任何端口**：只是往一个本地文件夹写 .py，Houdini 那边
# 有个后台线程捡起来在主线程上执行，结果写成 .out。所以这个脚本
# 连 Python 都不用起——纯 bash，token 最省。
#
# 对应实现见 houdini_ai/bridge.py。

set -u

BRIDGE="${HOUDINI_AI_BRIDGE_DIR:-$HOME/.houdini-ai-bridge}"
QUEUE="$BRIDGE/queue"
INSTANCES="$BRIDGE/instances"
TIMEOUT=30

CODE_FILE=""
CODE_INLINE=""
PID=""
LIST_ONLY=0

while [ $# -gt 0 ]; do
  case "$1" in
    -f) CODE_FILE="$2"; shift 2 ;;
    -p) PID="$2"; shift 2 ;;
    -t) TIMEOUT="$2"; shift 2 ;;
    --list) LIST_ONLY=1; shift ;;
    -h|--help) sed -n '2,16p' "$0" | sed 's/^# \{0,1\}//'; exit 0 ;;
    *) CODE_INLINE="$1"; shift ;;
  esac
done

if [ ! -d "$BRIDGE" ]; then
  echo "没有 $BRIDGE —— Houdini 那边还没启动过命令桥。" >&2
  echo "（桥由 pythonrc.py 在 Houdini 启动时拉起；也可以在 Python Shell 里跑：" >&2
  echo "  import houdini_ai.bridge as b; b.start(verbose=True)）" >&2
  exit 2
fi

# ---- 挑实例 ----
pick_instance() {
  # 只挑心跳新鲜的；多个的话取最近启动的那个
  local now newest="" newest_seen=0
  now=$(date +%s)
  for f in "$INSTANCES"/*.json; do
    [ -e "$f" ] || continue
    local seen pid started
    seen=$(sed -n 's/.*"seen"[: ]*\([0-9.]*\).*/\1/p' "$f" | head -1)
    pid=$(sed -n 's/.*"pid"[: ]*\([0-9]*\).*/\1/p' "$f" | head -1)
    started=$(sed -n 's/.*"started"[: ]*\([0-9.]*\).*/\1/p' "$f" | head -1)
    [ -n "$pid" ] || continue
    # 心跳超过 20 秒没更新就当它没了
    if [ -n "$seen" ]; then
      local age
      age=$(awk -v a="$now" -v b="$seen" 'BEGIN{printf "%d", a-b}')
      [ "$age" -gt 20 ] && continue
    fi
    if [ -z "$newest" ] || awk -v a="${started:-0}" -v b="$newest_seen" \
        'BEGIN{exit !(a>b)}'; then
      newest="$pid"; newest_seen="${started:-0}"
    fi
  done
  printf '%s' "$newest"
}

show_instances() {
  local any=0 now age bad=0
  now=$(date +%s)
  for f in "$INSTANCES"/*.json; do
    [ -e "$f" ] || continue
    local pid seen scene pwd_
    pid=$(sed -n 's/.*"pid"[: ]*\([0-9]*\).*/\1/p' "$f" | head -1)
    # ★ 连 pid 都读不出来 = 文件是坏的（实测遇到过：Houdini 被强杀，
    #   NTFS 留下一个全是 NUL 的心跳文件）。以前这里没守卫，会打印出
    #   「pid=  心跳 ?  ?」这种莫名其妙的一行。跳过并单独报个数，
    #   别让它冒充成一个活着的实例。
    [ -n "$pid" ] || { bad=$((bad + 1)); continue; }
    any=1
    seen=$(sed -n 's/.*"seen"[: ]*\([0-9.]*\).*/\1/p' "$f" | head -1)
    scene=$(sed -n 's/.*"scene"[: ]*"\([^"]*\)".*/\1/p' "$f" | head -1)
    pwd_=$(sed -n 's/.*"pwd"[: ]*"\([^"]*\)".*/\1/p' "$f" | head -1)
    if [ -n "$seen" ]; then
      age=$(awk -v a="$now" -v b="$seen" 'BEGIN{printf "%d", a-b}')
    else
      age="?"
    fi
    # ★ 心跳超过 20 秒 = 那个进程已经没了。以前这里不分死活，把死的一起
    #   列在"活着的 Houdini 实例"标题底下——那是**在说谎**，会让人以为
    #   桥能用（实测就被误导过一次）。现在明确标出来。
    local mark=""
    if [ -z "$seen" ] || [ "$age" = "?" ] || [ "$age" -gt 20 ]; then
      mark="  ← 已失效（这个进程没了）"
    fi
    printf '  pid=%-8s 心跳 %-4ss  %s   %s%s\n' \
      "$pid" "$age" "${scene:-?}" "${pwd_:-}" "$mark"
  done
  [ "$any" = 1 ] || echo "  （一个活着的实例都没有）"
  [ "$bad" = 0 ] || echo "  （另有 $bad 个读不出来的心跳文件——多半是被强杀的旧进程留下的，删掉即可）"
}

if [ "$LIST_ONLY" = 1 ]; then
  echo "活着的 Houdini 实例："
  show_instances
  exit 0
fi

[ -n "$PID" ] || PID=$(pick_instance)
if [ -z "$PID" ]; then
  echo "找不到活着的 Houdini 实例。现有：" >&2
  show_instances >&2
  exit 2
fi

# ---- 取命令内容 ----
# ★ 参数校验放在**重定向之前**：下面那个 `fi > ... 2>/dev/null` 会把整个 if 块的
# stderr 一起吞掉，写在里面的 `echo ... >&2` 根本发不出来——退出码 2 却一个字不说。
if [ -n "$CODE_FILE" ] && [ ! -f "$CODE_FILE" ]; then
  echo "读不到 $CODE_FILE" >&2
  exit 2
fi

# ★ 什么代码都没给、stdin 又是个终端 —— 直接报错退出。
# 这个判断必须留在 `cat` 之前：否则 `cat` 会**挂在那儿等键盘输入**，
# 命令看起来像卡死了。（这一条曾经被误删过，是个回归。）
if [ -z "$CODE_FILE" ] && [ -z "$CODE_INLINE" ] && [ -t 0 ]; then
  echo "没给代码。用 -f 文件 或者直接传一段。" >&2
  exit 2
fi

if [ -n "$CODE_FILE" ]; then
  # ★ 这一行曾经漏掉过：只检查了文件存在，**没有把内容吐出来**（另外两个分支
  # 都有 printf / cat，就它漏了）。结果 `-f 文件` 一直在**静默发送空脚本**——
  # Houdini 那边 exec("") 什么都不做，返回 ok:true、stdout 空、什么也没发生。
  # 看起来一切正常，最难查的那种失败。
  cat "$CODE_FILE"
elif [ -n "$CODE_INLINE" ]; then
  printf '%s\n' "$CODE_INLINE"
else
  cat
fi > /tmp/.hcmd_body.$$
# 注：这里**不要**加 `2>/dev/null`。加了的话 `cat` 读文件失败（权限、被占用）
# 会变成静默的空 body，然后被下面那条空 body 拦截报成「命令是空的」——
# 把「读不出来」误报成「你没给内容」。上面那些参数校验已经挪到重定向之前了，
# 所以这里放开 stderr 不会漏出多余噪音。

BODY="/tmp/.hcmd_body.$$"

# ★ 空 body 一律拦住。exec("") 不会报错，只会安静地什么都不做——上面那个 bug
# 的症状就是它。宁可在这里明确报错，也不要发一条看起来成功的空命令。
#
# ⚠️ 判据必须是「**有没有非空白字符**」，不能是 `[ ! -s "$BODY" ]`（文件大小）——
# 纯空白（"   \n\t\n"）大小非零，用 -s 判断会放过去，照样排一条空脚本。
# （这是补测试的时候当场抓出来的。）
if ! grep -q '[^[:space:]]' "$BODY" 2>/dev/null; then
  rm -f "$BODY"
  echo "命令是空的，没有发送。用 -f 文件 或者直接传一段代码。" >&2
  exit 2
fi

REQ="${PID}__$(date +%s%N)"
IN="$QUEUE/$REQ.py"
OUT="$QUEUE/$REQ.out"

mkdir -p "$QUEUE"
cat "$BODY" > "$IN"
rm -f "$BODY"

# ---- 等结果 ----
waited=0
while [ ! -f "$OUT" ]; do
  if [ "$waited" -ge "$((TIMEOUT * 10))" ]; then
    echo "等了 ${TIMEOUT}s 没等到结果。" >&2
    echo "（Houdini 可能正忙，或者在弹模态窗口。命令还留在 $IN）" >&2
    exit 3
  fi
  sleep 0.1
  waited=$((waited + 1))
done

cat "$OUT"
# 有 error 就用非零退出码，好在脚本里判断
if grep -q '"error": "[^"]' "$OUT" 2>/dev/null; then
  exit 1
fi
exit 0
