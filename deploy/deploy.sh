#!/usr/bin/env bash
# pattern-tool 一键部署脚本（deploy/部署文档.md 的自动化封装）
#
# 用法（在 deploy/ 目录下执行）：
#   ./deploy.sh -d www.example.com                 域名部署：宿主机已有证书 → 直接 HTTPS
#   ./deploy.sh -d www.example.com -m you@x.com    无证书 → 自动 certbot 签发后切 HTTPS
#   ./deploy.sh                                    无域名：HTTP 联调（服务器 IP 直连）
#   ./deploy.sh upgrade                            代码更新后滚动重建 pt-web（数据卷不动）
#   ./deploy.sh renew-cert                         证书续期（约 60–80 天一次，停网关约 10 秒）
#   ./deploy.sh status                             服务状态 + 冒烟检查
#
# 红线（部署文档 7 节，脚本不越线）：worker=1 不动；绝不下 down -v；
# 不跑 start.sh；密钥只走 .env（脚本不读不打印其内容）。

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
SELF="$SCRIPT_DIR/$(basename "${BASH_SOURCE[0]}")"   # cd 前锚定自身绝对路径（usage 回读用）
REPO_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"
CONF="$SCRIPT_DIR/nginx/conf.d/pattern-tool.conf"
HTTP_EXAMPLE="$SCRIPT_DIR/nginx/pattern-tool.http.conf.example"
PROD_STASH="$SCRIPT_DIR/nginx/conf.d/.pattern-tool.prod.conf.bak"

say()  { printf '\033[1;32m[deploy]\033[0m %s\n' "$*"; }
warn() { printf '\033[1;33m[warn]\033[0m %s\n' "$*"; }
die()  { printf '\033[1;31m[error]\033[0m %s\n' "$*" >&2; exit 1; }

# ---- docker 权限自适应（2026-08-28 服务器实测：ubuntu 不在 docker 组，
# 裸 docker 报 permission denied → 脚本退化为用户手动 sudo compose 起栈，
# 跳过 HTTP 引导直接撞证书崩溃。统一 SUDO 前缀修复）----
SUDO=""
if ! docker info >/dev/null 2>&1; then
  SUDO="sudo"
  $SUDO docker info >/dev/null 2>&1 || die "docker 不可用（sudo 也不行）——检查 docker 服务与用户组"
fi
dk() { $SUDO docker "$@"; }

cd "$SCRIPT_DIR"   # 所有 docker compose 命令在此目录执行（compose 自动读 ./docker-compose.yml）

# ---- 前置检查 ----

preflight() {
  command -v docker >/dev/null 2>&1 || die "docker 未安装（文档 3 节前置条件）"
  docker compose version >/dev/null 2>&1 || die "docker compose v2 不可用"
  if [[ ! -f "$REPO_ROOT/.env" ]]; then
    warn "仓库根缺 .env —— 已从模板复制；外部 API key 为空，对应步骤将零误伤跳过（服务可起）"
    cp "$REPO_ROOT/.env.example" "$REPO_ROOT/.env"
  fi
}

# ---- conf 渲染（不动用户手改内容：生产版被 HTTP 版临时替换前先暂存） ----

conf_domain() {  # 从当前 conf 提取第一个 server_name（无 conf 返回空）
  [[ -f "$CONF" ]] && sed -n 's/.*server_name \([^ ;]*\).*/\1/p' "$CONF" | head -1 || true
}

write_http_conf() {  # $1 = 域名或 IP（签证书前的联调形态）
  if [[ -f "$CONF" ]] && grep -q "listen 443 ssl" "$CONF"; then
    cp "$CONF" "$PROD_STASH"
    say "生产版 conf 已暂存（$PROD_STASH）——签发证书后自动恢复"
  fi
  sed "s/pattern\.example\.com/$1/g" "$HTTP_EXAMPLE" > "$CONF"
}

restore_prod_conf() {
  if [[ -f "$PROD_STASH" ]]; then
    mv "$PROD_STASH" "$CONF"
  else
    die "生产版 conf 暂存缺失——请按文档 4.4 手动恢复后重跑 ./deploy.sh"
  fi
}

retarget_conf_domain() {  # $1 = 新域名：把 conf 里 4 处域名（含证书路径）整体替换
  local current placeholder escaped
  current="$(conf_domain)"
  placeholder="pattern.example.com"
  if [[ "$current" == "$1" ]]; then return 0; fi
  escaped="${current:-$placeholder}"
  escaped="${escaped//./\\.}"
  sed -i "s/$escaped/$1/g" "$CONF"
  say "conf 域名已替换：${current:-$placeholder} → $1"
}

certs_exist() { [[ -n "${DOMAIN:-}" && -n "$(cert_dir_for "$DOMAIN")" ]]; }

cert_dir_for() {  # $1=域名 → 探测 SAN 覆盖该域名的 live 目录名（无则空）
  # 陷阱记档（v19.2 FAQ）：live 目录名 = 签发时第一个 -d 域名，未必等于
  # 目标域名（如 -d 裸域在前、证书含 www）。按证书实际 SAN 探测而非路径猜
  local dir dom
  dom="${1//./\\.}"
  for dir in $($SUDO ls /etc/letsencrypt/live 2>/dev/null | grep -v '^README$'); do
    $SUDO openssl x509 -in "/etc/letsencrypt/live/$dir/fullchain.pem" -noout -ext subjectAltName 2>/dev/null \
      | grep -qE "DNS:$dom(,|$)" && { echo "$dir"; return 0; }
  done
  return 0
}

align_conf_cert_path() {  # $1=域名：把 conf 证书路径对准真实 live 目录
  local live_dir
  live_dir="$(cert_dir_for "$1")"
  [[ -n "$live_dir" ]] || return 0
  sed -i "s|/etc/letsencrypt/live/[^/]*/|/etc/letsencrypt/live/$live_dir/|g" "$CONF"
  say "conf 证书路径已对准实际目录：live/$live_dir/"
}

# ---- 健康与冒烟 ----

wait_healthy() {  # 等 pt-web healthcheck 转 healthy（build 后模型预热最长 ~60s）
  local cid deadline
  cid="$(dk compose ps -q pt-web)"
  [[ -n "$cid" ]] || die "pt-web 容器未创建"
  deadline=$((SECONDS + 120))
  while :; do
    if [[ "$(dk inspect -f '{{.State.Health.Status}}' "$cid" 2>/dev/null || true)" == "healthy" ]]; then
      say "pt-web healthy"; return 0
    fi
    (( SECONDS < deadline )) || die "pt-web 120s 内未 healthy —— dk compose logs pt-web 排障"
    sleep 3
  done
}

smoke() {  # $1 = http|https（经 nginx 网关本机回环验证，不依赖外网 DNS）
  if [[ "$1" == "https" ]]; then
    curl -skf --resolve "${DOMAIN}:443:127.0.0.1" "https://${DOMAIN}/api/meta" >/dev/null
  else
    curl -sf -H "Host: ${TARGET:-127.0.0.1}" "http://127.0.0.1/api/meta" >/dev/null
  fi
}

# ---- 主流程 ----

deploy() {
  preflight

  # 域名推断：-d 显式 > 已有 conf 里的 server_name（用户手改过则沿用）> 无域名 HTTP
  if [[ -z "${DOMAIN:-}" ]]; then
    DOMAIN="$(conf_domain)"
    if [[ -n "$DOMAIN" && "$DOMAIN" != "pattern.example.com" && "$DOMAIN" != "_" ]]; then
      say "沿用 conf 中已有域名：$DOMAIN"
    else
      DOMAIN=""
    fi
  fi
  TARGET="${DOMAIN:-$(hostname -I 2>/dev/null | awk '{print $1}')}"
  TARGET="${TARGET:-127.0.0.1}"

  if certs_exist; then
    say "模式：HTTPS（探测到覆盖 $DOMAIN 的证书：live/$(cert_dir_for "$DOMAIN")）"
    [[ -f "$CONF" ]] || die "缺 $CONF —— git checkout 恢复后重跑"
    retarget_conf_domain "$DOMAIN"
    align_conf_cert_path "$DOMAIN"
    dk compose up -d --build
    wait_healthy
    smoke https && say "冒烟通过：https://$DOMAIN/api/meta"
  elif [[ -n "$DOMAIN" && -n "${EMAIL:-}" ]]; then
    say "模式：HTTP 起栈 → certbot 签发 → 切 HTTPS（域名 $DOMAIN）"
    write_http_conf "$DOMAIN"
    dk compose up -d --build
    wait_healthy
    smoke http || true
    say "停网关释放 80 端口，certbot standalone 签发…"
    dk compose stop nginx
    dk run --rm -p 80:80 certbot/certbot certonly --standalone \
      -d "$DOMAIN" --agree-tos -m "$EMAIL" --non-interactive
    restore_prod_conf
    retarget_conf_domain "$DOMAIN"
    align_conf_cert_path "$DOMAIN"   # 新签目录名=本命令的 -d（$DOMAIN 在前）——保险对准
    dk compose up -d nginx
    smoke https && say "冒烟通过：https://$DOMAIN/api/meta"
  else
    [[ -n "$DOMAIN" ]] && warn "未提供 --email 且无证书 → HTTP 联调形态（$DOMAIN）"
    say "模式：HTTP 联调（$TARGET）"
    write_http_conf "$TARGET"
    dk compose up -d --build
    wait_healthy
    smoke http && say "冒烟通过：http://$TARGET/api/meta"
    if [[ -n "$DOMAIN" ]]; then
      say "下一步签证书：./deploy.sh -d $DOMAIN -m 你的邮箱（会自动走完整 HTTPS 流程）"
    fi
  fi

  echo
  say "服务状态："; dk compose ps
  echo
  say "日常：./deploy.sh status | upgrade | renew-cert；完整手册见 deploy/部署文档.md"
}

upgrade() {
  preflight
  say "滚动重建 pt-web（nginx 不动，数据卷保留）…"
  dk compose build pt-web
  dk compose up -d pt-web
  wait_healthy
  if certs_exist; then smoke https && say "升级完成，冒烟通过（https）"
  else smoke http && say "升级完成，冒烟通过（http）"; fi
}

renew_cert() {
  preflight
  say "停网关 → certbot 续期 → 起网关（约 10 秒窗口）…"
  dk compose stop nginx
  dk run --rm -p 80:80 certbot/certbot renew
  dk compose start nginx
  if certs_exist; then smoke https && say "续期完成，冒烟通过"; fi
}

status() {
  dk compose ps
  if certs_exist; then
    smoke https && say "冒烟：https OK" || warn "冒烟失败（https）"
  else
    smoke http && say "冒烟：http OK" || warn "冒烟失败（http）"
  fi
}

usage() {
  sed -n '2,12p' "$SELF" | sed 's/^# \{0,1\}//'
}

# ---- 参数解析与分发 ----

ACTION="deploy"
while [[ $# -gt 0 ]]; do
  case "$1" in
    -d)            DOMAIN="${2:-}"; shift 2 ;;
    -m|--email)    EMAIL="${2:-}"; shift 2 ;;
    -h|--help)     usage; exit 0 ;;
    deploy|upgrade|renew-cert|status) ACTION="$1"; shift ;;
    *) die "未知参数：$1（--help 看用法）" ;;
  esac
done

case "$ACTION" in
  deploy)    deploy ;;
  upgrade)   upgrade ;;
  renew-cert) renew_cert ;;
  status)    status ;;
esac
