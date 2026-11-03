#!/usr/bin/env bash
# Fetch and inspect the configured upstream; never merge or execute a delivery.
set -euo pipefail
cd "${1:-.}"
cd "$(git rev-parse --show-toplevel)"
BRANCH="$(git symbolic-ref --quiet --short HEAD)" || { echo '[inbox] ERROR: detached HEAD' >&2; exit 2; }
git rev-parse --verify HEAD >/dev/null
REMOTE_NAME="$(git config --get "branch.$BRANCH.remote" || true)"
REMOTE_NAME="${REMOTE_NAME:-origin}"
if [ "$REMOTE_NAME" != '.' ]; then
    # Respect configured proxy/network policy; do not silently retry direct access.
    git fetch "$REMOTE_NAME" || { echo '[inbox] ERROR: fetch failed; no result inferred' >&2; exit 1; }
fi
UP="$(git rev-parse --symbolic-full-name '@{upstream}' 2>/dev/null || true)"
if [ -z "$UP" ]; then
    if [ "$REMOTE_NAME" = '.' ]; then
        echo '[inbox] ERROR: local upstream is unresolved' >&2; exit 2
    fi
    UP="refs/remotes/$REMOTE_NAME/$BRANCH"
fi
LOCAL="$(git rev-parse HEAD)"
REMOTE="$(git rev-parse --verify "$UP^{commit}" 2>/dev/null)" || {
    echo "[inbox] ERROR: no upstream commit: $UP" >&2; exit 2;
}
printf '[inbox] local=%s remote=%s ref=%s\n' "$LOCAL" "$REMOTE" "$UP"
if [ "$LOCAL" = "$REMOTE" ]; then
    echo '[inbox] UP_TO_DATE: 无新提交'
elif git merge-base --is-ancestor "$LOCAL" "$REMOTE"; then
    echo '[inbox] REMOTE_AHEAD: 远端有新提交；核对作者和轮次后判断是否为绿区交付'
    git --no-pager log --oneline "$LOCAL..$REMOTE"
    echo '== 全部变更路径 =='
    git --no-pager diff --no-ext-diff --no-textconv --name-status "$LOCAL" "$REMOTE"
    echo '== 完整交接文档差异 =='
    git --no-pager diff --no-ext-diff --no-textconv "$LOCAL" "$REMOTE" -- pingpong.md history.md
    printf '[inbox] 授权同步时执行: git merge --ff-only %q\n' "$UP"
elif git merge-base --is-ancestor "$REMOTE" "$LOCAL"; then
    echo '[inbox] LOCAL_AHEAD: 本地有未推送提交；没有远端新交付'
    git --no-pager log --oneline "$REMOTE..$LOCAL"
else
    echo '[inbox] DIVERGED: 双方有独立提交；保留现场，不 force-push'
    git --no-pager log --oneline --left-right "$LOCAL...$REMOTE"
    exit 3
fi
