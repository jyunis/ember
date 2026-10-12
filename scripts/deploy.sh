#!/usr/bin/env bash
#
# deploy.sh — build and (re)start Ember from the canonical deploy tree.
#
# The container is image-built (not bind-mounted), so what runs is whatever was
# committed on `master` in the MAIN worktree at build time. This is the only
# supported way to ship: it refuses to build from a drifted or dirty tree,
# stamps the image with the git SHA (EMBER_IMAGE_VERSION, which feeds the eval
# fingerprint), keeps a rollback tag (ember:<sha>), and restarts the container.
#
# Runs the same no matter where it is invoked from — it always locates the main
# worktree and operates there, so a cwt worktree can never build itself.
#
# Usage:
#   scripts/deploy.sh                 # build master @ HEAD, tag, restart (prompts)
#   scripts/deploy.sh -y              # unattended (skip the confirm prompt)
#   scripts/deploy.sh --no-build      # just restart the current image
#   scripts/deploy.sh --rollback SHA  # retag ember:<SHA> as latest and restart
#   scripts/deploy.sh --allow-dirty   # build despite uncommitted changes (unsafe)
#
# Exit codes:
#   0  deployed (or rolled back)
#   1  refused: not on master / dirty tree / bad args / build failure
#
set -euo pipefail

SELF="$(readlink -f "$0")"   # absolute path, captured before we cd elsewhere
IMAGE="ember"
# Rollback tags (ember:<sha>) to keep after a successful build; older ones are
# untagged. Each is ~2 GB and / is a 110 GB SSD — 46 had piled up by 2026-09-29.
KEEP_BUILDS=5

ASSUME_YES=0
DO_BUILD=1
ALLOW_DIRTY=0
ROLLBACK_SHA=""

while [ $# -gt 0 ]; do
  case "$1" in
    -y|--yes)      ASSUME_YES=1 ;;
    --no-build)    DO_BUILD=0 ;;
    --allow-dirty) ALLOW_DIRTY=1 ;;
    --rollback)    ROLLBACK_SHA="${2:?--rollback needs a SHA}"; shift ;;
    -h|--help)     sed -n '2,26p' "$SELF"; exit 0 ;;
    *) echo "deploy: unknown arg: $1 (see --help)" >&2; exit 1 ;;
  esac
  shift
done

confirm() {
  [ "$ASSUME_YES" = 1 ] && return 0
  [ -t 0 ] || return 0   # non-interactive shell: proceed
  local a; read -r -p "$1 [y/N] " a; [ "$a" = y ] || [ "$a" = Y ]
}

# Always operate on the MAIN worktree (the deploy tree), regardless of where
# this copy of the script lives — a cwt worktree must not build itself.
COMMON_GIT="$(git rev-parse --git-common-dir 2>/dev/null)" || {
  echo "deploy: not inside a git repository" >&2; exit 1; }
DEPLOY_TREE="$(cd "$(dirname "$(readlink -f "$COMMON_GIT")")" && pwd)"
cd "$DEPLOY_TREE"

# --- rollback path -----------------------------------------------------------
if [ -n "$ROLLBACK_SHA" ]; then
  if ! docker image inspect "$IMAGE:$ROLLBACK_SHA" >/dev/null 2>&1; then
    echo "deploy: no local image $IMAGE:$ROLLBACK_SHA to roll back to" >&2
    echo "        available: $(docker images "$IMAGE" --format '{{.Tag}}' | tr '\n' ' ')" >&2
    exit 1
  fi
  confirm "Roll Ember back to $IMAGE:$ROLLBACK_SHA (restarts the container)?" || { echo "aborted"; exit 0; }
  docker tag "$IMAGE:$ROLLBACK_SHA" "$IMAGE:latest"
  docker compose up -d
  echo "deploy: rolled back to $ROLLBACK_SHA"
  exit 0
fi

# --- guardrails --------------------------------------------------------------
BRANCH="$(git symbolic-ref --short HEAD 2>/dev/null || echo DETACHED)"
if [ "$BRANCH" != master ]; then
  echo "deploy: deploy tree $DEPLOY_TREE is on '$BRANCH', not master." >&2
  echo "        The deploy tree must stay on master; integrate via scripts/cwt-merge.sh." >&2
  exit 1
fi
if [ "$ALLOW_DIRTY" = 0 ] && [ -n "$(git status --porcelain)" ]; then
  echo "deploy: deploy tree has uncommitted changes — the SHA stamp would not match the built image:" >&2
  git status --short >&2
  echo "        Commit or stash them, or pass --allow-dirty (stamp becomes unreliable)." >&2
  exit 1
fi

SHA="$(git rev-parse --short HEAD)"
echo "deploy: master @ $SHA — $(git log -1 --pretty=%s)"

if [ "$DO_BUILD" = 1 ]; then
  confirm "Build $IMAGE:$SHA and restart Ember (brief downtime, kills in-flight work)?" \
    || { echo "aborted"; exit 0; }
  EMBER_GIT_SHA="$SHA" docker compose build
  docker tag "$IMAGE:latest" "$IMAGE:$SHA"
else
  confirm "Restart Ember on the current image (brief downtime)?" || { echo "aborted"; exit 0; }
fi

docker compose up -d

# Record the deploy (data/ is the mounted volume, outside git).
if [ -d "$DEPLOY_TREE/data" ]; then
  printf '%s\t%s\t%s\n' "$(date -u +%FT%TZ)" "$SHA" "$(git log -1 --pretty=%s)" \
    >> "$DEPLOY_TREE/data/deploy.log"
fi

# Prune old rollback tags: keep the newest $KEEP_BUILDS ember:<sha> by image
# creation time, plus anything sharing latest's image ID (so a rolled-back
# latest never loses its sha tag). latest/prev are never touched.
if [ "$DO_BUILD" = 1 ]; then
  LATEST_ID="$(docker image inspect -f '{{.Id}}' "$IMAGE:latest")"
  docker images "$IMAGE" --format '{{.Tag}}' | grep -vxE 'latest|prev' \
    | while read -r t; do
        printf '%s %s %s\n' "$(docker image inspect -f '{{.Created}} {{.Id}}' "$IMAGE:$t")" "$t"
      done \
    | sort -r | tail -n +"$((KEEP_BUILDS + 1))" \
    | while read -r _ id t; do
        [ "$id" = "$LATEST_ID" ] && continue
        docker rmi "$IMAGE:$t" >/dev/null && echo "deploy: pruned old rollback tag $IMAGE:$t"
      done
fi

docker compose ps
echo "deploy: Ember up on $IMAGE:$SHA"
