"""Server-capable collection into an isolated candidate; never mutates stable data."""
from datetime import datetime, timezone
from pathlib import Path
import os
import requests

from agri_research_agent.oilseed_positions.stock_api import StockApiSources, StockApiError
from agri_research_agent.oilseed_positions.sources import SourceNotPublished
from agri_research_agent.positions import delivery as codec
from agri_research_agent.positions.bundle import verified_files
from agri_research_agent.sugar_positions.storage import publish
from agri_research_agent.soybean_margin.api_sources import _worker
from agri_research_agent.oilseed_positions.sources import Sources
from agri_research_agent.sugar_positions.sources import OfficialSources


def collect(baseline, output: Path, project_root: Path, day, *, source=None, contracts=None, calendar=None,
            include_official=True, official_sources=None):
    """No Windows paths, local browsers, credentials in output, or scheduler edits."""
    snapshots, _ = codec.validate_archive(baseline, project_root)
    output = codec.unlinked(Path(output).absolute())
    project_root = project_root.resolve()
    if output.is_relative_to(project_root) or project_root.is_relative_to(output) or output.exists():
        raise ValueError('持仓候选必须是仓库外的全新独立目录')
    formal = os.getenv('PUBLIC_MARKET_DATA_RUNTIME_ROOT', '').strip()
    if formal and (output.is_relative_to(Path(formal).resolve()) or Path(formal).resolve().is_relative_to(output)):
        raise ValueError('持仓候选不能与正式runtime目录重叠')
    if day.weekday() >= 5:
        raise ValueError('持仓采集需要交易日')
    calendar = calendar or (lambda: _worker('calendar', 35))
    dates = set(calendar())
    if not dates or day.isoformat() > max(dates) or day.isoformat() not in dates:
        raise ValueError('持仓日期不在已核验交易日历覆盖内')
    if contracts is not None:
        import re
        if not contracts or len(contracts) != len(set(contracts)) or any(
                not re.fullmatch(r'[MYP]\d{4}', c) for c in contracts):
            raise ValueError('持仓具体合约列表无效')
    store = output / 'collection'
    codec.materialize(baseline, store)
    attempts = []
    # Download each day once, then separate boards without mixing statistics.
    all_rows, captures = [], []
    for contract in (contracts if contracts is not None else [None]):
        try:
            source = source or StockApiSources()
            varieties = (contract[0],) if contract else ('M', 'Y', 'P')
            rows, raw, url = source.holdings(day, varieties, contract=contract)
            all_rows.extend(rows)
            key = f'stockapi_DCE_{day:%Y%m%d}' + ('_' + contract if contract else '')
            captures.append((key, raw, url, 'json'))
        except (SourceNotPublished, StockApiError) as exc:
            attempts.append(dict(contract=contract,
                status='not_published' if isinstance(exc, SourceNotPublished) else 'failed', error=str(exc)))
    for domain, varieties in (('soybean', {'M', 'Y'}), ('palm', {'P'})):
        old = snapshots[domain]
        existing = {(r['scope'], r['report_date']) for r in old['domestic']}
        # Existing same-day/manual reports take precedence. Changing those needs explicit review.
        rows = [r for r in all_rows if r['scope'].rstrip('0123456789') in varieties
                and (r['scope'], r['report_date']) not in existing]
        observed = dict(domain=domain, status='ok' if rows else 'no_change', rows=len(rows))
        attempts.append(observed)
        if rows:
            selected_captures = [c for c in captures if not contracts or c[0].split('_')[-1][0] in varieties]
            publish(store / domain, [], rows, selected_captures, [observed], preserve_untouched=True)
    if include_official:
        official_sources = official_sources or {'sugar': OfficialSources(), 'rapeseed': Sources()}
        for domain in ('sugar', 'rapeseed'):
            old = snapshots[domain]
            if any(r['report_date'] == day.isoformat() for r in old['domestic']):
                attempts.append(dict(domain=domain, status='no_change', rows=0))
                continue
            try:
                rows, raw, url = (official_sources[domain].czce(day) if domain == 'sugar' else
                    official_sources[domain].czce(day, ('RS', 'OI', 'RM')))
                if not rows or any(r['report_date'] != day.isoformat() for r in rows):
                    raise ValueError('官方持仓日期不一致或未发布')
                observed = dict(domain=domain, status='ok', rows=len(rows))
                publish(store/domain, [], rows, [(f'czce_{day:%Y%m%d}', raw, url, 'xlsx')],
                        [observed], preserve_untouched=True)
            except (ValueError, KeyError, TypeError, requests.RequestException):
                observed = dict(domain=domain, status='failed', error='czce_source_unavailable_or_invalid')
            attempts.append(observed)
    files = {}
    for domain in codec.DOMAINS:
        _, inputs = verified_files(store / domain, project_root, domain)
        files.update({domain+'/'+name: data for name, data in inputs.items()})
    candidate = codec.encode(files)
    codec.validate_archive(candidate, project_root)
    changes = codec.observations(candidate, baseline, project_root)
    if changes['revised_partitions']:
        raise ValueError('持仓已发布分区修订需要单独复核')
    (output / 'positions_archive.json').write_bytes(codec.canonical(candidate))
    receipt = dict(schema_version='server-positions-collection/1', published=False,
        report_date=day.isoformat(), attempted_at=datetime.now(timezone.utc).isoformat(),
        attempts=attempts, observations=changes)
    (output / 'receipt.json').write_bytes(codec.canonical(receipt))
    return receipt
