"""Shared FTS5 query-shape helper for the search tools (history + docs).

`_plain_terms` decides whether a zero-hit query may be retried OR-joined
(doc #54 Decision #9 / UoW-27 step 4; doc #59 UoW-35 step 4 reuses the
identical rule so the model has one mental model across both tools).
"""

from __future__ import annotations

# Characters / keywords that mean the caller is using FTS5 syntax
# deliberately — such a query is never rewritten by the OR-broadening
# fallback (see `_plain_terms`).
_FTS_SYNTAX_CHARS = set('"()*:^-')
_FTS_KEYWORDS = {"AND", "OR", "NOT", "NEAR"}


def _plain_terms(query: str) -> list[str] | None:
    """The query's terms iff it is a plain multi-word bag of words.

    FTS5 treats `itchy rash bug bites` as an implicit AND — every term
    must appear in one message, so each extra word silently narrows the
    search until a real hit ("oak leaf itch mites") vanishes behind one
    absent word. Those queries are safe to broaden to OR on zero hits.
    Single terms need no broadening, and anything using FTS5 syntax
    (quotes, operators, column filters) is left exactly as written.
    """
    if any(ch in _FTS_SYNTAX_CHARS for ch in query):
        return None
    terms = query.split()
    if len(terms) < 2 or any(t in _FTS_KEYWORDS for t in terms):
        return None
    return terms


def _or_expr(terms: list[str]) -> str:
    """OR-join terms, each double-quoted so operator characters stay literal."""
    return " OR ".join('"' + t.replace('"', '""') + '"' for t in terms)
