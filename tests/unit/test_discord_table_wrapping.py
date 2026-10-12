"""Markdown-table → code-fence post-processing for Discord.

Discord doesn't render GFM tables; wrapping them in triple-backtick
fences is the readable form. The helper is mechanical, not semantic —
it triggers off the `|---|---|` alignment-row signature, which is
unambiguous enough to keep false positives near zero.
"""

from __future__ import annotations

from ember.channels.discord import _is_table_row, _wrap_markdown_tables


class TestRowDetection:
    def test_header_row_is_a_row(self) -> None:
        assert _is_table_row("| a | b |") is True

    def test_indented_row_is_a_row(self) -> None:
        assert _is_table_row("    | a | b |") is True

    def test_prose_with_one_pipe_is_not_a_row(self) -> None:
        assert _is_table_row("use a | b construct") is False

    def test_empty_string_is_not_a_row(self) -> None:
        assert _is_table_row("") is False


class TestPassThrough:
    def test_no_pipes_returned_unchanged(self) -> None:
        text = "Hello, world.\n\nNo tables here."
        assert _wrap_markdown_tables(text) == text

    def test_prose_with_pipes_but_no_alignment_row_unchanged(self) -> None:
        text = "use the `|` operator. another | example here."
        assert _wrap_markdown_tables(text) == text

    def test_already_fenced_table_not_double_wrapped(self) -> None:
        text = "Look:\n```\n| a | b |\n| --- | --- |\n| 1 | 2 |\n```\nDone."
        assert _wrap_markdown_tables(text) == text


class TestWrapping:
    def test_single_table_gets_wrapped(self) -> None:
        text = "| col1 | col2 |\n| --- | --- |\n| a | b |"
        result = _wrap_markdown_tables(text)
        assert result.startswith("```\n")
        assert result.endswith("\n```")
        # Original rows still present, in order.
        assert "| col1 | col2 |" in result
        assert "| --- | --- |" in result
        assert "| a | b |" in result

    def test_table_in_middle_of_prose(self) -> None:
        text = (
            "Here's the data:\n"
            "| col1 | col2 |\n"
            "| --- | --- |\n"
            "| 1 | 2 |\n"
            "| 3 | 4 |\n"
            "Hope that helps."
        )
        result = _wrap_markdown_tables(text)
        # Prose lines untouched, just the table wrapped.
        assert result.startswith("Here's the data:\n```\n")
        assert "\n```\nHope that helps." in result
        # Body rows preserved.
        assert "| 3 | 4 |" in result

    def test_two_tables_in_one_message_each_wrapped(self) -> None:
        text = (
            "First:\n"
            "| a | b |\n"
            "| --- | --- |\n"
            "| 1 | 2 |\n"
            "\n"
            "Second:\n"
            "| x | y |\n"
            "| --- | --- |\n"
            "| 9 | 8 |\n"
        )
        result = _wrap_markdown_tables(text)
        # Each table region opens AND closes a fence.
        assert result.count("```") == 4

    def test_pipe_alignment_specifiers_recognised(self) -> None:
        # `:---`, `---:`, `:---:` are valid GFM alignment markers.
        text = "| a | b | c |\n| :--- | ---: | :---: |\n| 1 | 2 | 3 |"
        result = _wrap_markdown_tables(text)
        assert result.startswith("```\n")
        assert "| :--- | ---: | :---: |" in result

    def test_table_followed_by_fenced_codeblock_doesnt_swallow_fence(self) -> None:
        text = "| a | b |\n| --- | --- |\n| 1 | 2 |\nThen code:\n```\nprint('hi')\n```\n"
        result = _wrap_markdown_tables(text)
        # Original code block preserved exactly.
        assert "```\nprint('hi')\n```" in result
        # And the table got its own wrapping (so total fences = 4).
        assert result.count("```") == 4

    def test_trailing_newline_preserved(self) -> None:
        text_with_newline = "Hello.\n"
        assert _wrap_markdown_tables(text_with_newline) == text_with_newline
        text_no_newline = "Hello."
        assert _wrap_markdown_tables(text_no_newline) == text_no_newline
