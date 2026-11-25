"""Build a local-only white sugar spread quote candidate from explicit inputs."""

from __future__ import annotations

import argparse
import csv
from datetime import datetime
import io
from pathlib import Path
import sys
from zoneinfo import ZoneInfo

from filelock import FileLock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "03_src"))

from agri_research_agent.application.sugar_spreads import QUOTE_FIELDS, SugarQuote, load_quotes
from agri_research_agent.shared.atomic_storage import atomic_write_bytes


CANDIDATES = ROOT / "01_data/candidates/sugar_spreads"


def serialize_quotes(quotes: list[SugarQuote]) -> bytes:
    output = io.StringIO(newline="")
    writer = csv.writer(output, lineterminator="\n")
    writer.writerow(QUOTE_FIELDS)
    for quote in quotes:
        writer.writerow((quote.trade_date, quote.contract, quote.price_type, quote.price,
                         quote.provider, quote.source_url))
    return output.getvalue().encode("utf-8")


def merge_quotes(quotes: list[SugarQuote]) -> list[SugarQuote]:
    merged = {}
    for quote in quotes:
        key = quote.trade_date, quote.contract, quote.price_type
        previous = merged.setdefault(key, quote)
        if (quote.price, quote.provider) != (previous.price, previous.provider):
            raise ValueError(f"同一日期、合约及口径有冲突 {key}")
    return [merged[key] for key in sorted(merged)]


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", required=True, type=Path, help="独立工作区候选目录下的新 CSV 路径")
    parser.add_argument("--input", action="append", required=True, type=Path, help="已核对的同格式报价或上次候选")
    args = parser.parse_args()
    target = args.output.resolve()
    if not target.is_relative_to(CANDIDATES) or target.suffix.lower() != ".csv":
        parser.error(f"本地候选输出必须位于 {CANDIDATES}")
    if target.exists():
        parser.error("输出已存在。请另选候选路径，并通过 --input 接续已验证数据。")
    cutoff = datetime.now(ZoneInfo("Asia/Shanghai")).date()
    quotes = [quote for path in args.input for quote in load_quotes(path, as_of=cutoff)]
    merged = merge_quotes(quotes)
    if not merged:
        parser.error("无有效行情，未写入候选")
    target.parent.mkdir(parents=True, exist_ok=True)
    with FileLock(str(target) + ".lock", timeout=0):
        if target.exists():
            parser.error("输出已被另一个候选任务占用，请另选路径")
        atomic_write_bytes(target, serialize_quotes(merged))
    print(f"{target}  {len(merged)} 条，{merged[0].trade_date} 至 {merged[-1].trade_date}")


if __name__ == "__main__":
    main()
