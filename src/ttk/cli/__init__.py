"""Command line: ``ttk <command>``; ``ttk --help`` lists them all.

Each area's module registers its commands (``register``) and names a handler
for each (``HANDLERS``); a handler takes the parsed arguments and the settings
and returns the exit code."""

from __future__ import annotations

import argparse

from ttk.cli import betting, forward, ingest, ops, research
from ttk.cli._common import ROOT, Handler, migrate
from ttk.config import get_settings

__all__ = ["ROOT", "build_parser", "handlers", "main", "migrate"]

MODULES = (ops, ingest, research, forward, betting)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="ttk")
    sub = parser.add_subparsers(dest="command", required=True)
    for module in MODULES:
        module.register(sub)
    return parser


def handlers() -> dict[str, Handler]:
    table: dict[str, Handler] = {}
    for module in MODULES:
        overlap = table.keys() & module.HANDLERS.keys()
        if overlap:
            raise RuntimeError(f"commands handled twice: {sorted(overlap)}")
        table.update(module.HANDLERS)
    return table


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    return handlers()[args.command](args, get_settings())
