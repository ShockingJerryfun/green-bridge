#!/usr/bin/env bash
# init_blue.sh — 在目标仓库初始化蓝绿 pingpong 协作（蓝区侧，每仓库一次）
# 用法: bash init_blue.sh <repo_path> [branch]
#
# 生成/更新：
#   pingpong.md   当前任务与状态（模板首建，不覆盖已有）
#   history.md    轮次档案（模板首建，不覆盖已有）
#   AGENTS.md     绿区守则（幂等 upsert：标记块内替换，仓库已有内容保留）
# 之后蓝区 commit + push，绿区只需一句话接入（见末尾输出）。
set -euo pipefail
SKILL_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
REPO="${1:?usage: init_blue.sh <repo_path> [branch]}"
BRANCH="${2:-}"

cd "$REPO" || { echo "[init] ERROR: no such dir: $REPO" >&2; exit 2; }
git rev-parse --git-dir >/dev/null 2>&1 || { echo "[init] ERROR: not a git repo: $REPO" >&2; exit 2; }
cd "$(git rev-parse --show-toplevel)"

CUR_BRANCH="$(git symbolic-ref --quiet --short HEAD)" || { echo '[init] ERROR: detached HEAD' >&2; exit 2; }
if [ -n "$BRANCH" ] && [ "$BRANCH" != "$CUR_BRANCH" ]; then
    echo "[init] ERROR: current branch is '$CUR_BRANCH', requested '$BRANCH'; no files changed" >&2
    exit 2
fi
REMOTE_URL="$(git remote get-url origin 2>/dev/null || echo '(no origin remote)')"

# Validate existing markers before changing any files.
BEGIN='<!-- blue-green-pingpong:green-rules:start -->'
END='<!-- blue-green-pingpong:green-rules:end -->'
for target in AGENTS.md pingpong.md history.md; do
    if [ -L "$target" ] || { [ -e "$target" ] && [ ! -f "$target" ]; }; then
        echo "[init] ERROR: expected a regular file or absent path: $target" >&2; exit 2
    fi
done
if [ -f AGENTS.md ]; then
    awk -v begin="$BEGIN" -v end="$END" '
        $0 == begin { starts++; if (inside || ends) bad=1; inside=1 }
        $0 == end { ends++; if (!inside) bad=1; inside=0 }
        END { if (bad || inside || starts != ends || starts > 1) exit 1 }
    ' AGENTS.md || { echo '[init] ERROR: malformed collaboration markers; no files changed' >&2; exit 2; }
fi

# 1) pingpong.md / history.md（不覆盖已有）
for pair in "pingpong.md:pingpong.template.md" "history.md:history.template.md"; do
    target="${pair%%:*}"; tpl="${pair##*:}"
    if [ -e "$target" ]; then
        echo "[init] exists, skipped: $target"
    else
        cp "$SKILL_DIR/assets/$tpl" "$target"
        echo "[init] created: $target"
    fi
done

# 2) AGENTS.md 绿区守则（幂等 upsert）
BLOCK="$(mktemp)"
UPDATED=''
trap 'rm -f "$BLOCK"; if [ -n "$UPDATED" ]; then rm -f "$UPDATED"; fi' EXIT
{
    echo "$BEGIN"
    echo
    cat "$SKILL_DIR/assets/agents_green.template.md"
    echo
    echo "$END"
} > "$BLOCK"
if [ -e AGENTS.md ] && grep -qxF "$BEGIN" AGENTS.md; then
    UPDATED="$(mktemp ./.pingpong-agents.XXXXXX)"
    cp -p AGENTS.md "$UPDATED"
    awk -v blockfile="$BLOCK" -v begin="$BEGIN" -v end="$END" '
        $0 == begin { while ((getline line < blockfile) > 0) print line; close(blockfile); skip=1; next }
        skip && $0 == end { skip=0; next }
        !skip { print }
    ' AGENTS.md > "$UPDATED" && mv "$UPDATED" AGENTS.md
    echo "[init] updated green-rules block in: AGENTS.md"
elif [ -e AGENTS.md ]; then
    { echo; cat "$BLOCK"; } >> AGENTS.md
    echo "[init] appended green-rules block to existing: AGENTS.md"
else
    { echo "# AGENTS.md"; echo; cat "$BLOCK"; } > AGENTS.md
    echo "[init] created: AGENTS.md"
fi
# 3) 远端连通性（只读；尊重已有网络/代理配置）
if [ "$REMOTE_URL" != "(no origin remote)" ]; then
    if git ls-remote origin HEAD >/dev/null 2>&1; then
        echo "[init] remote reachable: $REMOTE_URL"
    else
        echo "[init] WARNING: remote unreachable now: $REMOTE_URL（初始化可继续，推送时再查网络/代理）"
    fi
fi

echo
echo "[init] done. repo=$REPO branch=$CUR_BRANCH"
echo "[init] 蓝区下一步: git add AGENTS.md pingpong.md history.md && git commit -s -m 'init blue-green pingpong' && git push"
echo
echo "[init] 给绿区的接入消息（复制这句话发给对侧即可，绿区零安装）："
if [ "$REMOTE_URL" != '(no origin remote)' ]; then
    printf '  git clone -b %q %q\n' "$CUR_BRANCH" "$REMOTE_URL"
    echo '  进入仓库后告知代理：你是绿区执行代理。读取根 AGENTS.md 和 pingpong.md 的 Current round，按当前派单执行并回传；没有派单则等待。'
else
    echo '  尚无 origin；先确定共享远端再生成 clone 接入消息。'
fi
