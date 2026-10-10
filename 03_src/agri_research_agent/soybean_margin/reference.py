"""Submission-bound reference results, stored beside CNF in the same authorized DB."""
from datetime import date, datetime
import hashlib
import json
from pathlib import Path
import sqlite3
from uuid import uuid4

from . import store
from .api_inputs import select_fx
from .api_sources import SHANGHAI, SourceError
from .latest_quotes import capture
from .model import FIELDS, PARAMETERS, calculate, contracts, number
from .runtime import validate_cnf_write

DDL = '''CREATE TABLE IF NOT EXISTS quote_submissions (
 request_id TEXT PRIMARY KEY, business_date TEXT NOT NULL, origin TEXT NOT NULL,
 revision INTEGER NOT NULL, submitted_at TEXT NOT NULL, cnf_json TEXT NOT NULL,
 result_json TEXT, result_sha256 TEXT,
 UNIQUE(business_date,origin,revision));'''
MAX_BYTES = 256 * 1024


def encoded(value):
    raw = json.dumps(value, ensure_ascii=False, sort_keys=True, allow_nan=False,
                     separators=(',', ':'))
    if len(raw.encode('utf-8')) > MAX_BYTES:
        raise ValueError('参考行情记录超出大小上限')
    return raw


def reference_rows(day, origin, values, quotes):
    rows = []
    for month in range(1, 13):
        cbot, domestic, year = contracts(day, month)
        tenor = (year-day.year)*12 + month-day.month
        fx, kind, interpolated, lower, upper = select_fx(quotes['fx_curve'], tenor)
        row = dict(business_date=day.isoformat(), origin=origin, shipment_year=year,
            shipment_month=month, shipment_period=f'{year}-{month:02}',
            cbot_contract=cbot, domestic_contract=domestic,
            cnf_cents_per_bushel=number(values[month]),
            cbot_price_cents_per_bushel=quotes['cbot'].get(cbot), fx_value=fx,
            soymeal_price_cny_per_tonne=quotes['domestic'].get('M'+domestic),
            soyoil_price_cny_per_tonne=quotes['domestic'].get('Y'+domestic),
            fx_kind=kind, fx_tenor=tenor, fx_is_interpolated=interpolated,
            fx_lower_tenor=lower, fx_upper_tenor=upper)
        row.update(calculate(*(row[field] for field in FIELDS)))
        row['missing_inputs'] = [field for field in FIELDS if row[field] is None]
        rows.append(row)
    return rows


def submit(path: Path, day, origin, values, expected_version, *, collector=capture,
           authorize=validate_cnf_write, now=None):
    now = now or datetime.now(SHANGHAI)
    if now.utcoffset() is None:
        raise ValueError('CNF提交时间缺少时区')
    values = dict(values)
    authorize(path)
    submission = dict(request_id=uuid4().hex, submitted_at=now.isoformat())
    revision = store.save(path, day, origin, values, expected_version, submission=submission)
    result = dict(schema_version='soybean-reference/1', business_date=day.isoformat(),
        origin=origin, revision=revision, **submission, parameters=PARAMETERS,
        status='unavailable', rows=[], quotes=None, error=None)
    try:
        # A historical CNF correction must never attach today's quotes to an old day.
        if day != now.astimezone(SHANGHAI).date():
            raise SourceError('historical_cnf_saved_without_current_quotes')
        quotes = collector(day)
        if quotes['business_date'] != day.isoformat():
            raise SourceError('quote_business_date_mismatch')
        result.update(quotes=quotes, rows=reference_rows(day, origin, values, quotes))
        result['status'] = 'available' if all(r['net_margin'] is not None for r in result['rows']) else 'partial'
    except Exception as exc:
        result['error'] = str(exc) if isinstance(exc, SourceError) else 'collection_or_schema_failed'
    try:
        raw = encoded(result)
    except (ValueError, TypeError):
        result.update(status='unavailable', rows=[], quotes=None, error='reference_result_invalid_or_too_large')
        raw = encoded(result)
    authorize(path)
    with sqlite3.connect(path, timeout=10) as db:
        changed = db.execute('UPDATE quote_submissions SET result_json=?,result_sha256=? '
            'WHERE request_id=? AND result_json IS NULL',
            (raw, hashlib.sha256(raw.encode('utf-8')).hexdigest(), submission['request_id'])).rowcount
        if changed != 1:
            raise ValueError('参考行情请求已完成或不存在')
    return result


def read_latest(path: Path, day, origin, revision):
    if not path.is_file():
        return None
    with sqlite3.connect(path.resolve().as_uri()+'?mode=ro', uri=True) as db:
        if not db.execute("SELECT name FROM sqlite_master WHERE name='quote_submissions'").fetchone():
            return None
        row = db.execute('SELECT result_json,result_sha256 FROM quote_submissions '
            'WHERE business_date=? AND origin=? AND revision=?',
            (day.isoformat(), origin, revision)).fetchone()
    if not row or row[0] is None:
        return None
    raw, sha = row
    if len(raw.encode('utf-8')) > MAX_BYTES or hashlib.sha256(raw.encode('utf-8')).hexdigest() != sha:
        raise ValueError('参考行情记录身份校验失败')
    result = json.loads(raw)
    if (result['schema_version'] != 'soybean-reference/1' or result['business_date'] != day.isoformat()
            or result['origin'] != origin or result['revision'] != revision):
        raise ValueError('参考行情业务身份不符')
    return result
