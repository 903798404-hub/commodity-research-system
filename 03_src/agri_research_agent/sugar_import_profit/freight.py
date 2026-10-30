"""Dated observations and explicit last-quote assumptions; no source gap filling."""
from __future__ import annotations

from datetime import date
import hashlib
import json
import math
from pathlib import Path
import re

SCHEMA = 'hisugar-freight/1'
DEFINITION = '泛糖测算采用运费，非SYCAC00或逐月船期成交报价'


def _valid_source(source):
    return (isinstance(source, dict)
        and re.fullmatch(r'https://www\.hisugar\.com/home/articleContent\?id=\d{16,30}', source.get('article_url', ''))
        and isinstance(source.get('image_url'), str)
        and source['image_url'].startswith('https://www.hisugar.com/image/')
        and re.fullmatch(r'[a-f0-9]{64}', source.get('image_sha256', '')))


def load_freight(root: Path):
    root = Path(root)
    pointer = root / 'current.json'
    if not pointer.exists(): return []
    if pointer.is_symlink() or pointer.stat().st_size > 16384:
        raise ValueError('泛糖运费索引无效')
    manifest = json.loads(pointer.read_text(encoding='utf-8'))
    if not isinstance(manifest, dict): raise ValueError('泛糖运费索引无效')
    name = manifest.get('file')
    if (not isinstance(name, str) or Path(name).name != name or not name.endswith('.json')
            or manifest.get('schema_version') != SCHEMA):
        raise ValueError('泛糖运费版本或文件名无效')
    path = root / name
    if path.is_symlink() or path.resolve().parent != root.resolve() or path.stat().st_size > 16 * 1024 * 1024:
        raise ValueError('泛糖运费文件无效')
    raw = path.read_bytes()
    if hashlib.sha256(raw).hexdigest() != manifest.get('sha256'):
        raise ValueError('泛糖运费内容校验失败')
    bundle = json.loads(raw.decode('utf-8'))
    if not isinstance(bundle, dict): raise ValueError('泛糖运费文件结构无效')
    if (bundle.get('schema_version') != SCHEMA or bundle.get('unit') != 'USD/tonne'
            or bundle.get('provider') != 'HiSugar' or bundle.get('definition') != DEFINITION):
        raise ValueError('泛糖运费来源、单位或口径无效')
    rows = bundle.get('observations')
    if not isinstance(rows, list) or len(rows) > 10000 or len(rows) != manifest.get('row_count'):
        raise ValueError('泛糖运费记录数量无效')
    seen = set()
    for row in rows:
        if not isinstance(row, dict): raise ValueError('泛糖运费记录结构无效')
        stamp = date.fromisoformat(row['date'])
        value = row.get('freight_usd_per_tonne')
        if (row.get('country') != '巴西' or stamp.isoformat() in seen or stamp.isoformat() != row['date']
                or isinstance(value, bool) or not isinstance(value, (int, float))
                or not math.isfinite(value) or value < 0):
            raise ValueError('泛糖运费日期、国家或数值无效')
        seen.add(stamp.isoformat())
        sources = row.get('sources')
        if not isinstance(sources, list) or not sources:
            raise ValueError('泛糖运费缺少原始证据')
        for source in sources:
            if not _valid_source(source) or not source.get('published_at'):
                raise ValueError('泛糖运费来源凭证无效')
    if [r['date'] for r in rows] != sorted(seen):
        raise ValueError('泛糖运费日期顺序无效')
    return rows


def reference_on(rows, day: str, country: str):
    """Only the exact observation date; previous/next dates never substitute."""
    return next((r for r in rows if r['date'] == day and r['country'] == country), None)


def latest_reference(rows, day: str, country: str):
    return max((r for r in rows if r['country'] == country and r['date'] <= day),
               key=lambda r: r['date'], default=None)


def provenance(reference, value, day=None):
    """Manual null/zero/edits remain explicit and never resurrect a public default."""
    if value is None: return dict(kind='missing')
    if reference is not None and value == reference['freight_usd_per_tonne']:
        return dict(kind='hisugar_reference', quote_date=reference['date'],
                    value=value, basis='generic_daily_reference_not_forward', sources=reference['sources'][:1])
    return dict(kind='manual', basis='user_entered', **({'quote_date': day} if day else {}))


def freight_default(rows, records, day: str, country: str, period: str):
    """Reuse freight only for a new daily record, preserving manual overrides.

    The existing manual family also represents the user's standing carry policy;
    source_kind and original evidence distinguish it from a newly entered price.
    Older readers can still read these snapshots without a schema migration.
    """
    from .model import RESEARCH_VERSION
    from copy import deepcopy
    prior = sorted((r['payload'] for r in records
                    if r['payload']['country'] == country and r['payload']['date'] < day
                    and r['payload']['schema_version'] == RESEARCH_VERSION),
                   key=lambda p: p['date'], reverse=True)
    saved_reference = None
    for payload in prior:
        row = next((r for r in payload['quotes'] if r['period'] == period), None)
        if row is None:
            continue
        value, origin = row.get('freight'), row.get('freight_origin') or {}
        if value is None:
            if origin.get('explicit_clear'):
                return None, deepcopy(origin)
            continue
        source_kind = origin.get('source_kind', origin.get('kind', 'manual'))
        original_date = origin.get('quote_date', payload['date'])
        if source_kind == 'manual':
            return value, dict(kind='manual', basis='last_known_freight', source_kind='manual',
                               quote_date=original_date, value=value, sources=[])
        saved_reference = dict(date=original_date, country=country,
                               freight_usd_per_tonne=value, sources=deepcopy(origin['sources']))
        break
    reference = latest_reference(rows, day, country)
    if saved_reference and (reference is None or saved_reference['date'] > reference['date']):
        reference = saved_reference
    if reference is None:
        return None, provenance(None, None)
    value = reference['freight_usd_per_tonne']
    if reference['date'] == day:
        return value, provenance(reference, value)
    return value, dict(kind='manual', basis='last_known_freight', source_kind='hisugar_reference',
                       quote_date=reference['date'], value=value, sources=deepcopy(reference['sources'][:1]))


def validate_provenance(origin, value, day):
    if not isinstance(origin, dict) or origin.get('kind') not in ('missing', 'manual', 'hisugar_reference'):
        raise ValueError('运费来源标记无效')
    if value is None:
        if origin['kind'] != 'missing': raise ValueError('空运费不能保留有效报价标记')
        return
    if origin['kind'] == 'missing': raise ValueError('有效运费不能标为缺失')
    if origin.get('basis') == 'last_known_freight':
        if (origin['kind'] != 'manual' or origin.get('source_kind') not in ('manual', 'hisugar_reference')
                or origin.get('value') != value or date.fromisoformat(origin['quote_date']) > day):
            raise ValueError('沿用运费原始日期、来源或数值不符')
        if origin['source_kind'] == 'hisugar_reference':
            if not origin.get('sources') or any(not _valid_source(s) for s in origin['sources']):
                raise ValueError('沿用泛糖运费缺少原始凭证')
    if origin['kind'] == 'hisugar_reference':
        if (origin.get('value') != value or origin.get('basis') != 'generic_daily_reference_not_forward'
                or date.fromisoformat(origin['quote_date']) != day or not origin.get('sources')):
            raise ValueError('泛糖参考运费日期、口径或数值不符')
        for source in origin['sources']:
            if not _valid_source(source):
                raise ValueError('泛糖参考运费缺少原始凭证')
