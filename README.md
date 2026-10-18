# pi-skills

[PI-Desktop](https://pi-desktop.app) 用的 skills 集合。每个 skill 一个目录。

## codex-claude-transcript-import

把 **Codex** 和 **Claude Code** 的本地会话记录读出来：既能导出成可读的 transcript，也能**直接导入成 PI-Desktop 的历史会话**（可在 History 里打开、续聊）。

算法参考 Cline 的 `session-import` 模块（Apache-2.0），这里是为 PI-Desktop 独立重写的实现。

### 安装

把 `codex-claude-transcript-import/` 整个目录复制到本机的 skills 目录（就是 PI-Desktop 里 **Settings → Skills** 显示的那个位置），重启后即可在列表中看到。

也可以不安装，直接从本仓库运行。

### 用法

```bash
S=~/pi-skills/codex-claude-transcript-import/scripts

# 列出本机所有可导入的会话
python3 $S/transcript.py list

# 导出成可读的 Markdown（--json 输出结构化数据）
python3 $S/transcript.py dump --session <id> -o /tmp/session.md
python3 $S/transcript.py dump --session <id> --max-tool-chars 200 -o /tmp/session.md

# 真导入进 PI-Desktop（自动备份数据库）
python3 $S/import_to_pi.py --session <id> --dry-run   # 先看计划，不写库
python3 $S/import_to_pi.py --session <id>
python3 $S/import_to_pi.py --list                     # 已导入清单
python3 $S/import_to_pi.py --undo <pi-session-id>     # 回滚
```

### 支持的源

| 源 | 位置 | 状态 |
|---|---|---|
| Codex | `$CODEX_HOME` 或 `~/.codex/sessions/**/rollout-*.jsonl` | 支持 |
| Claude Code | `$CLAUDE_CONFIG_DIR` 或 `~/.claude/projects/**/*.jsonl` | 支持 |
| opencode | `$XDG_DATA_HOME/opencode/opencode.db` | 未实现（见 `references/format-mapping.md`） |

### 处理了哪些坑

- Claude 的 jsonl 是**树**（`parentUuid` 分叉），不是流水账：从最后一条对话行回溯出活跃分支
- 跳过子代理对话（`isSidechain`）与系统 meta 行；合并同一 `message.id` 的流式分片
- Codex 续聊会产生新 rollout 文件但复用原 `session_meta.id`：按 id 归并，取内容最全的一份
- 过滤 Codex 注入的 `<environment_context>` / `<user_instructions>` / AGENTS.md 等伪用户消息
- 修 `tool_use` ↔ `tool_result` 配对，剥掉只对原 provider 有效的思考签名

### 安全边界

- 只**读** Codex / Claude Code 的存储，绝不写回
- 导入 PI 前用 sqlite backup API 自动备份（`~/.pi-desktop/pi.sqlite.bak-<时间>`）
- 每次导入可用 `--undo` 干净回滚（删会话行、消息、来源登记与 `.jsonl`）

### 验证

```bash
S=~/pi-skills/codex-claude-transcript-import/scripts
python3 $S/transcript.py list | head -5
python3 $S/import_to_pi.py --list
```

## ssh-fleet

把**一台控制端**和**一台或多台被控端**用 SSH 免密打通，让 agent 能在别的机器上无交互执行命令。

难点不在 `ssh` 命令本身，而在两处：两侧各要做什么、以及各类静默失败（权限不对 sshd 直接忽略公钥、`sudo` 下 `$HOME` 变 `/root`、代理 TUN 劫持跨网段流量）。这个 skill 把流程和坑一起固化了。

### 安装

把 `ssh-fleet/` 整个目录复制到本机的 skills 目录，重启后生效。

### 用法

流程只涉及两台机器，**不需要在两者之间传文件**（被控端还没有 SSH，传不过去）。

**控制端：**

```bash
bash ~/.agents/skills/ssh-fleet/scripts/setup-controller.sh
```

它生成密钥、写 `~/.ssh/config` 基础段，并打印**一段可直接粘贴到被控端执行的命令**（已内嵌你的公钥）。

**被控端**（把上一步打印的内容粘过去）：

```bash
sudo apt install -y openssh-server && sudo systemctl enable --now ssh
mkdir -p ~/.ssh && chmod 700 ~/.ssh && echo '<你的公钥>' >> ~/.ssh/authorized_keys && chmod 600 ~/.ssh/authorized_keys
```

**回到控制端加别名并验证：**

```bash
bash ~/.agents/skills/ssh-fleet/scripts/setup-controller.sh pfahero 192.168.1.50 ubuntu
ssh pfahero "whoami; hostname"
```

也可以通过 U 盘等方式把脚本带到被控端，一次跑完：

```bash
sudo bash enroll-remote.sh 'ssh-ed25519 AAAA...'        # 装 sshd + 写公钥 + 防火墙
sudo bash enroll-remote.sh --dry-run 'ssh-ed25519 AAAA...'   # 先看要做什么
```

### 配置要点

`~/.ssh/config` 的基础段，面向自动化调用缺一不可：

```sshconfig
Host *
  IdentitiesOnly yes
  BatchMode yes                      # 绝不弹密码提示，失败立即返回
  ConnectTimeout 8                   # 连不上 8 秒返回，不挂死任务
  StrictHostKeyChecking accept-new   # 首次自动记录指纹
  ControlMaster auto                 # 连接复用：214ms → 19ms
  ControlPath ~/.ssh/cm-%r@%h:%p
  ControlPersist 10m
```

### 已知的坑

- **权限不对 → sshd 静默忽略**：`.ssh` 必须 `700`，`authorized_keys` 必须 `600`
- **`sudo` 写公钥 → 落到 `/root`**：脚本用 `$SUDO_USER` 定位真实用户家目录
- **`BatchMode yes` 会让 `ssh-copy-id` 失败**：手工装公钥时加 `-o BatchMode=no`
- **代理 TUN 劫持跨网段流量**：表现为"端口开着但连接被立刻关闭"，用 `ip route get` 确认
- **mDNS 只在同一局域网有效**，跨网段要用组网工具
- **Windows 管理员账号**的公钥必须写 `C:\ProgramData\ssh\administrators_authorized_keys` 并限定 ACL

### 安全边界

- 私钥只留在控制端，权限 `600`
- 写入公钥是**单向**授权：被控端拿到公钥无法反向登录控制端
- 需要更严可加 `restrict,from="固定IP"` 前缀
