"""Profile-update proposer (v9, Task #4) — UoW-8 steps 1-3, FC-8.

These tests exercise the contract surface of `ember.identity.proposer`
without reading the implementation. Tester-role per the implementer/
tester separation protocol (contracts doc § Development protocol).

Surface under test (from spec):

- `ProfileProposal` dataclass with fields:
  `proposed_text: str`, `diff: str`, `source_user_message: str`.
- `Proposer.propose(user_msg, current_profile) -> ProfileProposal | None`.
- Internally the proposer makes two cheap-tier calls — a classifier and
  an extractor — both via `router.resolve_cheap_tier()`.
- Classifier reply parsing: yes/y/yeah/etc → True; no/n/nope/empty →
  False; unrecognized → False (conservative).
- If the classifier says no, the extractor is NOT invoked.
- If the extractor's result equals the current profile (after strip),
  no proposal is emitted (returns None).
- If the cheap-tier provider isn't configured (`ConfigError`), the
  proposer returns None without raising (graceful disable, like the
  compactor — FC-10 / TLC-7).
- If either provider call raises any exception, the proposer returns
  None (log + skip).
- Module exposes non-empty `_CLASSIFIER_PROMPT` and `_EXTRACTOR_PROMPT`
  constants.

Ambiguities flagged for human review:

1. **Diff format.** UoW-8 says "render diff" but doesn't pin the
   format. Tests assert it's a unified-diff-style string (`+`/`-`
   lines visible) and that `render_diff(a, a) == ""`. Implementer can
   choose `difflib.unified_diff`, `difflib.ndiff`, or similar — the
   spec is silent on exact framing. Resolved here as: when a change
   exists, the rendered diff MUST contain at least one line starting
   with `+` and one with `-` so the user can read the change. Confirm
   with spec owner.

2. **`source_user_message` field.** The dataclass spec says "for
   logging" — tests assert it's populated with the inbound user
   message verbatim, since that's the only sensible value.

3. **Classifier output protocol.** Spec gives examples ("yes", "YES",
   "yeah", "y") but doesn't define a strict regex. Tests use the
   examples directly and additionally pin:
   - leading/trailing whitespace + punctuation tolerated;
   - case-insensitive;
   - empty string → False;
   - unrecognized text ("maybe", "perhaps") → False (conservative,
     per spec).

4. **Extractor empty/no-change semantics.** Spec says "0 changes →
   stop." Resolved here as: extractor's `result.text.strip() == ""`
   OR `result.text.strip() == current_profile.strip()` → return None.
"""

from __future__ import annotations

from typing import Any

import pytest

from ember.errors import ConfigError, ProviderError
from ember.providers.routing import ProviderRouter
from tests.fakes.fake_provider import FakeProvider

# Imports under test — these will fail until the implementer ships the
# module. The collection-time failure is intentional per the tester
# briefing: "Tests should fail with `ModuleNotFoundError:
# ember.identity.proposer`. Confirm."
from ember.identity import proposer as _proposer_module  # noqa: E402  isort:skip
from ember.identity.proposer import ProfileProposal, Proposer  # noqa: E402  isort:skip


# --- Helpers --------------------------------------------------------------


def _named_provider(name: str) -> FakeProvider:
    fp = FakeProvider()
    fp.name = name
    return fp


def make_router(*, with_cheap: bool = True) -> tuple[ProviderRouter, FakeProvider, FakeProvider]:
    """Build a ProviderRouter with a `claude_cli` primary and (optionally)
    a `cheap` provider registered as the cheap tier. Returns
    `(router, primary, cheap)`."""
    primary = _named_provider("claude_cli")
    cheap = _named_provider("cheap")
    providers: dict[str, Any] = {"claude_cli": primary, "cheap": cheap}
    return (
        ProviderRouter(
            providers,
            cheap_tier_route="cheap" if with_cheap else None,
        ),
        primary,
        cheap,
    )


# A baseline profile string used across tests.
_CURRENT_PROFILE = """\
# User profile — Alice

## Identity
- Name: Alice

## Communication preferences
- Direct, no filler.
"""


# --- ProfileProposal shape -----------------------------------------------


class TestProfileProposalShape:
    def test_fields_exist(self) -> None:
        p = ProfileProposal(
            proposed_text="new",
            diff="--- a\n+++ b\n",
            source_user_message="user msg",
        )
        assert p.proposed_text == "new"
        assert p.diff == "--- a\n+++ b\n"
        assert p.source_user_message == "user msg"


# --- Module-level prompt constants ---------------------------------------


class TestSystemPromptConstants:
    def test_classifier_prompt_exists_and_nonempty(self) -> None:
        # Spec: "fixed module-level system prompt — `_CLASSIFIER_PROMPT`."
        assert hasattr(_proposer_module, "_CLASSIFIER_PROMPT")
        val = _proposer_module._CLASSIFIER_PROMPT
        assert isinstance(val, str)
        assert val.strip() != ""

    def test_extractor_prompt_exists_and_nonempty(self) -> None:
        assert hasattr(_proposer_module, "_EXTRACTOR_PROMPT")
        val = _proposer_module._EXTRACTOR_PROMPT
        assert isinstance(val, str)
        assert val.strip() != ""


# --- Classifier-no short-circuit -----------------------------------------


class TestClassifierShortCircuit:
    async def test_classifier_no_skips_extractor(self) -> None:
        """First cheap call returns 'no' → propose returns None and the
        extractor is never invoked. Verify by queueing only ONE response;
        a second call would error (queue empty returns default empty
        CompletionResult, which we'd see in cheap.calls regardless — so
        the strong assertion is `len(cheap.calls) == 1`)."""
        router, _, cheap = make_router()
        cheap.queue_text("no")

        p = Proposer(router=router)
        result = await p.propose(user_msg="hi", current_profile=_CURRENT_PROFILE)

        assert result is None
        # Exactly one cheap call (classifier). No extractor call.
        assert len(cheap.calls) == 1

    async def test_classifier_yes_triggers_extractor(self) -> None:
        """Classifier 'yes' → extractor invoked."""
        router, _, cheap = make_router()
        cheap.queue_text("yes")
        # Extractor returns a profile that differs from current.
        new_profile = _CURRENT_PROFILE + "\n## Sources\n- source A\n"
        cheap.queue_text(new_profile)

        p = Proposer(router=router)
        result = await p.propose(
            user_msg="I read source A daily",
            current_profile=_CURRENT_PROFILE,
        )

        assert result is not None
        assert isinstance(result, ProfileProposal)
        # Two cheap calls: classifier + extractor.
        assert len(cheap.calls) == 2

    async def test_only_cheap_tier_used(self) -> None:
        """Both calls go through the cheap-tier provider — never the
        primary. (FC-10: no subscription burn for bookkeeping.)"""
        router, primary, cheap = make_router()
        cheap.queue_text("yes")
        cheap.queue_text(_CURRENT_PROFILE + "\n## new\n- thing\n")

        p = Proposer(router=router)
        await p.propose(user_msg="x", current_profile=_CURRENT_PROFILE)

        assert primary.calls == []
        assert len(cheap.calls) == 2


# --- Classifier reply parsing --------------------------------------------


class TestClassifierReplyParsing:
    @pytest.mark.parametrize(
        "reply",
        [
            "yes",
            "YES",
            "Yes.",
            " yes ",
            "y",
            "Y",
            "yeah",
            "Yeah",
        ],
    )
    async def test_positive_replies(self, reply: str) -> None:
        router, _, cheap = make_router()
        cheap.queue_text(reply)
        # Queue extractor response so the propose call doesn't fail mid-flow.
        new_profile = _CURRENT_PROFILE + "\n## new\n- thing\n"
        cheap.queue_text(new_profile)

        p = Proposer(router=router)
        result = await p.propose(user_msg="x", current_profile=_CURRENT_PROFILE)

        # Positive classifier → extractor called → propose returns a proposal.
        assert result is not None, f"reply {reply!r} should parse positive"
        assert len(cheap.calls) == 2

    @pytest.mark.parametrize(
        "reply",
        [
            "no",
            "No.",
            "NO",
            "n",
            "N",
            "nope",
            "",
            "   ",
            "maybe",
            "perhaps",
            "I don't know",
            "kind of",
        ],
    )
    async def test_negative_or_unrecognized_replies(self, reply: str) -> None:
        """Negative and unrecognized replies both short-circuit (the spec
        is conservative: don't ask the user about a weak signal)."""
        router, _, cheap = make_router()
        cheap.queue_text(reply)

        p = Proposer(router=router)
        result = await p.propose(user_msg="x", current_profile=_CURRENT_PROFILE)

        assert result is None, f"reply {reply!r} should parse negative"
        # Only the classifier ran.
        assert len(cheap.calls) == 1


# --- Extractor: no-change / empty -----------------------------------------


class TestExtractorNoChange:
    async def test_extractor_returns_unchanged_profile_returns_none(self) -> None:
        """Extractor returns text identical to `current_profile` → no
        proposal."""
        router, _, cheap = make_router()
        cheap.queue_text("yes")
        cheap.queue_text(_CURRENT_PROFILE)  # unchanged

        p = Proposer(router=router)
        result = await p.propose(user_msg="x", current_profile=_CURRENT_PROFILE)

        assert result is None
        assert len(cheap.calls) == 2

    async def test_extractor_returns_unchanged_with_trailing_whitespace(self) -> None:
        """Spec: 'result.text.strip() equals current_profile (no change)'."""
        router, _, cheap = make_router()
        cheap.queue_text("yes")
        cheap.queue_text(_CURRENT_PROFILE + "   \n\n  ")  # whitespace-only diff

        p = Proposer(router=router)
        result = await p.propose(user_msg="x", current_profile=_CURRENT_PROFILE)

        assert result is None

    async def test_extractor_returns_empty_string_returns_none(self) -> None:
        """Empty extractor output is treated as 'no change'."""
        router, _, cheap = make_router()
        cheap.queue_text("yes")
        cheap.queue_text("")

        p = Proposer(router=router)
        result = await p.propose(user_msg="x", current_profile=_CURRENT_PROFILE)

        assert result is None

    async def test_extractor_returns_whitespace_only_returns_none(self) -> None:
        router, _, cheap = make_router()
        cheap.queue_text("yes")
        cheap.queue_text("   \n\t  ")

        p = Proposer(router=router)
        result = await p.propose(user_msg="x", current_profile=_CURRENT_PROFILE)

        assert result is None


# --- Successful propose path ---------------------------------------------


class TestSuccessfulPropose:
    async def test_returns_profile_proposal_with_proposed_text(self) -> None:
        new_profile = _CURRENT_PROFILE + "\n## Sources\n- a new source\n"
        router, _, cheap = make_router()
        cheap.queue_text("yes")
        cheap.queue_text(new_profile)

        p = Proposer(router=router)
        result = await p.propose(
            user_msg="I read a-new-source.com daily",
            current_profile=_CURRENT_PROFILE,
        )

        assert result is not None
        # `proposed_text` is the extractor's output (full new profile).
        assert result.proposed_text == new_profile

    async def test_diff_is_nonempty_string(self) -> None:
        new_profile = _CURRENT_PROFILE + "\n## Sources\n- a new source\n"
        router, _, cheap = make_router()
        cheap.queue_text("yes")
        cheap.queue_text(new_profile)

        p = Proposer(router=router)
        result = await p.propose(
            user_msg="I read a-new-source.com",
            current_profile=_CURRENT_PROFILE,
        )

        assert result is not None
        assert isinstance(result.diff, str)
        assert result.diff.strip() != ""

    async def test_diff_contains_added_and_removed_markers(self) -> None:
        """Tester-resolved ambiguity #1: diff is unified-diff-style with
        `+`/`-` lines so the user can read the change. (`difflib.unified_diff`
        is the natural choice; the test asserts only that the rendered
        string contains the markers.)"""
        # Use a diff that both adds and removes content so both markers
        # are guaranteed.
        before = "line A\nline B\n"
        after = "line A\nline C\n"

        router, _, _ = make_router()
        p = Proposer(router=router)
        rendered = p.render_diff(before, after)

        assert isinstance(rendered, str)
        # Must contain at least one '+' and one '-' line (unified-diff
        # convention).
        added_lines = [
            ln for ln in rendered.splitlines() if ln.startswith("+") and not ln.startswith("+++")
        ]
        removed_lines = [
            ln for ln in rendered.splitlines() if ln.startswith("-") and not ln.startswith("---")
        ]
        assert added_lines, f"diff must include at least one '+' line; got: {rendered!r}"
        assert removed_lines, f"diff must include at least one '-' line; got: {rendered!r}"

    async def test_render_diff_no_change_returns_empty(self) -> None:
        """`render_diff(a, a)` returns an empty string (no change to render)."""
        router, _, _ = make_router()
        p = Proposer(router=router)
        same = "identical content\n"
        assert p.render_diff(same, same) == ""

    async def test_source_user_message_carried_through(self) -> None:
        """Tester-resolved ambiguity #2: `source_user_message` is the
        verbatim user message passed in."""
        router, _, cheap = make_router()
        cheap.queue_text("yes")
        cheap.queue_text(_CURRENT_PROFILE + "\n## new\n- thing\n")

        p = Proposer(router=router)
        msg = "I just moved to Minneapolis MN"
        result = await p.propose(user_msg=msg, current_profile=_CURRENT_PROFILE)

        assert result is not None
        assert result.source_user_message == msg


# --- Cheap-tier unavailable (graceful disable) ---------------------------


class TestCheapTierUnavailable:
    async def test_no_cheap_tier_configured_returns_none(self) -> None:
        """`router.resolve_cheap_tier()` raises ConfigError → propose
        returns None, swallowing the error (FC-10 / TLC-7 graceful
        disable, like the compactor)."""
        router, _, cheap = make_router(with_cheap=False)
        # Sanity: cheap-tier really is unconfigured.
        with pytest.raises(ConfigError):
            router.resolve_cheap_tier()

        p = Proposer(router=router)
        result = await p.propose(user_msg="x", current_profile=_CURRENT_PROFILE)

        assert result is None
        # No provider calls at all.
        assert cheap.calls == []


# --- Provider error during classify or extract ---------------------------


class TestProviderErrors:
    async def test_classifier_provider_error_returns_none(self) -> None:
        router, _, cheap = make_router()
        cheap.queue_error(ProviderError("classify boom"))

        p = Proposer(router=router)
        result = await p.propose(user_msg="x", current_profile=_CURRENT_PROFILE)

        assert result is None

    async def test_extractor_provider_error_returns_none(self) -> None:
        router, _, cheap = make_router()
        cheap.queue_text("yes")
        cheap.queue_error(ProviderError("extract boom"))

        p = Proposer(router=router)
        result = await p.propose(user_msg="x", current_profile=_CURRENT_PROFILE)

        assert result is None

    async def test_classifier_arbitrary_exception_returns_none(self) -> None:
        """Any exception, not just ProviderError, is non-fatal."""
        router, _, cheap = make_router()
        cheap.queue_error(RuntimeError("network nope"))

        p = Proposer(router=router)
        result = await p.propose(user_msg="x", current_profile=_CURRENT_PROFILE)

        assert result is None

    async def test_extractor_arbitrary_exception_returns_none(self) -> None:
        router, _, cheap = make_router()
        cheap.queue_text("yes")
        cheap.queue_error(RuntimeError("nope"))

        p = Proposer(router=router)
        result = await p.propose(user_msg="x", current_profile=_CURRENT_PROFILE)

        assert result is None
