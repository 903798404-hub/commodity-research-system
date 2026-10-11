"""Server-local Public Current delivery; preserve basis, CAS and actual app gates."""
from __future__ import annotations

import base64
import importlib.util
import json
import os
from pathlib import Path
import shutil
import subprocess
import time
import uuid

ROOT=Path(__file__).resolve().parents[2]
RUNTIME=Path('/var/lib/market-data/production-data-automation/scheduled')
FILES=('historical_price_long.xlsx','historical_spread_database.xlsx','historical_spread_database.parquet')


def assert_scope(request,manifest):
    old=request['public_manifest']
    keep=lambda value:{entry['path']:entry for entry in value['files'] if not entry['path'].startswith('consumer-artifacts/domestic-spread/')}
    if keep(old)!=keep(manifest) or old['current_identities']!=manifest['current_identities'] or old['source_max_dates']!=manifest['source_max_dates']:
        raise ValueError('public collection changed unrelated datasets')


def snapshot(host,config,work):
    allocation=host._protected(config['allocation_root'])
    store=Path(allocation)/'01_data/public-data-server-store'
    def managed(path):
        selected=host._no_links(path)
        for entry in (selected,*selected.parents):
            if entry==store.parent:break
            info=entry.stat();owner=config['public_store_owner']
            host.require(info.st_uid==owner['uid'] and info.st_gid==owner['gid'] and not info.st_mode & 0o022,
                         'public store publisher ownership or permissions differ')
        return selected
    managed(store)
    pointer=host.read_json(managed(store/'current.json'))
    host.require(re.fullmatch(r'public-current-[0-9a-f]{24}',pointer['package_id']), 'public package ID invalid')
    package=managed(store/'releases'/pointer['package_id'])
    manifest=host.read_json(managed(package/'manifest.json'))
    host.require(all(pointer[k]==manifest[k] for k in ('current_identity_sha256','delivery_identity_sha256','bundle_sha256')),'public pointer differs')
    continuation=RUNTIME/'akshare-continuation.json'
    if continuation.exists():
        state=host.read_json(host._protected(continuation))
        source=host._protected(host._under(state['directory'],RUNTIME/'continuations'))
        entries=state['files']
    else:
        source=host._protected(config['initial_baseline'])
        pin=host._protected(source/'baseline_manifest.json')
        host.require(host.sha256_file(pin)==config['initial_baseline_sha256'],'initial spread baseline changed')
        entries=host.read_json(pin)['files']
    host.require(set(entries)==set(FILES),'spread continuation set differs')
    host.require(entries['historical_spread_database.parquet']['sha256']==manifest['delivery_artifacts']['domestic-spread']['sha256'],
                 'spread continuation and actual Public Current differ')
    files,contents={},{}
    def add(relative,path,identity):
        host._file_identity(identity)
        host._no_links(path)
        host.require(path.is_file() and path.stat().st_size <= 64*1024**2,'spread input absent or oversized')
        raw=path.read_bytes()
        host.require({'sha256':host.sha256_bytes(raw),'size_bytes':len(raw)}==identity,'spread input identity differs')
        files[relative]=identity; contents[relative]=base64.b64encode(raw).decode('ascii')
    for name,identity in entries.items(): add('01_data/'+name,source/name,identity)
    prefix='public-package/'+pointer['package_id']
    for item in manifest['files']:
        host.require(type(item['path']) is str and re.fullmatch(r'[A-Za-z0-9_./-]+',item['path']) and
                     not Path(item['path']).is_absolute() and '..' not in Path(item['path']).parts,'public input path unsafe')
        add(prefix+'/data/'+item['path'],managed(package/'data'/item['path']),{k:item[k] for k in ('sha256','size_bytes')})
    raw=(package/'manifest.json').read_bytes()
    add(prefix+'/manifest.json',package/'manifest.json',{'sha256':host.sha256_bytes(raw),'size_bytes':len(raw)})
    host.require(host.read_json(store/'current.json')==pointer,'Public Current moved while reading')
    return {'files':files,'contents':contents},{'public_pointer':pointer,'public_manifest':manifest,
        'public_manifest_sha256':host.sha256_bytes(raw),'public_package_relative':prefix}


def transport():
    spec=importlib.util.spec_from_file_location('scheduled_public_transport',ROOT/'04_scripts/transfer_public_data_package.py')
    value=importlib.util.module_from_spec(spec); spec.loader.exec_module(value)
    value._transport_deadline=time.monotonic()+1500
    return value


def invoke(args):
    name='scheduled-public-'+uuid.uuid4().hex
    command=args[:3]+['--name',name]+args[3:]
    try:
        value=subprocess.run(command,capture_output=True,timeout=1200)
        if value.returncode:
            raise RuntimeError('offline public publisher failed; exit='+str(value.returncode))
        return json.loads(value.stdout.decode('utf-8','strict').strip().splitlines()[-1])
    finally:
        subprocess.run(['docker','rm','-f',name],capture_output=True,timeout=30)


def deliver(host,config,work,extra,publish):
    result=host.read_json(work/'collection/collection-result.json')
    host.require(re.fullmatch(r'packages/public-current-[0-9a-f]{24}',result['package_path']), 'public candidate path invalid')
    package=host._no_links(work/'collection'/result['package_path'])
    manifest=host.read_json(package/'manifest.json'); assert_scope(extra,manifest)
    store=Path(config['allocation_root'])/'01_data/public-data-server-store'
    previous=extra['public_pointer']['package_id']; candidate=manifest['package_id']
    host.require(candidate==result['package_id'],'public candidate ID changed')
    trans=transport(); runtime=trans._discover_application_runtime(None,str(store))
    owner=config['public_store_owner'];info=host._no_links(store).stat()
    host.require(info.st_uid==owner['uid'] and info.st_gid==owner['gid'] and owner['uid']!=runtime['uid'],
                 'public publisher owner changed or equals application owner')
    upload=store/'incoming'/(candidate+'.upload-'+work.name)
    host._no_links(upload)
    upload.mkdir(mode=0o700)
    before=host._file_set(package)
    for rel in before:
        path=upload/rel; path.parent.mkdir(parents=True,exist_ok=True)
        host._write_exclusive(path,(package/rel).read_bytes())
    host.require(host._file_set(upload)==before and host._file_set(package)==before,'public payload changed sealing')
    # Preserve the existing Windows basis publisher account; only this new upload changes ownership.
    for path in (upload,*upload.rglob('*')):
        host._no_links(path);os.chown(path,owner['uid'],owner['gid'])
    sealed=trans._ssh(None,['sh','-c',trans._SEAL_FOR_RUNTIME_READ_SCRIPT,'seal-owned-server-upload',str(upload),str(runtime['uid'])])
    host.require(sealed.returncode==0,'public candidate seal failed')
    pre=trans._run_application_runtime_gate(None,runtime,phase='PRE_SWITCH',
        package_root=runtime['container_store']+'/incoming/'+upload.name,expected_package_id=candidate)
    host.require(pre['STATUS']=='PASS','actual application cannot read candidate')
    mounted='/runtime/public-data-server-store'
    common=['docker','run','--rm','--pull','never','--network','none','--read-only','--cap-drop','ALL',
        '--security-opt','no-new-privileges','--memory','768m','--cpus','1','--pids-limit','96',
        '--user',f'{owner["uid"]}:{owner["gid"]}',
        '--tmpfs','/tmp:rw,nosuid,nodev,noexec,size=128m',
        '--env',f'PUBLIC_DATA_SERVER_STORE_ROOT={mounted}',
        '--mount',f'type=bind,src={store},dst={mounted}']
    command=[config['image_id'],'python','-I','-B','/app/04_scripts/activate_public_data_package.py',
        '--incoming-package',mounted+'/incoming/'+upload.name,'--store-root',mounted]
    readonly=common[:];readonly[readonly.index('--mount')+1]+=',readonly'
    validation=invoke(readonly+command+['--validate-only'])
    host.require(validation['status']=='VALIDATED' and validation['package_id']==candidate,'public offline validation differs')
    host.require(validation.get('manifest_sha256')==host.sha256_file(upload/'manifest.json') and
        'public-current-server-cas/1' in validation.get('activation_capabilities',[]),'public offline CAS identity invalid')
    receipt={'status':'CANDIDATE','package_id':candidate,'validation':validation,'application_pre_switch':pre}
    if not publish: return receipt
    if candidate==previous:
        host.require(host.read_json(store/'current.json')==extra['public_pointer'],'unchanged public pointer moved')
        receipt['status']='NO_CHANGE'
    else:
        args=['--expected-current-id',previous,'--expected-current-artifact-sha256',extra['public_manifest']['delivery_artifacts']['domestic-spread']['sha256'],
              '--expected-current-manifest-sha256',extra['public_manifest_sha256'],'--expected-candidate-id',candidate,
              '--expected-candidate-artifact-sha256',manifest['delivery_artifacts']['domestic-spread']['sha256'],
              '--expected-candidate-manifest-sha256',host.sha256_file(upload/'manifest.json')]
        activated=invoke(common+command+args)
        host.require(activated['status'] in {'SYNCED','NO_CHANGE'} and activated['package_id']==candidate,'public activation failed')
        receipt.update(status=activated['status'],activation=activated)
    post=trans._run_application_runtime_gate(None,runtime,phase='POST_SWITCH',package_root=None,expected_package_id=candidate)
    if post['STATUS']!='PASS':
        rollback=invoke(common+[config['image_id'],'python','-I','-B','/app/04_scripts/activate_public_data_package.py',
            '--store-root',mounted,'--rollback-after-application-readability-failure','--expected-failed-current-id',candidate,'--rollback-current-id',previous])
        host.require(rollback['status']=='ROLLED_BACK','application readability rollback failed')
        readback=trans._run_application_runtime_gate(None,runtime,phase='ROLLBACK_READBACK',package_root=None,expected_package_id=previous)
        host._write_json_exclusive(work/'public-rollback.json',{'rollback':rollback,'readback':readback})
        raise RuntimeError('application read failed; previous Current restored')
    receipt['application_post_switch']=post
    saved=RUNTIME/'continuations'/work.name
    saved.mkdir(mode=0o700,parents=True)
    entries={}
    for name in FILES:
        raw=(work/'collection/project/01_data'/name).read_bytes(); host._write_exclusive(saved/name,raw)
        entries[name]={'sha256':host.sha256_bytes(raw),'size_bytes':len(raw)}
    host._make_tree_readonly(saved)
    from_run=importlib.util.spec_from_file_location('scheduled_fx_state',ROOT/'09_deploy/foreign_fx_server/run.py')
    state_module=importlib.util.module_from_spec(from_run); from_run.loader.exec_module(state_module)
    state_module.write_state(host,RUNTIME/'akshare-continuation.json',{'directory':str(saved),'files':entries,'package_id':candidate})
    return receipt


import re
