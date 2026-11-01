"""Database backups: a consistent copy of the live SQLite file, checked before it counts.

SQLite's online backup API copies the database page by page while the collector and
the forward runner keep writing (the write-ahead log included), so nothing has to be
stopped. The copy is written under a temporary name, checked with ``quick_check``
and only then renamed, so a file named ``time_to_kill-<stamp>.db`` is always a whole,
readable database.

Retention is opt-in: ``keep`` removes the oldest timestamped backups beyond that
number, and only files this module named. Anything else in the folder (a hand-made
copy, a pre-migration snapshot) is never touched.
"""

from __future__ import annotations

import re
import sqlite3
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

BACKUP_DIR = "backups"
PREFIX = "time_to_kill-"
NAME = re.compile(r"^time_to_kill-\d{8}-\d{6}\.db$")
PAGES_PER_STEP = 65_536
"""About 256 MB a step at 4 KB pages: writers get the lock back between steps."""


class BackupError(RuntimeError):
    pass


@dataclass
class BackupResult:
    path: Path
    size: int
    check: str
    removed: list[Path] = field(default_factory=list)


def backups(folder: Path) -> list[Path]:
    """This module's backups in ``folder``, oldest first (the stamp sorts by time)."""
    if not folder.exists():
        return []
    return sorted(p for p in folder.iterdir() if NAME.match(p.name))


def backup_database(
    db_path: Path, data_dir: Path, *, now: datetime, keep: int | None = None
) -> BackupResult:
    if not db_path.exists():
        raise BackupError(f"no database at {db_path}")
    if keep is not None and keep < 1:
        raise BackupError("keep must be at least 1")
    folder = data_dir / BACKUP_DIR
    folder.mkdir(parents=True, exist_ok=True)
    final = folder / f"{PREFIX}{now:%Y%m%d-%H%M%S}.db"
    if final.exists():
        raise BackupError(f"{final} already exists")
    partial = final.with_name(final.name + ".partial")
    partial.unlink(missing_ok=True)  # a leftover from an interrupted run of ours
    source = sqlite3.connect(f"file:{db_path.as_posix()}?mode=ro", uri=True)
    target = sqlite3.connect(partial)
    try:
        source.backup(target, pages=PAGES_PER_STEP)
        check = str(target.execute("pragma quick_check").fetchone()[0])
    finally:
        target.close()
        source.close()
    if check != "ok":
        partial.unlink(missing_ok=True)
        raise BackupError(f"the copy failed its integrity check: {check}")
    partial.rename(final)
    removed: list[Path] = []
    if keep is not None:
        for old in backups(folder)[:-keep]:
            old.unlink()
            removed.append(old)
    return BackupResult(final, final.stat().st_size, check, removed)
