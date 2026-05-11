#!/usr/bin/env python3
"""
Daily snapshot export for an active RIECS labelling session.

Intended to be run by Windows Task Scheduler at 23:50 each day.
Saves output/session_{id}/YYYYMMDD_label_results_session_{id}.xlsx when:
  (a) a session is currently active, AND
  (b) data has changed since the last daily export (or no export exists yet).

When the admin ends the session via the web UI, admin.py generates a
_final.xlsx and zips the whole session folder automatically.
"""

import asyncio
import sys
from datetime import date, datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from sqlalchemy import func, select

from app.database import SessionLocal
from app.models import (
    AddedLabel,
    MandatoryClassification,
    Session as ReviewSession,
    StoryRejection,
    StoryRelevance,
)
from app.routers.export import generate_workbook


async def _active_session(db) -> ReviewSession | None:
    result = await db.execute(
        select(ReviewSession)
        .where(ReviewSession.ended_at.is_(None))
        .order_by(ReviewSession.started_at.desc())
    )
    return result.scalar_one_or_none()


async def _last_change(db, session_id: int) -> datetime | None:
    """Return the most recent write timestamp across all labelling tables."""
    candidates: list[datetime] = []
    for model, col in [
        (AddedLabel,              AddedLabel.created_at),
        (MandatoryClassification, MandatoryClassification.updated_at),
        (StoryRejection,          StoryRejection.updated_at),
        (StoryRelevance,          StoryRelevance.updated_at),
    ]:
        r = await db.execute(
            select(func.max(col)).where(model.session_id == session_id)
        )
        ts = r.scalar()
        if ts:
            candidates.append(ts)
    return max(candidates) if candidates else None


async def main() -> None:
    async with SessionLocal() as db:
        session = await _active_session(db)
        if not session:
            print("No active session — nothing to do.")
            return

        session_dir = Path("output") / f"session_{session.id}"
        session_dir.mkdir(parents=True, exist_ok=True)

        today_str = date.today().strftime("%Y%m%d")

        # Already ran today?
        if list(session_dir.glob(f"{today_str}_*.xlsx")):
            print(f"Export already exists for {today_str} — skipping.")
            return

        # Any labelling activity in this session at all?
        last_dt = await _last_change(db, session.id)
        if not last_dt:
            print("No labelling data yet — skipping.")
            return

        # Changed since last export?
        existing = sorted(session_dir.glob("????????_*.xlsx"))
        if existing:
            last_file_date_str = existing[-1].name[:8]
            try:
                last_file_date = datetime.strptime(last_file_date_str, "%Y%m%d").date()
                if last_dt.date() <= last_file_date:
                    print(f"No new changes since {last_file_date} — skipping.")
                    return
            except ValueError:
                pass

        wb, _ = await generate_workbook(db, session.id)
        out_path = session_dir / f"{today_str}_label_results_session_{session.id}.xlsx"
        wb.save(out_path)
        print(f"Exported: {out_path}  ({out_path.stat().st_size:,} bytes)")


if __name__ == "__main__":
    asyncio.run(main())
