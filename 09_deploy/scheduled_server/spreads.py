"""Check source completeness before allocating a Public Current working copy."""
from __future__ import annotations

import base64
from datetime import date
import importlib.util
import io
import json
from pathlib import Path
import shutil

import pandas as pd

ROOT = Path(__file__).resolve().parents[2]


def load(name, relative):
    spec = importlib.util.spec_from_file_location(name, ROOT / relative)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def price_input(request, raw):
    from agri_research_agent.automation import production_data_delta as delivery
    import hashlib
    expected = request['baseline_files']['01_data/historical_price_long.xlsx']
    delivery.require({'size_bytes': len(raw), 'sha256': hashlib.sha256(raw).hexdigest()} == expected,
                     'public price baseline differs')
    return pd.read_excel(io.BytesIO(raw), sheet_name='price_long')


def preflight(request, output, encoded_prices):
    """Only source evidence and new rows are written; history stays in memory."""
    updater = load('scheduled_spread_prices', '04_scripts/update_price_long_from_akshare.py')
    existing = price_input(request, base64.b64decode(encoded_prices, validate=True))
    target = date.fromisoformat(request['business_date'])
    candidates = updater.build_active_candidates(ROOT / '02_configs/historical_spread_config.xlsx', target)
    if candidates.empty:
        raise ValueError('no active spread contracts')
    planned = updater.plan_contract_refreshes(candidates, updater.per_contract_watermarks(existing, candidates), target_date=target)
    success, failures, raw = updater.fetch_daily_history(planned, target_date=target)
    # Retain the actual public response, including missing-date and failure evidence.
    updater.write_report(output / 'source-evidence.xlsx', success=success, failures=failures,
                         raw_spot=raw, to_append=pd.DataFrame(), existing_today_rows=pd.DataFrame())
    incoming = updater.build_price_long_rows(success, list(existing.columns)) if not success.empty else existing.iloc[:0].copy()
    combined, added, duplicates = updater.upsert_daily_closes(existing, incoming)
    required = set(candidates['symbol'].astype(str))
    present, missing = updater.target_date_completeness(combined, required, target_date=target)
    outcome = 'SOURCE_FAILED' if not failures.empty else 'WAITING_FOR_SOURCE' if missing else 'CHANGED' if len(added) else 'NO_CHANGE'
    proof = {'schema_version': 'public-spread-preflight/1', 'outcome': outcome,
             'business_date': target.isoformat(), 'baseline_files': request['baseline_files'],
             'public_pointer': request['public_pointer'], 'required_contracts': sorted(required),
             'present_contracts': sorted(present), 'missing_contracts': sorted(missing),
             'endpoint_failure_contracts': len(failures),
             'columns': list(existing.columns),
             'new_rows': json.loads(added.to_json(orient='records', date_format='iso')),
             'metrics': {'endpoint_requested_contracts': len(planned),
                         'endpoint_skipped_current_contracts': len(candidates) - len(planned),
                         'to_append_rows': len(added), 'idempotent_no_change_rows': duplicates,
                         'overwritten_rows': 0, 'full_bundle_written': False}}
    (output / 'preflight.json').write_text(json.dumps(proof, ensure_ascii=False), encoding='utf-8')
    return {'business_status': outcome, 'metrics': proof['metrics'],
            'required_contracts': len(required), 'success_contracts': len(present),
            'missing_contracts': sorted(missing)}


def replay(request, baseline):
    """Recheck the append and target coverage offline against the pinned baseline."""
    updater = load('scheduled_spread_replay', '04_scripts/update_price_long_from_akshare.py')
    proof = request['public_preflight']
    if (proof['schema_version'] != 'public-spread-preflight/1' or proof['outcome'] != 'CHANGED'
            or proof['baseline_files'] != request['baseline_files']
            or proof['public_pointer'] != request['public_pointer']
            or proof['business_date'] != request['business_date']
            or proof['endpoint_failure_contracts'] != 0 or proof['missing_contracts']):
        raise ValueError('public preflight identity or completeness differs')
    existing = price_input(request, (baseline / '01_data/historical_price_long.xlsx').read_bytes())
    if proof['columns'] != list(existing.columns):
        raise ValueError('price schema changed')
    incoming = pd.DataFrame(proof['new_rows'], columns=proof['columns'])
    incoming['date'] = pd.to_datetime(incoming['date'])
    combined, added, duplicates = updater.upsert_daily_closes(existing, incoming)
    target = date.fromisoformat(request['business_date'])
    candidates = updater.build_active_candidates(ROOT / '02_configs/historical_spread_config.xlsx', target)
    required = set(candidates['symbol'].astype(str))
    present, missing = updater.target_date_completeness(combined, required, target_date=target)
    if (not len(added) or duplicates or len(added) != proof['metrics']['to_append_rows']
            or missing or sorted(required) != proof['required_contracts']
            or sorted(present) != proof['present_contracts']
            or incoming['date'].max().date() > target):
        raise ValueError('public preflight replay differs')
    return combined, added


def materialize(request, baseline, output):
    from agri_research_agent.automation import production_data_delta as delivery
    from agri_research_agent.pipelines.public_data_delivery import validate_production_package
    from agri_research_agent.pipelines.domestic_spread_integrity import (
        HistoricalMutationPolicy, HistoricalPublicationMode, validate_historical_publication)
    combined, added = replay(request, baseline)
    public = baseline / request['public_package_relative']
    package = validate_production_package(public)
    if package.manifest != request['public_manifest']:
        raise ValueError('public manifest differs')
    artifact = public / 'data' / package.manifest['delivery_artifacts']['domestic-spread']['package_path']
    # All completeness, identity and append-only checks passed before this first copy.
    project = output / 'project'
    data = project / '01_data'
    data.mkdir(parents=True)
    shutil.copyfile(baseline / '01_data/historical_spread_database.xlsx', data / 'historical_spread_database.xlsx')
    history = delivery.adopt_public_spread_history(data, public, expected_manifest=request['public_manifest'])
    combined.to_excel(data / 'historical_price_long.xlsx', sheet_name='price_long', index=False)
    server = load('scheduled_spread_materializer', '04_scripts/server_update_spreads.py')
    status, _, affected = server.apply_direct_incremental_materialization(
        price_baseline=baseline / '01_data/historical_price_long.xlsx',
        price_candidate=data / 'historical_price_long.xlsx', spread_baseline=artifact,
        spread_output=data / 'historical_spread_database.parquet',
        spread_workbook=data / 'historical_spread_database.xlsx',
        config_file=ROOT / '02_configs/historical_spread_config.xlsx')
    policy = HistoricalMutationPolicy(HistoricalPublicationMode.NORMAL,
        added['date'].min().date(), date.fromisoformat(request['business_date']), frozenset(), affected)
    diff = validate_historical_publication(artifact, data / 'historical_spread_database.parquet', policy)
    import logging
    summaries = server.validate_database_outputs(data / 'historical_spread_database.xlsx',
        data / 'historical_spread_database.parquet', logging.getLogger(__name__))
    status.update(status='success', latest_date=summaries['parquet']['latest_date'],
                  historical_diff_guard='PASS', historical_changed_keys=['|'.join(k) for k in diff.changed_keys])
    server.atomic_write_json(data / 'update_status.json', status)
    # A source-only addition can advance the continuation without rebuilding Public Current.
    if not diff.changed_keys:
        return {'business_status': 'NO_CHANGE', 'continuation_changed': True,
                'metrics': {**request['public_preflight']['metrics'], **status, 'full_bundle_written': False}}
    result = delivery._build_public_package(project, public, output / 'packages',
        partitioned=request.get('partitioned_public_packages', False))
    worker = load('scheduled_spread_cache', '09_deploy/scheduled_server/collect.py')
    load('scheduled_spread_scope', '09_deploy/scheduled_server/public.py').assert_scope(request, result.manifest)
    cache_removed = worker.clear_monthly_build_cache(output, result)
    return {'package_id': result.package_id, 'package_path': str(result.directory.relative_to(output)),
            'business_status': 'CHANGED', 'latest_date': status['latest_date'],
            'metrics': {**request['public_preflight']['metrics'], 'full_bundle_written': True,
                        'calculated_spread_rows': status['calculated_spread_rows'],
                        'historical_rows_preserved': history['history_rows'],
                        'package_bytes': sum(f['size_bytes'] for f in result.manifest['files']),
                        'package_files': result.manifest['file_count'],
                        'temporary_monthly_cache_files_removed': cache_removed}}
