"""Network-enabled unprivileged collector. Only its isolated output is writable."""
from __future__ import annotations

from datetime import datetime
import hashlib
import json
from pathlib import Path
import sys

sys.dont_write_bytecode = True
sys.path.insert(0, '/app/03_src')
from agri_research_agent.market_data import foreign_fx_delivery as fx


def prepare(request, baseline_root, output, *, fetcher=fx.fetch_delivery_official):
    fx.exact(request, ('run_id', 'checked_at', 'producer', 'baseline_files'), 'server FX request')
    stable = '01_data/processed/foreign_fx/daily.json'
    sources = '01_data/processed/foreign_fx/source_evidence.json'
    status = '01_data/processed/foreign_fx/status.json'
    files = fx.exact(request['baseline_files'], (stable, sources, status), 'FX baseline')
    if len({identity is None for identity in files.values()}) != 1:
        raise fx.FxDataError('FX server baseline incomplete')
    for relative, identity in files.items():
        path = baseline_root / relative
        if identity is None:
            if path.exists(): raise fx.FxDataError('Absent FX baseline exists')
        else:
            raw = path.read_bytes()
            if identity != dict(sha256=hashlib.sha256(raw).hexdigest(), size_bytes=len(raw)):
                raise fx.FxDataError('FX server baseline bytes differ')
    baseline = json.loads((baseline_root / stable).read_bytes()) if files[stable] is not None else None
    if baseline is not None:
        fx.observations(baseline, None, json.loads((baseline_root / sources).read_bytes()))
    now = datetime.fromisoformat(request['checked_at'])
    if now.tzinfo is None: raise fx.FxDataError('FX check timestamp lacks timezone')
    snapshot, evidence = fx.collect(now.date(), output / 'raw', fetcher=fetcher)
    semantic = fx.observations(snapshot, baseline, evidence)
    package = output / 'package'
    package.mkdir()
    payloads = {}
    for name, value in [('daily.json', snapshot), ('source_evidence.json', evidence)]:
        raw = fx.canonical(value)
        (package / name).write_bytes(raw)
        payloads[name] = dict(sha256=hashlib.sha256(raw).hexdigest(), size_bytes=len(raw))
    business_status = 'initialized' if baseline is None else 'updated' if semantic['business_changed'] else 'no_change'
    delta = dict(schema_version='production-data-delta/1', status='CANDIDATE', delta_id=request['run_id'],
                 generated_at_utc=now.isoformat(), domain='foreign_fx', producer=request['producer'],
                 run=dict(run_id=request['run_id'], git_head=request['producer']['commit'],
                          business_status=business_status, published=False),
                 baseline=dict(files=files),
                 domain_metadata={key: semantic[key] for key in ('record_count', 'latest_dates', 'added', 'revised')},
                 payloads=payloads)
    (package / 'delta_contract.json').write_bytes(fx.canonical(delta))
    return semantic


def main():
    output = Path('/output')
    result = dict(status='FAIL')
    try:
        request = json.loads(Path('/baseline/request.json').read_bytes())
        result.update(status='PASS', observations=prepare(request, Path('/baseline'), output))
    except Exception as exc:
        result['error'] = fx.failure_details(exc, 'collect_official_sources')
    finally:
        (output / 'collection-result.json').write_bytes(fx.canonical(result))
    print(json.dumps(result, ensure_ascii=False))
    return 0 if result['status'] == 'PASS' else 1


if __name__ == '__main__':
    raise SystemExit(main())
