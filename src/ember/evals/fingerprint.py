"""Config fingerprint (doc #36 § Config fingerprint / UoW-21).

Two runs are comparable iff you can see exactly what differed. The fingerprint
captures the four axes that change routing/tone behavior:

- identity content hash — a content-sensitive digest over the identity files the
  agent actually reads (`soul.md` / `profile.md` / `lessons.md`). This hashes
  file CONTENT, not the `/data/identity` git HEAD: the bot reads the working-tree
  files (`IdentityStore.read_soul()`), which can diverge from HEAD when an edit
  is uncommitted, so a HEAD-based sha would mis-measure exactly the change being
  evaluated (doc #36 Decisions #12).
- sorted skill-file-set hash — a content-sensitive digest over every skill
  file, so editing a skill body (not just adding/removing one) moves it.
- provider route — the route routing/tone/tool_flow completions executed on.
- ember image version — `EMBER_IMAGE_VERSION` (build-time git SHA, Dockerfile),
  falling back to the pinned claude CLI version when the image wasn't stamped.

Pure/deterministic given the same inputs — no clocks, no randomness. Every
component is best-effort: an unreadable dir yields `None` for that axis rather
than raising, so `/eval run` never dies on fingerprinting.
"""

from __future__ import annotations

import hashlib
import logging
import os
from pathlib import Path

from ember.evals.base import Fingerprint

_log = logging.getLogger("ember.evals.fingerprint")

# Skill files the hash walks (matches the loader: TOML manifests, Python
# modules, and one-level-deep SKILL.md dirs — FC-5 / FC-14).
_SKILL_GLOBS = ("*.toml", "*.py", "*/SKILL.md")

# Identity files the agent reads into the system block, in fixed order. Hashing
# their CONTENT (not git HEAD) keeps the fingerprint faithful to what actually
# drives behavior even when an edit is uncommitted. `lessons.md` lands in v12.5;
# it's included now so the fingerprint picks it up automatically when it exists.
_IDENTITY_FILES = ("soul.md", "profile.md", "lessons.md")


def _identity_content_hash(identity_dir: Path) -> str | None:
    """Content-sensitive sha256 over the identity files the agent reads.

    Digest input is, for each existing file (fixed order): `<name>\\0<sha256(bytes)>\\n`.
    Editing soul/profile/lessons content moves the hash — committed or not.
    Returns None if the dir has none of the identity files / can't be read.
    """
    h = hashlib.sha256()
    found = False
    try:
        for name in _IDENTITY_FILES:
            path = identity_dir / name
            if not path.is_file():
                continue
            digest = hashlib.sha256(path.read_bytes()).hexdigest()
            h.update(name.encode("utf-8"))
            h.update(b"\0")
            h.update(digest.encode("ascii"))
            h.update(b"\n")
            found = True
    except OSError as e:
        _log.info("fingerprint: identity hash unavailable: %s", e)
        return None
    return h.hexdigest() if found else None


def _skills_hash(skills_dir: Path) -> str | None:
    """Content-sensitive sha256 over the sorted skill-file set.

    Digest input is, for each skill file (sorted by relative path):
    `<relpath>\\0<sha256(bytes)>\\n`. Adding, removing, or editing any skill
    file moves the hash; renames move it; reordering the filesystem does not
    (sorted). Returns None if the dir doesn't exist or can't be read.
    """
    if not skills_dir.is_dir():
        return None
    entries: list[tuple[str, str]] = []
    seen: set[Path] = set()
    try:
        for pattern in _SKILL_GLOBS:
            for path in skills_dir.glob(pattern):
                if not path.is_file() or path in seen:
                    continue
                # Skip loader-ignored noise (bytecode caches, draft dir).
                rel = path.relative_to(skills_dir)
                if "__pycache__" in rel.parts or rel.parts[0] == "_drafts":
                    continue
                seen.add(path)
                digest = hashlib.sha256(path.read_bytes()).hexdigest()
                entries.append((rel.as_posix(), digest))
    except OSError as e:
        _log.info("fingerprint: skills hash unavailable: %s", e)
        return None
    entries.sort()
    h = hashlib.sha256()
    for relpath, digest in entries:
        h.update(relpath.encode("utf-8"))
        h.update(b"\0")
        h.update(digest.encode("ascii"))
        h.update(b"\n")
    return h.hexdigest()


def _image_version(fallback: str | None) -> str | None:
    """`EMBER_IMAGE_VERSION` (build-time git SHA), or `fallback` (the pinned
    claude CLI version) when the image wasn't stamped (`unset`/`unknown`)."""
    stamped = os.environ.get("EMBER_IMAGE_VERSION", "").strip()
    if stamped and stamped.lower() != "unknown":
        return stamped
    return fallback


def compute_fingerprint(
    *,
    identity_dir: Path,
    skills_dir: Path,
    provider_route: str | None,
    cli_version: str | None = None,
) -> Fingerprint:
    """Build the config fingerprint. `provider_route` is the route the run
    executes on; `cli_version` is the image-version fallback (pinned claude
    CLI version). Deterministic given identical inputs on disk."""
    identity_sha = _identity_content_hash(identity_dir)
    skills_hash = _skills_hash(skills_dir)
    image_version = _image_version(cli_version)

    # Composite is a stable hash over the four axes — the value stored on the
    # run and compared for equality. Order-fixed; `None` rendered as the empty
    # string so a missing axis is still deterministic.
    composite_src = "\n".join(
        [
            f"identity={identity_sha or ''}",
            f"skills={skills_hash or ''}",
            f"route={provider_route or ''}",
            f"image={image_version or ''}",
        ]
    )
    composite = hashlib.sha256(composite_src.encode("utf-8")).hexdigest()[:16]
    return Fingerprint(
        composite=composite,
        identity_sha=identity_sha,
        skills_hash=skills_hash,
        provider_route=provider_route,
        image_version=image_version,
    )
