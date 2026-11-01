import sqlite3
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from ttk.services.backup import BackupError, backup_database, backups

T0 = datetime(2026, 11, 1, 9, tzinfo=UTC)


def live_db(path: Path) -> None:
    con = sqlite3.connect(path)
    con.execute("pragma journal_mode=wal")
    con.execute("create table t (x integer)")
    con.executemany("insert into t values (?)", [(i,) for i in range(1000)])
    con.commit()
    con.close()


def test_backup_is_a_whole_checked_copy(tmp_path: Path) -> None:
    db = tmp_path / "time_to_kill.db"
    live_db(db)
    writer = sqlite3.connect(db)  # a writer still open, as the collector would be
    writer.execute("insert into t values (1000)")
    writer.commit()
    result = backup_database(db, tmp_path, now=T0)
    writer.close()
    assert result.path.name == "time_to_kill-20261101-090000.db" and result.check == "ok"
    copy = sqlite3.connect(result.path)
    assert copy.execute("select count(*) from t").fetchone()[0] == 1001  # WAL rows included
    copy.close()
    assert not list((tmp_path / "backups").glob("*.partial"))


def test_keep_removes_only_our_oldest_backups(tmp_path: Path) -> None:
    db = tmp_path / "time_to_kill.db"
    live_db(db)
    folder = tmp_path / "backups"
    folder.mkdir()
    other = folder / "time_to_kill.pre-0014.db"  # a pre-migration snapshot: never touched
    other.write_bytes(b"x")
    for i in range(3):
        backup_database(db, tmp_path, now=T0 + timedelta(days=i))
    result = backup_database(db, tmp_path, now=T0 + timedelta(days=3), keep=2)
    assert [p.name for p in result.removed] == [
        "time_to_kill-20261101-090000.db",
        "time_to_kill-20261102-090000.db",
    ]
    assert [p.name for p in backups(folder)] == [
        "time_to_kill-20261103-090000.db",
        "time_to_kill-20261104-090000.db",
    ]
    assert other.exists()


def test_refuses_bad_input(tmp_path: Path) -> None:
    with pytest.raises(BackupError, match="no database"):
        backup_database(tmp_path / "missing.db", tmp_path, now=T0)
    db = tmp_path / "time_to_kill.db"
    live_db(db)
    with pytest.raises(BackupError, match="at least 1"):
        backup_database(db, tmp_path, now=T0, keep=0)
    backup_database(db, tmp_path, now=T0)
    with pytest.raises(BackupError, match="already exists"):
        backup_database(db, tmp_path, now=T0)
