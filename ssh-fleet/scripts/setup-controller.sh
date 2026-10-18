#!/usr/bin/env bash
# ============================================================
#  setup-controller.sh —— 在【控制端】（你平时坐着的那台）运行
#
#  作用：确保密钥存在、生成 ~/.ssh/config 的标准主机模板、
#        打印公钥供你拿到被控端去写入。
#
#  用法：
#     bash setup-controller.sh                 # 准备好并打印公钥
#     bash setup-controller.sh pfahero 192.168.1.50 ubuntu
#                                              # 再加一台机器到 config
# ============================================================
set -euo pipefail

SSH_DIR="$HOME/.ssh"
KEY="$SSH_DIR/id_ed25519"
CONFIG="$SSH_DIR/config"

mkdir -p "$SSH_DIR"
chmod 700 "$SSH_DIR"

# ---------- 1. 密钥 ----------
if [[ -f "$KEY" ]]; then
    echo "[1/3] 密钥已存在，复用"
else
    echo "[1/3] 生成新密钥 ..."
    ssh-keygen -t ed25519 -f "$KEY" -N "" -C "$(whoami)@$(hostname)" >/dev/null
    echo "      已生成 $KEY"
fi
chmod 600 "$KEY"; chmod 644 "$KEY.pub"

# ---------- 2. config 基础段 ----------
if [[ -f "$CONFIG" ]] && grep -q "ControlPersist" "$CONFIG"; then
    echo "[2/3] ~/.ssh/config 已有基础段，跳过"
else
    echo "[2/3] 写入 ~/.ssh/config 基础段 ..."
    [[ -f "$CONFIG" ]] && cp "$CONFIG" "$CONFIG.bak.$(date +%Y%m%d%H%M%S)"
    cat >> "$CONFIG" <<'EOF'

# ===== 通用默认值（面向自动化调用）=====
Host *
  IdentityFile ~/.ssh/id_ed25519
  IdentitiesOnly yes
  BatchMode yes                      # 绝不弹密码提示，认证失败立即返回
  ConnectTimeout 8
  ServerAliveInterval 15
  ServerAliveCountMax 3
  StrictHostKeyChecking accept-new   # 首次自动记录指纹
  ControlMaster auto                 # 连接复用
  ControlPath ~/.ssh/cm-%r@%h:%p
  ControlPersist 10m
EOF
    chmod 600 "$CONFIG"
fi

# ---------- 3. 可选：追加一台主机 ----------
if [[ $# -ge 3 ]]; then
    NAME="$1"; HOST="$2"; USER_NAME="$3"
    if grep -qE "^Host[[:space:]]+${NAME}$" "$CONFIG"; then
        echo "[3/3] Host $NAME 已存在，跳过"
    else
        cat >> "$CONFIG" <<EOF

Host $NAME
  HostName $HOST
  User $USER_NAME
EOF
        chmod 600 "$CONFIG"
        echo "[3/3] 已追加 Host $NAME -> $USER_NAME@$HOST"
    fi
else
    echo "[3/3] 未指定主机（可选参数：<名字> <IP> <用户名>）"
fi

# ---------- 输出：给被控端可直接粘贴的一段 ----------
PUB="$(cat "$KEY.pub")"

echo
echo "=============================================================="
echo " 被控端（对机）要做的两件事"
echo "=============================================================="
echo
echo "【第 1 步】装并启动 sshd —— 若对机是 Ubuntu 桌面版通常没装："
echo
echo "    sudo apt install -y openssh-server && sudo systemctl enable --now ssh"
echo
echo "  已经装过就跳过。确认：sudo systemctl is-active ssh"
echo
echo "【第 2 步】写入公钥 —— 用【普通用户】执行，不要加 sudo："
echo
echo "    mkdir -p ~/.ssh && chmod 700 ~/.ssh && echo '$PUB' >> ~/.ssh/authorized_keys && chmod 600 ~/.ssh/authorized_keys"
echo
echo "  幂等版（重复执行不会写两遍）："
echo
echo "    install -d -m 700 ~/.ssh; K='$PUB'; grep -qxF \"\$K\" ~/.ssh/authorized_keys 2>/dev/null || echo \"\$K\" >> ~/.ssh/authorized_keys; chmod 600 ~/.ssh/authorized_keys"
echo
echo "【第 3 步】自检 —— 权限必须是 700 / 600，否则 sshd 会静默忽略："
echo
echo "    echo \"user=\$(whoami) ip=\$(hostname -I | awk '{print \$1}')\"; ls -ld ~/.ssh ~/.ssh/authorized_keys"
echo
echo "=============================================================="
echo " 本机（控制端）接着做"
echo "=============================================================="
echo
echo "  bash $0 <名字> <对机IP> <对机用户名>"
echo "  ssh <名字> \"whoami; hostname\""
echo
echo "公钥指纹：$(ssh-keygen -lf "$KEY.pub" | awk '{print $2}')"
echo "=============================================================="
