import argparse

import pytest

from ttk.cli import ROOT, build_parser, handlers, main


def _commands(parser: argparse.ArgumentParser) -> set[str]:
    (sub,) = [a for a in parser._actions if isinstance(a, argparse._SubParsersAction)]
    return set(sub.choices)


def test_every_command_has_exactly_one_handler() -> None:
    assert _commands(build_parser()) == set(handlers())


def test_root_is_the_repository() -> None:
    assert (ROOT / "pyproject.toml").exists() and (ROOT / "migrations").is_dir()


def test_unknown_command_is_an_argparse_error() -> None:
    with pytest.raises(SystemExit) as exc:
        main(["no-such-command"])
    assert exc.value.code == 2
