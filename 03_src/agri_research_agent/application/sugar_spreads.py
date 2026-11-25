"""Validated exact-contract daily quotes and white sugar spread calculations."""

from __future__ import annotations

import csv
from dataclasses import dataclass
from datetime import date, timedelta
from decimal import Decimal
import gzip
from hashlib import sha256
import io
import json
from pathlib import Path
import re
from urllib.parse import urlsplit


DATA_PATH = Path("processed/sugar_spreads/daily_quotes.csv")
DOMESTIC_CURVE_PATH = Path("processed/sugar_spreads/domestic_curve.csv")
PREVIEW_ROOT = Path("01_data/candidates/sugar_spreads/preview")
PREVIEW_RELEASES = DATA_PATH.parent / "preview-releases"
PREVIEW_FILES = (DATA_PATH.name, DOMESTIC_CURVE_PATH.name, "historical_spread_database.parquet")
SERVER_RELEASES = DATA_PATH.parent / "official-releases"
SERVER_SCHEMA = "sugar-spread-quotes/1"
MARKET_FILES = {"domestic": DOMESTIC_CURVE_PATH.name, "foreign": DATA_PATH.name}
QUOTE_FIELDS = ("trade_date", "contract", "price_type", "price", "provider", "source_url")
CONTRACT = re.compile(r"(SB|SR|W)(20\d{2})(0[1-9]|1[0-2])")
MONTHS = {"SB": (3, 5, 7, 10), "W": (3, 5, 8, 10, 12), "SR": (1, 3, 5, 7, 9, 11)}
RAW_PAIRS = {"03–05": (3, 5), "05–07": (5, 7), "07–10": (7, 10), "10–次年03": (10, 3)}
PREMIUM_PAIRS = {"03月": (3, 3), "05月": (5, 5), "10月": (10, 10)}
CENTS_PER_POUND_TO_USD_PER_TONNE = Decimal(10) / Decimal("0.45359237")


@dataclass(frozen=True)
class SugarQuote:
    trade_date: date
    contract: str
    price_type: str
    price: Decimal
    provider: str
    source_url: str


@dataclass(frozen=True)
class SugarSpread:
    left: SugarQuote
    right: SugarQuote
    value: Decimal

    @property
    def trade_date(self) -> date:
        return self.left.trade_date

    @property
    def delivery_month(self) -> date:
        return contract_month(self.left.contract)


def contract_month(contract: str) -> date:
    match = CONTRACT.fullmatch(contract)
    if match is None or int(match[3]) not in MONTHS[match[1]]:
        raise ValueError(f"无效的白糖期货合约 {contract}")
    return date(int(match[2]), int(match[3]), 1)


def load_quotes(path: Path, *, as_of: date) -> list[SugarQuote]:
    """Read one complete snapshot. Any ambiguous observation fails the snapshot."""
    quotes = []
    keys = set()
    opener = gzip.open if path.suffix == ".gz" else open
    with opener(path, "rt", encoding="utf-8-sig", newline="") as stream:
        reader = csv.DictReader(stream)
        if tuple(reader.fieldnames or ()) != QUOTE_FIELDS:
            raise ValueError("行情文件字段不符")
        for row in reader:
            if None in row or not all(row.values()):
                raise ValueError(f"行情文件第 {reader.line_num} 行字段不完整")
            try:
                day = date.fromisoformat(row["trade_date"])
                maturity = contract_month(row["contract"])
                price = Decimal(row["price"])
                source = urlsplit(row["source_url"])
            except (ValueError, ArithmeticError) as exc:
                raise ValueError(f"行情文件第 {reader.line_num} 行格式无效") from exc
            key = day, row["contract"], row["price_type"]
            # SR can still trade during its delivery month. The exchange daily
            # table supplies actual closes and thus bounds the listed sessions.
            expired = day.replace(day=1) > maturity if row["contract"].startswith("SR") else day >= maturity
            if (day.isoformat() != row["trade_date"] or day.weekday() >= 5
                    or day > as_of or expired
                    or row["price_type"] not in {"close", "settlement"}
                    or not price.is_finite() or price <= 0
                    or not row["provider"].strip() or source.scheme != "https"
                    or not source.hostname or source.username is not None
                    or key in keys):
                raise ValueError(f"行情文件第 {reader.line_num} 行日期、价格、来源或唯一键无效")
            keys.add(key)
            quotes.append(SugarQuote(day, row["contract"], row["price_type"], price,
                                     row["provider"], row["source_url"]))
    return sorted(quotes, key=lambda quote: (quote.trade_date, quote.contract, quote.price_type))


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


def resolve_preview_release(project_root: Path, data_root: Path) -> Path | None:
    """Pin one local release for all three charts before any reader opens a file."""
    project = project_root.resolve()
    preview = project / PREVIEW_ROOT
    if data_root.resolve() != preview or not (project / ".git").is_file():
        return None
    base = preview / PREVIEW_RELEASES
    pointer = base / "current.json"
    if not pointer.exists():
        return None
    if pointer.resolve() != pointer:
        raise ValueError("白糖预览更新指针路径异常")
    payload = json.loads(pointer.read_text(encoding="utf-8"))
    if not isinstance(payload, dict) or set(payload) != {"release_id", "files"}:
        raise ValueError("白糖预览更新清单异常")
    identity, files = payload["release_id"], payload["files"]
    if (not isinstance(identity, str) or re.fullmatch(r"[a-f0-9]{24}", identity) is None
            or not isinstance(files, dict) or set(files) != set(PREVIEW_FILES)):
        raise ValueError("白糖预览快照身份异常")
    release = base / identity
    for name, expected in files.items():
        path = release / name
        if path.resolve() != path or sha256(path.read_bytes()).hexdigest() != expected:
            raise ValueError(f"白糖预览文件校验失败。{name}")
    return release


def server_snapshots(base: Path, pointer: bytes) -> dict[str, Path]:
    """Verify the exact market files named by an atomic publication pointer."""
    payload = json.loads(pointer.decode("utf-8", errors="strict"))
    if (not isinstance(payload, dict) or set(payload) != {"schema_version", "markets"}
            or payload["schema_version"] != SERVER_SCHEMA or not isinstance(payload["markets"], dict)
            or not set(payload["markets"]) <= MARKET_FILES.keys()):
        raise ValueError("白糖正式行情清单无效")
    paths = {}
    for market, digest in payload["markets"].items():
        if not isinstance(digest, str) or re.fullmatch(r"[a-f0-9]{64}", digest) is None:
            raise ValueError("白糖正式行情哈希无效")
        target = base / "snapshots" / (digest + ".csv.gz")
        if target.resolve() != target.absolute() or sha256(target.read_bytes()).hexdigest() != digest:
            raise ValueError(f"白糖正式行情校验失败。{market}")
        paths[market] = target
    return paths


def resolve_server_quotes(data_root: Path) -> dict[str, Path]:
    base = data_root / SERVER_RELEASES
    pointer = base / "current.json"
    if not pointer.exists() and not pointer.is_symlink():
        return {}
    if pointer.resolve() != pointer.absolute():
        raise ValueError("白糖正式行情指针路径异常")
    return server_snapshots(base, pointer.read_bytes())


def spread_history(
    quotes: list[SugarQuote], near_month: int, far_month: int, *, premium: bool, price_type: str,
) -> list[SugarSpread]:
    """Pair actual two-leg observations on the same market date and basis."""
    prices = {(quote.trade_date, quote.contract): quote
              for quote in quotes if quote.price_type == price_type}
    result = []
    market = "W" if premium else "SB"
    for left in prices.values():
        if not left.contract.startswith(market):
            continue
        maturity = contract_month(left.contract)
        if maturity.month != near_month:
            continue
        right_year = maturity.year + (far_month < near_month)
        right = prices.get((left.trade_date, f"SB{right_year:04d}{far_month:02d}"))
        if right is None or left.provider != right.provider:
            continue
        converted = right.price * CENTS_PER_POUND_TO_USD_PER_TONNE if premium else right.price
        result.append(SugarSpread(left, right, left.price - converted))
    return sorted(result, key=lambda point: (point.trade_date, point.delivery_month))


def latest_pair(points: list[SugarSpread], reference_date: date) -> SugarSpread | None:
    """Prefer the nearest pair observed within a week of the latest market date."""
    recent = [point for point in points if reference_date - timedelta(days=7) <= point.trade_date <= reference_date]
    if recent:
        maturity = min(point.delivery_month for point in recent)
        return max((point for point in recent if point.delivery_month == maturity),
                   key=lambda point: point.trade_date)
    return points[-1] if points else None
