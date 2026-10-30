"""Server-owned schedules; pinned isolated collection, existing CAS publishers."""
from __future__ import annotations

import argparse
import base64
from datetime import datetime, timedelta, timezone
import importlib.util
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
import uuid

ROOT = Path(__file__).resolve().parents[2]
RUNTIME = Path('/var/lib/market-data/production-data-automation/scheduled')
CONFIG_ROOT = Path('/etc/market-data/scheduled-server')
SECRET_ROOT = Path('/etc/market-data/production-input-secrets')
JOBS = {
    'akshare': ('public_current', ['Mon..Fri *-*-* 16:30:00 Asia/Shanghai','Mon..Fri *-*-* 18:30:00 Asia/Shanghai','Mon..Fri *-*-* 22:30:00 Asia/Shanghai']),
    'canola_exports': ('canola_exports', ['*-*-* 08:30:00 Asia/Shanghai']),
    'positions_domestic': ('commodity_positions', ['Mon..Fri *-*-* 16:35:00 Asia/Shanghai', 'Mon..Fri *-*-* 18:00:00 Asia/Shanghai',
        'Mon..Fri *-*-* 22:30:00 Asia/Shanghai', 'Tue..Sat *-*-* 08:20:00 Asia/Shanghai']),
    'positions_foreign': ('commodity_positions', ['*-*-* 07:45:00 Asia/Shanghai']),
    'soybean_crop_progress': ('soybean_crop_progress', ['Tue..Thu *-*-* 06:30:00 Asia/Shanghai']),
    'soybean_export_sales': ('soybean_export_sales', ['Sat *-*-* 06:15:00 Asia/Shanghai']),
    'brazil_soy': ('brazil_soy', ['*-*-* 08:10:00 Asia/Shanghai']),
}
KEYS = {'soybean_crop_progress': 'NASS_API_KEY', 'soybean_export_sales': 'FAS_EXPORT_SALES_API_KEY'}


def render_units(job,source,config):
    if job not in JOBS or any(not re.fullmatch(r'/[A-Za-z0-9_./-]+',v) or '..' in Path(v).parts for v in (source,config)):
        raise ValueError('unit inputs invalid')
    service=(ROOT/'09_deploy/scheduled_server/market-data-scheduled.service.template').read_text(encoding='utf-8')
    service=service.replace('@JOB@',job).replace('@SOURCE_ROOT@',source).replace('@CONFIG_PATH@',config)
    name='market-data-'+job.replace('_','-')
    timer='[Unit]\nDescription=Beijing schedule for '+job+'\n\n[Timer]\n'
    timer+=''.join('OnCalendar='+clock+'\n' for clock in JOBS[job][1])
    timer+='Persistent=true\nAccuracySec=1s\nRandomizedDelaySec=0\nUnit='+name+'.service\n\n[Install]\nWantedBy=timers.target\n'
    return service,timer


def module(name, relative):
    spec = importlib.util.spec_from_file_location(name, ROOT / relative)
    value = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(value)
    return value


def helpers():
    fx = module('scheduled_fx_host', '09_deploy/foreign_fx_server/run.py')
    return fx, fx.load_host()


def validate_config(host, value):
    required = {'schema_version', 'job', 'policy', 'policy_sha256', 'image_id', 'data_disk_uuid',
                'secret_file', 'initial_baseline', 'initial_baseline_sha256', 'allocation_root', 'producer', 'public_store_owner'}
    if isinstance(value, dict) and 'incremental_public_packages' in value:
        required.update({'incremental_public_packages','public_object_pool'})
        host.require(value.get('job')=='akshare' and value['incremental_public_packages'] is True,
                     'incremental public packages require explicit public job enablement')
        host._under(value['public_object_pool'],Path('/var/lib/market-data/storage-hygiene'))
        host.require(Path(value['public_object_pool']).name=='objects','public object pool path invalid')
    if isinstance(value, dict) and 'partitioned_public_packages' in value:
        required.add('partitioned_public_packages')
        host.require(value.get('job') == 'akshare' and type(value['partitioned_public_packages']) is bool,
                     'partitioned packages require explicit public job boolean')
    if isinstance(value, dict) and 'positions_stock_api' in value:
        required.add('positions_stock_api')
        host.require(value.get('job') == 'positions_domestic' and type(value['positions_stock_api']) is bool,
                     'stock API requires explicit domestic positions boolean')
    c = host._exact(value, required, 'scheduled server config')
    host.require(c['schema_version'] == 'scheduled-server-config/1' and c['job'] in JOBS, 'scheduled job invalid')
    host.require(type(c['image_id']) is str and host.IMAGE_ID.fullmatch(c['image_id']), 'scheduled image invalid')
    host.require(type(c['data_disk_uuid']) is str and re.fullmatch(r'[0-9a-f]{8}-(?:[0-9a-f]{4}-){3}[0-9a-f]{12}', c['data_disk_uuid']), 'data disk UUID invalid')
    host._under(c['allocation_root'], host.ALLOCATION_ROOT)
    p = host._exact(c['producer'], {'commit', 'tree', 'origin'}, 'producer')
    host.require(p['origin'] == 'https://github.com/903798404-hub/commodity-research-system.git'
                 and all(type(p[k]) is str and host.HEX40.fullmatch(p[k]) for k in ('commit','tree')), 'producer identity invalid')
    if credential_key(c):
        host._under(c['secret_file'], SECRET_ROOT)
    else:
        host.require(c['secret_file'] is None, 'unexpected server credential')
    if c['job'] == 'akshare':
        owner=host._exact(c['public_store_owner'],{'uid','gid'},'public store owner')
        host.require(all(type(v) is int and 0 < v < 65532 for v in owner.values()),'public publisher owner invalid')
        host.require(c['policy'] is None and c['policy_sha256'] is None, 'public package cannot use delta policy')
        host._under(c['initial_baseline'], RUNTIME / 'initial-baselines')
        host.require(type(c['initial_baseline_sha256']) is str and host.HEX64.fullmatch(c['initial_baseline_sha256']), 'spread baseline pin invalid')
    else:
        host.require(c['public_store_owner'] is None,'unexpected public store owner')
        host._under(c['policy'], host.POLICY_ROOT)
        host.require(type(c['policy_sha256']) is str and host.HEX64.fullmatch(c['policy_sha256']), 'policy pin invalid')
        host.require(c['initial_baseline'] is None and c['initial_baseline_sha256'] is None, 'unexpected static domain baseline')
    return c


def credential_key(config):
    if config['job'] == 'positions_domestic' and config.get('positions_stock_api', False):
        return 'STOCK_API_TOKEN'
    return KEYS.get(config['job'])


def check_identity(host, config):
    host.require(host._git_identity(ROOT) == config['producer'], 'server source differs from approval')
    host._inspect_image({'image_id':config['image_id'], 'commit':config['producer']['commit'], 'tree':config['producer']['tree']})
    if config['job'] == 'akshare':
        host._protected(host._under(config['allocation_root'], host.ALLOCATION_ROOT))
        return {'approved_producer':config['producer'], 'allocation_root':config['allocation_root']}
    host.require(host.sha256_file(host._protected(config['policy'])) == config['policy_sha256'], 'server policy changed')
    policy, digest = host._load_policy(config['policy'])
    host.require(policy['domain'] == JOBS[config['job']][0] and digest == config['policy_sha256']
        and policy['approved_producer'] == config['producer'] and policy['source_clone'] == str(ROOT)
        and policy['allocation_root'] == config['allocation_root']
        and policy['validation_image']['image_id'] == config['image_id'], 'server policy binding differs')
    return policy


def secret_input(host, config):
    key = credential_key(config)
    if key is None:
        return b'{}'
    path = host._protected(config['secret_file'])
    host.require(path.stat().st_size <= 8192 and path.stat().st_mode & 0o077 == 0, 'server secret permissions invalid')
    values = {}
    for line in path.read_text(encoding='utf-8').splitlines():
        if not line.strip() or line.lstrip().startswith('#'): continue
        name, sep, value = line.partition('=')
        host.require(sep and name == key and name not in values, 'credential key invalid')
        value = value.strip()
        if len(value) > 1 and value[0] == value[-1] and value[0] in "\"'": value=value[1:-1]
        host.require(value and not any(ord(c) < 32 for c in value), 'credential value invalid')
        values[name] = value
    host.require(set(values) == {key}, 'credential missing')
    return json.dumps(values).encode('utf-8')


def copy_snapshot(host, snapshot, destination, request):
    host.require(set(snapshot['files']) == set(snapshot['contents']), 'baseline content set differs')
    destination.mkdir(mode=0o755)
    total = 0
    for relative, identity in snapshot['files'].items():
        host.require(not Path(relative).is_absolute() and '..' not in Path(relative).parts, 'baseline path unsafe')
        encoded = snapshot['contents'][relative]
        host._file_identity(identity, nullable=True)
        if identity is None:
            host.require(encoded is None, 'absent input has content'); continue
        raw = base64.b64decode(encoded,validate=True)
        total += len(raw)
        host.require(total <= 256 * 1024**2 and dict(sha256=host.sha256_bytes(raw),size_bytes=len(raw)) == identity, 'baseline copy differs')
        path = destination / relative
        path.parent.mkdir(parents=True,exist_ok=True)
        # Large positions/CGC baseline files are mounted read-only as exact files.
        # Keep only a mountpoint placeholder in the per-run baseline directory.
        mounted = (request['job'].startswith('positions_') and relative == host.POSITIONS_STABLE) or (
            request['job']=='canola_exports' and relative in {host.EXPORTS_STABLE,host.EXPORTS_SOURCES}) or (
            request['job']=='soybean_crop_progress' and relative in {host.CROP_PROGRESS,host.CROP_CONDITION,
                host.CROP_LEGACY_PROGRESS,host.CROP_LEGACY_CONDITION}) or (
            request['job']=='soybean_export_sales' and relative == host.FAS_STABLE)
        host._write_exclusive(path, b'' if mounted else raw)
        if mounted and request['job'].startswith('positions_'):
            # Nested bind target must exist before the baseline becomes read-only.
            (path.parent/'.position-store').mkdir(mode=0o755)
    host._write_json_exclusive(destination / 'request.json',request)
    host._make_tree_readonly(destination,public_read=True)


def disk_budget(host, config, snapshot, stats=None):
    size=sum(item['size_bytes'] for item in snapshot['files'].values() if item is not None)
    host.require(size <= 256*1024**2, 'scheduled baseline exceeds disk budget')
    # Copies, collector output, sealed upload and a new release can coexist.
    reserve=768*1024**2
    required=size*(5 if config['job']=='akshare' else 3)+reserve
    stats=os.statvfs(config['allocation_root']) if stats is None else stats
    available=stats.f_bavail*stats.f_frsize
    host.require(available >= required and stats.f_favail > 1000, 'scheduled data disk capacity insufficient')
    return {'baseline_bytes':size,'required_free_bytes':required,'available_bytes':available,'reserved_bytes':reserve}


def collector_command(config, work, *, public_preflight=False):
    public_materialize=config['job']=='akshare' and not public_preflight
    baseline='preflight-baseline' if public_preflight else 'baseline'
    output='preflight' if public_preflight else 'collection'
    command = ['docker','run','--rm','-i','--name',work.name,'--pull','never','--network','bridge',
        '--read-only','--cap-drop','ALL','--security-opt','no-new-privileges','--user','65532:65532',
        '--memory','1024m' if config['job']=='akshare' else '768m','--cpus','1','--pids-limit','96',
        '--log-driver','json-file','--log-opt','max-size=20m','--log-opt','max-file=5',
        '--tmpfs','/tmp:rw,noexec,nosuid,nodev,size=128m','--env','TZ=Asia/Shanghai',
        '--mount',f'type=bind,src={ROOT},dst=/source,readonly',
        '--mount',f'type=bind,src={work / "baseline"},dst=/baseline,readonly',
        '--mount',f'type=bind,src={work / "collection"},dst=/output']
    if config['job'].startswith('positions_'):
        relative='01_data/processed/commodity_positions/positions_archive.json'
        command += ['--mount',f'type=bind,src={config["allocation_root"]}/{relative},dst=/baseline/{relative},readonly']
        # Host launcher is stdlib-only; the pinned image interprets this store.
        DIRECTORY='.position-store'
        pool=Path(config['allocation_root'])/Path(relative).parent/DIRECTORY
        if pool.exists():
            host=helpers()[1]
            host._protected_tree(pool)
            command+=['--mount',f'type=bind,src={pool},dst=/baseline/{Path(relative).parent/DIRECTORY},readonly']
    if config['job']=='canola_exports':
        request=json.loads((work/'baseline/request.json').read_text(encoding='utf-8'))
        for relative in ('01_data/processed/canola_exports/weekly.json',
                         '01_data/processed/canola_exports/source_evidence.json'):
            if request['baseline_files'][relative] is not None:
                command+=['--mount',f'type=bind,src={config["allocation_root"]}/{relative},dst=/baseline/{relative},readonly']
    if config['job'] in {'soybean_crop_progress','soybean_export_sales'}:
        request=json.loads((work/'baseline/request.json').read_text(encoding='utf-8'))
        relative_paths = ('01_data/processed/soybean_export_sales/soybean_export_sales_weekly.parquet',) if config['job']=='soybean_export_sales' else (
            '01_data/processed/soybean_crop_progress/soybeans_crop_progress_weekly.parquet',
            '01_data/processed/soybean_crop_progress/soybeans_crop_condition_weekly.parquet',
            '01_data/processed/soybean_crop_progress/soybeans_crop_progress_weekly_2021_2026.parquet',
            '01_data/processed/soybean_crop_progress/soybeans_crop_condition_weekly_2021_2026.parquet')
        for relative in relative_paths:
            if request['baseline_files'][relative] is not None:
                command+=['--mount',f'type=bind,src={config["allocation_root"]}/{relative},dst=/baseline/{relative},readonly']
    if config['job']=='akshare':
        command[command.index('--network')+1]='none' if public_materialize else 'bridge'
        command=[arg.replace(f'src={work / "baseline"},',f'src={work / baseline},')
                    .replace(f'src={work / "collection"},',f'src={work / output},') for arg in command]
        if public_materialize and config.get('incremental_public_packages',False):
            request=json.loads((work/'baseline/request.json').read_text(encoding='utf-8'))
            package_id=request['public_pointer']['package_id']
            if not re.fullmatch(r'public-current-[0-9a-f]{24}',package_id):raise ValueError('public mount identity invalid')
            source=Path(config['allocation_root'])/'01_data/public-data-server-store/releases'/package_id
            command+=['--mount',f'type=bind,src={source},dst=/public-baseline/{package_id},readonly']
    return command + ['--entrypoint','python',config['image_id'],'-I','-B','/source/09_deploy/scheduled_server/collect.py'] + (['--public-preflight'] if public_preflight else [])


def collect(host, config, work, *, public_preflight=False, input_data=None):
    output=work/('preflight' if public_preflight else 'collection'); output.mkdir(mode=0o700); os.chown(output,65532,65532)
    credential=secret_input(host,config) if input_data is None else input_data
    try:
        completed=subprocess.run(collector_command(config,work,public_preflight=public_preflight),input=credential,capture_output=True,timeout=2400)
        # No provider dumps or credential-bearing exception strings cross the boundary.
        host._write_json_exclusive(work/('preflight-exit.json' if public_preflight else 'collector-exit.json'),{'exit_code':completed.returncode})
        path=host._no_links(output/'collection-result.json')
        host.require(path.is_file() and path.stat().st_size <= 256*1024, 'collector result absent or oversized')
        result=host.read_json(path)
        host.require(completed.returncode == 0 and result.get('status') == 'PASS', 'network collector failed: '+str(result.get('error_type','unknown')))
        return result
    except subprocess.TimeoutExpired:
        cleanup=subprocess.run(['docker','rm','-f',work.name],capture_output=True,timeout=30)
        host._write_json_exclusive(work/'timeout-cleanup.json',{'exit_code':cleanup.returncode})
        raise
    finally:
        credential=b''


def seal_delta(host, config, work):
    package=work/'collection/package'
    delta=host.validate_delta_document(host.read_json(package/host.MANIFEST_NAME))
    host.require(delta['delta_id']==work.name and delta['domain']==JOBS[config['job']][0]
                 and delta['producer']==config['producer'], 'collector delta identity differs')
    expected={host.MANIFEST_NAME,*delta['payloads']}
    before=host._file_set(package)
    host.require(set(before)==expected,'unexpected collector payload')
    upload=host.UPLOAD_ROOT/work.name; host._no_links(upload); upload.mkdir(mode=0o700)
    for name in sorted(expected): host._write_exclusive(upload/name,(package/name).read_bytes())
    host.require(host._file_set(upload)==before and host._file_set(package)==before,'payload changed sealing')
    digest=host.sha256_file(upload/host.MANIFEST_NAME)
    receipt=host.stage_upload(config['policy'],upload,digest)
    host.receive(config['policy'],receipt['incoming_path'],digest)
    return host.validate(config['policy'],work.name)


def execute(fx, host, config, work, publish, business_date=None):
    now=datetime.now(timezone.utc)
    result={'schema_version':'scheduled-server-result/1','job':config['job'],'run_id':work.name,
        'producer':config['producer'],'execution_host':'server','status':'FAIL','published':False,'checked_at':now.isoformat()}
    stage='baseline'
    volume_before=fx.volume_usage(host,config)
    try:
        check_identity(host,config)
        if config['job']=='akshare':
            public=module('server_public_delivery','09_deploy/scheduled_server/public.py')
            snapshot, extra=public.snapshot(host,config,work)
        else:
            snapshot=host.snapshot_fx_baseline(config['policy']); extra={}
            host.require(snapshot['producer']==config['producer'] and snapshot['image_id']==config['image_id']
                and snapshot['policy_sha256']==config['policy_sha256'] and snapshot['allocation_root']==config['allocation_root'], 'fresh baseline identity differs')
        request={'job':config['job'],'run_id':work.name,'checked_at':now.isoformat(),
            'business_date':now.astimezone(timezone(timedelta(hours=8))).date().isoformat(),
            'producer':config['producer'],'baseline_files':snapshot['files'],**extra}
        if config['job']=='soybean_export_sales' and snapshot.get('fas_reconciliation_check') is not None:
            request['fas_reconciliation_check']=snapshot['fas_reconciliation_check']
        if config['job'] == 'akshare':
            request['partitioned_public_packages'] = config.get('partitioned_public_packages', False)
            request['incremental_public_packages'] = config.get('incremental_public_packages',False)
            if business_date is not None:
                selected=datetime.strptime(business_date,'%Y-%m-%d').date()
                host.require(selected.isoformat()==business_date and selected<=now.astimezone(timezone(timedelta(hours=8))).date(), 'invalid public business date')
                request['business_date']=business_date
        if config['job'] == 'positions_domestic':
            request['positions_stock_api'] = config.get('positions_stock_api', False)
        result['disk_budget']=disk_budget(host,config,snapshot)
        if config['job']=='akshare':
            # No historical files are persisted for this network-only phase.
            phase=work/'preflight-baseline'; phase.mkdir(mode=0o755)
            host._write_json_exclusive(phase/'request.json',request)
            host._make_tree_readonly(phase,public_read=True)
            stage='source_preflight'
            checked=collect(host,config,work,public_preflight=True,
                input_data=json.dumps({'price_baseline':snapshot['contents']['01_data/historical_price_long.xlsx']}).encode('utf-8'))
            result['preflight']=checked
            proof=public.read_preflight(host,work,request,checked)
            host._make_tree_readonly(work/'preflight')
            if checked['business_status']=='SOURCE_FAILED':
                raise ValueError('public source request failed')
            if checked['business_status']=='WAITING_FOR_SOURCE':
                result.update(status='WAITING_FOR_SOURCE',published=False); return result
            if checked['business_status']=='NO_CHANGE':
                receipt=public.unchanged(host,config,extra,publish)
                result.update(status=receipt['status'],receipt=receipt,published=False); return result
            request['public_preflight']=proof
        copy_snapshot(host,snapshot,work/'baseline',request)
        stage='collect_sources'; collected=collect(host,config,work); result['collection']=collected
        if collected.get('out_of_season'):
            result.update(status='OUT_OF_SEASON'); return result
        stage='offline_validation'
        if config['job']=='akshare':
            receipt=public.deliver(host,config,work,extra,publish=publish)
        else:
            report=seal_delta(host,config,work)
            result.update(validation_report=report['report_path'],validation_sha256=report['report_sha256'])
            receipt={'status':'CANDIDATE'}
            if publish:
                stage='publish'; check_identity(host,config); fx.check_mount(host,config)
                receipt=host.publish(config['policy'],work.name,report['report_path'],report['report_sha256'])
        result.update(status=receipt['status'],published=receipt['status'] in {'PUBLISHED','SYNCED'},receipt=receipt)
        if any(a.get('status')=='failed' for a in collected.get('attempts',[])):
            result.update(delivery_status=result['status'],status='PARTIAL_FAILURE')
    except Exception as exc:
        result.update(error={'stage':stage,'error_type':type(exc).__name__})
        raise
    finally:
        result['checked_at']=datetime.now(timezone.utc).isoformat()
        result['volume_metrics']=fx.volume_change(host,config,volume_before)
        host._write_json_exclusive(work/'result.json',result)
        fx.write_state(host,RUNTIME/(config['job']+'-last-run.json'),result)
    return result


def run(config_path,publish=False,business_date=None):
    fx,host=helpers()
    host.require(sys.platform=='linux' and os.geteuid()==0 and sys.flags.isolated and sys.dont_write_bytecode,
                 'protected Linux Python -I -B required')
    path=host._protected(host._under(config_path,CONFIG_ROOT))
    config=validate_config(host,host.read_json(path)); digest=host.sha256_file(path)
    host.require(business_date is None or config['job']=='akshare','business date override is public-only')
    fx.check_mount(host,config); check_identity(host,config); host._protected(RUNTIME)
    import fcntl
    lock=host._no_links(RUNTIME/(JOBS[config['job']][0]+'.lock'))
    with lock.open('a+b') as handle:
        try: fcntl.flock(handle.fileno(),fcntl.LOCK_EX|fcntl.LOCK_NB)
        except BlockingIOError: return {'status':'SKIPPED','reason':'same data domain already running','job':config['job']}
        # Bound concurrency without blocking or interrupting any existing task.
        with host._no_links(RUNTIME/'collector.lock').open('a+b') as resource:
            fcntl.flock(resource.fileno(),fcntl.LOCK_EX)
            host.require(host.sha256_file(path)==digest,'config changed during admission')
            name=config['job'].replace('_','-')+'-server-'+datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%S').lower()+'-'+uuid.uuid4().hex[:12]
            work=RUNTIME/name; work.mkdir(mode=0o755)
            return execute(fx,host,config,work,publish,business_date)


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--config',required=True,type=Path); parser.add_argument('--publish',action='store_true')
    parser.add_argument('--business-date',help='Explicit bounded public-market business date for an authorized retry')
    args=parser.parse_args()
    try:
        result=run(args.config,args.publish,args.business_date); print(json.dumps(result,ensure_ascii=False))
        return 2 if result['status']=='PARTIAL_FAILURE' else 0
    except Exception as exc:
        print(json.dumps({'status':'FAIL','error_type':type(exc).__name__}),file=sys.stderr); return 1


if __name__=='__main__': raise SystemExit(main())
