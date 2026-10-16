"""Conservative host retention: explicit retired images and recoverable staging.

No prune, no business/runtime/rollback deletion. Successful staging expires into
verified content-addressed gzip objects; receipts and complete restore indexes
remain. Unresolved, unknown, mounted and pinned resources fail closed.
"""
from __future__ import annotations

import argparse
from collections import defaultdict
from contextlib import ExitStack
from datetime import datetime, timedelta, timezone
import gzip
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import re
import stat
import subprocess
import uuid

BASE = Path('/srv/market-data-disk/active/var/lib/market-data')
STATE = BASE / 'storage-hygiene/runtime-retention'
IMAGE = re.compile(r'sha256:[0-9a-f]{64}')
RUN = re.compile(r'[a-z0-9-]+-server-\d{8}t\d{6}-[a-f0-9]{12}')


def require(ok, message):
    if not ok:
        raise ValueError(message)


def safe(path):
    path = Path(path)
    require(path.is_absolute() and '..' not in path.parts, 'absolute canonical path required')
    for p in (path, *path.parents):
        require(not p.is_symlink(), 'symlink refused')
    return path


def sha(path):
    h = hashlib.sha256()
    with safe(path).open('rb') as stream:
        for data in iter(lambda: stream.read(1024 * 1024), b''):
            h.update(data)
    return h.hexdigest()


def read(path):
    def pairs(items):
        d = {}
        for k, v in items:
            require(k not in d, 'duplicate JSON key')
            d[k] = v
        return d
    return json.loads(safe(path).read_text(encoding='utf-8'), object_pairs_hook=pairs)


def write_new(path, value):
    path = safe(path)
    with path.open('x', encoding='utf-8') as stream:
        json.dump(value, stream, ensure_ascii=False, sort_keys=True, indent=2)
        stream.flush()
        os.fsync(stream.fileno())
    sync_directory(path.parent)


def sync_directory(path):
    if os.name == 'posix':
        fd = os.open(safe(path), os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(fd)
        finally:
            os.close(fd)


def command(*args):
    return subprocess.check_output(args, timeout=180).decode().strip()


def overlap(a, b):
    return a == b or a in b.parents or b in a.parents


def inventory(root):
    root = safe(root)
    require(root.is_dir(), 'staging directory absent')
    rows = []
    for p in sorted(root.rglob('*')):
        safe(p)
        st = p.lstat()
        require(stat.S_ISREG(st.st_mode) or stat.S_ISDIR(st.st_mode), 'special file refused')
        require(not any(x in p.name.lower() for x in ('.env', 'credential', 'secret', '.pem', '.key')), 'sensitive file refused')
        if not stat.S_ISREG(st.st_mode):
            continue
        require(st.st_nlink == 1, 'shared file refused')
        rows.append({'path': p.relative_to(root).as_posix(), 'sha256': sha(p),
                     'bytes': st.st_size, 'mode': stat.S_IMODE(st.st_mode),
                     'uid': getattr(st, 'st_uid', 0), 'gid': getattr(st, 'st_gid', 0),
                     'mtime_ns': st.st_mtime_ns})
    return rows


def successful(d):
    return (d.get('collection', {}).get('status') == 'PASS' and (
        (d.get('status') in {'PUBLISHED', 'SYNCED'} and d.get('published') is True)
        or (d.get('status') == 'NO_CHANGE' and d.get('published') is False)))


def same_positions_scope(old, new):
    """A later success alone is not proof that a failed request was replaced.

    Positions receipts expose source attempts and exact added partition keys.
    Unknown jobs, changed producers, unresolved sources and revisions stay put.
    All bytes (including unique source evidence) are still archived, not dropped.
    """
    if (old.get('job') not in {'positions_domestic', 'positions_foreign'}
            or old.get('job') != new.get('job')
            or old.get('status') not in {'FAIL', 'PARTIAL_FAILURE', 'CANDIDATE'}
            or old.get('published') is not False or not successful(new)):
        return False
    producer = old.get('producer', {})
    if (not all(isinstance(producer.get(k), str) and producer[k] for k in ('origin', 'commit', 'tree'))
            or producer != new.get('producer')):
        return False
    old_c, new_c = old.get('collection', {}), new.get('collection', {})
    if old_c.get('status') != 'PASS':
        return False
    attempts = []
    for collection in (old_c, new_c):
        rows = collection.get('attempts')
        if not isinstance(rows, list) or not rows:
            return False
        if any(not isinstance(r, dict) or not r.get('domain') or not r.get('source_id')
               or r.get('status') not in {'ok', 'no_change', 'not_published'} for r in rows):
            return False
        identities = [(r['domain'], r['source_id']) for r in rows]
        if len(set(identities)) != len(identities):
            return False
        attempts.append(sorted(json.dumps(r, sort_keys=True) for r in rows))
    if attempts[0] != attempts[1]:
        return False
    added = []
    for collection in (old_c, new_c):
        observations = collection.get('observations', {})
        if observations.get('revised_partitions') != []:
            return False
        keys = observations.get('added_partitions')
        if not isinstance(keys, list) or any(not isinstance(k, str) or not k for k in keys):
            return False
        added.append(set(keys))
    return added[0] <= added[1]


def staged_plan(base, now, keep_days=3, keep_count=2, busy=(), pins=()):
    require(keep_days >= 3 and keep_count >= 2, 'minimum retention is 3 days and 2 successful runs')
    base = safe(base)
    scheduled = base / 'production-data-automation/scheduled'
    groups = defaultdict(list)
    unresolved = []
    for receipt in sorted(scheduled.glob('*/result.json')):
        safe(receipt)
        d = read(receipt)
        if not RUN.fullmatch(receipt.parent.name) or d.get('run_id') != receipt.parent.name:
            continue
        if not successful(d) and d.get('status') not in {'FAIL', 'PARTIAL_FAILURE', 'CANDIDATE'}:
            continue
        when = datetime.fromisoformat(d['checked_at'].replace('Z', '+00:00'))
        require(when.tzinfo is not None, 'receipt timezone absent')
        if when > now:
            continue
        if successful(d):
            groups[d['job']].append((when, receipt, d))
        else:
            unresolved.append((when, receipt, d))
    selected = []
    for entries in groups.values():
        for when, receipt, d in sorted(entries, reverse=True)[keep_count:]:
            if when <= now - timedelta(days=keep_days):
                selected.append((receipt, d, 'expired_success', []))
    for when, receipt, d in unresolved:
        for later, replacement, success in sorted(groups.get(d.get('job'), []), reverse=True):
            if later > when and same_positions_scope(d, success):
                selected.append((receipt, d, 'superseded_positions_scope', [
                    {'path': str(replacement), 'sha256': sha(replacement)}]))
                break
    plan = []
    for receipt, d, reason, evidence in selected:
        run = d['run_id']
        roots = [receipt.parent / p for p in (
            'baseline', 'collection/package', 'collection/bundle', 'collection/collection')]
        roots.append(base / 'production-data-uploads' / run)
        for family in ('production-data-candidates', 'production-data-incoming'):
            roots.extend(sorted((base / family).glob('*/' + run)))
        for root in roots:
            if not root.exists():
                continue
            if any(overlap(root, Path(p)) for p in (*busy, *pins)):
                continue
            rows = inventory(root)
            if rows:
                plan.append({'root': str(root), 'receipt': str(receipt),
                             'receipt_sha256': sha(receipt), 'files': rows,
                             'reason': reason, 'supersession_evidence': evidence})
    return plan


def object_path(state, digest):
    require(re.fullmatch(r'[a-f0-9]{64}', digest), 'object hash invalid')
    return safe(state) / 'objects' / (digest + '.gz')


def verify_object(path, expected, size):
    h = hashlib.sha256()
    count = 0
    with gzip.open(safe(path), 'rb') as stream:
        for data in iter(lambda: stream.read(1024 * 1024), b''):
            h.update(data)
            count += len(data)
            require(count <= size, 'archive size overflow')
    require(count == size and h.hexdigest() == expected, 'archive verification failed')


def verify_receipts(item):
    require(sha(Path(item['receipt'])) == item['receipt_sha256'], 'receipt changed')
    for evidence in item.get('supersession_evidence', []):
        require(sha(Path(evidence['path'])) == evidence['sha256'], 'replacement receipt changed')


def archive(item, state):
    """Prepare and verify every recovery object before removing any file."""
    state = safe(state)
    root = safe(item['root'])
    verify_receipts(item)
    require(inventory(root) == item['files'], 'stale staging plan')
    objects = state / 'objects'
    objects.mkdir(exist_ok=True)
    for row in item['files']:
        target = object_path(state, row['sha256'])
        if not target.exists():
            tmp = objects / (uuid.uuid4().hex + '.tmp')
            with tmp.open('xb') as raw:
                with gzip.GzipFile(filename='', mode='wb', fileobj=raw, mtime=0) as zipped:
                    with (root / row['path']).open('rb') as source:
                        for chunk in iter(lambda: source.read(1024 * 1024), b''):
                            zipped.write(chunk)
                raw.flush()
                os.fsync(raw.fileno())
            verify_object(tmp, row['sha256'], row['bytes'])
            os.chmod(tmp, 0o400)
            os.replace(tmp, target)
            sync_directory(objects)
        verify_object(target, row['sha256'], row['bytes'])
    require(inventory(root) == item['files'], 'staging changed during archive')
    verify_receipts(item)
    indexes = state / 'indexes'
    indexes.mkdir(exist_ok=True)
    index = indexes / (uuid.uuid4().hex + '.json')
    write_new(index, {'schema_version': 'staging-recovery-index/1', **item})
    # No recursive delete: exact verified files only, preserving parent receipts.
    for row in item['files']:
        p = safe(root / row['path'])
        require(sha(p) == row['sha256'], 'file changed before unlink')
        p.unlink()
    for p in sorted(root.rglob('*'), key=lambda p: len(p.parts), reverse=True):
        safe(p).rmdir()
    root.rmdir()
    return str(index)


def restore(index, state, destination):
    """Restore to a new isolated destination; never overwrite a runtime."""
    destination = safe(destination)
    require(not destination.exists(), 'restore destination already exists')
    doc = read(index)
    require(doc['schema_version'] == 'staging-recovery-index/1', 'unknown recovery index')
    names = set()
    for row in doc['files']:
        rel = Path(row['path'])
        require(not rel.is_absolute() and '..' not in rel.parts and rel.parts and row['path'] not in names, 'unsafe recovery path')
        names.add(row['path'])
        verify_object(object_path(state, row['sha256']), row['sha256'], row['bytes'])
    destination.mkdir()
    for row in doc['files']:
        p = destination / row['path']
        p.parent.mkdir(parents=True, exist_ok=True)
        with gzip.open(object_path(state, row['sha256']), 'rb') as source, p.open('xb') as target:
            for chunk in iter(lambda: source.read(1024 * 1024), b''):
                target.write(chunk)
        if hasattr(os, 'chown'):
            os.chown(p, row['uid'], row['gid'])
        os.chmod(p, row['mode'])
        os.utime(p, ns=(row['mtime_ns'], row['mtime_ns']))
        require(sha(p) == row['sha256'], 'restored bytes differ')


def images_to_retire(entries, protected, existing):
    selected = []
    for entry in entries:
        image = entry['image_id']
        require(IMAGE.fullmatch(image), 'full image ID required')
        require(entry.get('status') == 'ACCEPTED_SUPERSEDED', 'image retirement not accepted')
        require(entry.get('reason') and entry.get('evidence'), 'retirement evidence required')
        for evidence in entry['evidence']:
            require(sha(Path(evidence['path'])) == evidence['sha256'], 'retirement evidence changed')
        if image in existing and image not in protected:
            selected.append(entry)
    return selected


def docker_state():
    ids = command('docker', 'ps', '-aq').splitlines()
    images, mounts = set(), []
    for cid in ids:
        d = json.loads(command('docker', 'inspect', '--format',
                               '{"image":{{json .Image}},"mounts":{{json .Mounts}}}', cid))
        images.add(d['image'])
        mounts.extend(str(Path(m['Source']).resolve()) for m in d['mounts'] if m['Type'] == 'bind')
    return images, mounts


def config_image_pins(config_root=Path('/etc/market-data')):
    pins = set()
    # Never read secret/credential files; only known host configuration names.
    paths = list((config_root / 'scheduled-server').glob('*.json'))
    paths += list((config_root / 'foreign-fx-server').glob('*.json'))
    paths += list((config_root / 'production-data-delivery').glob('*.json'))
    paths += [config_root / 'warehouse-server/config.json']
    for path in paths:
        if any(w in path.name.lower() for w in ('secret', 'credential')) or not path.exists():
            continue
        config = read(path)
        image = config.get('image_id') or config.get('validation_image', {}).get('image_id')
        require(image is None or IMAGE.fullmatch(image), 'configured image invalid')
        if image:
            pins.add(image)
    return pins


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--config', type=Path, required=True)
    parser.add_argument('--config-sha256', required=True)
    parser.add_argument('--apply', action='store_true')
    parser.add_argument('--restore-index', type=Path)
    parser.add_argument('--restore-to', type=Path)
    args = parser.parse_args()
    require(os.geteuid() == 0, 'root host maintenance required')
    config_stat = safe(args.config).stat()
    require(config_stat.st_uid == 0 and config_stat.st_mode & 0o022 == 0, 'configuration must be root protected')
    require(sha(args.config) == args.config_sha256, 'configuration identity differs')
    c = read(args.config)
    require(c['schema_version'] == 'runtime-retention/1', 'unknown configuration')
    require(command('findmnt', '-n', '-o', 'UUID', '/srv/market-data-disk') == c['data_disk_uuid'], 'data disk identity differs')
    require(c['data_disk_uuid'] == '109b359d-6e55-4911-a665-c67735dc0a10', 'unexpected data disk')
    require(BASE.stat().st_dev == Path('/srv/market-data-disk').stat().st_dev == Path('/var/lib/docker').stat().st_dev, 'data paths on wrong device')
    safe(STATE).mkdir(parents=True, exist_ok=True)
    require(STATE.stat().st_uid == 0 and STATE.stat().st_mode & 0o022 == 0, 'state must be root protected')
    spec = importlib.util.spec_from_file_location('package_locks', Path(__file__).with_name('public_package_retention.py'))
    helper = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(helper)
    report = {'started_at': datetime.now(timezone.utc).isoformat(), 'status': 'FAIL',
              'apply': args.apply, 'history_versions_deleted': 0, 'images_deleted': [], 'archived': []}
    before = os.statvfs(BASE)
    try:
        with ExitStack() as stack:
            stack.enter_context(helper.lock(STATE / 'maintenance.lock'))
            for path in c['publisher_locks']:
                stack.enter_context(helper.lock(Path(path)))
            if args.restore_index:
                require(args.restore_to is not None and not args.apply, 'isolated restore arguments required')
                require(args.restore_index.parent == STATE / 'indexes', 'restore index outside state')
                require(args.restore_to.parent == STATE / 'restore-tests', 'restore must use isolated state directory')
                args.restore_to.parent.mkdir(exist_ok=True)
                restore(args.restore_index, STATE, args.restore_to)
                report['restored_to'] = str(args.restore_to)
            else:
                protected, busy = docker_state()
                protected |= config_image_pins() | set(c['protected_images'])
                existing = set(command('docker', 'images', '-aq', '--no-trunc').splitlines())
                retired = images_to_retire(c['retired_images'], protected, existing)
                staged = staged_plan(BASE, datetime.now(timezone.utc), c['keep_days'], c['keep_success_count'], busy, c['protected_paths'])
                report['images_planned'] = retired
                report['staging_planned'] = staged
                if args.apply:
                    for item in staged:
                        _, current_busy = docker_state()
                        require(not any(overlap(Path(item['root']), Path(p)) for p in current_busy), 'new mount reference')
                        report['archived'].append(archive(item, STATE))
                    for entry in retired:
                        live, _ = docker_state()
                        require(entry['image_id'] not in live | config_image_pins() | set(c['protected_images']), 'new image reference')
                        images_to_retire([entry], set(), existing)
                        command('docker', 'image', 'rm', entry['image_id'])
                        report['images_deleted'].append(entry['image_id'])
            report['status'] = 'PASS'
    except Exception as exc:
        report['error_type'] = type(exc).__name__
        report['error'] = str(exc)[:300]
        raise
    finally:
        after = os.statvfs(BASE)
        report['recovered_bytes'] = (after.f_bavail - before.f_bavail) * after.f_frsize
        report['available_bytes'] = after.f_bavail * after.f_frsize
        report['finished_at'] = datetime.now(timezone.utc).isoformat()
        receipt = STATE / (datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ-') + uuid.uuid4().hex + '.json')
        write_new(receipt, report)
        print(json.dumps({'receipt': str(receipt), 'status': report['status'], 'recovered_bytes': report['recovered_bytes']}))


if __name__ == '__main__':
    main()
