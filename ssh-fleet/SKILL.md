---
name: ssh-fleet
description: 配置一台电脑 SSH 免密远控另一台电脑：控制端建密钥与 ~/.ssh/config，被控端装 sshd 并写入公钥，两侧分别要做什么、生成可直接粘贴的命令，含权限静默失败、Clash TUN 劫持、mDNS、Windows 管理员密钥等坑
tags: ["自习"]
---

# SSH 机器舰队配置

把「一台控制端」和「一台或多台被控端」配通，让 agent 能无交互执行命令。

## When to Use

- 换了新电脑，要重新建立"控制其他机器"的能力
- 要给一台机器开 SSH，让 agent 能直接在上面跑命令
- `ssh <名字>` 突然连不上，要按顺序排查
- 不适用：需要图形桌面远控（用 RustDesk / ToDesk，SSH 管不了）

## 核心概念：方向是单向的

| | 私钥 `id_ed25519` | 公钥 `id_ed25519.pub` |
|---|---|---|
| 在哪 | **只在控制端**，权限 600 | 复制到每台被控端 |
| 作用 | 证明"我是我" | 让被控端认出"这是谁的钥匙" |

被控端 `~/.ssh/authorized_keys` 里放控制端的**公钥**，含义是：
「持有对应私钥的一端，可以登录本机的这个用户」。

所以**被控端拿到公钥无法反过来登录控制端**（ed25519 靠离散对数，公钥推不出私钥）。
反向控制需要控制端另装 sshd 并授权对方公钥——默认不做。

## 两台机器各自要做什么

**关键：不要试图在两台之间传文件。** 被控端还没有 SSH，`scp` 传不过去。
做法是让控制端**生成一段可直接粘贴的命令**，你复制到被控端终端执行。

```
控制端 A（你坐着的那台）                     被控端 B（要被控制的那台）
──────────────────────                     ─────────────────────────
1. 生成密钥（已有则复用）
2. 写 ~/.ssh/config 的 Host 段
3. 输出「B 可直接粘贴的命令」
        │
        └──── 复制这段文本，手工带到 B ────→  4. 粘贴执行：装 sshd + 写公钥
                                             5. 回报：用户名 + IP
        ←──────────── 拿到用户/IP ────────────┘
6. ssh <名字> "whoami" 验证
```

一共两侧各 2–3 件事，**没有文件传输**。

## Procedure

### A. 控制端 A

```bash
bash ~/.agents/skills/ssh-fleet/scripts/setup-controller.sh
```

它做三件事：

1. 密钥不存在就生成（`~/.ssh/id_ed25519`）
2. 写 `~/.ssh/config` 基础段（已存在则跳过，写前备份）
3. **打印被控端可直接粘贴的完整命令**（已内嵌你的公钥）

基础段关键项，缺一不可：

```sshconfig
Host *
  IdentityFile ~/.ssh/id_ed25519
  IdentitiesOnly yes
  BatchMode yes                      # 绝不弹密码提示，认证失败立即返回
  ConnectTimeout 8                   # 连不上 8 秒返回，不挂死任务
  ServerAliveInterval 15
  ServerAliveCountMax 3
  StrictHostKeyChecking accept-new   # 首次自动记录指纹，不阻塞
  ControlMaster auto                 # 连接复用：214ms → 19ms
  ControlPath ~/.ssh/cm-%r@%h:%p
  ControlPersist 10m
```

前两项面向自动化：没有 `BatchMode yes`，agent 会卡在密码提示上直到超时。

### B. 被控端 B

把 A 打印的那段命令复制过去执行。拆开是这样两条：

**B-1 装并启动 sshd**（Ubuntu 桌面版默认没装；服务器版一般自带）

```bash
sudo apt install -y openssh-server && sudo systemctl enable --now ssh
systemctl is-active ssh          # 应输出 active
```

**B-2 写入公钥**（用**普通用户**执行，不要加 `sudo`）

```bash
mkdir -p ~/.ssh && chmod 700 ~/.ssh && echo 'ssh-ed25519 AAAA... 描述' >> ~/.ssh/authorized_keys && chmod 600 ~/.ssh/authorized_keys
```

幂等版（重复执行不会写两遍）：

```bash
install -d -m 700 ~/.ssh; K='ssh-ed25519 AAAA... 描述'; grep -qxF "$K" ~/.ssh/authorized_keys 2>/dev/null || echo "$K" >> ~/.ssh/authorized_keys; chmod 600 ~/.ssh/authorized_keys
```

**B-3 自检**

```bash
echo "user=$(whoami) ip=$(hostname -I | awk '{print $1}')"; ls -ld ~/.ssh ~/.ssh/authorized_keys
```

期望：`drwx------` 和 `-rw-------`。**权限不对 sshd 会静默忽略**，不报任何错。

> 如果能通过 U 盘等方式把脚本带过去，`enroll-remote.sh` 一步做完全部：
> `sudo bash enroll-remote.sh 'ssh-ed25519 AAAA...'`（先看效果加 `--dry-run`）

### C. 控制端 A 加别名

```bash
bash ~/.agents/skills/ssh-fleet/scripts/setup-controller.sh pfahero 192.168.1.50 ubuntu
```

或手工在 `~/.ssh/config` 追加：

```sshconfig
Host pfahero
  HostName 192.168.1.50
  User ubuntu

Host pfahero-mdns           # 同机 mDNS 入口，IP 变了用它
  HostName mymini.local
  User ubuntu
```

命名规则用 `前缀+标识`，**名字就是别名**：`pfahero`、`pfaduobu`、`pfashao`。

### D. 验证

```bash
ssh -G pfahero | grep -E "^(hostname|user|batchmode) "   # 配置解析
ssh pfahero "whoami; hostname"                            # 免密登录
```

## Pitfalls

**1. 权限不对 → sshd 静默忽略（最难查）**
`.ssh` 必须 `700`，`authorized_keys` 必须 `600`，属主是该用户。
权限松了 sshd **不报错、不提示**，表现是"公钥明明写了却还要密码"。

**2. `sudo` 写公钥 → 落到 /root**
`sudo bash script.sh` 后 `$HOME` 变 `/root`。脚本必须用 `$SUDO_USER` 定位真实用户家目录，
否则公钥写进 `/root/.ssh/`，普通用户永远读不到。`enroll-remote.sh` 已处理。

**3. `BatchMode yes` 会让 `ssh-copy-id` 失败**
它禁掉了密码交互，而 `ssh-copy-id` 需要输密码。手工装公钥时临时关掉：

```bash
ssh-copy-id -o BatchMode=no 用户名@IP
```

**4. 代理软件 TUN 劫持跨网段流量**
Clash/mihomo 的 TUN 会接管非直连网段，表现是**端口开着但连接立刻被关闭**
（`kex_exchange_identification: Connection closed by remote host`），极易误判成对方 sshd 坏了。
排查：`ip route get <目标IP>`，看到 `dev Meta` / `198.18.x.x` 就是被劫持。
修复：给代理加规则 `- IP-CIDR,<目标网段>,DIRECT,no-resolve`。

**5. mDNS 只在同一局域网有效**
`.local` 名字靠链路本地多播（`224.0.0.251:5353`），路由器不转发。换网段后 `-mdns` 同样连不上。

**6. DHCP 导致 IP 变动**
用 mDNS 别名兜底，或用组网工具（蒲公英 / Tailscale）给固定虚拟 IP。

**7. Windows 被控端：管理员账号的公钥位置不同**
管理员账号的 `~/.ssh/authorized_keys` 会被忽略，必须写：

```
C:\ProgramData\ssh\administrators_authorized_keys
```

且 ACL 只能是 `Administrators` + `SYSTEM`，多一个权限位 sshd 就拒绝读。
默认 shell 是 `cmd.exe`，建议改成 PowerShell：

```powershell
Set-ItemProperty -Path "HKLM:\SOFTWARE\OpenSSH" -Name DefaultShell `
  -Value "C:\Windows\System32\WindowsPowerShell\v1.0\powershell.exe"
```

## Verification

```bash
# 控制端
ssh -G pfahero | grep -E "^(hostname|user|batchmode) "
ssh pfahero "whoami; hostname"
ssh pfahero "echo ok" >/dev/null && echo 复用连接正常

# 被控端（怀疑权限问题时）
echo "user=$(whoami) home=$HOME"; ls -ld ~/.ssh ~/.ssh/authorized_keys; cat ~/.ssh/authorized_keys
```

指纹对照：`ssh-keygen -lf ~/.ssh/id_ed25519.pub`，与被控端 `authorized_keys` 里的公钥比对，确认是同一把钥匙。

## 安全边界

写入公钥 = **允许持有对应私钥的一端以该用户身份登录**。所以：

- 私钥权限保持 `600`，不要复制到别处、上传或贴进聊天
- 被控端不要为了「方便」把控制端的 sshd 也开起来，除非明确需要反向访问
- 生产机器可加限制：`restrict,from="固定IP" ssh-ed25519 AAAA...`
