"""Unprivileged network worker. Formal data and source clone are read-only."""
from __future__ import annotations

import importlib.util
import json
import os
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
    contract=host.DOMAIN_CONTRACTS[domain]
    delta={'schema_version':host.SCHEMA_VERSION,'status':'CANDIDATE','delta_id':request['run_id'],
        'generated_at_utc':request['checked_at'],'domain':domain,'producer':request['producer'],
        'run':{'run_id':request['run_id'],'git_head':request['producer']['commit'],'business_status':status,'published':False},
        'baseline':{'files':request['baseline_files']},'domain_metadata':{k:semantic[k] for k in contract['metadata']},
        'payloads':{name:delivery._identity(target/name) for name in contract['payloads']}}
    host.validate_delta_document(delta)
    (target/host.MANIFEST_NAME).write_bytes(delivery.canonical_json_bytes(delta))


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
    if job.startswith('positions_'):
        from agri_research_agent.automation.positions_scheduled_collection import collect_bundle
        from agri_research_agent.positions import delivery as codec
        previous=host.read_json(baseline/host.POSITIONS_STABLE)
        bundle,attempts=collect_bundle(previous,output,markets=job.removeprefix('positions_'),today=date.fromisoformat(request['business_date']))
        archive=codec.archive_bundle(bundle,SOURCE); semantic=codec.observations(archive,previous,SOURCE)
        package(request,output,{'positions_archive.json':codec.canonical(archive)},semantic,host)
        return {'attempts':attempts,'observations':semantic}
    if job=='canola_exports':
        from agri_research_agent.automation.production_data_delta_exports import _collect
        from agri_research_agent.canola_exports.delivery import observations
        previous=host.read_json(baseline/host.EXPORTS_STABLE)
        evidence=host.read_json(baseline/host.EXPORTS_SOURCES)
        path,sources=_collect({'python':sys.executable,'initial_years':2},output,previous,evidence)
        candidate=host.read_json(path); semantic=observations(candidate,previous,sources)
        package(request,output,{'weekly.json':delivery.canonical_json_bytes(candidate),
            'source_evidence.json':delivery.canonical_json_bytes(sources)},semantic,host)
        return {'observations':semantic}
    project=output/'project'; project.mkdir()
    for relative,expected in request['baseline_files'].items():
        if expected is None: continue
        path=project/relative; path.parent.mkdir(parents=True,exist_ok=True); shutil.copyfile(baseline/relative,path)
    if job=='soybean_crop_progress':
        config=project/'02_configs/soybean_crop_progress_display.yaml'
        config.parent.mkdir(parents=True,exist_ok=True)
        shutil.copyfile(SOURCE/'02_configs/soybean_crop_progress_display.yaml',config)
        from agri_research_agent.pipelines.soybean_crop_weekly_update import run_soybean_crop_weekly_update
        audit=run_soybean_crop_weekly_update(project_root=project,dry_run=True,git_head=request['producer']['commit'],env=credentials)
        if audit['status']=='out_of_season': return {'out_of_season':True}
        delivery.collect_delta(job,project,output/'package',request['producer'],request['baseline_files'],run_id=request['run_id'])
        return {'business_status':audit['status'],'latest_week':audit.get('latest_week')}
    if job=='soybean_export_sales':
        from agri_research_agent.soybean_exports.fas import FasAdapter,run_fas_pipeline
        audit=run_fas_pipeline(runtime_root=project,adapter=FasAdapter(api_key=credentials['FAS_EXPORT_SALES_API_KEY'],use_environment_proxy=False),
            git_head=request['producer']['commit'],publish=False)
        delivery.collect_delta(job,project,output/'package',request['producer'],request['baseline_files'],run_id=request['run_id'])
        return {'business_status':audit['status'],'latest_week':audit['validation']['latest_week']}
    if job=='akshare':
        for directory in ('02_configs','03_src','04_scripts'):
            shutil.copytree(SOURCE/directory,project/directory)
        result=subprocess.run([sys.executable,'-I','-B','-X','utf8',str(project/'04_scripts/server_update_spreads.py'),
            '--update-from-akshare','--end-date',request['business_date']],capture_output=True,timeout=2100)
        # Network script logs contain no credentials for this public source.
        (output/'spread.stdout').write_bytes(result.stdout[-65536:]); (output/'spread.stderr').write_bytes(result.stderr[-65536:])
        if result.returncode: raise ValueError('spread collector returned '+str(result.returncode))
        status=host.read_json(project/'01_data/update_status.json')
        if status.get('target_date_data_completeness')=='PARTIAL': raise ValueError('spread coverage incomplete')
        public_package=delivery._build_public_package(project,baseline/request['public_package_relative'],output/'packages')
        load('public_worker','09_deploy/scheduled_server/public.py').assert_scope(request,public_package.manifest)
        return {'package_id':public_package.package_id,'package_path':str(public_package.directory.relative_to(output)),
            'business_status':status['status'],'latest_date':status['latest_date'],'required_contracts':status['required_contracts'],
            'success_contracts':status['success_contracts']}
    raise ValueError('unsupported scheduled job')


def main():
    output=Path('/output'); result={'status':'FAIL'}
    try:
        request=json.loads(Path('/baseline/request.json').read_bytes())
        credentials=json.load(sys.stdin)
        expected={'soybean_crop_progress':{'NASS_API_KEY'},'soybean_export_sales':{'FAS_EXPORT_SALES_API_KEY'}}.get(request['job'],set())
        if set(credentials)!=expected: raise ValueError('unexpected credentials')
        result.update(prepare(request,Path('/baseline'),output,credentials),status='PASS')
    except Exception as exc:
        result['error_type']=type(exc).__name__
    finally:
        (output/'collection-result.json').write_bytes(json.dumps(result,ensure_ascii=False).encode('utf-8'))
    print(json.dumps(result,ensure_ascii=False))
    return 0 if result['status']=='PASS' else 1


if __name__=='__main__': raise SystemExit(main())
