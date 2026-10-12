#!/usr/bin/env bash
#
# upgrade-claude.sh — deliberately bump the pinned claude CLI version, but
# only KEEP it if it still works with Ember's MCP bridge.
#
# The claude CLI is version-pinned in the Dockerfile (CLAUDE_CODE_VERSION)
# because unpinned upgrades have repeatedly broken Ember silently:
#   - stateless-session tool registration → "No such tool available: mcp__agent__*"
#   - ToolSearch deferral hiding the agent tools → "No such tool available: ToolSearch"
# This script rebuilds with a candidate version, runs a smoke test that
# exercises exactly those failure modes against the live bridge, and rolls
# back automatically if anything regresses.
#
# Usage:
#   scripts/upgrade-claude.sh                 # try latest published version
#   scripts/upgrade-claude.sh 2.1.191         # try a specific version
#   scripts/upgrade-claude.sh --commit --notify   # auto-commit the pin + Discord alert
#   TRIALS=8 scripts/upgrade-claude.sh        # more smoke-test iterations (default 5)
#
# Exit codes:
#   0  upgraded (or already on target)
#   2  candidate failed the smoke test → rolled back, pin unchanged (a clean
#      decline, not an error — systemd treats this via SuccessExitStatus=2)
#   1  unexpected/infra error → rolled back to the prior pin where possible
#
# Run as root for --notify (reads root-only /etc/notify-discord.conf); the
# git commit is made as the repo owner (the checkout's owner) via runuser.

set -euo pipefail

cd "$(dirname "$0")/.."          # repo root (scripts/ is one level down)
DOCKERFILE="Dockerfile"
REPO="$(pwd)"
TRIALS="${TRIALS:-5}"
DO_COMMIT=0
DO_NOTIFY=0
VERSION_ARG=""

while [ $# -gt 0 ]; do
  case "$1" in
    --commit) DO_COMMIT=1; shift;;
    --notify) DO_NOTIFY=1; shift;;
    -*) echo "unknown flag: $1" >&2; exit 1;;
    *)  VERSION_ARG="$1"; shift;;
  esac
done

err()  { printf '\033[31m%s\033[0m\n' "$*" >&2; }
ok()   { printf '\033[32m%s\033[0m\n' "$*"; }
info() { printf '\033[36m%s\033[0m\n' "$*"; }

# Discord alert (scheduled context only). notify-discord no-ops if it can't
# read the token, so this is safe to call unconditionally when --notify is set.
notify() {  # notify <info|warn|crit> <message>
  [ "$DO_NOTIFY" = 1 ] || return 0
  command -v notify-discord >/dev/null 2>&1 || return 0
  notify-discord -t "claude upgrade" -l "$1" "$2" || true
}

git_owner() {  # run git as the repo owner so .git keeps its original owner
  local owner; owner="$(stat -c '%U' "$REPO/.git")"
  if [ "$(id -un)" = "$owner" ]; then git -C "$REPO" "$@"
  else runuser -u "$owner" -- git -C "$REPO" "$@"; fi
}

CURRENT="$(grep -oE 'ARG CLAUDE_CODE_VERSION=[0-9.]+' "$DOCKERFILE" | cut -d= -f2)"
[ -n "$CURRENT" ] || { err "Could not read current pin from $DOCKERFILE"; exit 1; }

# Default target = latest published, queried via the (currently running) ember
# container's npm so we don't need npm on the host or an extra image pull.
TARGET="${VERSION_ARG:-$(docker exec ember npm view @anthropic-ai/claude-code version 2>/dev/null | tr -d '[:space:]' || true)}"
[ -n "$TARGET" ] || { err "Could not determine target version (pass one explicitly)"; exit 1; }

info "Current pin: $CURRENT   Target: $TARGET"
if [ "$CURRENT" = "$TARGET" ]; then ok "Already on $TARGET — nothing to do."; exit 0; fi

# From here on, any unexpected failure rolls back to the known-good pin.
rollback_build() {
  docker compose build --build-arg "CLAUDE_CODE_VERSION=$CURRENT" >/dev/null 2>&1 || true
  docker compose up -d >/dev/null 2>&1 || true
}
on_error() {
  trap - ERR
  err "Unexpected error upgrading to $TARGET — rolling back to $CURRENT"
  rollback_build
  exit 1   # systemd OnFailure handles the generic alert
}
trap on_error ERR

info "Building candidate image with claude $TARGET ..."
docker compose build --build-arg "CLAUDE_CODE_VERSION=$TARGET"
docker compose up -d

info "Waiting for the MCP bridge ..."
for _ in $(seq 1 30); do
  docker logs ember 2>&1 | grep -q "MCP bridge listening" && break || sleep 2
done

GOT="$(docker exec ember claude --version 2>/dev/null | grep -oE '[0-9]+\.[0-9]+\.[0-9]+' | head -1)"
[ "$GOT" = "$TARGET" ] || { err "Container reports claude $GOT, expected $TARGET"; false; }
ok "Container running claude $GOT"

ALLOW="$(docker exec ember bash -lc 'cd /app && uv run python - <<PY 2>/dev/null
import asyncio
from mcp.client.streamable_http import streamablehttp_client
from mcp import ClientSession
async def main():
    async with streamablehttp_client("http://127.0.0.1:9931/mcp") as (r,w,_):
        async with ClientSession(r,w) as s:
            await s.initialize(); t=await s.list_tools()
            print(",".join("mcp__agent__"+x.name for x in t.tools))
asyncio.run(main())
PY')"
NTOOLS="$(echo "$ALLOW" | tr ',' '\n' | grep -c mcp__agent__ || true)"
[ "$NTOOLS" -ge 10 ] || { err "Bridge exposed only $NTOOLS tools — unhealthy"; false; }
info "Bridge exposes $NTOOLS agent tools"

# Smoke test under Ember's REAL invocation shape (full allowlist, ToolSearch
# disallowed, system prompt). Assert per trial:
#   - agent tools shown directly, NOT deferred  (guards ToolSearch regression)
#   - web_fetch actually returns 200            (guards stateful-attach regression)
trap - ERR   # smoke-test outcome is decided explicitly below, not via the trap
SYS="$(printf 'You are Ember, a personal assistant. %.0s' {1..200})"
fails=0
for i in $(seq 1 "$TRIALS"); do
  out="$(docker exec ember bash -lc "cd /app && timeout 60 claude --print --output-format stream-json --verbose \
    --mcp-config /data/agent-mcp.json --allowed-tools '$ALLOW' \
    --disallowed-tools 'WebFetch,WebSearch,Task,ToolSearch,Skill,TodoWrite' \
    --permission-mode bypassPermissions --append-system-prompt '$SYS' \
    -p 'Use web_fetch on https://example.com and report only the status_code.' 2>&1" || true)"
  shown="$(printf '%s' "$out" | python3 -c 'import sys,json
n=0
for l in sys.stdin:
    try: e=json.loads(l)
    except: continue
    if e.get("subtype")=="init":
        n=len([t for t in e.get("tools",[]) if "agent" in t]); break
print(n)' 2>/dev/null || echo 0)"
  if printf '%s' "$out" | grep -q "No such tool available"; then
    err "  trial $i: FAIL — 'No such tool available' (registration/deferral regressed)"; fails=$((fails+1))
  elif [ "${shown:-0}" -eq 0 ]; then
    err "  trial $i: FAIL — agent tools deferred (0 shown; ENABLE_TOOL_SEARCH not honored)"; fails=$((fails+1))
  elif printf '%s' "$out" | grep -qE '\b200\b'; then
    ok  "  trial $i: ok (web_fetch 200, $shown tools shown)"
  else
    err "  trial $i: FAIL — no 200 from web_fetch"; fails=$((fails+1))
  fi
done

if [ "$fails" -ne 0 ]; then
  err "Smoke test FAILED ($fails/$TRIALS) on $TARGET — rolling back to $CURRENT"
  rollback_build
  notify warn "claude $TARGET failed smoke test ($fails/$TRIALS) — rolled back to $CURRENT, pin unchanged."
  exit 2
fi

# Passed — persist the new pin and (optionally) commit it.
sed -i "s/ARG CLAUDE_CODE_VERSION=$CURRENT/ARG CLAUDE_CODE_VERSION=$TARGET/" "$DOCKERFILE"
ok "Smoke test passed ${TRIALS}/${TRIALS}. Pinned claude $CURRENT -> $TARGET."
if [ "$DO_COMMIT" = 1 ]; then
  git_owner add Dockerfile
  git_owner commit -m "Bump claude CLI to $TARGET (monthly, smoke-tested)" >/dev/null
  ok "Committed the pin bump."
  notify info "claude CLI upgraded $CURRENT -> $TARGET (smoke test ${TRIALS}/${TRIALS}, committed)."
else
  info "Review and commit:  git -C $REPO add Dockerfile && git commit -m 'Bump claude CLI to $TARGET'"
  notify info "claude CLI upgraded $CURRENT -> $TARGET (smoke test ${TRIALS}/${TRIALS}). Dockerfile pin updated — commit it."
fi
