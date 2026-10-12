"""Word-aware Discord message chunking (v16.1 follow-up).

The naive fixed-width `_chunk` split mid-word across message bubbles —
invisible while replies were one buffered message, glaring once block
streaming (TLC-28) posts several messages per turn. Split preference:
last newline in the window, else last space, else a hard cut (only for
a single oversized token such as a long URL).
"""

from ember.channels.discord import _DISCORD_MSG_CHAR_LIMIT, DiscordChannel

LIMIT = _DISCORD_MSG_CHAR_LIMIT


def rejoin(chunks: list[str]) -> str:
    """Chunks with the dropped single separator restored as a space —
    used to assert no content characters are lost beyond separators."""
    return " ".join(chunks)


def test_short_content_single_chunk():
    assert DiscordChannel._chunk("hello") == ["hello"]


def test_empty_content_yields_one_empty_chunk():
    assert DiscordChannel._chunk("") == [""]


def test_exact_limit_not_split():
    content = "x" * LIMIT
    assert DiscordChannel._chunk(content) == [content]


def test_splits_at_newline_before_limit():
    first = "a" * 1500
    second = "b" * 1000
    chunks = DiscordChannel._chunk(first + "\n" + second)
    assert chunks == [first, second]


def test_splits_at_space_when_no_newline():
    words = ("word " * 500).strip()  # 2499 chars of 4-char words
    chunks = DiscordChannel._chunk(words)
    assert len(chunks) == 2
    for chunk in chunks:
        assert len(chunk) <= LIMIT
        assert not chunk.startswith(" ") and not chunk.endswith(" ")
        # No word was severed: every fragment is made of whole "word"s.
        assert set(chunk.split(" ")) == {"word"}


def test_never_splits_mid_word():
    # 300 ten-char words separated by spaces -> 3299 chars.
    words = " ".join(f"w{i:08d}x" for i in range(300))
    chunks = DiscordChannel._chunk(words)
    assert len(chunks) >= 2
    vocab = set(words.split(" "))
    for chunk in chunks:
        assert len(chunk) <= LIMIT
        for token in chunk.split(" "):
            assert token in vocab, f"severed token: {token!r}"


def test_prefers_newline_over_space():
    # Both a space and a later newline inside the window: cut at newline.
    content = "alpha beta" + "\n" + "c" * (LIMIT - 5) + " tail"
    chunks = DiscordChannel._chunk(content)
    assert chunks[0] == "alpha beta"


def test_oversized_single_token_hard_cut():
    url = "h" * (LIMIT + 500)
    chunks = DiscordChannel._chunk(url)
    assert chunks == ["h" * LIMIT, "h" * 500]


def test_no_empty_chunks_and_all_within_limit():
    content = ("paragraph one\n" * 200) + ("longword" * 400)
    chunks = DiscordChannel._chunk(content)
    assert all(chunks)
    assert all(len(c) <= LIMIT for c in chunks)


def test_no_newline_prefers_sentence_boundary_over_space():
    # One giant paragraph: sentences of 400 chars, no newlines anywhere.
    sentence = "w" * 398 + ". "
    content = (sentence * 6).strip()  # 2398 chars
    chunks = DiscordChannel._chunk(content)
    assert len(chunks) == 2
    # Cut lands after the punctuation of the last whole sentence, not at
    # the last inter-word space.
    assert chunks[0].endswith(".")
    assert chunks[1].startswith("w")


def test_sentence_boundary_handles_question_and_bang():
    content = "a" * 1000 + "? " + "b" * 900 + "! " + "c" * 900
    chunks = DiscordChannel._chunk(content)
    assert chunks[0] == "a" * 1000 + "? " + "b" * 900 + "!"
    assert chunks[1] == "c" * 900


def test_newline_still_wins_over_sentence_boundary():
    content = "First sentence. More prose here\n" + "x" * 2500
    chunks = DiscordChannel._chunk(content)
    assert chunks[0] == "First sentence. More prose here"


def test_no_content_lost_beyond_separators():
    words = " ".join(f"tok{i}" for i in range(1000))
    chunks = DiscordChannel._chunk(words)
    assert rejoin(chunks).split(" ") == words.split(" ")


# --- Fence-aware splitting (🔒 confirm prompts carrying a full body) -----


def _fence_balanced(chunk: str) -> bool:
    return sum(1 for ln in chunk.split("\n") if ln.strip().startswith("```")) % 2 == 0


def test_long_fenced_block_reopens_fence_in_every_chunk():
    body = "\n".join(f"- bullet line {i} " + "x" * 60 for i in range(80))
    content = f"🔒 Confirm: gmail_send → a@b.c\n```\n{body}\n```\nReply 'yes' or 'no'."
    chunks = DiscordChannel._chunk(content)
    assert len(chunks) >= 3
    assert all(len(c) <= LIMIT for c in chunks)
    for c in chunks:
        assert _fence_balanced(c), f"unbalanced fence in chunk: {c[:80]!r}"
    assert chunks[0].startswith("🔒 Confirm: gmail_send → a@b.c\n```\n")
    assert chunks[0].endswith("\n```")
    for c in chunks[1:-1]:
        assert c.startswith("```\n") and c.endswith("\n```")
    assert chunks[-1].endswith("\n```\nReply 'yes' or 'no'.")
    # No body content dropped: strip the fence lines and compare.
    kept = [ln for c in chunks for ln in c.split("\n") if not ln.strip().startswith("```")]
    assert "\n".join(kept) == f"🔒 Confirm: gmail_send → a@b.c\n{body}\nReply 'yes' or 'no'."


def test_diff_fence_language_tag_is_preserved_on_reopen():
    body = "\n".join(f"+added line {i} " + "y" * 50 for i in range(60))
    content = f"🔒 Confirm: profile_update:\n```diff\n{body}\n```\nReply 'yes' or 'no'."
    chunks = DiscordChannel._chunk(content)
    assert len(chunks) >= 2
    for c in chunks[1:]:
        assert c.startswith("```diff\n")
    assert all(_fence_balanced(c) for c in chunks)


def test_fence_close_fits_when_cut_lands_at_limit():
    # A fenced body whose natural newline cut lands within 4 chars of
    # the limit: the closing fence must still fit (chunk re-cut).
    line = "q" * 99
    body = "\n".join([line] * 40)  # 100-char stride, cut at 1999/2000
    content = "```\n" + body + "\n```"
    chunks = DiscordChannel._chunk(content)
    assert all(len(c) <= LIMIT for c in chunks)
    assert all(_fence_balanced(c) for c in chunks)
    kept = [ln for c in chunks for ln in c.split("\n") if not ln.strip().startswith("```")]
    assert kept == [line] * 40


def test_unfenced_content_gets_no_fence_markers():
    content = "\n".join(f"line {i}" for i in range(600))
    chunks = DiscordChannel._chunk(content)
    assert not any("```" in c for c in chunks)
    assert "\n".join(chunks) == content


def test_fence_closed_before_cut_does_not_reopen():
    short_code = "```\ncode\n```\n"
    content = short_code + "\n".join(f"prose {i}" for i in range(600))
    chunks = DiscordChannel._chunk(content)
    assert chunks[0].startswith(short_code)
    for c in chunks[1:]:
        assert "```" not in c
