"""Profile-update proposer (v9, UoW-8 + FC-8).

When a user message reveals new information that should update the
persistent profile, the proposer runs two cheap-tier provider calls:

1. **Classifier** — is this message self-revealing? yes/no.
2. **Extractor** — given the current profile + the message, produce the
   FULL updated profile.md text.

The proposer renders a unified diff for user display and returns a
`ProfileProposal`. The caller (agent loop) stashes it on the conversation
and asks the user to confirm; on confirmation, `ProfileWriter.commit`
persists.

All work runs through the cheap-tier provider (FC-10). If the cheap tier
isn't configured, or any provider call raises, the proposer returns
`None` — graceful disable, like the compactor.
"""

from __future__ import annotations

import difflib
import logging
import re
from dataclasses import dataclass

from ember.errors import ConfigError
from ember.providers.base import Message
from ember.providers.routing import ProviderRouter

_log = logging.getLogger("ember.identity.proposer")


_CLASSIFIER_PROMPT = """\
You are evaluating whether a user's message reveals new information that
should update their persistent profile (preferences, expertise, life context,
sources they trust, communication style). Reply with a single word: "yes"
if the message is meaningfully self-revealing, "no" otherwise. Default to
"no" for casual chat, questions, factual exchanges, and tool-related back-
and-forth.
"""


_EXTRACTOR_PROMPT = """\
You are updating a user profile based on a self-revealing message. The
current profile is provided as context (in the user message). Your output
is the FULL UPDATED profile.md — preserving structure, headings, and
existing content unless directly contradicted. Add new facts in the
appropriate section. Do not invent details. Do not include commentary.
Output the updated profile content only.
"""


# Matches y / Y / yes / Yes / YES / yeah / Yes. / YES! / " yes ".
# Does not match: maybe, perhaps, n, no, empty, "I don't know".
_POSITIVE_REPLIES = re.compile(r"^\s*y(?:es|eah?)?\s*[.!]?\s*$", re.IGNORECASE)


def _is_positive(reply: str) -> bool:
    return bool(_POSITIVE_REPLIES.match(reply or ""))


@dataclass(frozen=True, slots=True)
class ProfileProposal:
    """A pending profile update awaiting user confirmation.

    Fields:
      proposed_text: the FULL new profile.md content (extractor's output).
      diff: rendered unified diff between current and proposed text, for
        display to the user.
      source_user_message: the verbatim user message that triggered the
        proposal — for logging / commit-body context.
    """

    proposed_text: str
    diff: str
    source_user_message: str


class Proposer:
    """Runs the classifier + extractor cheap-tier flow.

    Stateless aside from the configured router; every `propose` call
    re-resolves the cheap-tier route so a config change between turns
    takes effect immediately.
    """

    def __init__(self, *, router: ProviderRouter) -> None:
        self._router = router

    @staticmethod
    def render_diff(before: str, after: str) -> str:
        """Render a unified diff. Returns "" when the texts match."""
        if before == after:
            return ""
        before_lines = before.splitlines(keepends=True)
        after_lines = after.splitlines(keepends=True)
        diff_iter = difflib.unified_diff(
            before_lines,
            after_lines,
            fromfile="profile.md",
            tofile="profile.md",
        )
        return "".join(diff_iter)

    async def propose(
        self,
        *,
        user_msg: str,
        current_profile: str,
    ) -> ProfileProposal | None:
        """Run classifier → extractor → diff. Return None on any
        non-success path (graceful disable). Never raises."""
        # 1. Cheap-tier resolution — bail if not configured.
        try:
            resolved = self._router.resolve_cheap_tier()
        except ConfigError as e:
            _log.info("proposer: cheap-tier unavailable: %s", e)
            return None

        # 2. Classifier call.
        classifier_msgs = [Message(role="user", content=user_msg)]
        try:
            classify_result = await resolved.provider.complete(
                system=_CLASSIFIER_PROMPT,
                messages=classifier_msgs,
                tools=None,
                model=resolved.model,
                session_id=None,
                stream_handler=None,
            )
        except Exception as e:
            _log.warning(
                "proposer: classifier provider error: %s: %s",
                type(e).__name__,
                e,
            )
            return None

        verdict = "yes" if _is_positive(classify_result.text) else "no"
        _log.info(
            "proposer: classifier verdict=%s reply=%r user_msg=%r",
            verdict,
            classify_result.text.strip()[:80],
            user_msg[:80],
        )
        if verdict == "no":
            return None

        # 3. Extractor call. Compose current profile + recent message in
        # one user message so the system prompt stays byte-stable.
        extractor_msgs = [
            Message(
                role="user",
                content=(
                    f"## Current profile\n\n{current_profile}\n\n"
                    f"## User's recent message\n\n{user_msg}"
                ),
            ),
        ]
        try:
            extract_result = await resolved.provider.complete(
                system=_EXTRACTOR_PROMPT,
                messages=extractor_msgs,
                tools=None,
                model=resolved.model,
                session_id=None,
                stream_handler=None,
            )
        except Exception as e:
            _log.warning(
                "proposer: extractor provider error: %s: %s",
                type(e).__name__,
                e,
            )
            return None

        # 4. No-change guards.
        proposed_text = extract_result.text
        if proposed_text.strip() == "":
            _log.info("proposer: extractor returned empty; no proposal")
            return None
        if proposed_text.strip() == current_profile.strip():
            _log.info("proposer: extractor returned unchanged profile; no proposal")
            return None

        # 5. Render diff and return proposal.
        diff = self.render_diff(current_profile, proposed_text)
        return ProfileProposal(
            proposed_text=proposed_text,
            diff=diff,
            source_user_message=user_msg,
        )

    async def render_updated_profile(self, *, current_profile: str, fact: str) -> str | None:
        """Render the FULL updated profile.md from the current file + a single
        durable fact, using the extractor only (no classifier — the fact was
        already judged worth learning when the retrospective proposed it).

        This is the v12.5 approve-time renderer (learning-loop applier, seam
        (a)): a `profile` proposal stores a fact, not a diff, so approval must
        turn that fact into the full file `ProfileWriter.commit` expects — and
        it renders against the profile as it is NOW, so a proposal that sat
        pending for weeks merges into any intervening edits instead of
        clobbering them. Reuses `_EXTRACTOR_PROMPT`, so it inherits the same
        "preserve structure, add in the right section, invent nothing" behavior
        as the interactive proposer. Returns the full updated text, or None on
        any failure / no-op (graceful — the applier surfaces "no change")."""
        try:
            resolved = self._router.resolve_cheap_tier()
        except ConfigError as e:
            _log.info("render_updated_profile: cheap-tier unavailable: %s", e)
            return None

        msgs = [
            Message(
                role="user",
                content=(
                    f"## Current profile\n\n{current_profile}\n\n"
                    f"## New fact to incorporate\n\n{fact}"
                ),
            ),
        ]
        try:
            result = await resolved.provider.complete(
                system=_EXTRACTOR_PROMPT,
                messages=msgs,
                tools=None,
                model=resolved.model,
                session_id=None,
                stream_handler=None,
            )
        except Exception as e:
            _log.warning("render_updated_profile: extractor error: %s: %s", type(e).__name__, e)
            return None

        proposed = result.text
        if proposed is None or proposed.strip() == "":
            _log.info("render_updated_profile: extractor returned empty")
            return None
        return proposed
