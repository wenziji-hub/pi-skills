#!/usr/bin/env bash
# ============================================================
#  enroll-remote.sh —— 在【被控端】（要被控制的那台）运行
#
#  作用：装并启动 sshd、写入授权公钥、设置权限、放行防火墙、打印信息。
#        幂等：重复运行不会重复写公钥。
#
#  用法：
#     sudo bash enroll-remote.sh '<公钥内容>'
#     sudo bash enroll-remote.sh --dry-run '<公钥内容>'      # 只看不改
#     sudo bash enroll-remote.sh                             # 从控制端粘贴
#
#  注意：写入的是【公钥】，只能让持有私钥的一端登录本机；
#        本机拿不到对方任何权限。
# ============================================================
set -euo pipefail

DRY_RUN=0
if [[ "${1:-}" == "--dry-run" ]]; then
    DRY_RUN=1; shift
fi
PUBKEY="${1:-}"

run() {
    if [[ $DRY_RUN -eq 1 ]]; then echo "    [dry-run] $*"; else "$@"; fi
}

[[ $EUID -eq 0 ]] || { echo "请用 sudo 运行：sudo bash $0 '<公钥>'" >&2; exit 1; }
[[ -n "$PUBKEY" ]] || { echo "缺少公钥参数。用法：sudo bash $0 '<公钥>'" >&2; exit 1; }
[[ "$PUBKEY" == ssh-* ]] || { echo "参数不像公钥（应以 ssh-ed25519 / ssh-rsa 开头）" >&2; exit 1; }

# 目标用户 = 调用 sudo 的真实用户（不是 root！）
TARGET_USER="${SUDO_USER:-$(id -un)}"
if [[ "$TARGET_USER" == "root" ]]; then
    echo "警告：检测不到普通用户，公钥将写入 root。" >&2
    TARGET_HOME="/root"
else
    TARGET_HOME="$(getent passwd "$TARGET_USER" | cut -d: -f6)"
fi
[[ -d "$TARGET_HOME" ]] || { echo "找不到用户 $TARGET_USER 的家目录" >&2; exit 1; }

echo "=================================================="
echo " 目标用户 : $TARGET_USER"
echo " 家目录   : $TARGET_HOME"
echo " 系统     : $(. /etc/os-release 2>/dev/null && echo "$PRETTY_NAME" || uname -s)"
[[ $DRY_RUN -eq 1 ]] && echo " 模式     : DRY-RUN（不改动）"
echo "=================================================="

# ---------- 1. sshd ----------
echo "[1/4] 检查 OpenSSH Server ..."
if command -v sshd >/dev/null 2>&1 || dpkg -s openssh-server >/dev/null 2>&1; then
    echo "    已安装"
else
    echo "    未安装，开始安装 ..."
    if command -v apt-get >/dev/null 2>&1; then
        run env DEBIAN_FRONTEND=noninteractive apt-get update -qq
        run env DEBIAN_FRONTEND=noninteractive apt-get install -y -qq openssh-server
    else
        echo "    非 Debian 系，请手工安装 openssh-server" >&2; exit 1
    fi
fi

# ---------- 2. 启动 ----------
echo "[2/4] 启动 sshd ..."
run systemctl enable ssh >/dev/null 2>&1 || true
run systemctl start ssh

# ---------- 3. 公钥 ----------
echo "[3/4] 写入授权公钥 ..."
SSH_DIR="$TARGET_HOME/.ssh"
AUTH_FILE="$SSH_DIR/authorized_keys"

if [[ $DRY_RUN -eq 0 ]]; then
    mkdir -p "$SSH_DIR"; chmod 700 "$SSH_DIR"
    chown "$TARGET_USER":"$TARGET_USER" "$SSH_DIR"
    if [[ -f "$AUTH_FILE" ]]; then
        cp -a "$AUTH_FILE" "$AUTH_FILE.bak.$(date +%Y%m%d%H%M%S)"
        echo "    已备份原 authorized_keys"
    fi
    if [[ -f "$AUTH_FILE" ]] && grep -qF "$PUBKEY" "$AUTH_FILE"; then
        echo "    公钥已存在，跳过"
    else
        echo "$PUBKEY" >> "$AUTH_FILE"
        echo "    公钥已写入"
    fi
    # 权限错会导致 sshd 静默忽略，务必设置
    chmod 600 "$AUTH_FILE"
    chown "$TARGET_USER":"$TARGET_USER" "$AUTH_FILE"
else
    echo "    [dry-run] 将写入 $AUTH_FILE"
fi

# ---------- 4. 防火墙 ----------
echo "[4/4] 防火墙 ..."
if command -v ufw >/dev/null 2>&1 && ufw status 2>/dev/null | grep -q "Status: active"; then
    IFACE="$(ip route show default 2>/dev/null | awk '/default/{print $5; exit}')"
    SUBNET="$(ip -o -f inet addr show "$IFACE" 2>/dev/null | awk '{print $4}' | head -1)"
    if [[ -n "$SUBNET" ]]; then
        echo "    ufw 已启用，放行 $SUBNET -> 22"
        run ufw allow from "$SUBNET" to any port 22 proto tcp >/dev/null
    else
        echo "    无法探测网段，跳过"
    fi
else
    echo "    ufw 未启用，无需处理"
fi

# ---------- 汇报 ----------
echo
echo "=================== 完成 ==================="
[[ $DRY_RUN -eq 0 ]] && { printf " sshd   : "; systemctl is-active ssh; }
printf " 用户   : %s\n" "$TARGET_USER"
printf " IP     : %s\n" "$(hostname -I 2>/dev/null | awk '{print $1}')"
echo " 配置端执行："
echo "   ssh ${TARGET_USER}@$(hostname -I 2>/dev/null | awk '{print $1}') \"whoami; hostname\""
echo "============================================"
