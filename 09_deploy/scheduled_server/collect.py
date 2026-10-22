"""Unprivileged network worker. Formal data and source clone are read-only."""
from __future__ import annotations

import importlib.util
import hashlib
import json
import os
import re
from pathlib import Path
import shutil
import subprocess
import sys

sys.dont_write_bytecode=True
SOURCE=Path('/source')
sys.path.insert(0,str(SOURCE/'03_src'))


def load(name,relative):
    spec=importlib.util.spec_from_file_location(name,SOURCE/relative)
    value=importlib.util.module_from_spec(spec); spec.loader.exec_module(value); return value


def package(request,output,payloads,semantic,host):
    from agri_research_agent.automation import production_data_delta as delivery
    target=output/'package'; target.mkdir()
    for name,raw in payloads.items(): (target/name).write_bytes(raw)
    domain={'positions_domestic':'commodity_positions','positions_foreign':'commodity_positions'}.get(request['job'],request['job'])
    status='updated' if semantic['business_changed'] else 'no_change'
    if domain=='canola_exports' and request['baseline_files'][host.EXPORTS_STABLE] is None:
        status='initialized'
    contract=host.DOMAIN_CONTRACTS[domain]
    delta={'schema_version':host.SCHEMA_VERSION,'status':'CANDIDATE','delta_id':request['run_id'],
        'generated_at_utc':request['checked_at'],'domain':domain,'producer':request['producer'],
        'run':{'run_id':request['run_id'],'git_head':request['producer']['commit'],'business_status':status,'published':False},
        'baseline':{'files':request['baseline_files']},'domain_metadata':{k:semantic[k] for k in contract['metadata']},
        'payloads':{name:delivery._identity(target/name) for name in contract['payloads']}}
    host.validate_delta_document(delta)
    (target/host.MANIFEST_NAME).write_bytes(delivery.canonical_json_bytes(delta))


def clear_monthly_build_cache(output, package):
    """Drop only byte-verified cache duplicates in this worker's own output."""
    pool=output/'packages/.monthly-objects'
    if not pool.exists():return 0
    if pool.is_symlink() or package.directory.parent!=output/'packages':
        raise ValueError('monthly cache scope invalid')
    references={entry['sha256']:package.directory/'data'/entry['path'] for entry in package.manifest['files']
        if entry['path'].startswith('consumer-artifacts/domestic-spread/')}
    objects=list(pool.iterdir())
    verified=[]
    for obj in objects:
        if obj.is_symlink() or not obj.is_file() or not re.fullmatch(r'[0-9a-f]{64}\.parquet',obj.name):
            raise ValueError('unknown monthly cache file retained')
        ref=references.get(obj.stem)
        if ref is None or hashlib.sha256(obj.read_bytes()).hexdigest()!=obj.stem or hashlib.sha256(ref.read_bytes()).hexdigest()!=obj.stem:
            raise ValueError('monthly cache lacks an identical retained package file')
        if obj.stat().st_nlink>2:raise ValueError('monthly cache has an external reference')
        verified.append((obj,ref))
    for obj,ref in verified:
        if sys.platform=='win32':obj.chmod(0o600)
        obj.unlink()
        ref.chmod(0o444)
    pool.rmdir()
    return len(verified)


def prepare(request,baseline,output,credentials):
    from agri_research_agent.automation import production_data_delta as delivery
    host=delivery._host_contract()
    from datetime import date
    for relative,expected in request['baseline_files'].items():
        path=baseline/relative
        if expected is None:
            if path.exists(): raise ValueError('absent baseline file exists')
        elif delivery._identity(path)!=expected: raise ValueError('baseline file identity differs')
    job=request['job']
    if job=='akshare':
        return load('public_spread_materialize','09_deploy/scheduled_server/spreads.py').materialize(request,baseline,output)
    if job=='brazil_soy':
        from agri_research_agent.pipelines.brazil_soy_scheduled import collect
        bundle,evidence,semantic=collect(baseline/host.BRAZIL_STABLE,output/'official-reports')
        payloads={'soy_weekly.json':delivery.canonical_json_bytes(bundle),
                  'source_evidence.json':delivery.canonical_json_bytes(evidence)}
        package(request,output,payloads,semantic,host)
        return {'observations':semantic,'source_checks':semantic['source_checks']}
    if job.startswith('positions_'):
        from agri_research_agent.automation.positions_scheduled_collection import collect_bundle
        from agri_research_agent.positions import delivery as codec
        previous=codec.read_archive(baseline/host.POSITIONS_STABLE)
        kwargs={}
        if request.get('positions_stock_api', False):
            if job != 'positions_domestic': raise ValueError('stock API is domestic only')
            from agri_research_agent.oilseed_positions.stock_api import StockApiSources
            kwargs['stock_source']=StockApiSources(token=credentials['STOCK_API_TOKEN'])
        increment,attempts=collect_bundle(previous,output,markets=job.removeprefix('positions_'),today=date.fromisoformat(request['business_date']),
            cftc_annual=job=='positions_foreign',incremental=True,**kwargs)
        archive=codec.resolve_increment(increment,previous)
        semantic=codec.observations(archive,previous,SOURCE)
        payload=codec.canonical(increment)
        # Even NO_CHANGE carries a tiny baseline-bound proof through the existing
        # independent validator and CAS publisher, never a full historical package.
        package(request,output,{'positions_archive.json':payload},semantic,host)
        return {'attempts':attempts,'observations':semantic,'metrics':{
            'transport_schema':codec.INCREMENT_SCHEMA,'payload_bytes':len(payload),
            'new_source_objects':len(increment['objects']),
            'new_records':sum(len(u[k]) for u in increment['updates'].values() for k in ('foreign','domestic')),
            'full_bundle_written':False}}
    if job=='canola_exports':
        from agri_research_agent.automation.production_data_delta_exports import _collect
        from agri_research_agent.canola_exports.delivery import observations
        from agri_research_agent.canola_exports.increment import build, canonical, SCHEMA
        previous=(host.read_json(baseline/host.EXPORTS_STABLE)
                  if request['baseline_files'][host.EXPORTS_STABLE] is not None else None)
        evidence=host.read_json(baseline/host.EXPORTS_SOURCES) if previous is not None else None
        path,sources=_collect({'python':sys.executable,'initial_years':2},output,previous,evidence)
        candidate=host.read_json(path); semantic=observations(candidate,previous,sources)
        increment,source_increment=build(candidate,previous,sources,evidence)
        payloads={'weekly.json':canonical(increment),'source_evidence.json':canonical(source_increment)}
        package(request,output,payloads,semantic,host)
        return {'observations':semantic,'metrics':{'transport_schema':SCHEMA,
            'payload_bytes':sum(len(raw) for raw in payloads.values()),
            'new_source_objects':len(source_increment['sources']),
            'changed_records':len(increment['updates']),'full_transport_bundle_written':False}}
    project=output/'project'; project.mkdir()
    if job=='soybean_crop_progress':
        config=project/'02_configs/soybean_crop_progress_display.yaml'
        config.parent.mkdir(parents=True,exist_ok=True)
        shutil.copyfile(SOURCE/'02_configs/soybean_crop_progress_display.yaml',config)
        from agri_research_agent.pipelines.soybean_crop_weekly_update import run_soybean_crop_weekly_update
        audit=run_soybean_crop_weekly_update(project_root=project,baseline_root=baseline,incremental_transport=True,
            dry_run=True,git_head=request['producer']['commit'],env=credentials)
        if audit['status']=='out_of_season': return {'out_of_season':True}
        delivery.collect_delta(job,project,output/'package',request['producer'],
            {rel.removeprefix('01_data/'):identity for rel,identity in request['baseline_files'].items()},
            run_id=request['run_id'],baseline_root=baseline)
        return {'business_status':audit['status'],'latest_week':audit.get('latest_week'),
                'metrics':{'transport_schema':'agri-table-increment/1','full_transport_bundle_written':False,'baseline_copied':False}}
    if job=='soybean_export_sales':
        from agri_research_agent.soybean_exports.fas import FasAdapter,run_fas_pipeline
        audit=run_fas_pipeline(runtime_root=project,adapter=FasAdapter(api_key=credentials['FAS_EXPORT_SALES_API_KEY'],use_environment_proxy=False),
            git_head=request['producer']['commit'],publish=False,incremental=True,baseline_root=baseline,incremental_transport=True,
            reconciliation_check=request.get('fas_reconciliation_check'))
        delivery.collect_delta(job,project,output/'package',request['producer'],
            {rel.removeprefix('01_data/'):identity for rel,identity in request['baseline_files'].items()},
            run_id=request['run_id'],baseline_root=baseline)
        return {'business_status':audit['status'],'latest_week':audit['validation']['latest_week'],
                'metrics':{**audit.get('collection_metrics',{}),'transport_schema':'agri-table-increment/1',
                           'full_transport_bundle_written':False,'baseline_copied':False}}
    raise ValueError('unsupported scheduled job')


def main():
    output=Path('/output'); result={'status':'FAIL'}
    try:
        request=json.loads(Path('/baseline/request.json').read_bytes())
        credentials=json.load(sys.stdin)
        expected={'soybean_crop_progress':{'NASS_API_KEY'},'soybean_export_sales':{'FAS_EXPORT_SALES_API_KEY'}}.get(request['job'],set())
        if request.get('positions_stock_api', False):
            if request['job'] != 'positions_domestic': raise ValueError('stock API is domestic only')
            expected={'STOCK_API_TOKEN'}
        if '--public-preflight' in sys.argv:
            expected={'price_baseline'}
        if set(credentials)!=expected: raise ValueError('unexpected credentials')
        if '--public-preflight' in sys.argv:
            if request['job'] != 'akshare' or set(credentials) != {'price_baseline'}:
                raise ValueError('public preflight input differs')
            prepared=load('public_spread_preflight','09_deploy/scheduled_server/spreads.py').preflight(request,output,credentials['price_baseline'])
        else:
            prepared=prepare(request,Path('/baseline'),output,credentials)
        result.update(prepared,status='PASS')
    except Exception as exc:
        result['error_type']=type(exc).__name__
    finally:
        (output/'collection-result.json').write_bytes(json.dumps(result,ensure_ascii=False).encode('utf-8'))
    print(json.dumps(result,ensure_ascii=False))
    return 0 if result['status']=='PASS' else 1


if __name__=='__main__': raise SystemExit(main())
