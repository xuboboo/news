"""多来源采集调度：同一把 crawler 锁内顺序采集全部已接入数据源，合并统计。

已接入：IT之家（列表页即频道真相）/ 快科技（文章页面包屑即频道真相）。
新来源接入：实现 ingest_all(db) 返回同构 dict → 在 SOURCES 注册一行。
loop.py 与 tasks.py 的 crawler_task 均经此调度，detail 展示用 format_detail。
"""
import logging

from sqlalchemy.orm import Session

from app.crawler import ithome, mydrivers

logger = logging.getLogger(__name__)

# (展示名, 模块)：顺序即采集次序
SOURCES: list[tuple[str, object]] = [
    ("IT之家", ithome),
    (mydrivers.SOURCE_NAME, mydrivers),
]


def ingest_all_sources(db: Session) -> dict:
    """顺序采集所有来源并合并结果。

    合并后的 dict 保持与单来源 ingest_all 同构的键（scanned/new/skipped/rechanneled/
    new_ids/per_channel），调用方无需区分来源；另带 per_source 分源明细供 detail 展示。
    """
    merged: dict = {
        "scanned": 0,
        "new": 0,
        "skipped": 0,
        "rechanneled": 0,
        "new_ids": [],
        "per_channel": {},
    }
    per_source: dict[str, dict] = {}
    for name, module in SOURCES:
        # 单源异常隔离：一个来源失败不拖累另一个来源当轮零采集。
        # rollback 必须做——失败事务的 PendingRollback 状态会让下一来源首条查询即炸
        try:
            result = module.ingest_all(db)
        except Exception:
            logger.exception("source %s ingest failed, skipping this round", name)
            db.rollback()
            per_source[name] = {"new_ids": [], "error": "采集失败（详见 crawler 日志）"}
            continue
        per_source[name] = result
        merged["scanned"] += result.get("scanned", 0)
        merged["new"] += len(result.get("new_ids", []))
        merged["skipped"] += result.get("skipped", 0)
        merged["rechanneled"] += result.get("rechanneled", 0)
        merged["new_ids"].extend(result.get("new_ids", []))
        for ch, n in (result.get("per_channel") or {}).items():
            merged["per_channel"][ch] = merged["per_channel"].get(ch, 0) + n

    merged["per_source"] = per_source
    logger.info(
        "multi-source crawl done: %s",
        {name: len(r.get("new_ids", [])) for name, r in per_source.items()},
    )
    return merged


def has_source_error(result: dict) -> bool:
    """任一来源本轮采集异常（供任务层把部分失败标为 degraded，不再静默记成功）。"""
    return any(r.get("error") for r in (result.get("per_source") or {}).values())


def format_detail(result: dict) -> str:
    """task_run detail：分源「来源 新增X · 扫描Y · 过滤Z」+ 全局频道新增分布。"""
    parts: list[str] = []
    for name, r in (result.get("per_source") or {}).items():
        if r.get("error"):
            parts.append(f"{name} {r['error']}")
            continue
        seg = "%s 新增 %d · 扫描 %d" % (name, len(r.get("new_ids", [])), r.get("scanned", 0))
        if r.get("rechanneled"):
            seg += " · 频道纠偏 %d" % r["rechanneled"]
        if r.get("filtered"):
            seg += " · 白名单过滤 %d" % r["filtered"]
        parts.append(seg)
    detail = " ｜ ".join(parts)
    per_channel = result.get("per_channel") or {}
    if per_channel:
        detail += " ｜ 新增分布：" + " / ".join(f"{ch} {n}" for ch, n in per_channel.items())
    return detail
