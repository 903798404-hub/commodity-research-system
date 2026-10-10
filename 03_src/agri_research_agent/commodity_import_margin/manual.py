"""Evidence-backed partial manual CNF entries through the existing CAS writer."""
from datetime import date, datetime
import hashlib
import json
from pathlib import Path

from .model import PROFILES, daily_rows, number
from .store import load_cnf, save_cnf, read_market
from .live import capture
from zoneinfo import ZoneInfo

SHANGHAI = ZoneInfo("Asia/Shanghai")


def import_manual_cnf(path: Path, request_file: Path, *, expected_version: int, authorize, now=None, refresh_current=False):
    now = now or datetime.now(SHANGHAI)
    if request_file.stat().st_size > 16 * 1024:
        raise ValueError("人工报价文件超出16KiB上限")
    raw = request_file.read_bytes()
    if len(raw) > 16 * 1024:
        raise ValueError("人工报价文件超出16KiB上限")
    value = json.loads(raw.decode("utf-8", errors="strict"))
    if not isinstance(value, dict) or set(value) != {"business_date", "commodity", "unit", "source", "quotes"}:
        raise ValueError("人工报价文件字段无效")
    day = date.fromisoformat(value["business_date"])
    if now.tzinfo is None or day > now.astimezone(SHANGHAI).date():
        raise ValueError("人工报价业务日期不能晚于实际录入日期")
    commodity = value["commodity"]
    if commodity not in PROFILES or value["unit"] != "USD/tonne":
        raise ValueError("人工报价品种或单位无效")
    source = value["source"]
    if not isinstance(source, str) or not source.strip() or len(source) > 512:
        raise ValueError("人工报价须注明来源")
    quotes = value["quotes"]
    if not isinstance(quotes, dict) or not quotes:
        raise ValueError("人工报价船期为空")
    periods = {r["shipment_period"]: r["shipment_month"] for r in daily_rows(day, commodity, {})}
    if not set(quotes) <= set(periods):
        raise ValueError("人工报价须使用未来12个月的完整船期年月")
    if any(v is not None and (number(v) is None or number(v) < 0) for v in quotes.values()):
        raise ValueError("完整CNF必须为非负有限数值或空值")
    existing, version = load_cnf(path, day, commodity)
    if version != expected_version:
        raise ValueError("CNF版本已变化，请核对后重试")
    merged = {m: existing.get(m) for m in range(1,13)}
    merged.update({periods[p]: number(v) for p,v in quotes.items()})
    provenance = dict(entry_method="manual_codex", source=source.strip(), unit="USD/tonne",
        entered_at=now.astimezone(SHANGHAI).isoformat(), quote_time=None,
        request_sha256=hashlib.sha256(raw).hexdigest(), provided_periods=sorted(quotes), request=value)
    market, identity = None, None
    if refresh_current and day == now.astimezone(SHANGHAI).date():
        authorize(path)
        _, identity = read_market(path, day, commodity)
        market = capture(commodity)
    return save_cnf(path, day, commodity, merged, expected_version, authorize=authorize,
                    provenance=provenance, market_snapshot=market, expected_identity=identity)
