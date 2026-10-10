"""清理两类历史草稿的自动 AI 声明，保留正文、参考来源和生成记录。

默认仅预览；确认备份后使用 --apply 写入。每篇更新增加 revision，避免旧编辑覆盖。
用法（容器内）：python scripts/remove_draft_ai_disclosures.py [--apply]
"""
import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from sqlalchemy import select, text
from sqlalchemy.orm import Session

from app.core.db import SessionLocal
from app.models.article_draft import ArticleDraft
from app.services.article_gen_service import _strip_generated_footer_sections


def clean_draft_disclosures(db: Session, *, apply: bool = False) -> dict:
    ids = list(db.scalars(select(ArticleDraft.id).where(ArticleDraft.channel.in_(("wechat", "site")))))
    db.rollback()
    changed_ids: list[int] = []
    skipped_ids: list[int] = []
    for draft_id in ids:
        try:
            if db.get_bind().dialect.name == "postgresql":
                acquired = db.execute(
                    text("SELECT pg_try_advisory_xact_lock(hashtext('article_draft:' || :i))"),
                    {"i": draft_id},
                ).scalar()
                if not acquired:
                    skipped_ids.append(draft_id)
                    continue
            row = db.scalar(
                select(ArticleDraft).where(ArticleDraft.id == draft_id)
                .with_for_update().execution_options(populate_existing=True)
            )
            if row is None or row.status == "generating":
                skipped_ids.append(draft_id)
                continue
            original = row.content_md or ""
            cleaned = _strip_generated_footer_sections(original, strip_sources=False)
            if cleaned == original:
                continue
            if apply:
                row.content_md = cleaned
                row.revision = (row.revision or 1) + 1
                db.commit()
            changed_ids.append(draft_id)
        finally:
            # 预览、不变和跳过时释放锁；写入后的空事务回滚也不会撤销已提交修改。
            db.rollback()
    return {
        "mode": "apply" if apply else "preview",
        "scanned": len(ids),
        "changed_ids": changed_ids,
        "skipped_ids": skipped_ids,
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--apply", action="store_true", help="实际清理；不传时只预览")
    args = parser.parse_args()
    with SessionLocal() as db:
        print(json.dumps(clean_draft_disclosures(db, apply=args.apply), ensure_ascii=False))


if __name__ == "__main__":
    main()
