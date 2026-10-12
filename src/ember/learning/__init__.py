"""Learning loop Layer 3–4 helpers (v12.5).

Deterministic, LLM-free surfaces the learning loop needs on top of the store:
`digest.format_digest` renders the weekly approval digest; `appliers` dispatch
an approved proposal through the existing writer paths (FC-23). SQL stays in
`store/repo.py` (FC-7); these modules only shape/route.
"""
