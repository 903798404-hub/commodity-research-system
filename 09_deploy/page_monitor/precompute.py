"""Isolated, network-free derived charts using the original page functions."""
from __future__ import annotations

import argparse
from datetime import datetime, timezone
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import re
import shutil
import stat
import subprocess
import sys
from uuid import uuid4

ROOT=Path(__file__).resolve().parent.parent.parent
CONFIG_ROOT=Path('/etc/market-data/page-precompute')
RUN_ROOT=Path('/var/lib/market-data/production-data-automation/page-precompute')
MAX_BYTES=64*1024**2


def sha(raw):return hashlib.sha256(raw).hexdigest()


def render_units(source,config):
    if any(not re.fullmatch(r'/[A-Za-z0-9_./-]+',p) or '..' in p.split('/') for p in [source,config]):
        raise ValueError('Precompute unit path invalid')
    service='[Unit]\nDescription=Original basis and Brazil chart precomputation\nAfter=docker.service\nRequires=docker.service\n\n[Service]\nType=oneshot\nExecStart=/usr/bin/python3 -I -B '+source+'/09_deploy/page_monitor/precompute.py --config '+config+' --publish\nTimeoutStartSec=1500\nNice=10\nIOSchedulingClass=idle\nNoNewPrivileges=true\nPrivateTmp=true\nUMask=0022\n'
    timer='[Unit]\nDescription=Queue changed-input page precomputation\n\n[Timer]\nOnCalendar=*-*-* *:00,15,30,45:00 Asia/Shanghai\nPersistent=true\nAccuracySec=1s\nUnit=market-data-task-enqueue@precompute.service\n\n[Install]\nWantedBy=timers.target\n'
    return {'market-data-page-precompute.service':service,'market-data-page-precompute.timer':timer}


def worker(output):
    sys.path[:0]=['/app/05_apps','/app/03_src']
    import computation_cache as cache
    import basis_page as basis
    from agri_research_agent.market_data.activated_runtime import resolve_public_data_root
    from agri_research_agent.market_data.public_basis_current import load_public_basis_current,resolve_public_basis_current_identity
    from agri_research_agent.pipelines import brazil_soy as brazil
    import brazil_soy_page as brazil_page
    data=Path(os.environ['PUBLIC_MARKET_DATA_RUNTIME_ROOT']);out=Path(output)
    release=cache._derived_release()
    basis_root=resolve_public_data_root(data)/'public-market-data/lutou-domestic-basis'
    before=resolve_public_basis_current_identity(basis_root)
    brazil_path=data/brazil.STABLE_RELATIVE_PATH;brazil_sha=brazil.sha256_file(brazil_path)
    season=brazil.current_season()
    inputs={'basis_release':before.release_id,'basis_manifest_sha256':before.manifest_sha256,
        'brazil_sha256':brazil_sha,'season':season}
    previous=data/'cache/page-precomputations'/release['git_commit']/'published-index.json'
    if previous.is_file():
        old=json.loads(previous.read_bytes())
        files=old.get('files',{})
        if (old.get('schema_version')=='page-precompute-index/1' and old.get('image')==release
            and old.get('inputs')==inputs and files and len(files)<=cache.MAX_ENTRIES
            and all(re.fullmatch(r'[0-9a-f]{64}\.json',name) and (previous.parent/name).is_file()
                and sha((previous.parent/name).read_bytes())==digest for name,digest in files.items())):
            return {'status':'NO_CHANGE','image':release,'inputs':inputs,'files':files}
    snapshot=load_public_basis_current(basis_root,expected_release_id=before.release_id,expected_manifest_sha256=before.manifest_sha256)
    frame=basis.filter_display_data(snapshot.records);years=basis._latest_three_years(frame)
    # Run only these original read-only display paths. Their native widget
    # defaults prepare exactly the first-view cards, without upload/save paths.
    basis._render_basis_tab(frame,years)
    basis._render_cash_price_tab(frame,years)
    basis._render_wholesale_spread_tab(frame,years)
    bundle=brazil.load_bundle(brazil_path);frame=brazil.observations_frame(bundle)
    extra=[s for s in sorted(set(frame['season'])-{season},reverse=True) if '2022/2023'<=s<season]
    for metric in ['PLANTED','HARVESTED']:
        for region in brazil.REGIONS:brazil_page.seasonal_figure(frame,region,metric,season,extra)
    for metric in brazil.STAGES:brazil_page.seasonal_figure(frame,'BR',metric,season,extra)
    after=resolve_public_basis_current_identity(basis_root)
    if (after.release_id,after.manifest_sha256)!=(before.release_id,before.manifest_sha256) or brazil.sha256_file(brazil_path)!=brazil_sha:
        raise ValueError('Authoritative input changed during precomputation')
    files={p.name:sha(p.read_bytes()) for p in (out/release['git_commit']).glob('*.json')}
    if not files:raise ValueError('No derived charts produced')
    return {'status':'PREPARED','image':release,'inputs':inputs,'files':files}


def host_module():
    spec=importlib.util.spec_from_file_location('_precompute_monitor_host',ROOT/'09_deploy/page_monitor/run.py')
    host=importlib.util.module_from_spec(spec);spec.loader.exec_module(host);return host


def config_value(host,path):
    config=host.read_json(host.protected(path,CONFIG_ROOT))
    if set(config)!={'schema_version','producer','image_id','reader_id','data_root','container_data_root','data_disk_uuid'} or config['schema_version']!='page-precompute-config/1':
        raise ValueError('Precompute config invalid')
    if config['container_data_root']!='/runtime/01_data':raise ValueError('Precompute container data scope differs')
    producer=config['producer']
    if set(producer)!={'commit','tree','origin'}:raise ValueError('Precompute producer invalid')
    if producer['origin'] not in {'https://github.com/903798404-hub/commodity-research-system.git','git@github.com:903798404-hub/commodity-research-system.git'}:
        raise ValueError('Precompute origin invalid')
    if not re.fullmatch(r'[0-9a-f]{64}',config['reader_id']):raise ValueError('Exact precompute reader ID required')
    for key,args in [('commit',['rev-parse','HEAD']),('tree',['rev-parse','HEAD^{tree}']),('origin',['remote','get-url','origin'])]:
        if host.command(['git','-C',str(host.protected(ROOT)),*args]).decode().strip()!=producer[key]:raise ValueError('Precompute source differs')
    if host.command(['git','-C',str(ROOT),'status','--porcelain','--untracked-files=all']).strip():raise ValueError('Precompute source dirty')
    host.protected(config['data_root'],Path('/var/lib/market-data/production-runtime'))
    host.protected(RUN_ROOT)
    for path in [RUN_ROOT,Path(config['data_root'])]:
        if host.command(['findmnt','-n','-o','UUID','-T',str(path)]).decode().strip()!=config['data_disk_uuid']:
            raise ValueError('Precompute data disk differs')
    if shutil.disk_usage(config['data_root']).free<20_000_000_000+MAX_BYTES*2:
        raise ValueError('Precompute data disk reserve insufficient')
    image=json.loads(host.command(['docker','image','inspect',config['image_id']]))[0]
    labels=image['Config']['Labels'] or {}
    if labels.get('org.opencontainers.image.revision')!=producer['commit'] or labels.get('market-data.git.tree')!=producer['tree']:
        raise ValueError('Precompute image identity differs')
    reader=json.loads(host.command(['docker','inspect',config['reader_id']]))[0]
    mounts=[m for m in reader['Mounts'] if m['Destination']==config['container_data_root']]
    env=dict(item.split('=',1) for item in reader['Config']['Env'])
    if (reader['Id']!=config['reader_id'] or not reader['State']['Running'] or reader['Image']!=config['image_id'] or len(mounts)!=1
        or mounts[0]['Source']!=config['data_root'] or mounts[0]['RW']
        or env.get('PUBLIC_MARKET_DATA_RUNTIME_ROOT')!=config['container_data_root']):
        raise ValueError('Precompute current reader or input binding differs')
    return config,env.get('TZ')


def validate_outputs(directory,result,config):
    if result.get('status')!='PREPARED' or result.get('image')!={'git_commit':config['producer']['commit'],'git_tree':config['producer']['tree']}:
        raise ValueError('Precompute worker result differs')
    expected=set(result['files'])
    if not expected or len(expected)>256 or {p.name for p in directory.iterdir()}!=expected:
        raise ValueError('Precompute output inventory differs')
    total=0
    for name in expected:
        if not re.fullmatch(r'[0-9a-f]{64}\.json',name):raise ValueError('Precompute output name unsafe')
        p=directory/name;info=p.lstat()
        if not stat.S_ISREG(info.st_mode) or info.st_nlink!=1 or info.st_size>4*1024**2:
            raise ValueError('Precompute output unsafe')
        raw=p.read_bytes();total+=len(raw);payload=json.loads(raw)
        if (total>MAX_BYTES or sha(raw)!=result['files'][name]
            or set(payload)!={'schema_version','image','key','result','result_sha256'}
            or payload['schema_version']!='page-precomputation/1' or payload['key']!=p.stem
            or payload['image']!=result['image']
            or payload['result']['kind'] not in {'bytes','plotly'}
            or sha(json.dumps(payload['result'],ensure_ascii=False,sort_keys=True,separators=(',',':'),allow_nan=False).encode())!=payload['result_sha256']):
            raise ValueError('Precompute output content differs')
    return total


def publish_outputs(host,directory,result,config):
    validate_outputs(directory,result,config)
    root=Path(config['data_root'])/'cache/page-precomputations'
    root.mkdir(parents=True,exist_ok=True);host.protected(root)
    target=root/config['producer']['commit'];target.mkdir(exist_ok=True);host.protected(target)
    index=target/'published-index.json'
    old=host.read_json(host.protected(index)) if index.exists() else {'files':{}}
    for name,digest in result['files'].items():
        final=target/name;raw=(directory/name).read_bytes()
        if final.exists():
            host.protected(final)
            if final.read_bytes()!=raw:raise ValueError('Existing derived key has conflicting bytes')
            continue
        temporary=target/('.'+name+'.'+uuid4().hex)
        try:
            with temporary.open('xb') as handle:handle.write(raw);handle.flush();os.fsync(handle.fileno())
            temporary.chmod(0o444);os.replace(temporary,final)
        finally:temporary.unlink(missing_ok=True)
    # Keep the current and immediately previous calculation inputs. These are
    # declared disposable derived files; a concurrent miss uses original code.
    keep=set(result['files'])|set(old.get('files',{}))
    removed=[]
    for p in target.glob('*.json'):
        if p==index or p.name in keep:continue
        host.protected(p);payload=host.read_json(p)
        if not re.fullmatch(r'[0-9a-f]{64}\.json',p.name) or payload.get('schema_version')!='page-precomputation/1' or payload.get('key')!=p.stem or payload.get('image')!=result['image']:
            raise ValueError('Unknown derived file retained')
        p.unlink();removed.append(p.name)
    host.atomic(index,{'schema_version':'page-precompute-index/1','image':result['image'],'inputs':result['inputs'],'files':result['files']})
    return {'published_charts':len(result['files']),'derived_files_removed':removed}


def run(config_path,*,publish=False):
    import fcntl
    host=host_module();config,tz=config_value(host,config_path)
    with (RUN_ROOT/'precompute.lock').open('a') as lock:
        fcntl.flock(lock,fcntl.LOCK_EX|fcntl.LOCK_NB)
        run_id=datetime.now(timezone.utc).strftime('%Y%m%dt%H%M%S')+'-'+uuid4().hex[:10]
        scope=RUN_ROOT/run_id;scope.mkdir(mode=0o700);out=scope/'output';out.mkdir(mode=0o755);os.chown(out,65532,65532)
        result={'status':'FAIL','published':False,'started_at':datetime.now(timezone.utc).isoformat(),'run_id':run_id}
        try:
            command=['docker','run','--rm','--pull','never','--name','page-precompute-'+run_id,'--read-only','--network','none',
                '--user','65532:65532','--cap-drop','ALL','--security-opt','no-new-privileges','--memory','512m','--memory-swap','512m',
                '--cpus','0.75','--pids-limit','96','--tmpfs','/tmp:rw,noexec,nosuid,nodev,size=128m',
                '--mount',f'type=bind,src={config["data_root"]},dst={config["container_data_root"]},readonly',
                '--mount',f'type=bind,src={out},dst=/output','--mount',f'type=bind,src={Path(__file__).resolve()},dst=/worker.py,readonly',
                '--env','PUBLIC_MARKET_DATA_RUNTIME_ROOT='+config['container_data_root'],'--env','MARKET_DATA_PRECOMPUTE_MODE=record',
                '--env','MARKET_DATA_PRECOMPUTE_OUTPUT=/output','--env','MPLCONFIGDIR=/tmp/matplotlib']
            if tz:command+=['--env','TZ='+tz]
            command+=['--entrypoint','python',config['image_id'],'-I','-B','/worker.py','--worker','--output','/output']
            with (scope/'worker.stdout').open('xb') as stdout,(scope/'worker.stderr').open('xb') as stderr:
                process=subprocess.run(command,stdout=stdout,stderr=stderr,timeout=1200)
            process.check_returncode();prepared=json.loads((out/'worker-result.json').read_bytes())
            if prepared['status']=='PREPARED':
                directory=out/config['producer']['commit'];total=validate_outputs(directory,prepared,config)
                if publish:result.update(publish_outputs(host,directory,prepared,config))
                result.update(status='PUBLISHED' if publish else 'CANDIDATE',published=publish,bytes=total)
            elif prepared['status']=='NO_CHANGE':result.update(status='NO_CHANGE')
            else:raise ValueError('Unknown precompute worker outcome')
            result.update(inputs=prepared['inputs'],image=prepared['image'],charts=len(prepared['files']))
            # Per-run staging charts are verified duplicates after publication;
            # remove only this worker's owned output, retaining all receipts.
            if publish and prepared['status']=='PREPARED':
                for name,digest in prepared['files'].items():
                    p=out/config['producer']['commit']/name
                    if sha(p.read_bytes())!=digest:raise ValueError('Staging chart changed')
                    p.unlink()
                (out/config['producer']['commit']).rmdir()
        except BaseException as exc:
            result.update(error_type=type(exc).__name__);raise
        finally:
            result['finished_at']=datetime.now(timezone.utc).isoformat()
            host.atomic(scope/'result.json',result);host.atomic(RUN_ROOT/'last-run.json',result)
        return result


def main():
    parser=argparse.ArgumentParser();parser.add_argument('--config');parser.add_argument('--publish',action='store_true')
    parser.add_argument('--worker',action='store_true');parser.add_argument('--output');args=parser.parse_args()
    if args.worker:
        if args.config or args.publish or args.output!='/output':raise ValueError('Worker scope invalid')
        result=worker(args.output)
        (Path(args.output)/'worker-result.json').write_text(json.dumps(result,ensure_ascii=False,sort_keys=True),encoding='utf-8')
    else:
        if not args.config:parser.error('--config is required')
        print(json.dumps(run(args.config,publish=args.publish)))


if __name__=='__main__':main()
