from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path

import pytest
from sqlalchemy.orm import Session, sessionmaker

from ttk.cli import migrate
from ttk.db.session import make_engine, make_session_factory


@pytest.fixture
def database_url(tmp_path: Path) -> str:
    url = f"sqlite:///{(tmp_path / 'test.db').as_posix()}"
    migrate(url)
    return url


@pytest.fixture
def session_factory(database_url: str) -> Iterator[sessionmaker[Session]]:
    engine = make_engine(database_url)
    yield make_session_factory(engine)
    engine.dispose()
