"""IdentityStore (FC-8, TLC-11, TLC-12).

Per the contracts doc:
- FC-8: read path is filesystem-only; write path is file → git → mirror;
  soul is read-only to the agent; profile updates are gated through
  UoW-8 in v1 (deferred to v7 — out of scope here).
- TLC-11: identity files are the canonical source of truth; mirrors are
  derivative.
- TLC-12: prompt-cache stability — the system block must be byte-identical
  across consecutive turns when underlying sources haven't changed.

v1's IdentityStore is read-only at the API level; updates ship in later
phases. We test the read surface, idempotent bootstrap, file-as-source-
of-truth semantics, and byte-stability.

We don't read implementation files for IdentityStore — these tests
assert observable behavior promised by the contracts doc and the
interface in `src/ember/identity/store.py`.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from ember.identity.store import IdentityStore


# --- Fixtures -------------------------------------------------------------


@pytest.fixture
async def identity(tmp_path: Path) -> IdentityStore:
    """A freshly-initialized IdentityStore on a per-test directory."""
    store = IdentityStore(tmp_path / "identity")
    await store.ensure_initialized()
    return store


# --- ensure_initialized: idempotency + bootstrap --------------------------


class TestEnsureInitialized:
    async def test_creates_directory(self, tmp_path: Path) -> None:
        d = tmp_path / "identity"
        assert not d.exists()
        store = IdentityStore(d)
        await store.ensure_initialized()
        assert d.exists()
        assert d.is_dir()

    async def test_writes_soul_and_profile(self, tmp_path: Path) -> None:
        d = tmp_path / "identity"
        store = IdentityStore(d)
        await store.ensure_initialized()
        soul = d / IdentityStore.SOUL_FILENAME
        profile = d / IdentityStore.PROFILE_FILENAME
        assert soul.exists()
        assert profile.exists()
        # Bootstrap defaults must be non-empty (the system_prompt has to
        # have something cacheable to advertise).
        assert soul.read_text().strip() != ""
        assert profile.read_text().strip() != ""

    async def test_initializes_git_repo(self, tmp_path: Path) -> None:
        # FC-8: write path is file → git add+commit → mirror. The git
        # repo lives at <identity_dir>/.git. v1 doesn't exercise commits
        # but the repo must exist after initialization so later phases
        # can commit without an extra bootstrap.
        d = tmp_path / "identity"
        store = IdentityStore(d)
        await store.ensure_initialized()
        assert (d / ".git").exists()
        assert (d / ".git").is_dir()

    async def test_idempotent_no_error(self, tmp_path: Path) -> None:
        d = tmp_path / "identity"
        store = IdentityStore(d)
        await store.ensure_initialized()
        # Second call must not raise.
        await store.ensure_initialized()

    async def test_idempotent_does_not_overwrite(self, tmp_path: Path) -> None:
        d = tmp_path / "identity"
        store = IdentityStore(d)
        await store.ensure_initialized()
        custom_soul = "my custom soul, edited by hand"
        custom_profile = "my custom profile, edited by hand"
        (d / IdentityStore.SOUL_FILENAME).write_text(custom_soul)
        (d / IdentityStore.PROFILE_FILENAME).write_text(custom_profile)
        # A second ensure_initialized must not stomp the user's edits.
        await store.ensure_initialized()
        assert (d / IdentityStore.SOUL_FILENAME).read_text() == custom_soul
        assert (d / IdentityStore.PROFILE_FILENAME).read_text() == custom_profile


# --- read_soul / read_profile --------------------------------------------


class TestReads:
    async def test_read_soul_returns_contents(self, tmp_path: Path) -> None:
        d = tmp_path / "identity"
        store = IdentityStore(d)
        await store.ensure_initialized()
        (d / IdentityStore.SOUL_FILENAME).write_text("soul body")
        assert store.read_soul() == "soul body"

    async def test_read_profile_returns_contents(self, tmp_path: Path) -> None:
        d = tmp_path / "identity"
        store = IdentityStore(d)
        await store.ensure_initialized()
        (d / IdentityStore.PROFILE_FILENAME).write_text("profile body")
        assert store.read_profile() == "profile body"

    async def test_read_soul_missing_file_raises(self, tmp_path: Path) -> None:
        # No ensure_initialized; the soul file does not exist.
        store = IdentityStore(tmp_path / "nope")
        with pytest.raises(FileNotFoundError):
            store.read_soul()

    async def test_read_profile_missing_file_raises(self, tmp_path: Path) -> None:
        store = IdentityStore(tmp_path / "nope")
        with pytest.raises(FileNotFoundError):
            store.read_profile()


# --- system_prompt: TLC-12 byte stability --------------------------------


class TestSystemPromptByteStability:
    async def test_system_prompt_contains_soul_and_profile(self, tmp_path: Path) -> None:
        d = tmp_path / "identity"
        store = IdentityStore(d)
        await store.ensure_initialized()
        (d / IdentityStore.SOUL_FILENAME).write_text("SOUL_MARKER_42")
        (d / IdentityStore.PROFILE_FILENAME).write_text("PROFILE_MARKER_77")
        prompt = store.system_prompt()
        assert "SOUL_MARKER_42" in prompt
        assert "PROFILE_MARKER_77" in prompt

    async def test_system_prompt_byte_stable_across_calls(self, tmp_path: Path) -> None:
        # TLC-12: when underlying inputs haven't changed, system_prompt()
        # must be byte-identical across consecutive calls.
        d = tmp_path / "identity"
        store = IdentityStore(d)
        await store.ensure_initialized()

        prompts = [store.system_prompt() for _ in range(5)]
        # All five must be byte-equal.
        assert len(set(prompts)) == 1
        # And they must be a real string with content (non-empty).
        assert isinstance(prompts[0], str)
        assert prompts[0] != ""


# --- TLC-11: file is canonical -------------------------------------------


class TestFileIsCanonical:
    async def test_direct_file_write_picked_up_on_next_call(self, tmp_path: Path) -> None:
        # TLC-11: identity is the file. Any in-memory cache must be
        # invalidated when the file changes. We write the file directly
        # between two system_prompt() calls and assert the second call
        # reflects the new content.
        d = tmp_path / "identity"
        store = IdentityStore(d)
        await store.ensure_initialized()

        first = store.system_prompt()
        # Edit profile.md by hand — simulating `vim profile.md` on the
        # host outside the agent process.
        new_profile = "the user is now a sysadmin. NEW_MARKER_X"
        (d / IdentityStore.PROFILE_FILENAME).write_text(new_profile)
        second = store.system_prompt()

        assert first != second
        assert "NEW_MARKER_X" in second

    async def test_manual_edit_to_soul_picked_up(self, tmp_path: Path) -> None:
        d = tmp_path / "identity"
        store = IdentityStore(d)
        await store.ensure_initialized()

        new_soul = "Ember is now a strict sysadmin. SOUL_NEW_42"
        (d / IdentityStore.SOUL_FILENAME).write_text(new_soul)
        # Both read_soul and system_prompt must reflect the edit.
        assert store.read_soul() == new_soul
        assert "SOUL_NEW_42" in store.system_prompt()

    async def test_manual_edit_to_profile_picked_up(self, tmp_path: Path) -> None:
        d = tmp_path / "identity"
        store = IdentityStore(d)
        await store.ensure_initialized()

        new_profile = "different profile content, PROFILE_NEW_42"
        (d / IdentityStore.PROFILE_FILENAME).write_text(new_profile)
        assert store.read_profile() == new_profile
        assert "PROFILE_NEW_42" in store.system_prompt()
