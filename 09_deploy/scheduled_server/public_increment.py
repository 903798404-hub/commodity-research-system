"""Root publisher: immutable object references for accepted public increments.

Candidate acceptance always copies independent bytes. Production release files
reference the existing protected retention pool, never writable collector files.
No chmod/chown/ACL operation is performed on a reused file inode.
"""
from pathlib import Path
import importlib.util
import os
import subprocess


def prepare_upload(host, config, work, extra, package, upload, runtime, publish, invoke):
    store = Path(config['allocation_root']) / '01_data/public-data-server-store'
    baseline = store / 'releases' / extra['public_pointer']['package_id']
    host.require(host.read_json(store / 'current.json') == extra['public_pointer'], 'increment Current moved')
    host.require(host.sha256_file(baseline / 'manifest.json') == extra['public_manifest_sha256'], 'increment baseline moved')
    sealed = host._file_set(package)
    mounted_baseline = '/baseline/' + baseline.name
    proof = invoke(['docker', 'run', '--rm', '--pull', 'never', '--network', 'none', '--read-only',
        '--cap-drop', 'ALL', '--security-opt', 'no-new-privileges', '--memory', '1024m', '--cpus', '1',
        '--log-driver', 'json-file', '--log-opt', 'max-size=20m', '--log-opt', 'max-file=5',
        '--pids-limit', '96', '--user', '65532:65532',
        '--mount', f'type=bind,src={package},dst=/increment,readonly',
        '--mount', f'type=bind,src={baseline},dst={mounted_baseline},readonly',
        '--entrypoint', 'python', config['image_id'], '-I', '-B', '/app/04_scripts/validate_public_increment.py',
        '--increment', '/increment', '--baseline', mounted_baseline])
    host.require(proof['status'] == 'PASS' and host._file_set(package) == sealed, 'increment offline validation differs')
    manifest = host.read_json(package / 'manifest.json')
    host.require(manifest['package_id'] == proof['package_id'], 'increment identity differs')
    host.require(host.read_json(store / 'current.json') == extra['public_pointer'], 'increment Current moved during validation')
    old = {f['path']: f for f in extra['public_manifest']['files']}
    pool = None
    if publish:
        pool = host._protected(config['public_object_pool'])
        host.require(pool.is_dir() and pool.stat().st_uid == 0 and pool.stat().st_mode & 0o777 == 0o700
                     and pool.stat().st_dev == store.stat().st_dev, 'immutable pool identity differs')
    # Reuse the maintenance tool's object format and exact-byte validation. Its
    # existing retention task recognizes these links without changing its rules.
    path = Path(__file__).resolve().parents[2] / '04_scripts/maintenance/public_package_retention.py'
    spec = importlib.util.spec_from_file_location('public_increment_objects', path)
    objects = importlib.util.module_from_spec(spec); spec.loader.exec_module(objects)
    upload.mkdir(mode=0o700)
    (upload / 'data').mkdir(mode=0o700)
    copied_bytes = reused_bytes = 0
    for item in manifest['files']:
        relative = item['path']
        host.require(not Path(relative).is_absolute() and '..' not in Path(relative).parts, 'increment path unsafe')
        source = (baseline if old.get(relative) == item else package) / 'data' / relative
        host._no_links(source)
        host.require(source.is_file() and source.stat().st_size == item['size_bytes']
                     and host.sha256_file(source) == item['sha256'], 'increment source changed')
        target = upload / 'data' / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        if publish:
            existed = (pool / item['sha256']).exists()
            obj = objects.immutable_object(pool, source, item)
            os.link(obj, target)
            if existed: reused_bytes += item['size_bytes']
            else: copied_bytes += item['size_bytes']
        else:
            host._write_exclusive(target, source.read_bytes())
            copied_bytes += item['size_bytes']
    host._write_exclusive(upload / 'manifest.json', (package / 'manifest.json').read_bytes())
    owner = config['public_store_owner']
    # Shared object files are root-owned 0444. Only new directories and this
    # release's own manifest receive publisher ownership and application ACLs.
    for directory in (upload, *sorted(p for p in upload.rglob('*') if p.is_dir())):
        host._no_links(directory)
        os.chown(directory, owner['uid'], owner['gid']); directory.chmod(0o700)
        subprocess.run(['setfacl', '-m', f'u:{runtime["uid"]}:r-x', '--', str(directory)],
                       check=True, capture_output=True, timeout=30)
    for file in (p for p in upload.rglob('*') if p.is_file()):
        if publish and file != upload / 'manifest.json': continue
        os.chown(file, owner['uid'], owner['gid']); file.chmod(0o400)
        subprocess.run(['setfacl', '-m', f'u:{runtime["uid"]}:r--', '--', str(file)],
                       check=True, capture_output=True, timeout=30)
    host.require(host._file_set(package) == sealed, 'increment changed during projection')
    # The generic upload file-set checker deliberately rejects shared inodes.
    # Here sharing is allowed only with the exact protected object just verified.
    actual={}
    for file in sorted((upload/'data').rglob('*')):
        host._no_links(file)
        host.require(file.is_file() or file.is_dir(),'special release file refused')
        if not file.is_file():continue
        identity={'sha256':host.sha256_file(file),'size_bytes':file.stat().st_size}
        if publish:
            host.require(os.path.samefile(file,pool/identity['sha256']) and not file.stat().st_mode & 0o222,
                         'release file is not an immutable pool object')
        else:host.require(file.stat().st_nlink==1,'candidate shares a file inode')
        actual[file.relative_to(upload/'data').as_posix()]=identity
    host.require(actual == {i['path']: {k: i[k] for k in ('sha256', 'size_bytes')} for i in manifest['files']},
                 'release projection differs')
    report = {**proof, 'storage_mode': 'IMMUTABLE_OBJECT_REFERENCES' if publish else 'INDEPENDENT_CANDIDATE_COPY',
              'new_object_bytes': copied_bytes if publish else 0, 'reused_object_bytes': reused_bytes,
              'candidate_copy_bytes': copied_bytes if not publish else 0,
              'object_pool': str(pool) if pool else None}
    host._write_json_exclusive(work / 'public-increment-storage.json', report)
    return report
