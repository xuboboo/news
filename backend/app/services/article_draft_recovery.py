"""Safe terminal-state recovery for article generation failures."""
import logging

from sqlalchemy import text

from app.core.db import new_session
from app.models.article_draft import ArticleDraft
from app.utils.text import truncate

logger = logging.getLogger(__name__)
LOCK_SQL = "SELECT pg_try_advisory_xact_lock(hashtext('article_draft:' || :i))"


def mark_failed_if_unowned(draft_id: int, generation: int, error: str) -> bool:
    """Mark exactly this generating generation failed, only when no generator owns it.

    The transaction-scoped advisory lock is shared with generate_article_for_draft.
    A failed try means a legitimate concurrent generator is still working; callers
    must leave its status alone. The generation/status predicates are a second fence.
    """
    db = new_session()
    try:
        if db.bind is not None and db.bind.dialect.name == "postgresql":
            if not db.execute(text(LOCK_SQL), {"i": draft_id}).scalar():
                db.rollback()
                return False
        changed = (
            db.query(ArticleDraft)
            .filter(
                ArticleDraft.id == draft_id,
                ArticleDraft.status == "generating",
                ArticleDraft.gen_count == generation,
            )
            .update(
                {ArticleDraft.status: "failed", ArticleDraft.error: truncate(error, 500),
                 ArticleDraft.revision: ArticleDraft.revision + 1},
                synchronize_session=False,
            )
        )
        if changed:
            db.commit()
            return True
        db.rollback()
        return False
    except Exception:
        db.rollback()
        raise
    finally:
        db.close()


def schedule_failure_confirmation(draft_id: int, generation: int, error: str) -> None:
    """Check again after the one-hour task hard limit, without preempting a live owner."""
    from app.tasks.tasks import article_gen_failure_confirm_task

    article_gen_failure_confirm_task.apply_async(
        args=(draft_id, generation, error), countdown=3605,
    )


def recover_failed_generation(draft_id: int, generation: int, error: str) -> None:
    """Best-effort recovery used by Celery parent signal and retry exhaustion."""
    try:
        if not mark_failed_if_unowned(draft_id, generation, error):
            # The owner may be a different legitimate task. A bounded delayed check
            # lets its one-hour hard limit expire before considering this generation.
            schedule_failure_confirmation(draft_id, generation, error)
    except Exception:
        # Recovery must never raise into Celery's worker parent/failure callback.
        logger.exception(
            "article draft recovery failed: draft_id=%s generation=%s",
            draft_id, generation,
        )
