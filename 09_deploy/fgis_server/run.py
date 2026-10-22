"""Protected FGIS collection, offline replay, CAS directory switch and rollback."""
from __future__ import annotations
import argparse
import datetime as dt
try:
    import fcntl
except ImportError:
    fcntl=None
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import shutil
import subprocess
import uuid

ROOT=Path(__file__).resolve().parents[2]
STATE=Path('/var/lib/market-data/production-data-automation/fgis')
DOMAIN='soybean_export_inspections'
STABLE=f'01_data/processed/{DOMAIN}/soybean_export_inspections_weekly.parquet'
MANIFEST=STABLE.replace('.parquet','.manifest.json')
STATUS=f'01_data/update_status/{DOMAIN}.json'
FILES=(STABLE,MANIFEST,STATUS)


def require(ok,message):
    if not ok:raise ValueError(message)
def sha(p):return hashlib.sha256(p.read_bytes()).hexdigest()
def read(p):return json.loads(p.read_bytes().decode('utf-8','strict'))
def identity(p):return {'sha256':sha(p),'size_bytes':p.stat().st_size}
def write(p,data):
    with p.open('x',encoding='utf-8') as f:json.dump(data,f,ensure_ascii=False,indent=2,allow_nan=False);f.flush();os.fsync(f.fileno())
def atomic(p,data):
    q=p.with_name(p.name+'.fgis-'+uuid.uuid4().hex);write(q,data);q.chmod(0o644);os.replace(q,p)
def host_module():
    spec=importlib.util.spec_from_file_location('fgis_protected_host',ROOT/'09_deploy/production_data_delivery/activate_production_data_delta.py')
    module=importlib.util.module_from_spec(spec);spec.loader.exec_module(module);return module


def approved_source(identity,c,origins):
    return (identity['commit']==c['commit'] and identity['tree']==c['tree']
            and identity['origin'] in origins)


def validate_config(path):
    host=host_module();host._protected(path)
    require(path.parent==Path('/etc/market-data/fgis-server') and path.stat().st_mode&0o777==0o600,'FGIS configuration path/mode invalid')
    c=read(path)
    require(set(c)=={'schema_version','source_root','commit','tree','image_id','allocation_root','data_disk_uuid'},'FGIS config fields differ')
    require(c['schema_version']=='fgis-server/1' and Path(c['source_root'])==ROOT,'FGIS source binding differs')
    require(approved_source(host._git_identity(ROOT),c,host.CANONICAL_ORIGINS),'FGIS approved Git identity differs')
    host._inspect_image({'image_id':c['image_id'],'commit':c['commit'],'tree':c['tree']})
    allocation=host._protected(host._under(c['allocation_root'],host.ALLOCATION_ROOT))
    actual=subprocess.check_output(['findmnt','-n','-o','UUID','-T',str(allocation)],text=True,timeout=15).strip()
    require(actual==c['data_disk_uuid'],'FGIS data disk identity differs')
    disk=os.statvfs(allocation);require(disk.f_bavail*disk.f_frsize>768*1024**2,'FGIS capacity insufficient')
    for relative in FILES:host._protected(allocation/relative)
    return c


def worker_command(c,work,*,replay=False):
    output=work/('verification' if replay else 'collection')
    cmd=['docker','run','--rm','--name',work.name+('-verify' if replay else '-collect'),'--pull','never',
        '--read-only','--network','none' if replay else 'bridge','--user','65532:65532',
        '--cap-drop','ALL','--security-opt','no-new-privileges','--memory','768m','--cpus','1','--pids-limit','96',
        '--tmpfs','/tmp:rw,noexec,nosuid,nodev,size=128m',
        '--mount',f'type=bind,src={c["allocation_root"]},dst=/runtime,readonly',
        '--mount',f'type=bind,src={work/"baseline"},dst=/baseline,readonly',
        '--mount',f'type=bind,src={output},dst=/output']
    if replay:cmd += ['--mount',f'type=bind,src={work/"collection"},dst=/candidate,readonly']
    cmd += ['--entrypoint','python',c['image_id'],'-I','-B','/app/04_scripts/soybean_exports/run_fgis_server_worker.py']
    if replay:cmd.append('--replay')
    return cmd


def seal(root):
    for p in root.rglob('*'):
        require(not p.is_symlink() and (p.is_file() or p.is_dir()),'FGIS output contains nonregular paths')
        os.chown(p,0,0);p.chmod(0o555 if p.is_dir() else 0o444)
    os.chown(root,0,0);root.chmod(0o555)


def remap(value):
    if isinstance(value,str):return value.replace('/output/project/','/runtime/')
    if isinstance(value,list):return [remap(x) for x in value]
    if isinstance(value,dict):return {k:remap(v) for k,v in value.items()}
    return value


def publish(c,work,request,result):
    host=host_module();allocation=Path(c['allocation_root'])
    require(all(identity(allocation/p)==x for p,x in request['baseline'].items()),'FGIS baseline drift before publication')
    candidate=work/'collection/project';formal=allocation/'01_data/processed'/DOMAIN
    require(set(host._file_set(formal))=={Path(STABLE).name,Path(MANIFEST).name},'FGIS managed directory contains unexpected files')
    backup=work/'backup';backup.mkdir();old_status=(allocation/STATUS).read_bytes();(backup/'status.json').write_bytes(old_status)
    # New raw evidence is immutable and retained even if later publication rolls
    # back. No historical source, revision log or backup is removed.
    raw_source=candidate/'01_data/raw'/DOMAIN
    for p in raw_source.iterdir():
        require(p.name==request['run_id'],'FGIS unexpected source run')
        destination=allocation/'01_data/raw'/DOMAIN/p.name
        require(not destination.exists(),'FGIS source history overwrite forbidden')
        shutil.copytree(p,destination);seal(destination)
    stage=formal.with_name('.fgis-publishing-'+request['run_id']);stage.mkdir(mode=0o755)
    shutil.copyfile(candidate/STABLE,stage/Path(STABLE).name)
    manifest=remap(read(candidate/MANIFEST));manifest['stable_path']='/runtime/'+STABLE
    write(stage/Path(MANIFEST).name,manifest)
    status=remap(read(candidate/STATUS));status['backup_dir']=str(backup)
    require(identity(stage/Path(STABLE).name)==result['files'][STABLE],'FGIS sealed stable differs')
    require(manifest['sha256'].lower()==sha(stage/Path(STABLE).name),'FGIS manifest not bound to stable')
    require(all(identity(allocation/p)==x for p,x in request['baseline'].items()),'FGIS stale publication rejected')
    seal(stage)
    revisions=candidate/'01_data/revisions'/DOMAIN
    if revisions.exists():shutil.copytree(revisions,backup/'revision-audit')
    switched=False
    try:
        host._renameat2(stage,formal,2);switched=True
        atomic(allocation/STATUS,status)
        require(sha(formal/Path(STABLE).name)==result['files'][STABLE]['sha256'],'FGIS production stable identity mismatch')
        os.rename(stage,backup/'processed')
    except Exception:
        if switched:
            require(stage.exists(),'FGIS rollback source missing')
            host._renameat2(stage,formal,2)
            restore=allocation/STATUS;tmp=restore.with_name(restore.name+'.restore-'+uuid.uuid4().hex);tmp.write_bytes(old_status);tmp.chmod(0o644);os.replace(tmp,restore)
        raise
    # Preserve native revision audit as an immutable run-owned file. Existing
    # history remains intact, and the native pipeline determines each revision.
    return {'status':'PUBLISHED','published':True,'backup_path':str(backup),
        'source_latest_week':status['last_success']['source_latest_week'],'revision_count':status['revision_count'],
        'stable_sha256':sha(formal/Path(STABLE).name),'published_at_utc':dt.datetime.now(dt.timezone.utc).isoformat()}


def run(config_path,*,publish_requested=False):
    require(os.name=='posix' and os.geteuid()==0 and fcntl is not None,'FGIS protected host required')
    c=validate_config(config_path);STATE.mkdir(parents=True,exist_ok=True,mode=0o755);host_module()._protected(STATE)
    with (STATE/'update.lock').open('a+b') as lock:
        fcntl.flock(lock,fcntl.LOCK_EX|fcntl.LOCK_NB)
        runid='fgis-'+dt.datetime.now(dt.timezone.utc).strftime('%Y%m%dT%H%M%S').lower()+'-'+uuid.uuid4().hex[:10]
        work=STATE/runid;work.mkdir(mode=0o755)
        baseline=work/'baseline';baseline.mkdir()
        request={'run_id':runid,'commit':c['commit'],'tree':c['tree'],'baseline':{p:identity(Path(c['allocation_root'])/p) for p in FILES}}
        write(baseline/'request.json',request);seal(baseline)
        outcome={'status':'FAIL','run_id':runid,'published':False}
        try:
            for replay in [False,True]:
                directory=work/('verification' if replay else 'collection');directory.mkdir(mode=0o755);os.chown(directory,65532,65532)
                command=worker_command(c,work,replay=replay)
                try:
                    proc=subprocess.run(command,capture_output=True,timeout=900)
                except subprocess.TimeoutExpired as error:
                    # This name was created exclusively for this locked run.
                    name=command[command.index('--name')+1]
                    stopped=subprocess.run(['docker','rm','-f',name],capture_output=True,timeout=30)
                    write(work/('verification-timeout.json' if replay else 'collection-timeout.json'),
                          {'container_name':name,'stop_exit_code':stopped.returncode})
                    raise RuntimeError('FGIS isolated worker exceeded its bounded runtime') from error
                (work/('verification' if replay else 'collection')).with_suffix('.stdout').write_bytes(proc.stdout)
                (work/('verification' if replay else 'collection')).with_suffix('.stderr').write_bytes(proc.stderr)
                require(proc.returncode==0,'FGIS isolated worker failed')
                observed=read(directory/'collection.json');seal(directory)
                if not replay:
                    result=observed
                    require(all(identity(Path(c['allocation_root'])/p)==x for p,x in request['baseline'].items()),'FGIS collector changed formal data')
                    if result['status']=='NO_CHANGE':
                        outcome={'status':'NO_CHANGE','published':False,'run_id':runid,
                                 'source_latest_week':read(Path(c['allocation_root'])/STATUS)['last_success']['source_latest_week'],
                                 'checked_at_utc':dt.datetime.now(dt.timezone.utc).isoformat()};break
                else:
                    require(observed.get('offline_replay')=='PASS','FGIS offline replay incomplete')
                    outcome={'status':'CANDIDATE','published':False,'run_id':runid,'source_latest_week':result['validation']['latest_week'],'offline_replay':'PASS'}
                    if publish_requested:
                        validate_config(config_path)
                        outcome.update(publish(c,work,request,result))
        finally:
            write(work/'result.json',outcome);atomic(STATE/'last-run.json',outcome)
        return outcome


def main():
    parser=argparse.ArgumentParser();parser.add_argument('--config',type=Path,required=True);parser.add_argument('--publish',action='store_true');args=parser.parse_args()
    print(json.dumps(run(args.config,publish_requested=args.publish),ensure_ascii=False));return 0
if __name__=='__main__':raise SystemExit(main())
