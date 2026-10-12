"""BC-8: re-enabled built-ins removed from the disallow list.

The contract says Read, Bash, Grep, Glob, Edit, Write, NotebookEdit are
NOT in the disallow list (so claude_cli can use them on uploaded files),
while Task / WebFetch / WebSearch / TodoWrite / ExitPlanMode /
EnterPlanMode etc. ARE still disallowed (they duplicate or don't fit
Ember's surface). The constant is published from `ember.main`.
"""

from __future__ import annotations


def _import_constant() -> list[str]:
    from ember.main import _DEFAULT_DISALLOWED_BUILTIN_TOOLS

    return _DEFAULT_DISALLOWED_BUILTIN_TOOLS


REENABLED_BUILTINS = [
    "Read",
    "Bash",
    "Grep",
    "Glob",
    "Edit",
    "Write",
    "NotebookEdit",
]

STILL_DISALLOWED = [
    "Task",
    "WebFetch",
    "WebSearch",
    "TodoWrite",
    "ExitPlanMode",
    "EnterPlanMode",
]


class TestReenabledBuiltinsAreNotDisallowed:
    def test_constant_is_iterable(self) -> None:
        const = _import_constant()
        # Must be iterable and the values must be string names.
        for v in const:
            assert isinstance(v, str)

    def test_read_bash_grep_glob_edit_write_notebookedit_are_not_in_list(self) -> None:
        const = _import_constant()
        for name in REENABLED_BUILTINS:
            assert name not in const, (
                f"{name!r} should be removed from _DEFAULT_DISALLOWED_BUILTIN_TOOLS (BC-8)"
            )


class TestStillDisallowedBuiltinsArePresent:
    def test_task_webfetch_websearch_todowrite_exitplan_enterplan_remain(self) -> None:
        const = _import_constant()
        for name in STILL_DISALLOWED:
            assert name in const, (
                f"{name!r} must remain in _DEFAULT_DISALLOWED_BUILTIN_TOOLS "
                "(duplicates an MCP tool or is out of Ember's scope per BC-8)"
            )
