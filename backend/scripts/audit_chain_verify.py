"""审计日志哈希链校验：检测行内容篡改与中间行删除。

链结构：row_hash = sha256(prev_hash ‖ 行内容规范化 JSON)，行按 id 链接；
row_hash 为空的行是哈希链上线前的遗留段，仅统计不校验。
首个链上行允许 prev_hash 指向已被保留期清理（purge）的行，但内容哈希必须自洽。

局限（诚实声明）：哈希链只能发现"部分篡改"——能重写整表的攻击者可重建整条链。
如需更强保证：定期把本脚本输出的链尾 hash 备份到外部（文件/工单/异地），
下次校验时比对。

用法：
  docker compose exec backend python scripts/audit_chain_verify.py
退出码：0 = 通过；1 = 断链/篡改/异常。
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.core.db import SessionLocal  # noqa: E402
from app.models.audit_log import AuditLog  # noqa: E402
from app.services.audit_service import verify_chain_rows  # noqa: E402


def main() -> int:
    db = SessionLocal()
    try:
        rows = db.query(AuditLog).order_by(AuditLog.id).all()
    finally:
        db.close()

    legacy = [r for r in rows if not r.row_hash]
    chained = [r for r in rows if r.row_hash]

    if legacy and chained and legacy[-1].id > chained[0].id:
        # 理论上不可能：哈希链上线后所有新写入都应有 row_hash
        print(f"异常：遗留段与链上行交错（遗留最大 id={legacy[-1].id} > 链上最小 id={chained[0].id}）")

    problems = verify_chain_rows(rows)
    head = chained[-1].row_hash if chained else "(空表/全部为遗留段)"

    print(f"审计日志总量 {len(rows)} 行 = 链前遗留段 {len(legacy)} + 哈希链 {len(chained)}")
    print(f"链尾 row_hash（建议定期备份比对）：{head}")
    if problems:
        print(f"\n校验失败：{len(problems)} 处问题")
        for p in problems[:50]:
            print(f"  - {p}")
        if len(problems) > 50:
            print(f"  ...（其余 {len(problems) - 50} 处略）")
        return 1
    print("\n校验通过：链上所有行内容自洽、链接完整（链首前驱允许已被清理）。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
