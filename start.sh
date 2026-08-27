#!/usr/bin/env bash
# pattern-tool 启动脚本：初始化虚拟环境与依赖 → 校验 .env → 以 uvicorn（worker=1）拉起服务。
# 用法：
#   ./start.sh          启动服务（.venv 缺失时自动完成初始化）
#   ./start.sh init     仅初始化/刷新依赖（requirements.txt 变更后执行）
#   ./start.sh check    只做环境自检，不安装不启动
# 端口取值优先级与 config.py 一致：真实环境变量 PT_PORT > .env 的 PT_PORT > 默认 8200
# 去水印二级档为佐糖云 API（无本地依赖）；历史 IOPaint/LaMa 档已退役（4.4 两档定稿）

set -euo pipefail

# 锚定项目根（脚本可在任意目录调用；src 包导入与 data/ 都相对根定位）
PROJECT_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$PROJECT_ROOT"

PYTHON_BIN="${PYTHON_BIN:-python3}"

log() { printf '[start.sh] %s\n' "$*"; }

# Python 版本闸：技术方案 7.1 只支持 3.11 / 3.12（3.13+ 的 opencv/rapidocr 轮子未验证）
assert_python_version() {
  local version
  version="$("$1" -c 'import sys; print(f"{sys.version_info.major}.{sys.version_info.minor}")')"
  case "$version" in
    3.11|3.12) ;;
    *) log "错误：需要 Python 3.11 或 3.12，当前 $version（$1）"; exit 1 ;;
  esac
}

init_deps() {
  if [ ! -x ".venv/bin/python" ]; then
    assert_python_version "$PYTHON_BIN"
    log "创建虚拟环境 .venv/"
    "$PYTHON_BIN" -m venv .venv
  fi
  log "安装依赖（requirements.txt 钉定版本）"
  .venv/bin/pip install --quiet --disable-pip-version-check -r requirements.txt
  # 负面清单修复（requirements.txt 顶部注释）：rapidocr-onnxruntime 会拉入非 headless
  # 的 opencv-python（5.x），必须卸载后 force-reinstall headless 4.x，否则 Linux 无 GUI 环境报 libGL 错
  if .venv/bin/pip show opencv-python >/dev/null 2>&1; then
    log "检测到 opencv-python（非 headless），执行修复：卸载并重装 headless 版"
    .venv/bin/pip uninstall --yes opencv-python >/dev/null
    .venv/bin/pip install --quiet --disable-pip-version-check --force-reinstall opencv-python-headless==4.14.0.94
  fi
}

prepare_env_file() {
  if [ ! -f .env ]; then
    log ".env 缺失，从 .env.example 复制模板（密钥等按需修改）"
    cp .env.example .env
  fi
}

resolve_port() {
  if [ -n "${PT_PORT:-}" ]; then printf '%s' "$PT_PORT"; return; fi
  if [ -f .env ]; then
    local value
    value="$(sed -n 's/^[[:space:]]*PT_PORT[[:space:]]*=[[:space:]]*\([0-9][0-9]*\).*/\1/p' .env | tail -n 1)"
    if [ -n "$value" ]; then printf '%s' "$value"; return; fi
  fi
  printf '8200'
}

# 启动前清掉占用端口的旧服务进程（自身除外）——避免"端口已被占用"启动失败
kill_port_occupant() {
  local port pids self
  port="$1"
  self="$$"
  pids="$(lsof -tiTCP:"${port}" -sTCP:LISTEN 2>/dev/null || true)"
  if [ -z "$pids" ]; then return; fi
  # 排除脚本自身（理论不会占端口，防御性排除）
  pids="$(printf '%s\n' "$pids" | grep -v "^${self}$" || true)"
  [ -z "$pids" ] && return
  log "端口 ${port} 被占用（PID: $(printf '%s ' $pids)），先终止旧进程"
  kill $pids 2>/dev/null || true
  # 优雅退出窗口，未退则强杀（uvicorn 处理中的图会按重启恢复口径置 failed）
  sleep 1
  local alive
  alive="$(printf '%s\n' "$pids" | while read -r pid; do kill -0 "$pid" 2>/dev/null && echo "$pid"; done || true)"
  if [ -n "$alive" ]; then
    kill -9 $alive 2>/dev/null || true
  fi
  sleep 0.5
}

# 启动前清理 data/ 运行时数据（批次文件、SQLite 库、API 结果缓存）——重启即全新状态。
# data/logs/ 豁免：崩溃后重启正是最需要日志的时刻，日志靠轮转封顶防增长。
# 显式枚举而非整目录删除（2026-08-27 第八次修订）：误删面最小，与 ttl.py
# _CACHE_DIR_NAMES 登记口径一致；生产环境若要保留数据（TTL 自动清理），
# 注释掉 run 分支里的 clear_runtime_data 调用即可。
clear_runtime_data() {
  local data_dir
  data_dir="${PROJECT_ROOT}/data"
  if [ -d "$data_dir" ]; then
    log "清理运行时数据：${data_dir}（批次/数据库/缓存，保留 logs）"
    rm -rf "$data_dir/jobs" "$data_dir/cache" "$data_dir"/pattern_tool.db*
  fi
}

case "${1:-run}" in
  init)
    init_deps
    prepare_env_file
    log "初始化完成，执行 ./start.sh 启动服务"
    ;;
  check)
    [ -x ".venv/bin/python" ] || { log "未就绪：.venv 不存在，先执行 ./start.sh init"; exit 1; }
    .venv/bin/python -c "import src.app.main" >/dev/null 2>&1 \
      || { log "未就绪：应用导入失败，先执行 ./start.sh init"; exit 1; }
    [ -f .env ] || log "提示：.env 不存在，启动时会从 .env.example 复制模板"
    log "自检通过：Python $(.venv/bin/python -c 'import sys; print(sys.version.split()[0])')，端口 $(resolve_port)"
    ;;
  run)
    [ -x ".venv/bin/python" ] || init_deps
    prepare_env_file
    PORT="$(resolve_port)"
    kill_port_occupant "$PORT"
    clear_runtime_data
    # 注意：${PORT} 必须带花括号——后面紧跟全角字符，macOS bash 3.2 会把多字节字符并入变量名
    log "启动 uvicorn 0.0.0.0:${PORT}（worker=1，SQLite 单写者前提，技术方案 6 节）"
    exec .venv/bin/python -m uvicorn src.app.main:app --host 0.0.0.0 --port "$PORT"
    ;;
  *)
    printf '用法：./start.sh [run|init|check]\n' >&2
    exit 1
    ;;
esac
