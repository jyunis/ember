"""Identity store (FC-8).

Soul + profile live as files at `/data/identity/`. The file is the
source of truth (TLC-11). Each `system_prompt()` re-reads from disk so
hand-edits land on the next turn — and is byte-stable across calls
when files are unchanged (TLC-12).

v1 surface is **read-only**. Update flows ship later:
- Mirror to documents MCP → v3
- Profile-update proposal/diff flow → v7
(See architecture doc § Deferrals log.)
"""

from __future__ import annotations

import logging
import subprocess
from pathlib import Path

_log = logging.getLogger("ember.identity")


_DEFAULT_SOUL = """\
# Ember

You are Ember, a personal AI assistant running on the user's home server.
You communicate via Discord; one thread per conversation.

## Tone
- Terse for terse questions; real reasoning for hard ones.
- Lead with the answer, not the qualification.
- Have opinions. Push back when you disagree. Don't hedge for politeness.
- Skip filler ("Great question!", "I'd be happy to help"). Just answer.

## Behavior
- Trust local code and infrastructure context unless contradicted.
- When invoking tools, the user sees a status line; keep replies tight.
- If you don't know, say so. Don't fabricate.
"""

_DEFAULT_PROFILE = """\
# User profile

## Identity
- Name: (left for the user to fill in)
- Located in: (left for the user to fill in)
- Server context: self-hosted Linux home server

## Work
(left for the user to fill in)

## Communication preferences
- Direct, no filler. Lead with the answer.
- Treat the user as an experienced engineer; skip basics.
- For architectural questions: give a recommendation + the main tradeoff in
  2–3 sentences before going deep.

## Sources / interests
(left for the user to fill in via `git commit` on the host)
"""


class IdentityStore:
    """File-backed soul + profile reader."""

    SOUL_FILENAME = "soul.md"
    PROFILE_FILENAME = "profile.md"
    LESSONS_FILENAME = "lessons.md"
    SECTION_SEPARATOR = "\n\n"

    def __init__(self, identity_dir: Path) -> None:
        self._dir = identity_dir
        self._soul_path = identity_dir / self.SOUL_FILENAME
        self._profile_path = identity_dir / self.PROFILE_FILENAME
        self._lessons_path = identity_dir / self.LESSONS_FILENAME

    async def ensure_initialized(self) -> None:
        """Idempotent. Creates `identity_dir`, writes default `soul.md`
        and `profile.md` if missing, and initializes a git repo with an
        initial commit if not present."""
        self._dir.mkdir(parents=True, exist_ok=True)
        wrote = False
        if not self._soul_path.exists():
            self._soul_path.write_text(_DEFAULT_SOUL)
            wrote = True
        if not self._profile_path.exists():
            self._profile_path.write_text(_DEFAULT_PROFILE)
            wrote = True

        git_dir = self._dir / ".git"
        if not git_dir.exists():
            self._git("init", "--quiet")
            self._git("config", "user.email", "ember@localhost")
            self._git("config", "user.name", "Ember")
            self._git("add", "-A")
            self._git("commit", "--quiet", "-m", "ember: initial identity")
        elif wrote:
            self._git("add", "-A")
            self._git("commit", "--quiet", "-m", "ember: initialize missing files")

    def _git(self, *args: str) -> None:
        try:
            subprocess.run(
                ["git", *args],
                cwd=self._dir,
                check=True,
                capture_output=True,
            )
        except FileNotFoundError:
            _log.warning("git not available; identity dir will not be versioned")
        except subprocess.CalledProcessError as e:
            stderr = e.stderr.decode(errors="replace")[:200] if e.stderr else ""
            _log.warning("git %s failed: %s", args[0], stderr)

    def read_soul(self) -> str:
        return self._soul_path.read_text()

    def read_profile(self) -> str:
        return self._profile_path.read_text()

    def read_lessons(self) -> str:
        """Lessons file content (FC-24), or "" if it does not exist yet.

        Lessons are the capped residual learning channel; the file is absent
        until the first lesson is approved. Read fresh every turn like
        soul/profile, so hand-edits land on the next turn."""
        try:
            return self._lessons_path.read_text()
        except FileNotFoundError:
            return ""

    def system_prompt(self) -> str:
        """soul + profile [+ lessons, if any], joined by the section
        separator. lessons.md is injected AFTER profile (FC-24) so TLC-12
        byte-stability holds unchanged: when there is no lessons file the
        result is byte-identical to the pre-v12.5 `soul + profile`, so existing
        conversations keep their cached system block; a lesson invalidates the
        cache exactly once, then the block is stable again."""
        prompt = self.read_soul() + self.SECTION_SEPARATOR + self.read_profile()
        lessons = self.read_lessons()
        if lessons.strip():
            prompt += self.SECTION_SEPARATOR + lessons
        return prompt

    # --- Mutating helpers (UoW-8 / FC-8) ---------------------------------

    def write_profile(self, text: str) -> None:
        """Atomic write of profile.md (temp+rename, atomic on POSIX)."""
        tmp = self._profile_path.with_suffix(".tmp")
        tmp.write_text(text)
        tmp.replace(self._profile_path)

    def git_commit_profile(self, *, body: str) -> str | None:
        """Stage profile.md and commit with the given body."""
        return self._git_commit_file(self.PROFILE_FILENAME, body=body)

    def write_lessons(self, text: str) -> None:
        """Atomic write of lessons.md (temp+rename, atomic on POSIX)."""
        tmp = self._lessons_path.with_suffix(".tmp")
        tmp.write_text(text)
        tmp.replace(self._lessons_path)

    def git_commit_lessons(self, *, body: str) -> str | None:
        """Stage lessons.md and commit with the given body (FC-24: same
        lifecycle as profile.md — the commit body references the proposal id)."""
        return self._git_commit_file(self.LESSONS_FILENAME, body=body)

    def _git_commit_file(self, filename: str, *, body: str) -> str | None:
        """Stage one file in the identity repo and commit with the
        given body. Returns the new HEAD sha, or None on failure
        (any CalledProcessError or git-not-available is logged +
        swallowed).
        """
        try:
            subprocess.run(
                ["git", "add", filename],
                cwd=self._dir,
                check=True,
                capture_output=True,
            )
            subprocess.run(
                ["git", "commit", "-m", body],
                cwd=self._dir,
                check=True,
                capture_output=True,
            )
            sha = subprocess.run(
                ["git", "rev-parse", "HEAD"],
                cwd=self._dir,
                check=True,
                capture_output=True,
                text=True,
            ).stdout.strip()
            return sha or None
        except FileNotFoundError:
            _log.warning("git not available; %s commit skipped", filename)
            return None
        except subprocess.CalledProcessError as e:
            stderr = e.stderr.decode(errors="replace")[:200] if e.stderr else ""
            _log.warning("git commit %s failed: %s", filename, stderr)
            return None
