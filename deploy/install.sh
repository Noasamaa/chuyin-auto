#!/usr/bin/env bash
# 在服务器上以 root 执行:
#   bash deploy/install.sh
# 可选:
#   APP_DIR=/opt/chuyin-auto RUN_USER=chuyin-auto bash deploy/install.sh
set -euo pipefail

# 安装标记：目标目录必须带此「普通文件」才允许 rsync --delete
MARKER_NAME=".chuyin-auto-root"
DEFAULT_APP_DIR="/opt/chuyin-auto"
APP_DIR="${APP_DIR:-$DEFAULT_APP_DIR}"
RUN_USER="${RUN_USER:-chuyin-auto}"
RUN_GROUP="${RUN_GROUP:-$RUN_USER}"
SRC_DIR="$(cd "$(dirname "$0")/.." && pwd -P)"

# 运行时目录：二次安装时绝不能 chown/chmod 整树扫进去
RUNTIME_SKIP_NAMES=(".venv" "logs" "config.yaml" "$MARKER_NAME")

die() { echo "[!] $*" >&2; exit 1; }
info() { echo "[*] $*"; }

require_root() {
  if [[ "${EUID}" -ne 0 ]]; then
    die "请用 root 执行: bash deploy/install.sh（或 sudo bash deploy/install.sh）"
  fi
}

# 以运行用户执行命令（兼容无 sudo 的环境：runuser / su / sudo）
run_as_user() {
  local user="$1"
  shift
  if command -v runuser >/dev/null 2>&1; then
    runuser -u "$user" -- "$@"
  elif command -v su >/dev/null 2>&1; then
    # su 的 -c 需要拼成一条 shell 命令
    local cmd
    printf -v cmd '%q ' "$@"
    su -s /bin/bash "$user" -c "$cmd"
  elif command -v sudo >/dev/null 2>&1; then
    sudo -u "$user" -- "$@"
  else
    die "需要 runuser、su 或 sudo 之一，才能以用户 $user 安装依赖"
  fi
}

is_symlink() {
  [[ -L "$1" ]]
}

in_allowed_app_dir() {
  case "$1" in
    /opt/chuyin-auto|/opt/chuyin-auto/*|/srv/chuyin-auto|/srv/chuyin-auto/*) return 0 ;;
    *) return 1 ;;
  esac
}

# 仅允许 /opt/chuyin-auto[...] 或 /srv/chuyin-auto[...]
validate_app_dir() {
  local raw="$1"
  [[ "$raw" == /* ]] || die "APP_DIR 必须是绝对路径: $raw"
  [[ "$raw" != *..* ]] || die "APP_DIR 不允许包含 .. : $raw"
  is_symlink "$raw" && die "APP_DIR 不能是符号链接: $raw"

  local parent base resolved parent_real
  parent="$(dirname -- "$raw")"
  base="$(basename -- "$raw")"

  is_symlink "$parent" && die "APP_DIR 父目录不能是符号链接: $parent"
  [[ -d "$parent" ]] || die "APP_DIR 父目录不存在: $parent"
  parent_real="$(cd "$parent" && pwd -P)"

  if [[ -e "$raw" ]]; then
    is_symlink "$raw" && die "APP_DIR 不能是符号链接: $raw"
    [[ -d "$raw" ]] || die "APP_DIR 存在但不是目录: $raw"
    resolved="$(cd "$raw" && pwd -P)"
  else
    resolved="${parent_real}/${base}"
  fi

  in_allowed_app_dir "$resolved" || die \
    "APP_DIR 不在允许范围 (/opt|/srv)/chuyin-auto[/...]: $resolved"

  case "$base" in
    chuyin-auto|chuyin-auto-*) ;;
    *) die "APP_DIR 最后一级必须以 chuyin-auto 开头: $base" ;;
  esac

  if [[ "$resolved" == "/opt" || "$resolved" == "/srv" || "$resolved" == "/" ]]; then
    die "拒绝危险 APP_DIR: $resolved"
  fi

  printf '%s\n' "$resolved"
}

# 不跟随 symlink 的“是否为普通文件”
is_regular_file_nofollow() {
  local p="$1"
  is_symlink "$p" && return 1
  [[ -f "$p" ]] || return 1
  return 0
}

marker_owner() {
  local p="$1"
  if stat --version >/dev/null 2>&1; then
    # GNU
    stat -c '%U' "$p"
  else
    # BSD
    stat -f '%Su' "$p"
  fi
}

write_marker() {
  local dir="$1"
  local m="$dir/$MARKER_NAME"
  # 若已是 symlink → 拒绝（绝不 touch/chmod 跟随目标）
  if is_symlink "$m"; then
    die "marker 是符号链接，拒绝: $m（请删除该链接后重试）"
  fi
  # 用 O_NOFOLLOW 语义：先确保不存在链接，再创建普通文件
  if [[ -e "$m" ]]; then
    is_regular_file_nofollow "$m" || die "marker 必须是普通文件: $m"
  else
    # umask 后 0644；属主 root
    : >"$m"
  fi
  chown root:root "$m"
  chmod 644 "$m"
  # 二次校验
  is_symlink "$m" && die "marker 变成了符号链接: $m"
  is_regular_file_nofollow "$m" || die "marker 不是普通文件: $m"
  [[ "$(marker_owner "$m")" == "root" ]] || die "marker 必须属主 root: $m"
}

ensure_safe_target() {
  local dir="$1"

  if is_symlink "$dir"; then
    die "APP_DIR 是符号链接，拒绝: $dir"
  fi

  if [[ ! -e "$dir" ]]; then
    mkdir -p "$dir"
    is_symlink "$dir" && die "mkdir 后 APP_DIR 变成符号链接: $dir"
    write_marker "$dir"
    return
  fi

  [[ -d "$dir" ]] || die "APP_DIR 存在但不是目录: $dir"
  is_symlink "$dir" && die "APP_DIR 是符号链接: $dir"

  # 非空统计：不跟随 symlink 条目本身也算内容
  local count
  count="$(find "$dir" -mindepth 1 -maxdepth 1 2>/dev/null | wc -l | tr -d ' ')"

  local m="$dir/$MARKER_NAME"
  if [[ "$count" -gt 0 ]]; then
    if is_symlink "$m"; then
      die "marker 是符号链接，拒绝安装: $m"
    fi
    if ! is_regular_file_nofollow "$m"; then
      die "目标目录非空且缺少合法 $MARKER_NAME（普通文件），拒绝 rsync --delete: $dir
    若确认这是本项目目录，可: rm -f $m && touch $m && chown root:root $m"
    fi
    # 已有 marker：必须 root 所有
    if [[ "$(marker_owner "$m")" != "root" ]]; then
      die "marker 属主不是 root（当前 $(marker_owner "$m")），拒绝: $m"
    fi
  fi

  write_marker "$dir"
}

ensure_run_user() {
  if ! id -u "$RUN_USER" >/dev/null 2>&1; then
    info "创建系统用户 $RUN_USER"
    useradd --system --home "$APP_DIR" --shell /usr/sbin/nologin "$RUN_USER" 2>/dev/null \
      || useradd --system --home "$APP_DIR" --shell /sbin/nologin "$RUN_USER"
  fi
  if ! getent group "$RUN_GROUP" >/dev/null 2>&1; then
    groupadd --system "$RUN_GROUP" || true
  fi
  usermod -a -G "$RUN_GROUP" "$RUN_USER" 2>/dev/null || true
}

should_skip_runtime() {
  local name="$1"
  local s
  for s in "${RUNTIME_SKIP_NAMES[@]}"; do
    [[ "$name" == "$s" ]] && return 0
  done
  return 1
}

# 只规范化「代码树」，跳过 .venv / logs / config.yaml / marker
normalize_code_perms() {
  info "规范化代码权限（跳过 .venv/logs/config.yaml/marker）"
  # 顶层目录本身
  chown root:"$RUN_GROUP" "$APP_DIR"
  chmod 755 "$APP_DIR"

  local entry name
  # 含隐藏项
  shopt -s nullglob dotglob
  for entry in "$APP_DIR"/*; do
    name="$(basename -- "$entry")"
    if should_skip_runtime "$name"; then
      continue
    fi
    # 拒绝代码树里的 symlink 逃逸（同步后若出现则报警）
    if is_symlink "$entry"; then
      die "安装树中出现符号链接，拒绝: $entry"
    fi
    chown -R root:"$RUN_GROUP" "$entry"
    if [[ -d "$entry" ]]; then
      if [[ -n "$(find "$entry" -type l -print -quit)" ]]; then
        die "代码目录内存在符号链接，拒绝: $entry"
      fi
      find "$entry" -type d -exec chmod 755 {} +
      find "$entry" -type f -exec chmod 644 {} +
    elif [[ -f "$entry" ]]; then
      chmod 644 "$entry"
    fi
  done
  shopt -u nullglob dotglob

  chmod 755 "$APP_DIR/main.py" "$APP_DIR/deploy/install.sh" 2>/dev/null || true
  write_marker "$APP_DIR"
}

restore_runtime_ownership() {
  # 二次安装后确保运行时可写目录仍属运行用户
  # （root 手动 dry-run 会留下 root 属主日志，service 用户写不了）
  if [[ -e "$APP_DIR/logs" ]]; then
    is_symlink "$APP_DIR/logs" && die "logs 不能是符号链接: $APP_DIR/logs"
    mkdir -p "$APP_DIR/logs"
    chown -R "$RUN_USER":"$RUN_GROUP" "$APP_DIR/logs"
    chmod 700 "$APP_DIR/logs"
    find "$APP_DIR/logs" -type f -exec chown "$RUN_USER":"$RUN_GROUP" {} + 2>/dev/null || true
    find "$APP_DIR/logs" -type f -exec chmod 600 {} + 2>/dev/null || true
  fi
  if [[ -e "$APP_DIR/.venv" ]]; then
    is_symlink "$APP_DIR/.venv" && die ".venv 不能是符号链接"
    chown -R "$RUN_USER":"$RUN_GROUP" "$APP_DIR/.venv"
    # 恢复 bin 下执行位（绝不 chmod 644 扫过 venv）
    if [[ -d "$APP_DIR/.venv/bin" ]]; then
      find "$APP_DIR/.venv/bin" -type f -exec chmod u+rwX,g+rX,o-rwx {} + 2>/dev/null || true
    fi
  fi
  if [[ -f "$APP_DIR/config.yaml" ]] && ! is_symlink "$APP_DIR/config.yaml"; then
    chown root:"$RUN_GROUP" "$APP_DIR/config.yaml"
    chmod 640 "$APP_DIR/config.yaml"
  fi
}

sync_code() {
  info "同步代码 $SRC_DIR -> $APP_DIR (root 属主，禁止保留上传者 uid)"
  is_symlink "$APP_DIR" && die "APP_DIR 是符号链接"

  rsync -rlptD --delete \
    --no-owner --no-group \
    --exclude '.venv' \
    --exclude 'logs' \
    --exclude 'config.yaml' \
    --exclude '__pycache__' \
    --exclude '.git' \
    --exclude "$MARKER_NAME" \
    "$SRC_DIR/" "$APP_DIR/"

  normalize_code_perms
  restore_runtime_ownership
}

setup_config_and_logs() {
  if [[ -e "$APP_DIR/logs" ]] && is_symlink "$APP_DIR/logs"; then
    die "logs 是符号链接: $APP_DIR/logs"
  fi
  mkdir -p "$APP_DIR/logs"
  chown "$RUN_USER":"$RUN_GROUP" "$APP_DIR/logs"
  chmod 700 "$APP_DIR/logs"

  if [[ ! -e "$APP_DIR/config.yaml" ]]; then
    if [[ -f "$SRC_DIR/config.yaml" ]] && ! is_symlink "$SRC_DIR/config.yaml"; then
      cp "$SRC_DIR/config.yaml" "$APP_DIR/config.yaml"
      info "已从源码目录复制 config.yaml"
    else
      cp "$APP_DIR/config.example.yaml" "$APP_DIR/config.yaml"
      info "已写入示例 config.yaml，请编辑账号: $APP_DIR/config.yaml"
    fi
  elif is_symlink "$APP_DIR/config.yaml"; then
    die "config.yaml 是符号链接，拒绝: $APP_DIR/config.yaml"
  fi
  chown root:"$RUN_GROUP" "$APP_DIR/config.yaml"
  chmod 640 "$APP_DIR/config.yaml"
}

setup_venv() {
  info "安装 venv + 依赖"
  if [[ -e "$APP_DIR/.venv" ]] && is_symlink "$APP_DIR/.venv"; then
    die ".venv 是符号链接"
  fi
  if [[ ! -x "$APP_DIR/.venv/bin/python" ]]; then
    python3 -m venv "$APP_DIR/.venv"
  fi
  chown -R "$RUN_USER":"$RUN_GROUP" "$APP_DIR/.venv"
  if [[ -d "$APP_DIR/.venv/bin" ]]; then
    find "$APP_DIR/.venv/bin" -type f -exec chmod u+rwX,g+rX,o-rwx {} + 2>/dev/null || true
  fi
  run_as_user "$RUN_USER" "$APP_DIR/.venv/bin/pip" install -U pip
  run_as_user "$RUN_USER" "$APP_DIR/.venv/bin/pip" install -r "$APP_DIR/requirements.txt"
}

render_units() {
  local tmpl="$APP_DIR/deploy/chuyin-auto.service.in"
  local out="/etc/systemd/system/chuyin-auto.service"
  [[ -f "$tmpl" ]] || die "缺少模板: $tmpl"
  is_symlink "$tmpl" && die "service 模板是符号链接"

  sed \
    -e "s|@APP_DIR@|${APP_DIR}|g" \
    -e "s|@RUN_USER@|${RUN_USER}|g" \
    -e "s|@RUN_GROUP@|${RUN_GROUP}|g" \
    "$tmpl" > "$out"

  cp "$APP_DIR/deploy/chuyin-auto.timer" /etc/systemd/system/chuyin-auto.timer
  systemctl daemon-reload
  systemctl enable --now chuyin-auto.timer
}

main() {
  require_root
  APP_DIR="$(validate_app_dir "$APP_DIR")"
  info "APP_DIR=$APP_DIR RUN_USER=$RUN_USER"

  [[ -f "$SRC_DIR/main.py" ]] || die "源码目录不像本项目（缺 main.py）: $SRC_DIR"
  [[ -f "$SRC_DIR/deploy/chuyin-auto.service.in" ]] || die "缺 deploy/chuyin-auto.service.in"

  ensure_safe_target "$APP_DIR"
  ensure_run_user
  sync_code
  setup_config_and_logs
  setup_venv
  render_units

  info "timer status:"
  systemctl status chuyin-auto.timer --no-pager || true
  info "next runs:"
  systemctl list-timers chuyin-auto.timer --no-pager || true
  echo
  echo "手动跑一次: systemctl start chuyin-auto.service"
  echo "看日志:     journalctl -u chuyin-auto.service -n 100 --no-pager"
  echo "文件日志:   $APP_DIR/logs/  (mode 700, 文件 600)"
  echo "改配置:     nano $APP_DIR/config.yaml   # 640 root:$RUN_GROUP"
  echo "改代码后:   请用 root 重新执行 install.sh（普通用户无法写 $APP_DIR）"
}

main "$@"
