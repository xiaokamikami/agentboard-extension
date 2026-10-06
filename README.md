# agentboard-zcode

让 [AgentBoard](https://agentboard.cc) 的统计合并上报本机 [ZCode](https://zcode.ai) 与 [DeepSeek Harness (dsh)](https://github.com/deepseek-ai/dsh) 的 token 用量。

AgentBoard 官方采集器目前不支持 ZCode 和 dsh（`collect_zcode.py` 在服务端返回 404）。本仓库在官方 `collect_codex.py` 的基础上做了最小侵入修改：**ZCode 与 dsh 的用量都以 `source=opencode` 上传**，会话 ID 分别为 `opencode:zcode:<session_id>` 和 `opencode:dsh:<session_id>`——两者的消耗在排行榜上合并显示在 OpenCode 名下，不会和真实 Codex 的用量混在一起，方便区分。

仓库里同时归档了官方 Claude Code 采集器的最新快照（`collect.py`，2026-08-31 版），方便多机适配时一并更新——官方安装脚本会把所有采集器内嵌输出，单文件更新无需重跑安装。

## 工作原理

- 读取本机 ZCode/KCode SQLite 数据库（自动探测 `~/.kcode/cli/db/db.sqlite` 与 `~/.zcode/cli/db/db.sqlite` 中最近活跃的那个），只读模式打开，不写入。
- token 数据源为 `model_usage` 表中 `status='completed'` 的请求（`turn_usage` 会严重漏计，约为真实用量的 1/8）。
- token 口径与官方 Codex 采集器一致：`input_tokens` / `output_tokens` 原样上报，`tokens_used` = `provider_total_tokens` = input + output；cache 单独记在 `cache_read_tokens` / `cache_creation_tokens`，上传前不再从 input 里扣除。子 agent 是独立 `session_id`，会单独上报。
- error/cancelled 请求也计入活跃时间窗口（限流重试等待是真实的工作时间），但不产生 token。
- 活跃窗口算法与官方 `build_engaged_windows` 语义严格一致（10 分钟断档切分、段尾补 gap、单会话 480 分钟 / 单日 960 分钟上限），已通过随机事件序列等价测试。
- 增量同步：每个 (session, date) 的聚合内容做哈希，存于 `~/.agentboard/zcode-sync-state.<hostname>.json`（dsh 为 `dsh-sync-state.<hostname>.json`）；无变化不重复上传，依赖服务端 (session_id, user, date) 幂等 upsert。
- 消息/工具只上传数量和工具名计数，不上传 prompt、回复、代码或路径内容。

### DeepSeek Harness (dsh) 采集

- 数据源为 `~/.dsh/sessions/<project>/<session-id>/session.v3.jsonl.zstd` 会话文件，用 `zstd -dc` 解压（回退查找 `/opt/homebrew/bin/zstd` 等绝对路径，适配 launchd 的最小 PATH）。
- 每个 `assistant/message` 事件的 usage（inputTokens / outputTokens / cacheReadTokens / cacheWriteTokens）按天聚合；同一会话同时存在新旧两份文件时优先取 v3、排除 `.bak`，避免重复计数。
- 模型调用失败的 attempt（无 usage）同样计入活跃时间，但不产生 token，与 ZCode 的 error 请求口径一致。
- 已核对：会话文件统计与 dsh 自带 usage 账本（`~/.dsh/dsh-usage/usage-ledger.json`）一致；账本在 dsh 旧版本中有漏记，因此以会话文件为准。
- dsh 侧同样有快速路径（文件 mtime/size 签名未变即跳过）和 45 天滑动窗口（`AGENTBOARD_DSH_DAYS` 可调）。

### 静默运行设计（内存与开销不随历史增长）

- **签名快速路径**：ZCode 每次同步先做一次纯 SQL 聚合签名（行数/最大时间戳/token 总和）；dsh 用会话文件 mtime/size 签名。签名没变直接跳过整个采集流程，只有几毫秒开销。
- **滑动窗口**：默认只采集最近 45 天（`AGENTBOARD_ZCODE_DAYS` / `AGENTBOARD_DSH_DAYS` 可调，最小 1）。更早的数据早已上传到服务端，不会再读、不会进 state，扫描时间和内存都是有界的。
- **合并区间代替事件点**：每天的活动时间用"合并后的区间列表"（每段一条）维护，而不是逐事件点，内存 O(活跃段数) 而非 O(事件数)，重度使用一整天也只有几十个区间。
- 守护进程（launchd 每 5 分钟）实际运行内存约 **34MB** 峰值；`--summary`（全量诊断用）约为其 9 倍属正常。

### 关于 Claude Code 的两件事（排查经验）

- **网站卡片上 Claude Code 的数字看起来很小是正常的**：卡片主数字是 `tokens_used` = input + output，**不含缓存**；而 Claude Code 会话里缓存通常占 95% 以上。排行榜/卡片的 total tokens 才是含缓存的大口径（`tokens_used + cache_read + cache_creation`）。网站与本地 `--summary` 的 `provider_total_tokens` 逐日精确吻合，可用于验证数据确实在推。
- **升级 collect.py 时必须先停旧守护进程**，否则旧代码会继续用旧版本号覆盖 state 文件、把已修正的数据翻转回去。正确流程：备份 → 覆盖 `collect.py` → `kill` 旧 daemon PID（`~/.agentboard/claude-sync.<host>.pid`）→ 手动 `--sync` 全量重发 → `nohup python3 ~/.agentboard/collect.py --daemon &` 重启并写回 pid 文件。2026-08-31 版在 `REPARSE_ON_UPGRADE_RELEASES` 里，升级会自动触发一次全量重扫（幂等），修正旧版 `lines_added` 的偏差（实测一天 3162 行 → 4141 行）。

## 安装

前提：本机已按官方脚本安装 AgentBoard CLI（存在 `~/.agentboard/config.json` 和 launchd 定时任务 `cc.agentboard.codex-sync`）。

1. 备份原文件：

   ```bash
   cp ~/.agentboard/collect_codex.py ~/.agentboard/collect_codex.py.bak
   ```

2. 用本仓库的 `collect_codex.py` 覆盖 `~/.agentboard/collect_codex.py`（scp 或直接下载均可）。

3. 本地验证（不上传）：

   ```bash
   python3 ~/.agentboard/collect_codex.py --summary
   ```

   输出中会同时包含真实 Codex 与 `opencode:zcode:` 前缀的 ZCode 会话。

4. 手动同步一次并观察日志：

   ```bash
   python3 ~/.agentboard/collect_codex.py --sync --json
   ```

   首次会全量重发 ZCode 历史；再跑一次应只增量同步活跃会话。

5. launchd 定时任务无需改动（仍是每 5 分钟跑 `--sync`），新采集器会在同一个锁内依次同步 Codex、ZCode、dsh。

### 更新单个采集器

每个采集器都是独立文件，可以单独更新，互不影响：

| 采集器 | 文件 | 官方最新版来源 | 本机状态 |
|---|---|---|---|
| Claude Code | `~/.agentboard/collect.py` | 安装脚本内嵌（heredoc） | 2026-08-31（已更新） |
| Codex | `~/.agentboard/collect_codex.py` | 安装脚本内嵌（heredoc） | **本仓库补丁版**（勿用官方覆盖） |
| Gemini CLI | `~/.agentboard/collect_gemini.py` | 安装脚本内嵌（heredoc） | 2026-04-30（最新，无需更新） |
| Claude Cowork | `~/.agentboard/collect_claude_cowork.py` | 安装脚本内嵌（heredoc） | 最新（无需更新） |
| OpenCode / OpenClaw / Kimi | `~/.agentboard/collect_*.py` | `https://agentboard.cc/collect_*.py` | 未安装（本机无对应工具） |

- **Claude / Gemini / Cowork**：官方源码内嵌在安装脚本里，从 `cat > "$COLLECT_FILE" <<'COLLECTEOF'` 与 `COLLECTEOF` 之间提取即可。
- **OpenCode / OpenClaw / Kimi**：官方托管在服务端，直接 `curl https://agentboard.cc/collect_opencode.py` 下载。
- **Codex：唯一不能用官方文件覆盖的**（见下）。

### 关于覆盖风险（重要）

**没有任何自动更新机制**：安装脚本、hook.sh、launchd 任务、采集器自身都不含下载/更新逻辑，所以本机补丁不会被自动覆盖。唯一的覆盖场景是**你手动重跑官方安装脚本**：

```bash
curl -sL https://agentboard.cc/install | bash -s -- <TOKEN>   # ← 会覆盖 collect_codex.py
```

重跑安装脚本时：

- `collect.py`、`collect_gemini.py`、`collect_claude_cowork.py`、`collect_opencode.py` 等会被官方最新版**无条件覆盖**（不备份）——这些没有本地改动，覆盖无害。
- `collect_codex.py` 会被官方版覆盖，**我们的 ZCode/dsh 采集和内存优化全部丢失**（官方 2026-09-16 版不含 ZCode/dsh 支持）。增量 state 文件（`zcode-sync-state.*.json`、`dsh-sync-state.*.json`）不受影响，装回补丁后会按内容哈希继续增量同步，不会重复上传。
- launchd plist 也会被重写（官方 Codex 任务间隔从 300s 改回 60s）；不想要的话改回 `StartInterval` 即可。

**防覆盖**：仓库提供 `reapply.sh`，重跑官方安装后执行一次即可装回补丁（含校验，下载到错误内容会中止且不改动现有文件）：

```bash
bash reapply.sh
# 或手动：
curl -fsSL https://raw.githubusercontent.com/xiaokamikami/agentboard-zcode/main/collect_codex.py -o ~/.agentboard/collect_codex.py
python3 ~/.agentboard/collect_codex.py --summary   # 验证
```

### 更新 Claude Code 采集器（collect.py）

仓库里的 `collect.py` 是官方 Claude 采集器的快照（2026-08-31）。官方升级后，本机按下面流程更新（详见上文"关于 Claude Code 的两件事"）：

```bash
# 1. 备份
cp ~/.agentboard/collect.py ~/.agentboard/collect.py.bak-$(date +%Y%m%d)

# 2. 覆盖（从本仓库）
curl -fsSL https://raw.githubusercontent.com/xiaokamikami/agentboard-zcode/main/collect.py -o ~/.agentboard/collect.py

# 3. 停旧守护进程（必须，否则旧代码会把 state 翻转回去）
kill "$(cat ~/.agentboard/claude-sync.*.pid)" 2>/dev/null

# 4. 全量重扫（版本升级自动触发，幂等）
python3 ~/.agentboard/collect.py --sync --json

# 5. 重启守护进程并记录 PID
nohup python3 ~/.agentboard/collect.py --daemon >/dev/null 2>&1 &
echo $! > ~/.agentboard/claude-sync.$(hostname | tr -c 'A-Za-z0-9_.-' '_').pid
```

官方最新版可从安装脚本提取（脚本内嵌全部采集器源码，无远程下载）：找 `cat > "$COLLECT_FILE" <<'COLLECTEOF'` 与 `COLLECTEOF` 之间的内容。

### 可配置项

- `AGENTBOARD_ZCODE_DB`：ZCode/KCode 数据库路径覆盖（默认自动探测，见下）；也可在 `~/.agentboard/config.json` 里设置 `zcode_db_path`。
- `AGENTBOARD_DSH_HOME`：dsh 目录覆盖（默认 `~/.dsh`）；也可在 `~/.agentboard/config.json` 里设置 `dsh_home`。
- `AGENTBOARD_ZCODE_DAYS` / `AGENTBOARD_DSH_DAYS`：滑动窗口天数（默认 45），设得越大回溯的历史越多。改大之后下次同步会把窗口内新纳入的天自动补传（幂等）。
- `AGENTBOARD_ZSTD_BIN`：zstd 可执行文件路径覆盖（一般不需要；未设置时自动查找 PATH 及 homebrew 常见路径）。

### ZCode 更名为 KCode（2026-10）

ZCode 在 2026-10 更名为 **KCode**，数据库迁到 `~/.kcode/cli/db/db.sqlite`（旧库 `~/.zcode` 停止写入）。两个库表结构完全相同，新库是旧库的完整超集。采集器会自动选择**最近活跃**的库（比较主库与 `-wal`/`-shm` 的 mtime），无需任何配置即可跟随改名——如果两个库同时存在，跟随正在写入的那一个。若需强制指定，用 `AGENTBOARD_ZCODE_DB` 环境变量或 config 的 `zcode_db_path`。

### 回滚

恢复备份文件即可；ZCode/dsh 增量状态文件 `~/.agentboard/{zcode,dsh}-sync-state.*.json` 可一并删除（删除后下次全量重发）。

## 注意事项

- 网站上的数据是**同一账号下所有设备**的合计；单机验证请以本地 `--summary` 为准。
- 网站"今天"卡片约滞后一天聚合，日趋势图才是当天实时值。
- 单位换算：1 亿 = 100M = 0.1B，1B = 10 亿。
- 修改会话语义（如统计口径变化）时需递增脚本内对应的 `*_SYNC_STATE_VERSION`，强制一次全量重发让服务端覆盖旧数据。
- dsh 采集依赖 `zstd` 命令行工具（macOS 上 `brew install zstd`）；未安装时 dsh 部分自动跳过，不影响 Codex/ZCode 同步。
- 本补丁基于官方 Codex 采集器 2026-04-30 版；官方已更新到 2026-09-16 版（新增 shadow 测量、消息镜像等特性）。如未来要跟进官方新版，需要把 ZCode/dsh 采集块移植到新版上（我们独有的部分：`zcode_*` / `dsh_*` 系列函数、`sync_zcode` / `sync_dsh`、以及 `sync_mode` 里的两处调用），不要直接覆盖。

## 许可

仅供个人使用，随官方脚本行为演进，无兼容性承诺。
