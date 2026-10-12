"""Skills module (v6 — imperative only).

Imperative skills are Python modules under `/data/skills/` loaded via
`importlib`. They expose a fixed set of module-level attributes plus
optional coroutine functions referenced by `scheduled_runs`.

Declarative (manifest) skills, `pre_turn`/`post_turn` hooks, and the
`/skill` command land in v7. See architecture doc § Skills and the
v6 milestone in § Phased build.
"""
