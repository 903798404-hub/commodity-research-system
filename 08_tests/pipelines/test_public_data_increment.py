from copy import deepcopy
import importlib.util
import json
import os
from pathlib import Path
import shutil
from types import SimpleNamespace

import pyarrow as pa
import pyarrow.parquet as pq
import pytest

from agri_research_agent.pipelines import public_data_delivery as delivery
from agri_research_agent.pipelines import public_data_increment as delta
from test_public_data_partitions import build

ROOT = Path(__file__).resolve().parents[2]


def added(tmp_path):
    _, source, base = build(tmp_path)
    rows = pq.read_table(source).to_pylist()
    new = dict(rows[-1]); new['date'] = new['date'].replace(day=2); new['updated_at'] = 'new'
    rows.append(new)
    pq.write_table(pa.Table.from_pylist(rows), source)
    before = {p: p.read_bytes() for p in base.directory.rglob('*') if p.is_file()}
    increment = delta.build_increment(base.directory, source, tmp_path / 'increments')
    return base, increment, before


def test_changed_month_only_and_no_full_public_package(tmp_path):
    base, increment, before = added(tmp_path)
    proof = delta.validate_increment(increment.directory, base.directory)
    assert proof['payload_files'] == 1 and proof['reused_files'] >= 2
    assert not proof['full_bundle_written'] and proof['payload_bytes'] < proof['logical_bytes']
    actual = [p for p in (increment.directory / 'data').rglob('*.parquet') if p.is_file()]
    assert len(actual) == 1 and actual[0].name.startswith('part-2026-09-')
    assert not (increment.directory / 'data/public-market-data').exists()
    assert not (tmp_path / 'increments/.monthly-objects').exists()
    assert all(p.read_bytes() == raw for p, raw in before.items())


def test_no_change_allocates_no_directory(tmp_path):
    _, source, base = build(tmp_path)
    assert delta.build_increment(base.directory, source, tmp_path / 'increments') is None
    assert not (tmp_path / 'increments').exists()


def test_revision_is_rejected_and_original_evidence_preserved(tmp_path):
    _, source, base = build(tmp_path)
    old = (base.directory / 'manifest.json').read_bytes()
    rows = pq.read_table(source).to_pylist()
    rows[-1].update(leg2_price=2980.0, spread_value=20.0)
    pq.write_table(pa.Table.from_pylist(rows), source)
    with pytest.raises(delivery.DeliveryError, match='historical rows'):
        delta.build_increment(base.directory, source, tmp_path / 'increments')
    assert (base.directory / 'manifest.json').read_bytes() == old


@pytest.mark.parametrize('tamper', ['payload', 'extra', 'base', 'manifest', 'escape', 'missing', 'duplicate'])
def test_tampered_sparse_delivery_fails_closed(tmp_path, tamper):
    base, increment, before = added(tmp_path)
    root = increment.directory
    if tamper == 'payload':
        next(p for p in (root / 'data').rglob('*.parquet') if p.is_file()).write_bytes(b'bad')
    elif tamper == 'extra':
        (root / 'data/undeclared.json').write_text('{}')
    elif tamper == 'base':
        (base.directory / 'manifest.json').write_text('{}')
    elif tamper == 'missing':
        next(p for p in (root / 'data').rglob('*.parquet') if p.is_file()).unlink()
    else:
        manifest = deepcopy(increment.manifest)
        if tamper == 'manifest': manifest['source_max_dates'] = {'unexpected': '2099-01-01'}
        elif tamper == 'escape': manifest['files'][0]['path'] = '../outside'
        else: manifest['files'].append(manifest['files'][0])
        (root / 'manifest.json').write_bytes(delta._json(manifest))
        proof = json.loads((root / 'increment.json').read_bytes())
        proof['manifest_sha256'] = delivery._sha256_file(root / 'manifest.json')
        (root / 'increment.json').write_bytes(delta._json(proof))
    with pytest.raises((delivery.DeliveryError, ValueError)):
        delta.validate_increment(root, base.directory)


def test_legacy_single_file_baseline_can_seed_increment_without_rewriting_it(tmp_path):
    _, source, base = build(tmp_path, partitioned=False)
    original = (base.directory / 'manifest.json').read_bytes()
    rows = pq.read_table(source).to_pylist(); new = dict(rows[-1]); new['date'] = new['date'].replace(day=2)
    pq.write_table(pa.Table.from_pylist(rows + [new]), source)
    candidate = delta.build_increment(base.directory, source, tmp_path / 'increments')
    assert delta.validate_increment(candidate.directory, base.directory)['payload_files'] == 2
    assert (base.directory / 'manifest.json').read_bytes() == original


@pytest.mark.parametrize('publish', [False, True])
def test_projection_is_independent_for_candidate_and_shared_immutable_for_production(tmp_path, monkeypatch, publish):
    base, increment, before = added(tmp_path)
    spec = importlib.util.spec_from_file_location('test_sparse_publisher', ROOT / '09_deploy/scheduled_server/public_increment.py')
    publisher = importlib.util.module_from_spec(spec); spec.loader.exec_module(publisher)
    allocation = tmp_path / 'allocation'
    store = allocation / '01_data/public-data-server-store'
    delivery.sync_to_local_server_store(base.directory, store_root=store)
    formal = store / 'releases' / base.package_id
    pool = tmp_path / 'pool'; pool.mkdir(mode=0o700)
    # Windows permission bits are not POSIX; exercise identical object bytes and
    # inode ownership logic with only the OS permission/ACL boundary adapted.
    real_stat = Path.stat
    def stat(path, *args, **kwargs):
        value = real_stat(path, *args, **kwargs)
        if path == pool:
            return SimpleNamespace(st_uid=0, st_mode=0o40700, st_dev=value.st_dev)
        return value
    monkeypatch.setattr(Path, 'stat', stat)
    monkeypatch.setattr(os, 'chown', lambda *a: None, raising=False)
    monkeypatch.setattr(publisher.subprocess, 'run', lambda *a, **k: SimpleNamespace(returncode=0))
    def require(value, message):
        if not value: raise ValueError(message)
    def file_set(root):
        return {p.relative_to(root).as_posix(): {'sha256':delivery._sha256_file(p),'size_bytes':p.stat().st_size}
                for p in root.rglob('*') if p.is_file()}
    def write(path, raw):
        with path.open('xb') as out: out.write(raw)
    host = SimpleNamespace(require=require, _protected=lambda p:Path(p), _no_links=lambda p:p,
        read_json=lambda p:json.loads(p.read_bytes()), sha256_file=delivery._sha256_file, _file_set=file_set,
        _write_exclusive=write, _write_json_exclusive=lambda p,v:write(p,delta._json(v)))
    extra = {'public_pointer':host.read_json(store/'current.json'),'public_manifest':base.manifest,
             'public_manifest_sha256':delivery._sha256_file(formal/'manifest.json')}
    config = {'allocation_root':str(allocation),'public_object_pool':str(pool),
              'public_store_owner':{'uid':1000,'gid':1000},'image_id':'test'}
    def invoke(command): return delta.validate_increment(increment.directory, formal)
    work = tmp_path / 'work'; work.mkdir()
    upload = store / 'incoming' / 'projection'
    report = publisher.prepare_upload(host,config,work,extra,increment.directory,upload,{'uid':65532},publish,invoke)
    assert report['payload_files'] == 1
    validated = delivery.validate_production_package(upload, require_directory_name=False)
    assert validated.package_id == increment.package_id
    for item in increment.manifest['files']:
        target = upload / 'data' / item['path']
        if publish:
            assert os.path.samefile(target,pool/item['sha256'])
            assert not target.stat().st_mode & 0o222
        else:
            if item in base.manifest['files']:
                assert not os.path.samefile(target,formal/'data'/item['path'])
    # Existing activation and rollback consume the projection with no schema or
    # reader changes. Deleting a version would unlink only its directory entries.
    assert delivery.activate_incoming_server_package(upload, store_root=store).status == 'SYNCED'
    restored=delivery.rollback_server_current_after_application_failure(store_root=store,
        expected_failed_package_id=increment.package_id,rollback_package_id=base.package_id)
    assert restored.status == 'ROLLED_BACK'
    assert all(p.read_bytes()==raw for p,raw in before.items())
    if publish:
        retained={p:(p.read_bytes(),p.stat().st_mode) for p in pool.iterdir()}
        second_work=tmp_path/'repeat'; second_work.mkdir()
        second=publisher.prepare_upload(host,config,second_work,extra,increment.directory,
            store/'incoming/second',{'uid':65532},True,invoke)
        assert second['new_object_bytes']==0 and second['reused_object_bytes']==proof_bytes(increment)
        assert all((p.read_bytes(),p.stat().st_mode)==value for p,value in retained.items())


def proof_bytes(package):
    return sum(item['size_bytes'] for item in package.manifest['files'])
