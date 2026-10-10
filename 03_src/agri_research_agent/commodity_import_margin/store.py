"""Local-only transactional CNF and immutable market revisions; no migration."""
from __future__ import annotations

from datetime import date
from contextlib import contextmanager
import hashlib
import json
import os
from pathlib import Path
import sqlite3

from .inputs import encoded, validate, MAX_BYTES
from .model import START, PROFILES, shipment_year, number

SOURCE = Path(__file__).resolve().parents[3]
DDL = """
CREATE TABLE IF NOT EXISTS cnf_day (
 business_date TEXT NOT NULL, commodity TEXT NOT NULL, version INTEGER NOT NULL,
 payload TEXT NOT NULL, PRIMARY KEY(business_date,commodity));
CREATE TABLE IF NOT EXISTS market_revisions (
 sha256 TEXT PRIMARY KEY, payload TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS market_current (
 business_date TEXT NOT NULL, commodity TEXT NOT NULL, sha256 TEXT NOT NULL,
 PRIMARY KEY(business_date,commodity));
CREATE TABLE IF NOT EXISTS cnf_entry_audit (
 business_date TEXT NOT NULL, commodity TEXT NOT NULL, version INTEGER NOT NULL,
 payload TEXT NOT NULL, provenance TEXT NOT NULL,
 PRIMARY KEY(business_date,commodity,version));
"""


def local_database() -> Path:
    if not os.getenv("LOCALAPPDATA"):
        raise ValueError("本地运行目录未配置")
    base = Path(os.environ["LOCALAPPDATA"]).absolute()
    if not base.is_dir() or base.resolve() != base:
        raise ValueError("本地运行目录不存在或存在路径别名")
    cache = base / "market-data-runtime"
    if cache.is_symlink() or cache.is_junction():
        raise ValueError("本地缓存目录存在路径别名")
    # Windows packaged-app filesystem virtualization is not a junction. Resolve
    # its actual user-owned cache once, then enforce the fixed child namespace.
    resolved_cache = cache.resolve()
    if not resolved_cache.is_relative_to(base):
        raise ValueError("本地缓存目录超出用户目录")
    path = resolved_cache / "canola-palm-local" / "research.sqlite3"
    if path.resolve() != path:
        raise ValueError("本地存储路径存在别名")
    return path


def authorize_local(path: Path) -> None:
    if os.getenv("MARKET_DATA_GIT_HEAD") or os.getenv("MARKET_DATA_EXECUTION_GRANT") or (SOURCE / "RELEASE.json").exists():
        raise ValueError("新模块当前仅支持本地开发，正式写入尚未接入")
    if os.getenv("COMMODITY_IMPORT_LOCAL_PREVIEW") != "1":
        raise ValueError("本地保存入口未启用")
    if Path(path).absolute() != local_database() or Path(path).absolute() != Path(path).resolve():
        raise ValueError("写入必须使用独立本地目录")


@contextmanager
def _connect(path: Path, *, write=False, authorize=None):
    if write:
        if authorize is None:
            raise ValueError("写入授权验证缺失")
        authorize(path)
        path.parent.mkdir(parents=True, exist_ok=True)
        authorize(path)
        db = sqlite3.connect(path, timeout=10)
        db.executescript(DDL)
    else:
        db = sqlite3.connect(path.resolve().as_uri()+"?mode=ro", uri=True)
    try:
        with db:
            yield db
    finally:
        db.close()


def _key(day: date, commodity: str):
    if type(day) is not date or day < START or commodity not in PROFILES:
        raise ValueError("品种或业务日期无效")
    return day.isoformat(), commodity


def _cnf(day, payload):
    value = json.loads(payload)
    if len(value) != 12 or {r["month"] for r in value} != set(range(1,13)):
        raise ValueError("CNF船期集合无效")
    for row in value:
        if row["year"] != shipment_year(day, row["month"]) or (row["value"] is not None and (number(row["value"]) is None or number(row["value"]) < 0)):
            raise ValueError("CNF船期年份或数值无效")
    return {r["month"]: r["value"] for r in value}


def load_cnf(path: Path, day: date, commodity: str):
    key = _key(day, commodity)
    if not path.is_file():
        return {}, 0
    with _connect(path) as db:
        row = db.execute("SELECT CASE WHEN length(CAST(payload AS BLOB))<=4096 THEN payload END,version FROM cnf_day WHERE business_date=? AND commodity=?", key).fetchone()
    if row is not None and row[0] is None:
        raise ValueError("CNF记录超出读取大小上限")
    return (_cnf(day, row[0]), row[1]) if row else ({}, 0)


def save_cnf(path: Path, day: date, commodity: str, values: dict, expected_version: int, *, authorize, provenance=None, market_snapshot=None, expected_identity=None):
    key = _key(day, commodity)
    if set(values) != set(range(1,13)) or any(type(k) is not int for k in values):
        raise ValueError("保存必须包含完整12个月")
    if type(expected_version) is not int or expected_version < 0:
        raise ValueError("CNF版本无效")
    rows = [dict(year=shipment_year(day, m), month=m, value=number(values[m])) for m in range(1,13)]
    if any(v is not None and (number(v) is None or number(v) < 0) for v in values.values()):
        raise ValueError("完整CNF必须为非负有限数值或空值")
    if provenance is not None and (not isinstance(provenance, dict) or provenance.get("entry_method") not in {"manual_user", "manual_codex"}):
        raise ValueError("CNF来源记录格式无效")
    provenance_raw = json.dumps(provenance if provenance is not None else {"entry_method": "manual_user"},
                                ensure_ascii=False, allow_nan=False)
    if len(provenance_raw.encode("utf-8")) > 4096:
        raise ValueError("CNF来源记录超出上限")
    payload = json.dumps(rows, allow_nan=False)
    market_raw = None
    if market_snapshot is not None:
        if validate_market(market_snapshot) != day or market_snapshot["commodity"] != commodity:
            raise ValueError("保存行情的业务键不符")
        market_raw = encoded(market_snapshot)
    with _connect(path, write=True, authorize=authorize) as db:
        db.execute("BEGIN IMMEDIATE")
        old = db.execute("SELECT version FROM cnf_day WHERE business_date=? AND commodity=?", key).fetchone()
        version = old[0] if old else 0
        if expected_version != version:
            raise ValueError("CNF已被另一会话修改，请重新读取后保存")
        if market_raw is not None:
            _write_market(db, market_snapshot, market_raw, expected_identity)
        db.execute("INSERT INTO cnf_day VALUES (?,?,?,?) ON CONFLICT DO UPDATE SET version=excluded.version,payload=excluded.payload",
                   (*key, version+1, payload))
        db.execute("INSERT INTO cnf_entry_audit VALUES (?,?,?,?,?)", (*key, version+1, payload, provenance_raw))
    return version+1


def cnf_provenance(path: Path, day: date, commodity: str, version: int):
    key = _key(day, commodity)
    if not path.is_file() or version == 0:
        return None
    with _connect(path) as db:
        if not db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='cnf_entry_audit'").fetchone():
            return None  # Older local rows remain readable without a migration.
        row = db.execute("SELECT CASE WHEN length(CAST(provenance AS BLOB))<=4096 THEN provenance END FROM cnf_entry_audit WHERE business_date=? AND commodity=? AND version=?", (*key, version)).fetchone()
    if row is None:
        return None
    if row[0] is None:
        raise ValueError("CNF来源记录超出读取上限")
    result = json.loads(row[0])
    if not isinstance(result, dict) or result.get("entry_method") not in {"manual_user", "manual_codex"}:
        raise ValueError("CNF来源记录格式无效")
    if result["entry_method"] == "manual_codex" and (not isinstance(result.get("source"), str) or not isinstance(result.get("entered_at"), str)):
        raise ValueError("CNF来源记录字段缺失")
    return result


def publish(path: Path, snapshot: dict, *, expected_identity: str | None, authorize):
    validate_market(snapshot)
    raw = encoded(snapshot)
    with _connect(path, write=True, authorize=authorize) as db:
        db.execute("BEGIN IMMEDIATE")
        return _write_market(db, snapshot, raw, expected_identity)


def validate_market(snapshot):
    if snapshot.get("schema_version") == "commodity-import-live-inputs/1":
        from .live import validate as validate_live
        return validate_live(snapshot)
    return validate(snapshot)


def _write_market(db, snapshot, raw, expected_identity):
    key = (snapshot["business_date"],snapshot["commodity"])
    sha = hashlib.sha256(raw).hexdigest()
    old = db.execute("SELECT sha256 FROM market_current WHERE business_date=? AND commodity=?",key).fetchone()
    if (old[0] if old else None) != expected_identity:
        raise ValueError("行情已被另一会话更新，请重试")
    db.execute("INSERT OR IGNORE INTO market_revisions VALUES (?,?)",(sha,raw.decode("utf-8")))
    db.execute("INSERT INTO market_current VALUES (?,?,?) ON CONFLICT DO UPDATE SET sha256=excluded.sha256",(*key,sha))
    return sha


def read_market(path: Path, day: date, commodity: str):
    key = _key(day, commodity)
    if not path.is_file():
        return None, None
    with _connect(path) as db:
        row = db.execute("SELECT CASE WHEN length(CAST(r.payload AS BLOB))<=? THEN r.payload END,c.sha256 FROM market_current c LEFT JOIN market_revisions r ON r.sha256=c.sha256 WHERE c.business_date=? AND c.commodity=?", (MAX_BYTES,*key)).fetchone()
    if row is None:
        return None, None
    if row[0] is None:
        raise ValueError("行情快照缺失或超出大小上限")
    raw = row[0].encode("utf-8")
    if len(raw) > MAX_BYTES or hashlib.sha256(raw).hexdigest() != row[1]:
        raise ValueError("行情快照身份验证失败")
    snapshot = json.loads(raw)
    if validate_market(snapshot) != day or snapshot["commodity"] != commodity:
        raise ValueError("行情业务键不符")
    return snapshot, row[1]


def history_dates(path: Path, commodity: str, as_of: date) -> list[date]:
    if not path.is_file():
        return []
    with _connect(path) as db:
        rows = db.execute("SELECT business_date FROM cnf_day WHERE commodity=? AND business_date<=? UNION SELECT business_date FROM market_current WHERE commodity=? AND business_date<=? ORDER BY business_date DESC LIMIT 4097",
                          (commodity, as_of.isoformat(), commodity, as_of.isoformat())).fetchall()
    if len(rows) > 4096:
        raise ValueError("本地历史超出4096日读取上限")
    return [date.fromisoformat(row[0]) for row in rows]
