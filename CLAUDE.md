# Ember — agent working notes

Ember is an autonomous assistant (Discord bot + scheduled jobs) running as a
single Docker container. Source is Python under `src/ember/`. The container is
**image-built** (`docker compose build`), not bind-mounted — code only reaches
production through a rebuild + restart.

## Branch & deploy workflow (READ THIS FIRST)

`master` is trunk **and** the only deployable branch. The main worktree
(`/srv/ember`) stays on `master` permanently and is the only tree the
container builds from. All work happens in cwt worktrees under
`.claude/worktrees/<name>/` on `claude/cwt-<name>` branches.

If you are an agent working in a cwt worktree:

1. **Work on your branch. Commit here.** Never `git checkout` another branch in
   the main worktree, and never commit directly on `master` except merges.
2. **Ship via the scripts, never by hand:**
   - `scripts/cwt-merge.sh` — fast-forwards your branch into `master` (with a
     test gate). It keeps your worktree and branch: cwt owns their lifecycle and
     removes them when you exit the worktree. `--rm` only for orphaned branches.
   - `scripts/deploy.sh` — builds a SHA-stamped image (`ember:<sha>`, sets
     `EMBER_IMAGE_VERSION`) from the master deploy tree and restarts Ember.
3. **Deploying restarts the container** — brief downtime that kills whatever is
   in flight (including a live conversation). Only deploy when asked / it's safe.
4. **Never `docker compose build` by hand from a cwt worktree** — it would build
   your branch's tree, not master. `deploy.sh` always targets the master deploy
   tree no matter where you run it.

Keep cwt branches short-lived and branched off current `master`; merge back
promptly and exit the worktree through cwt so it gets cleaned up (don't let
stale branches/worktrees pile up). Roll back with
`scripts/deploy.sh --rollback <sha>`.

Full rationale and the reasoning behind each guardrail:
[`docs/branch-and-deploy-workflow.md`](docs/branch-and-deploy-workflow.md).

## Dev

- Tests: `uv run --extra dev pytest -q`. Pre-commit runs ruff + ruff-format.
- Secrets/config: `.env` (git-ignored) — browserless, Brave, DeepSeek, Mealie, …
- Runtime state (SQLite, claude auth) lives in `./data` (mounted volume, not in git).
- Backups: the host's nightly restic job (`/usr/local/bin/backup-host`,
  `backup-host.timer`, 03:00) takes a transactionally-consistent
  `sqlite3 .backup` of `data/conversations.db` into `./dump/` (gitignored) and
  **excludes the live `conversations.db{,-wal,-shm}`** from the snapshot — the
  DB is WAL-mode and a raw file walk of it can be torn. `dump/conversations.db`
  is the restore path: stop the container, copy it over `data/conversations.db`
  (removing any stale `-wal`/`-shm`), start the container.
