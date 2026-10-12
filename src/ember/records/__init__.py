"""Records data plane (doc #59 — life-memory slice 1).

`records.db` is a separate SQLite file from `conversations.db` (Decision
#3): a derived, rebuildable mirror of paperless-ngx today, and the file
later sources (email, transactions) land in. Nothing in this package
imports providers or the router — every ingest path is deterministic
plumbing with zero LLM calls (TLC-29).

- `store`     — `RecordsStore` (FTS5 index, own schema ladder, FC-35)
- `paperless` — `PaperlessClient` (httpx sidecar client, FC-34)
- `indexer`   — `DocumentIndexer` (scheduled sync + `index_one`, UoW-33/34)
- `types`     — frozen row dataclasses
"""
