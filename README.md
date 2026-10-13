# Ember

A self-hosted personal AI agent. Ember lives in a private Discord server (plus
an optional Siri voice channel), runs scheduled skills, and has tools for
email, calendar, documents, recipes, the web, and a brokerage account. It runs
as one Docker container on a home server.

It's a hobby project, but because I want to trust it with sensitive info, it needed to be well-specced:
written contracts before code, invariants enforced in tests (~30k lines of
source and ~55k lines of tests), and a learning loop that measures whether the
agent is actually getting better.

> This is a public snapshot of a repo I use every day. The commit history and
> my personal skills and config were left out. The code below is the real
> thing.

## What's interesting here

**Provider routing and failover.** A turn goes to a route string
(`claude_cli:opus`, `anthropic_api:claude-sonnet-…`, `deepseek_api:…`). A
skill's preferred model, a task tier, or a per-conversation override picks the
route. Internal work (rotation handoffs, retrospective extraction, eval
judging) goes to a separate *cheap tier*, and a rate-limited primary falls
over to a secondary provider. See `src/ember/providers/`.

**Context lifecycle without mid-conversation summarization.** When a
conversation goes idle or its measured context passes a threshold, the *next*
turn starts on a fresh session that carries a short handoff brief. The live
context is never summarized in place, and every failure path is non-fatal: the
turn continues on the old session and rotation retries later. See
`src/ember/context/rotation.py`.

**Learning loop: feedback → evals → retrospective.** 👍/👎 reactions and
corrections are captured as structured feedback. A nightly retrospective turns
failed runs, silent tool errors, and user friction into *proposals*, never
direct changes. Once I approve a proposal, it goes through the same writers as
a manual edit (git-committed and revertible), and it can never touch the
agent's core persona or its source code. Evidence may only come from what the
user wrote or from recorded failures; the agent's own output never counts as
evidence. An eval harness (routing, tone, and tool-flow cases, with a config
fingerprint per run) checks whether a proposed change actually helps. See
`src/ember/learning/`, `src/ember/evals/`, `data/skills/retrospective.py`.

**Guardrails on tools that can do damage.** Tools can require
confirmation. A `y` typed into the same chat that proposed an action proves
nothing, so sensitive tools also need a TOTP code from a separate device
(RFC 6238, stdlib only, with replay protection). *Unlock windows* let one code
open a conversation for a set time instead of prompting on every call, and a
restart re-locks everything. The brokerage tools add per-order, per-batch, and
rolling daily notional caps and an account allowlist. The design is in
[`docs/brokerage-contracts.md`](docs/brokerage-contracts.md); see also
`src/ember/totp.py` and `src/ember/unlock.py`.

**Skills.** Skills come in three forms: declarative TOML (a focused
conversational mode with a tool allowlist), Markdown `SKILL.md`, and
imperative Python modules with cron-scheduled entry points. They hot-reload
from disk. A router prompt suggests the right skill, and a `skill_author`
meta-skill helps write new ones. Scheduled runs are recorded with
misfire-grace and coalescing semantics. See `src/ember/skills/`,
`src/ember/scheduler/`, `data/skills/`.

**LLM output is data, not control flow.** Wherever a model's answer feeds a
decision (retrospective extraction, digests, eval verdicts), the reply is
schema-validated and the surrounding pipeline is deterministic Python. A
malformed result is skipped and counted, never retried with looser rules.

## Layout

```
src/ember/
  agent.py        the turn loop
  channels/       Discord, Siri (HTTP), a mux, and a per-conversation turn gate
  providers/      claude CLI, Anthropic API, DeepSeek, and the router
  context/        session rotation, tool-output pruning, interrupts
  tools/          ~30 tools: gmail (read/search/draft/send), iCloud calendar
                  and reminders, paperless docs, mealie, web fetch/browse/search,
                  schwab, an MCP client, a secrets vault…
  skills/         loader (TOML / Markdown / Python), registry, router prompt
  scheduler/      cron runner, run recorder, notifier
  learning/       retrospective digest + proposal appliers
  evals/          cases, drivers, judge, runner, fingerprinting
  identity/       soul/profile/lessons files under git, with a proposer
  store/          SQLite (conversations, runs, proposals, evals), migrations
data/skills/      example skills (the retrospective lives here)
tests/            unit + integration, with fakes for every external service
docs/             contracts and the branch/deploy workflow
```

## How it's built

Most of Ember was built with Claude Code agents working in parallel git
worktrees. `CLAUDE.md` and
[`docs/branch-and-deploy-workflow.md`](docs/branch-and-deploy-workflow.md)
describe the guardrails that make that safe: one deployable trunk, merges
gated by tests, SHA-stamped images, and one-command rollback. Identifiers like
`TLC-n`, `UoW-n`, `FC-n`, and `doc #n` in comments point back to the design
documents (invariants, units of work, feature contracts) that each change was
written against.

## Running it

```
cp .env.example .env      # Discord bot token + IDs; every other integration is optional
docker compose up -d
docker compose exec agent claude login   # first run only
```

Each integration (Gmail, iCloud, paperless, Mealie, Schwab, …) only registers
its tools when its credentials are set in `.env`.

Tests: `uv run --extra dev pytest -q`

## License

MIT
