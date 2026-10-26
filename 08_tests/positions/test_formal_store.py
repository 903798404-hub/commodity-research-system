"""Formal append storage, real readers, rollback and immutable source evidence."""
from copy import deepcopy
import json
from pathlib import Path

import pytest

from agri_research_agent.positions import delivery as codec, formal_store as store
from agri_research_agent.positions.bundle import verified_files
from agri_research_agent.sugar_positions.storage import read_snapshot
from test_positions_delivery import archive, ROOT
from test_positions_incremental import increment
from test_server_collection import capture, parse


def baseline_at(tmp_path):
    formal = tmp_path / 'processed/commodity_positions'
    baseline = archive(day='2026-10-08')
    codec.materialize(baseline, formal)
    (formal/'positions_archive.json').write_bytes(codec.canonical(baseline))
    activate(formal, None)
    return formal, baseline


def activate(formal, selected):
    path = store.status_path(formal)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(codec.canonical({'position_store': selected} if selected else {'status':'updated'}))


def files_at(root):
    return {p.relative_to(root).as_posix(): (p.read_bytes(), p.stat().st_ino)
            for p in root.rglob('*') if p.is_file()}


def next_increment(baseline):
    value = capture()
    value['trade_date'] = '20261012'
    for row in value['pages'][0]['items']:
        row[0] = '20261012'
    rows = parse(value)
    builder = codec.IncrementBuilder(baseline, ROOT)
    for domain, varieties in (('soybean', {'M', 'Y'}), ('palm', {'P'})):
        fresh = [r for r in rows if r['scope'].rstrip('0123456789') in varieties]
        builder.publish(Path(domain), [], fresh,
            [('stockapi_DCE_20261012', codec.canonical(value),
              'https://t.xiaodefa.top/', 'json')], [])
    document, _ = builder.finish(ROOT)
    return document, codec.resolve_increment(document, baseline)


def test_append_reads_all_boards_preserves_legacy_and_rolls_back_without_copy(tmp_path):
    formal, baseline = baseline_at(tmp_path)
    legacy = files_at(formal)
    document, resolved = increment(baseline)
    selected, metrics = store.prepare(formal, baseline, document, resolved)
    assert metrics['bootstrap_object_bytes'] > 0
    assert not metrics['full_archive_written'] and metrics['full_history_copies'] == 0
    assert codec.read_archive(formal/'positions_archive.json') == baseline  # Not activated.
    activate(formal, selected)
    assert codec.read_archive(formal/'positions_archive.json') == resolved
    codec.verify_materialized(resolved, formal)
    snapshots = codec.archive_snapshots(resolved)[0]
    for domain in codec.DOMAINS:
        assert read_snapshot(formal/domain) == snapshots[domain]
        snapshot, exported = verified_files(formal/domain, ROOT, domain)
        assert snapshot == snapshots[domain]
        assert exported == {n.split('/',1)[1]:r for n,r in codec.decode(resolved).items()
                            if n.startswith(domain+'/')}
    for name, original in legacy.items():
        assert files_at(formal)[name] == original
    retained = files_at(formal)
    activate(formal, None)
    assert codec.read_archive(formal/'positions_archive.json') == baseline
    assert files_at(formal) == retained
    for domain in codec.DOMAINS:
        assert read_snapshot(formal/domain) == codec.archive_snapshots(baseline)[0][domain]


def test_next_publish_only_appends_segments_and_sources_repeat_adds_zero_objects(tmp_path):
    formal, baseline = baseline_at(tmp_path)
    document, first = increment(baseline)
    selected, _ = store.prepare(formal, baseline, document, first)
    activate(formal, selected)
    before = files_at(formal)
    document2, second = next_increment(first)
    selected2, metrics = store.prepare(formal, first, document2, second)
    assert metrics['bootstrap_object_bytes'] == 0
    assert 0 < metrics['new_object_bytes'] < len(codec.canonical(second)) // 2
    for name, original in before.items():
        assert files_at(formal)[name] == original
    for domain in ('soybean','palm'):
        release = document2['updates'][domain]['release_id']
        item = store.index(formal, selected2)['files'][f'{domain}/releases/{release}/snapshot.json']
        assert item['kind'] == 'snapshot' and len(item['updates']) == 2
        assert not (formal/store.DIRECTORY/'objects'/item['sha256']).exists()
    same, repeat = store.prepare(formal, first, document2, second)
    assert same == selected2 and repeat['new_object_bytes'] == repeat['index_bytes_written'] == 0
    activate(formal, selected2)
    assert codec.read_archive(formal/'positions_archive.json') == second
    codec.validate_archive(second, ROOT)  # Offline source replay, not only SHA.
    activate(formal, selected)
    assert codec.read_archive(formal/'positions_archive.json') == first


@pytest.mark.parametrize('change', ['object', 'index', 'pointer_path', 'pointer_size', 'extra', 'overlap'])
def test_store_fails_closed(tmp_path, change):
    formal, baseline = baseline_at(tmp_path)
    document, resolved = increment(baseline)
    selected, _ = store.prepare(formal, baseline, document, resolved)
    if change == 'overlap':
        invalid = deepcopy(document)
        invalid['updates']['palm']['domestic'] = codec.archive_snapshots(baseline)[0]['palm']['domestic']
        with pytest.raises(ValueError):
            store.prepare(formal, baseline, invalid, resolved)
        return
    if change == 'pointer_path': selected['index_sha256'] = '../outside'
    if change == 'pointer_size': selected['index_size_bytes'] = True
    if change == 'extra': selected['delete'] = True
    if change in {'object','index'}:
        name = ('objects' if change == 'object' else 'indexes')
        path = ((formal/store.DIRECTORY/name/(selected['index_sha256']+'.json')) if change == 'index'
                else next((formal/store.DIRECTORY/name).iterdir()))
        path.chmod(0o600)
        path.write_bytes(b'corrupt')
    activate(formal, selected)
    with pytest.raises((ValueError, KeyError)):
        codec.read_archive(formal/'positions_archive.json')


def test_incomplete_pointer_does_not_fall_back_to_stale_legacy(tmp_path):
    formal, baseline = baseline_at(tmp_path)
    selected, _ = store.prepare(formal, baseline, *increment(baseline))
    selected['index_sha256'] = '0'*64
    activate(formal, selected)
    with pytest.raises(ValueError): read_snapshot(formal/'palm')


@pytest.mark.parametrize('change', [None, 'view', 'status', 'extra'])
def test_manual_baseline_keeps_cas_pins_and_uses_active_view(tmp_path, change):
    from agri_research_agent.automation import production_data_delta_positions as producer
    formal, baseline = baseline_at(tmp_path)
    document, resolved = increment(baseline)
    selected, _ = store.prepare(formal, baseline, document, resolved)
    activate(formal, selected)
    host = producer.delivery._host_contract()
    root = tmp_path/'manual'
    files = {}
    for name, path in ((host.POSITIONS_STABLE,formal/'positions_archive.json'),
                       (host.POSITIONS_STATUS,store.status_path(formal))):
        target = root/name
        target.parent.mkdir(parents=True,exist_ok=True)
        target.write_bytes(path.read_bytes())
        files[name] = producer.delivery._identity(target)
    raw = codec.canonical(resolved)
    (root/'resolved_positions.json').write_bytes(raw)
    manifest = {'schema_version':producer.BASELINE_SCHEMA,'source_root':'/allocation',
                'files':files,'resolved_positions':store.identity(raw)}
    if change == 'view':
        raw = codec.canonical(baseline)
        (root/'resolved_positions.json').write_bytes(raw)
        manifest['resolved_positions'] = store.identity(raw)
    if change == 'status':
        status = codec.read_json(root/host.POSITIONS_STATUS)
        status['position_store']['archive_sha256'] = '0'*64
        (root/host.POSITIONS_STATUS).write_bytes(codec.canonical(status))
        manifest['files'][host.POSITIONS_STATUS] = producer.delivery._identity(root/host.POSITIONS_STATUS)
    if change == 'extra': (root/'unknown.json').write_bytes(b'{}')
    (root/'baseline_manifest.json').write_bytes(codec.canonical(manifest))
    config = {'baseline_root':str(root),'remote_allocation':'/allocation',
              'baseline_manifest_sha256':producer.delivery.sha256_file(root/'baseline_manifest.json')}
    if change:
        with pytest.raises(ValueError): producer.load_baseline(config)
    else:
        assert producer.load_baseline(config) == resolved
        assert codec.read_json(root/host.POSITIONS_STABLE) == baseline


def test_protected_snapshot_opt_in_exports_logical_view_without_changing_physical_pins(tmp_path,monkeypatch):
    import base64
    from contextlib import nullcontext
    from agri_research_agent.automation import production_data_delta_positions as producer
    formal,baseline=baseline_at(tmp_path/'01_data')
    selected,_=store.prepare(formal,baseline,*increment(baseline))
    activate(formal,selected)
    host=producer.delivery._host_contract()
    policy={'domain':'commodity_positions','allocation_root':str(tmp_path),'approved_producer':{},
            'validation_image':{'image_id':'pinned'}}
    monkeypatch.setattr(host,'_load_policy',lambda p:(policy,'a'*64))
    monkeypatch.setattr(host,'_protected',lambda p,**k:Path(p))
    monkeypatch.setattr(host,'_under',lambda p,*a,**k:Path(p))
    monkeypatch.setattr(host,'_lock',lambda p:nullcontext())
    monkeypatch.setattr(host,'_policy_unchanged',lambda *a:None)
    literal=host.snapshot_fx_baseline('/policy')
    expanded=host.snapshot_fx_baseline('/policy',resolved_positions=True)
    assert expanded['schema_version']=='production-domain-baseline-snapshot/2'
    assert expanded['files']==literal['files'] and expanded['contents']==literal['contents']
    raw=base64.b64decode(expanded['resolved_positions']['bytes_base64'],validate=True)
    assert codec.digest(raw)==selected['archive_sha256']
    assert codec.strict_json(raw)==codec.read_archive(formal/'positions_archive.json')
