"""Keep the history for a while: ``python -m app.retention --days N [--dry-run]``.

Deletes the runs that finished, completed or failed, more than ``N`` days
ago, with their diagnostics. The jobs still to do, queued or running runs,
are never deleted, nor a run whose pull request comment is still to be
published. Projects and rules are not touched, and a pull request's comment
record forgets the run it showed.

With ``INVARIANT_RETENTION_DAYS`` set, the worker does the same once an hour;
by default history is kept for ever.
"""

import argparse
import logging
from datetime import UTC, datetime, timedelta

from sqlalchemy import delete, func, or_, select
from sqlalchemy.orm import Session

from app.config import Settings
from app.database import SessionLocal
from app.logs import configure_logging
from app.models import AnalysisRun

logger = logging.getLogger(__name__)

BATCH = 500


def _expired(cutoff: datetime):
    return select(AnalysisRun.id).where(
        AnalysisRun.status.in_(("completed", "failed")),
        # Runs older than the timing columns have only their creation time.
        func.coalesce(AnalysisRun.finished_at, AnalysisRun.created_at) < cutoff,
        or_(
            AnalysisRun.feedback_status.is_(None),
            AnalysisRun.feedback_status != "pending",
        ),
    )


def prune(
    db: Session,
    keep: timedelta,
    *,
    now: datetime | None = None,
    dry_run: bool = False,
) -> int:
    """Delete the runs that finished before ``now - keep``; returns how many.

    Deletes in batches, each in its own transaction, so that the worker and
    the API are never held up for long.
    """
    cutoff = (now or datetime.now(UTC)) - keep
    if dry_run:
        return db.scalar(select(func.count()).select_from(_expired(cutoff).subquery()))
    deleted = 0
    while ids := list(
        db.scalars(_expired(cutoff).order_by(AnalysisRun.id).limit(BATCH))
    ):
        db.execute(
            delete(AnalysisRun)
            .where(AnalysisRun.id.in_(ids))
            .execution_options(synchronize_session=False)
        )
        db.commit()
        deleted += len(ids)
    if deleted:
        logger.info(
            "Deleted %s runs that finished more than %s days ago",
            deleted,
            keep.days,
            extra={"event": "retention_pruned", "runs": deleted, "days": keep.days},
        )
    return deleted


def main(argv: list[str] | None = None) -> int:
    settings = Settings()
    parser = argparse.ArgumentParser(
        prog="python -m app.retention",
        description="Delete analysis runs that finished more than DAYS days ago.",
    )
    parser.add_argument(
        "--days",
        type=int,
        default=settings.retention_days or None,
        help="how many days of history to keep (default: INVARIANT_RETENTION_DAYS)",
    )
    parser.add_argument(
        "--dry-run", action="store_true", help="count the runs, delete nothing"
    )
    options = parser.parse_args(argv)
    if options.days is None or options.days < 1:
        parser.error("give --days, at least 1, or set INVARIANT_RETENTION_DAYS")
    configure_logging(settings.log_level, settings.log_format, settings.secrets())
    with SessionLocal() as db:
        count = prune(db, timedelta(days=options.days), dry_run=options.dry_run)
    verb = "would be deleted" if options.dry_run else "deleted"
    print(f"{count} runs {verb}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
