"""Daily report: the latest saved one (``ttk daily-report``, 9:00 Eastern) or a day's."""

from __future__ import annotations

from datetime import date
from pathlib import Path
from typing import Annotated

from fastapi import APIRouter, HTTPException, Query, Request

from ttk.config import Settings
from ttk.services.daily_report import load_report, report_days

router = APIRouter()


def _data_dir(settings: Settings) -> Path:
    return Path(settings.database_url.removeprefix("sqlite:///")).parent


@router.get("/api/daily-report")
def daily_report(
    request: Request, day: Annotated[date | None, Query(alias="date")] = None
) -> dict[str, object]:
    data_dir = _data_dir(request.app.state.settings)
    report = load_report(data_dir, day)
    if report is None:
        raise HTTPException(
            404,
            "No daily report saved yet for that day: it is built each morning at 9:00 ET "
            "(or run `ttk daily-report`).",
        )
    return {"report": report, "days": report_days(data_dir)}
