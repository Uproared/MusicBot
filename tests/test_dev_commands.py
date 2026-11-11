"""
Batch 9: the code-running dev commands are gone (SEC4).
"""

import ast
import pathlib

REPO_ROOT = pathlib.Path(__file__).resolve().parent.parent


def test_debug_and_objgraph_commands_are_gone():
    from musicbot.bot import MusicBot

    # Both ran arbitrary Python typed into Discord for anyone in DevIDs.
    assert not hasattr(MusicBot, "cmd_debug")
    assert not hasattr(MusicBot, "cmd_objgraph")
    # The harmless dev tools stay.
    assert hasattr(MusicBot, "cmd_breakpoint")
    assert hasattr(MusicBot, "cmd_makeini")


def test_no_eval_or_exec_in_the_bot():
    """Guard: nothing in the bot calls the eval/exec builtins."""
    found = []
    for path in [*REPO_ROOT.joinpath("musicbot").rglob("*.py"), REPO_ROOT / "run.py"]:
        tree = ast.parse(path.read_text(encoding="utf8"))
        for node in ast.walk(tree):
            if (
                isinstance(node, ast.Call)
                and isinstance(node.func, ast.Name)
                and node.func.id in ("eval", "exec")
            ):
                found.append(f"{path.name}:{node.lineno}")
    assert found == []
