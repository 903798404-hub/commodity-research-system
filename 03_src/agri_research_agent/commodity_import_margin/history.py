"""Immutable user workbook history; never recalculate it with current inputs."""
from datetime import date
import hashlib
import json
from pathlib import Path
import re

from .model import number
from .store import _connect

SCHEMA = "commodity-import-excel-history/1"
LABELS = {"canola":"加拿大菜籽", "palm":"棕榈油", "canola_oil":"加拿大菜油"}
MAX_BYTES = 64*1024*1024
METRICS = ("cnf_usd_per_tonne","net_margin","fx_value","duty_paid_cost","meal_price","oil_price")
DDL = (
    "CREATE TABLE IF NOT EXISTS excel_history_revisions (revision TEXT PRIMARY KEY, commodity TEXT NOT NULL, payload TEXT NOT NULL)",
    "CREATE TABLE IF NOT EXISTS excel_history_current (commodity TEXT PRIMARY KEY, revision TEXT NOT NULL)",
)


def encode(value):
    raw=json.dumps(value,ensure_ascii=False,sort_keys=True,separators=(",",":"),allow_nan=False).encode("utf-8")
    if len(raw)>MAX_BYTES: raise ValueError("Excel历史超出64MiB上限")
    return raw


def validate(value):
    if not isinstance(value,dict) or value.get("schema_version")!=SCHEMA or value.get("commodity") not in LABELS:
        raise ValueError("Excel历史版本或品种无效")
    source=value.get("source",{})
    if not isinstance(source,dict) or not re.fullmatch(r"[0-9a-f]{64}",source.get("sha256","")) or not isinstance(source.get("size"),int) or not 0<source["size"]<=32*1024*1024:
        raise ValueError("Excel来源身份无效")
    if not source.get("sheet") or not source.get("original_path") or not source.get("filename"):
        raise ValueError("Excel来源位置缺失")
    if value.get("units")!={"cnf":"USD/tonne","profit":"CNY/tonne","fx":"CNH_per_USD"}:
        raise ValueError("Excel历史单位或币种无效")
    if value.get("semantics")!="source-cached-values; current-month-is-current-year; continuous-contract-prices":
        raise ValueError("Excel历史口径无效")
    imported_day=date.fromisoformat(value["imported_at"][:10])
    rows=value.get("rows")
    if not isinstance(rows,list) or not 0<len(rows)<=50000: raise ValueError("Excel历史记录数量无效")
    seen=set()
    for row in rows:
        if not isinstance(row,dict): raise ValueError("Excel历史记录格式无效")
        day=date.fromisoformat(row["business_date"]);month=row["shipment_month"]
        if day>imported_day or type(month) is not int or not 1<=month<=12: raise ValueError("Excel历史日期或船期无效")
        key=(day,month)
        if key in seen: raise ValueError("Excel历史日期船期重复")
        seen.add(key)
        offset=(month-day.month)%12
        year=day.year+(month<day.month)
        if row.get("source_tenor_months")!=offset or row.get("shipment_period")!=f"{year}-{month:02}": raise ValueError("Excel原船期解释不符")
        for field in METRICS:
            v=row.get(field)
            if v is not None and (type(v) not in (int,float) or number(v) is None): raise ValueError("Excel历史数值无效")
        if row["cnf_usd_per_tonne"] is not None and row["cnf_usd_per_tonne"]<0: raise ValueError("Excel历史CNF无效")
        if not isinstance(row.get("source_cells"),dict) or not isinstance(row.get("source_formulas"),dict) or not isinstance(row.get("source_errors"),dict): raise ValueError("Excel历史行凭证缺失")
        if row.get("profit_quality") not in {"verified","missing","unverifiable","mismatch"}: raise ValueError("Excel历史核验状态无效")
        if (row.get("net_margin") is None)!=(row["profit_quality"]=="missing"):
            raise ValueError("Excel利润空值与核验状态不符")
        if row.get("domestic_contract_year") is not None:
            raise ValueError("Excel连续价格不能推断完整合约年份")
    return value["commodity"]


def read_history(path: Path, commodity: str, as_of: date):
    if commodity not in LABELS: raise ValueError("历史品种无效")
    if not path.is_file(): return None,None
    with _connect(path) as db:
        if not db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='excel_history_current'").fetchone(): return None,None
        record=db.execute("SELECT CASE WHEN length(CAST(r.payload AS BLOB))<=? THEN r.payload END,c.revision FROM excel_history_current c LEFT JOIN excel_history_revisions r ON r.revision=c.revision WHERE c.commodity=?",(MAX_BYTES,commodity)).fetchone()
    if record is None: return None,None
    if record[0] is None: raise ValueError("Excel历史快照缺失或超限")
    raw=record[0].encode("utf-8")
    if hashlib.sha256(raw).hexdigest()!=record[1]: raise ValueError("Excel历史身份校验失败")
    value=json.loads(raw)
    if validate(value)!=commodity: raise ValueError("Excel历史品种不符")
    value["rows"]=[r for r in value["rows"] if r["business_date"]<=as_of.isoformat()]
    return value,record[1]


def publish_history(path: Path, value: dict, source_file: Path, *, expected_revision, authorize):
    commodity=validate(value);raw=encode(value);revision=hashlib.sha256(raw).hexdigest()
    if source_file.stat().st_size > 32*1024*1024:
        raise ValueError("Excel原始文件超出32MiB上限")
    source=source_file.read_bytes()
    if len(source)!=value["source"]["size"] or hashlib.sha256(source).hexdigest()!=value["source"]["sha256"]:
        raise ValueError("Excel原始文件已变化，请重新提取")
    authorize(path)
    archive=path.parent/"historical-sources"/f"{value['source']['sha256']}.xlsx"
    if archive.resolve()!=archive.absolute(): raise ValueError("Excel归档路径存在别名")
    archive.parent.mkdir(parents=True,exist_ok=True)
    try:
        with archive.open("xb") as stream: stream.write(source)
    except FileExistsError:
        pass
    if archive.stat().st_size!=len(source) or hashlib.sha256(archive.read_bytes()).hexdigest()!=value["source"]["sha256"]:
        raise ValueError("Excel来源归档身份不符")
    with _connect(path,write=True,authorize=authorize) as db:
        db.execute("BEGIN IMMEDIATE")
        for sql in DDL: db.execute(sql)
        previous=db.execute("SELECT revision FROM excel_history_current WHERE commodity=?",(commodity,)).fetchone()
        if (previous[0] if previous else None)!=expected_revision: raise ValueError("Excel历史已被另一会话更新")
        db.execute("INSERT OR IGNORE INTO excel_history_revisions VALUES (?,?,?)",(revision,commodity,raw.decode("utf-8")))
        db.execute("INSERT INTO excel_history_current VALUES (?,?) ON CONFLICT DO UPDATE SET revision=excluded.revision",(commodity,revision))
    return revision
