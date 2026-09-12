"""价目表种子：把本部署实际在用的真实计费模型写入 model_price（幂等，可重跑）。

背景（2026-09-12 审计）：价目表无内置种子，新部署成本看板全部按 ¥0 计、面板
长期挂「未计价盲区」告警。本脚本只 seed 当前链路实际产生账单的模型：

- deepseek-flash：DeepSeek 官方定价页（api-docs.deepseek.com/quick_start/pricing，
  2026-09 核实，USD 计价）。系统按北京时间自然日记账，无法区分供应商高峰/错峰
  （高峰=UTC 周一~五 01:00-04:00 与 06:00-10:00，恰覆盖北京时间上午），统一取
  高峰价保守口径——账面成本 ≥ 真实账单，不会反向低估。缓存命中价 price_cache
  取高峰 $0.006。换算按 1 USD = 7.2 CNY，与实际账单币种核对后可在面板改价。
- BAAI/bge-small-zh-v1.5：本地自托管 embedding（docker-compose.override.yml），
  无按量账单，不入价目表（已在 usage_service.LOCAL_MODELS 豁免盲区告警）。

已有价目一律不覆盖（面板手工调过的价不被冲掉）；新部署重跑即得完整价目。
用法（容器内）：python scripts/seed_model_prices.py
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.core.db import SessionLocal
from app.models.usage import ModelPrice

# (model, 输入价/百万token·元, 输出价/百万token·元, 缓存命中价/百万token·元或 None)
# 换算：deepseek-flash 高峰价 $0.30 / $1.20 / $0.006 每百万 token × 7.2
# deepseek-v4-flash：官方定价页注明旧名"已由 Flash 模型接管并按 Flash 价格计费"，
# 同价配齐（历史流水仍在 30 天窗口内按此名分组）；"m" 为审计脚本用例的假模型名
# （0 token，0 价声明收口盲区告警），新部署无此流水可从价目表删除该行。
SEED_PRICES = [
    ("deepseek-flash", 2.16, 8.64, 0.0432),
    ("deepseek-v4-flash", 2.16, 8.64, 0.0432),
    ("m", 0.0, 0.0, None),
]


def main() -> None:
    db = SessionLocal()
    try:
        created = skipped = 0
        for model, pin, pout, pcache in SEED_PRICES:
            row = db.get(ModelPrice, model)
            if row is not None:
                skipped += 1
                print(f"  = {model}: 已有价目（输入 {row.price_input} / 输出 {row.price_output}），不覆盖")
                continue
            db.add(ModelPrice(model=model, price_input=pin, price_output=pout, price_cache=pcache))
            created += 1
            print(f"  + {model}: 输入 {pin} / 输出 {pout} / 缓存 {pcache}（元/百万 token）")
        db.commit()
        print(f"done: created={created} skipped={skipped}")
    finally:
        db.close()


if __name__ == "__main__":
    main()
