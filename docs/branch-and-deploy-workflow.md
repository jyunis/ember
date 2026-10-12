# Branch & deploy workflow

## The principle

**One integration branch is the only thing that deploys, and it never leaves
that branch. All work flows into it.**

- `master` is trunk **and** the deployable branch.
- The **main worktree** (`/srv/ember`) stays on `master`
  permanently. It is the only tree `docker compose build` builds from (the
  container is image-built, not bind-mounted — see `compose.yml`).
- Parallel work happens in **cwt worktrees** under `.claude/worktrees/<name>/`,
  each on its own `claude/cwt-<name>` branch.

## Why (the failure mode this prevents)

The container ships whatever is committed on the branch the main worktree
happens to be on. When that tree was allowed to sit on a *feature* branch,
commits landed there instead of on `master`, `master` silently fell behind what
was actually running, and stale branches/worktrees piled up. Pinning the deploy
tree to `master` and routing every change through a fast-forward merge removes
that entire class of drift.

## Lifecycle of a change

```
spawn cwt worktree (off current master)
        │   claude/cwt-<task>
        ▼
   work + commit  ── in .claude/worktrees/<task>/
        │
        ▼
   scripts/cwt-merge.sh        # test gate → FF master (worktree + branch kept)
        │
        ▼
   exit the worktree via cwt   # cwt removes the worktree + branch itself
        │
        ▼
   scripts/deploy.sh           # build ember:<sha> → restart Ember
```

1. **Spawn** the worktree off current `master` (not off another feature branch —
   that is how branches end up dozens of commits behind).
2. **Work and commit** on your `claude/cwt-<task>` branch.
3. **Refresh before integrating** if `master` moved: `git merge master` (or
   rebase) inside your worktree so the merge is a clean fast-forward.
4. **Integrate** with `scripts/cwt-merge.sh` — it fast-forwards `master` in the
   deploy tree and leaves your worktree and branch in place.
5. **Exit the worktree through cwt** (remove) — cwt owns worktree lifecycle and
   cleans up the directory and branch itself.
6. **Deploy** with `scripts/deploy.sh` when it's safe to restart.

## The scripts

### `scripts/cwt-merge.sh [branch]`
Integrates a cwt branch into `master`. Refuses unless the deploy tree is on
`master` and the branch fast-forwards onto it. Runs `pytest` in the source
worktree as a gate (`--no-test` to skip) and fast-forwards `master`. The
worktree and branch are **kept** by default; `--rm` removes both, for branches
no session is using. Locates the deploy tree itself, so it works from any
worktree.

Why keep is the default: cwt (Claude Code's worktree manager) created the
worktree, the session's shell is standing in it, and cwt removes it on exit.
When the script deleted it instead, the live session lost its working directory
and cwt's own cleanup had nothing left to remove.

### `scripts/deploy.sh`
Builds and restarts Ember from the `master` deploy tree. Invariants it enforces:

- **Must be on `master`** — refuses to deploy a drifted tree.
- **Must be clean** — a dirty tree means the stamped `EMBER_IMAGE_VERSION` would
  not match the built image (README/src get `COPY`'d in). `--allow-dirty` to
  override.
- Stamps `EMBER_GIT_SHA` → `EMBER_IMAGE_VERSION`, tags the built image
  `ember:<sha>` (and `ember:latest`), restarts, and appends to `data/deploy.log`.

Restarting kills in-flight work (Ember goes down for a few seconds). Prompts
before doing so unless `-y`. Roll back with `scripts/deploy.sh --rollback <sha>`
(retags a prior `ember:<sha>` as latest and restarts — no rebuild).

## Deploy granularity

We deploy `master @ HEAD` and rely on per-SHA image tags (`ember:<sha>`) for the
"exactly what's running" property and for one-command rollback. No separate
`release`/`deploy` branch — add one only if you later want to batch several
merged changes and ship them deliberately (integrate ≠ ship).

## Hygiene

- cwt worktrees live under `.claude/worktrees/` — git-ignored and
  docker-ignored, so they never enter a commit or the build context (they were
  adding ~200 MB per build otherwise).
- Worktrees and branches are removed by cwt when you exit them (or by
  `cwt-merge.sh --rm` for orphaned ones). Periodically prune anything that
  slipped through:
  `git worktree prune` and `git branch --merged master | grep -v ' master$'`.
