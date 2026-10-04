#!/bin/bash
# 重新安装打过补丁的 Codex 采集器（含 ZCode / dsh 采集 + 内存优化）。
#
# 使用场景：官方安装脚本 `curl -sL https://agentboard.cc/install | bash`
# 会无条件覆盖 ~/.agentboard/collect_codex.py（官方版不含 ZCode/dsh 支持），
# 重跑安装后执行本脚本即可把补丁装回来。
#
# 用法：bash reapply.sh
set -euo pipefail

REPO_RAW="https://raw.githubusercontent.com/xiaokamikami/agentboard-zcode/main/collect_codex.py"
AB="${AGENTBOARD_DIR:-$HOME/.agentboard}"
TARGET="$AB/collect_codex.py"
STAMP=$(date +%Y%m%d-%H%M%S)

if [ ! -d "$AB" ]; then
  echo "ERROR: $AB 不存在（本机未安装 AgentBoard？）" >&2
  exit 1
fi

# 1. 备份当前版本（无论是不是补丁版，先留一份）
if [ -f "$TARGET" ]; then
  cp -p "$TARGET" "$TARGET.bak-$STAMP"
  echo "已备份当前版本 -> $TARGET.bak-$STAMP"
fi

# 2. 下载补丁版
echo "下载补丁版 collect_codex.py ..."
curl -fsSL --retry 3 "$REPO_RAW" -o "$TARGET.new"

# 3. 校验确实是补丁版（防止下载到错误内容或官方版）
if ! grep -q "ZCODE_SYNC_STATE_VERSION" "$TARGET.new" || ! grep -q "dsh_collect_sessions" "$TARGET.new"; then
  rm -f "$TARGET.new"
  echo "ERROR: 下载的文件不含 ZCode/dsh 采集代码，已中止（未改动现有文件）" >&2
  exit 1
fi

# 4. 语法检查
python3 -m py_compile "$TARGET.new"
echo "语法检查通过"

# 5. 原子替换
mv "$TARGET.new" "$TARGET"
echo "补丁已安装 -> $TARGET"

# 6. 本地验证（不上传）
python3 "$TARGET" --summary --json 2>/dev/null | python3 -c '
import json, sys
d = json.load(sys.stdin)
zs = [s for s in d["sessions"] if s["session_id"].startswith("opencode:zcode:")]
ds = [s for s in d["sessions"] if s["session_id"].startswith("opencode:dsh:")]
print(f"验证: ZCode 会话日 {len(zs)} 条, dsh 会话日 {len(ds)} 条")
' || echo "（--summary 验证跳过；可直接跑 python3 $TARGET --sync --json 检查）"

echo
echo "完成。下一次 launchd 定时同步（每 5 分钟）会自动使用新采集器；"
echo "也可以手动跑一次： python3 $TARGET --sync --json"
