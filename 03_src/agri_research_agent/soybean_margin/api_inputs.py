"""Verified daily API snapshots, independent of Public Current and CNF."""
from __future__ import annotations

from datetime import date, datetime
import hashlib
import json
import os
from pathlib import Path
import re
import tempfile
from zoneinfo import ZoneInfo

from filelock import FileLock
import pandas as pd

from .model import FIELDS, KEY, ORIGINS, contracts, number

SCHEMA = "soybean-api-inputs/1"
CUTOVER = date(2026, 10, 8)
FORWARD_FROM_MONTHS = 3
MAX_BYTES = 256 * 1024


def encoded(value) -> bytes:
    return (json.dumps(value, ensure_ascii=False, sort_keys=True,
                       separators=(",", ":"), allow_nan=False) + "\n").encode("utf-8")


def _unique_json(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("重复快照字段")
        result[key] = value
    return result


def _read(path: Path):
    if path.is_symlink() or path.stat().st_size > MAX_BYTES:
        raise ValueError("行情快照路径或大小无效")
    raw = path.read_bytes()
    return json.loads(raw.decode("utf-8"), object_pairs_hook=_unique_json), raw


def select_fx(curve: dict, tenor: int):
    """1-2M uses spot; >=3M uses the shipment's actual tenor, without extrapolation."""
    normalized = {int(k): number(v) for k, v in curve.items()}
    valid = {k: v for k, v in normalized.items() if v is not None and v > 0}
    if tenor < FORWARD_FROM_MONTHS:
        return valid.get(0), "spot", False, 0, 0
    if tenor in valid:
        return valid[tenor], "forward", False, tenor, tenor
    lower = max((k for k in valid if 0 < k < tenor), default=None)
    upper = min((k for k in valid if k > tenor), default=None)
    if lower is None or upper is None:
        return None, "forward", False, lower, upper
    rate = valid[lower] + (tenor-lower)/(upper-lower)*(valid[upper]-valid[lower])
    return rate, "forward", True, lower, upper


def validate_snapshot(value: dict) -> date:
    if not isinstance(value, dict) or value.get("schema_version") != SCHEMA:
        raise ValueError("行情快照版本无效")
    day = date.fromisoformat(value["business_date"])
    if day < CUTOVER or day.weekday() >= 5:
        raise ValueError("行情快照业务日期无效")
    captured = datetime.fromisoformat(value["captured_at"])
    if captured.utcoffset() is None or captured.astimezone(ZoneInfo("Asia/Shanghai")).date() != day:
        raise ValueError("行情快照缺少采集时区")
    if value.get("fx_currency") != "USD/CNY" or value.get("fx_unit") != "CNY_per_USD":
        raise ValueError("汇率币种或单位无效")
    if value.get("cbot_unit") != "US_cents/bushel" or value.get("domestic_unit") != "CNY/tonne":
        raise ValueError("行情单位无效")
    for key in ("cbot", "domestic", "fx_curve", "sources", "errors"):
        if not isinstance(value.get(key), dict):
            raise ValueError("行情快照字段不完整")
    expected_cbot = {contracts(day, m)[0] for m in range(1, 13)}
    expected_dce = {prefix+contracts(day, m)[1] for prefix in ("M", "Y") for m in range(1, 13)}
    if set(value["cbot"]) != expected_cbot or set(value["domestic"]) != expected_dce:
        raise ValueError("行情快照合约集合不符")
    if any(str(k) not in {"0", "1", "3", "6", "9", "12"} for k in value["fx_curve"]):
        raise ValueError("汇率期限无效")
    for quotes in (value["cbot"], value["domestic"], value["fx_curve"]):
        for item in quotes.values():
            if item is not None and (number(item) is None or number(item) <= 0):
                raise ValueError("行情快照数值无效")
    if set(value["sources"]) != {"cbot", "fx", "domestic"}:
        raise ValueError("行情快照缺少来源")
    if any(not isinstance(item, dict) for item in value["sources"].values()):
        raise ValueError("行情来源字段无效")
    if any(not isinstance(k, str) or not isinstance(v, str) for k, v in value["errors"].items()):
        raise ValueError("行情错误码无效")
    return day


def _atomic(path: Path, raw: bytes):
    descriptor, temporary = tempfile.mkstemp(prefix=".snapshot-", dir=path.parent)
    try:
        with os.fdopen(descriptor, "wb") as stream:
            stream.write(raw)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def publish(root: Path, snapshot: dict, *, authorize, expected_identity: str | None = None):
    """Append immutable revision, then atomically select it under a per-day lock."""
    day = validate_snapshot(snapshot)
    raw = encoded(snapshot)
    if len(raw) > MAX_BYTES:
        raise ValueError("行情快照大小无效")
    root = Path(root).absolute()
    authorize(root)
    if root != root.resolve():
        raise ValueError("行情存储不允许路径别名")
    directory = root / day.isoformat()
    authorize(directory)
    directory.mkdir(parents=True, exist_ok=True)
    authorize(directory / ".lock")
    with FileLock(str(directory / ".lock"), timeout=10):
        if expected_identity is not None:
            current = read_day(root, day)
            if current is None or hashlib.sha256(encoded(current)).hexdigest() != expected_identity:
                raise ValueError("行情快照并发版本冲突")
        identity = hashlib.sha256(raw).hexdigest()
        target = directory / f"{identity}.json"
        authorize(target)
        if target.exists():
            if target.read_bytes() != raw:
                raise ValueError("行情快照身份冲突")
        else:
            with target.open("xb") as stream:
                stream.write(raw)
                stream.flush()
                os.fsync(stream.fileno())
        pointer = directory / "current.json"
        authorize(pointer)
        _atomic(pointer, encoded(dict(schema_version=SCHEMA, business_date=day.isoformat(),
                                      sha256=identity, filename=target.name)))
    return identity


def read_day(root: Path, day: date):
    directory = Path(root) / day.isoformat()
    pointer = directory / "current.json"
    if not pointer.exists():
        return None
    if directory.is_symlink():
        raise ValueError("行情目录不允许符号链接")
    index, _ = _read(pointer)
    if not isinstance(index, dict):
        raise ValueError("行情快照指针无效")
    sha = index.get("sha256", "")
    if (index.get("schema_version") != SCHEMA or index.get("business_date") != day.isoformat()
            or not re.fullmatch(r"[a-f0-9]{64}", sha)
            or index.get("filename") != sha+".json"):
        raise ValueError("行情快照指针无效")
    value, raw = _read(directory / index["filename"])
    if hashlib.sha256(raw).hexdigest() != sha or validate_snapshot(value) != day:
        raise ValueError("行情快照身份校验失败")
    manual = value["sources"]["cbot"].get("manual")
    if manual:
        evidence = manual["evidence"]
        identity = evidence.get("sha256", "")
        if (not re.fullmatch(r"[a-f0-9]{64}", identity)
                or evidence.get("filename") not in {identity + ".png", identity + ".jpg"}):
            raise ValueError("人工报价证据指针无效")
        target = Path(root) / "evidence" / evidence["filename"]
        if target.is_symlink() or target.parent.is_symlink() or target.stat().st_size > 10 * 1024 * 1024:
            raise ValueError("人工报价证据文件无效")
        if hashlib.sha256(target.read_bytes()).hexdigest() != identity:
            raise ValueError("人工报价证据身份不符")
    return value


def archive_evidence(root: Path, source: Path, *, authorize):
    source = Path(source)
    if not source.is_file() or source.stat().st_size > 10 * 1024 * 1024:
        raise ValueError("人工报价证据文件无效")
    raw = source.read_bytes()
    suffix = ".png" if raw.startswith(b"\x89PNG\r\n\x1a\n") else ".jpg" if raw.startswith(b"\xff\xd8\xff") else None
    if suffix is None:
        raise ValueError("人工报价证据必须是PNG或JPEG")
    identity = hashlib.sha256(raw).hexdigest()
    directory = Path(root) / "evidence"
    authorize(directory)
    directory.mkdir(parents=True, exist_ok=True)
    target = directory / (identity + suffix)
    authorize(target)
    if not target.exists():
        try:
            with target.open("xb") as stream:
                stream.write(raw)
                stream.flush()
                os.fsync(stream.fileno())
        except FileExistsError:
            pass
    if target.read_bytes() != raw:
        raise ValueError("人工报价证据身份冲突")
    return dict(sha256=identity, filename=target.name)


def read_days(root: Path, as_of: date):
    if not Path(root).exists():
        return {}
    if Path(root).is_symlink():
        raise ValueError("行情存储不允许符号链接")
    result = {}
    size = 0
    for item in sorted(Path(root).iterdir()):
        if re.fullmatch(r"\d{4}-\d{2}-\d{2}", item.name):
            day = date.fromisoformat(item.name)
            if CUTOVER <= day <= as_of:
                value = read_day(root, day)
                if value is not None:
                    result[day] = value
                    size += len(encoded(value))
                    if len(result) > 4096 or size > 64 * 1024 * 1024:
                        raise ValueError("行情快照集合超限")
    return result


def apply_api_inputs(history: pd.DataFrame, snapshots: dict, as_of: date):
    """Keep legacy history, replace new-period inputs even when today's API is missing."""
    days = set(snapshots) | {as_of}
    data = history.copy()
    for snapshot_day, snapshot in snapshots.items():
        if validate_snapshot(snapshot) != snapshot_day:
            raise ValueError("行情快照日期与索引不符")
    keys = []
    for day in days:
        if day < CUTOVER or day > as_of or day.weekday() >= 5:
            continue
        for origin in ORIGINS:
            for month in range(1, 13):
                keys.append(dict(business_date=day, origin=origin,
                                 shipment_year=contracts(day, month)[2], shipment_month=month))
    all_keys = pd.concat([data[KEY], pd.DataFrame(keys, columns=KEY)]).drop_duplicates(KEY)
    data = all_keys.merge(data, on=KEY, how="left", validate="one_to_one")
    for field in FIELDS[1:]:
        data[field] = pd.to_numeric(data.get(field, pd.Series(index=data.index, dtype=float)), errors="coerce").astype(float)
    for field in ("fx_rate_kind", "soymeal_contract_code", "soyoil_contract_code", "commodity", "fx_is_interpolated"):
        data[field] = data.get(field, pd.Series(index=data.index, dtype=object)).astype(object)
    for index, row in data.loc[data.business_date.ge(CUTOVER)].iterrows():
        day, month = row.business_date, int(row.shipment_month)
        cbot, domestic, year = contracts(day, month)
        snapshot = snapshots.get(day)
        for field in FIELDS[1:]:
            data.loc[index, field] = None
        data.loc[index, "cbot_contract_year"] = 2000+int(cbot[:2])
        data.loc[index, "cbot_contract_month"] = int(cbot[2:])
        data.loc[index, "soymeal_contract_code"] = "M"+domestic
        data.loc[index, "soyoil_contract_code"] = "Y"+domestic
        data.loc[index, "commodity"] = "soybean"
        tenor = (year-day.year)*12+month-day.month
        value, kind, interpolated, lower, upper = select_fx(snapshot["fx_curve"] if snapshot else {}, tenor)
        data.loc[index, "fx_value"] = value
        data.loc[index, "fx_target_tenor"] = tenor
        data.loc[index, "fx_rate_kind"] = kind
        data.loc[index, "fx_is_interpolated"] = interpolated
        data.loc[index, "fx_lower_tenor"] = lower
        data.loc[index, "fx_upper_tenor"] = upper
        if snapshot:
            data.loc[index, FIELDS[1]] = snapshot["cbot"].get(cbot)
            data.loc[index, FIELDS[3]] = snapshot["domestic"].get("M"+domestic)
            data.loc[index, FIELDS[4]] = snapshot["domestic"].get("Y"+domestic)
    return data
