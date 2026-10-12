#!/usr/bin/env bash
#
# cwt-merge.sh — integrate a cwt worktree branch into master and clean up.
#
# Enforces the workflow: feature work happens on cwt branches in
# .claude/worktrees/*, and reaches production ONLY by fast-forward merge into
# master (the branch the deploy tree stays on). This runs an optional test
# gate and fast-forwards master. Follow with scripts/deploy.sh to actually ship.
#
# The worktree and branch are KEPT by default. cwt (Claude Code's worktree
# manager) owns the worktree lifecycle: the live session's shell is standing in
# that directory and the harness removes it itself on exit. Deleting it here
# yanks the cwd out from under the running session and breaks cwt's own
# cleanup. Pass --rm only for a branch no session is using.
#
# Usage:
#   scripts/cwt-merge.sh                 # merge the branch of the CURRENT worktree
#   scripts/cwt-merge.sh <branch>        # merge a specific branch
#   scripts/cwt-merge.sh --no-test       # skip the pytest gate
#   scripts/cwt-merge.sh --rm            # ALSO remove the worktree + delete the branch
#   scripts/cwt-merge.sh --keep          # (default; accepted for compatibility)
#
# The branch must fast-forward onto master. If it has diverged, run
# `git merge master` (or rebase) inside its worktree first, then retry.
#
# Exit codes:
#   0  merged (worktree + branch removed only with --rm)
#   1  refused: detached / on master / not a fast-forward / tests failed
#
set -euo pipefail

BRANCH=""
RUN_TESTS=1
KEEP=1
while [ $# -gt 0 ]; do
  case "$1" in
    --no-test) RUN_TESTS=0 ;;
    --rm)      KEEP=0 ;;
    --keep)    KEEP=1 ;;
    -h|--help) sed -n '2,29p' "$0"; exit 0 ;;
    -*) echo "cwt-merge: unknown flag: $1 (see --help)" >&2; exit 1 ;;
    *)  BRANCH="$1" ;;
  esac
  shift
done

BRANCH="${BRANCH:-$(git symbolic-ref --short HEAD 2>/dev/null || true)}"
[ -n "$BRANCH" ] || { echo "cwt-merge: no branch given and HEAD is detached" >&2; exit 1; }
if [ "$BRANCH" = master ]; then
  echo "cwt-merge: refusing to merge master into itself — run this from your cwt worktree." >&2
  exit 1
fi

# Deploy tree = the main worktree, which stays on master.
COMMON_GIT="$(git rev-parse --git-common-dir)"
DEPLOY_TREE="$(cd "$(dirname "$(readlink -f "$COMMON_GIT")")" && pwd)"
DEPLOY_BRANCH="$(git -C "$DEPLOY_TREE" symbolic-ref --short HEAD 2>/dev/null || echo DETACHED)"
if [ "$DEPLOY_BRANCH" != master ]; then
  echo "cwt-merge: deploy tree $DEPLOY_TREE is on '$DEPLOY_BRANCH', not master — fix that first." >&2
  exit 1
fi

# The worktree hosting $BRANCH, if any (needed for the test gate + cleanup).
SRC_WT="$(git worktree list --porcelain | awk -v b="branch refs/heads/$BRANCH" '
  /^worktree /{w=substr($0,10)} $0==b{print w}')"

if ! git merge-base --is-ancestor master "$BRANCH"; then
  echo "cwt-merge: '$BRANCH' is not a fast-forward onto master (it has diverged)." >&2
  echo "           In its worktree run:  git merge master   (or rebase onto master), then retry." >&2
  exit 1
fi

if [ "$RUN_TESTS" = 1 ]; then
  if [ -z "$SRC_WT" ]; then
    echo "cwt-merge: no worktree found for $BRANCH; skipping tests (use --no-test to silence)." >&2
  elif command -v uv >/dev/null 2>&1; then
    echo "cwt-merge: running test gate in $SRC_WT ..."
    ( cd "$SRC_WT" && uv run --extra dev pytest -q ) \
      || { echo "cwt-merge: tests failed — not merging. (--no-test to override)" >&2; exit 1; }
  else
    echo "cwt-merge: uv not found; skipping tests." >&2
  fi
fi

echo "cwt-merge: fast-forwarding master -> $BRANCH"
git -C "$DEPLOY_TREE" merge --ff-only "$BRANCH"

if [ "$KEEP" = 0 ]; then
  cd "$DEPLOY_TREE"   # never remove the worktree we're standing in
  if [ -n "$SRC_WT" ] && [ "$SRC_WT" != "$DEPLOY_TREE" ]; then
    git worktree remove "$SRC_WT" && echo "cwt-merge: removed worktree $SRC_WT"
  fi
  git branch -d "$BRANCH" && echo "cwt-merge: deleted branch $BRANCH"
else
  echo "cwt-merge: kept worktree + branch $BRANCH — exit it via cwt when done (or re-run with --rm)."
fi

echo "cwt-merge: master now at $(git -C "$DEPLOY_TREE" rev-parse --short HEAD). Run scripts/deploy.sh to ship."
