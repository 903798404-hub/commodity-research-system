"""Retention must preserve evidence and reject uncertain deletion candidates."""
from datetime import datetime, timezone, timedelta
import importlib.util
import json
import os
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location('runtime_retention', ROOT / '04_scripts/maintenance/runtime_retention.py')
m = importlib.util.module_from_spec(spec)
spec.loader.exec_module(m)
NOW = datetime(2026, 10, 15, tzinfo=timezone.utc)


def job(base, number, *, status='PUBLISHED', days=10):
    run = f'positions-domestic-server-20261001t100000-{number:012x}'
    work = base / 'production-data-automation/scheduled' / run
    work.mkdir(parents=True)
    result = {'run_id': run, 'job': 'positions_domestic', 'status': status,
              'published': status in {'PUBLISHED', 'SYNCED'}, 'collection': {'status': 'PASS'},
              'checked_at': (NOW - timedelta(days=days)).isoformat()}
    (work / 'result.json').write_text(json.dumps(result))
    package = work / 'collection/package'
    package.mkdir(parents=True)
    (package / 'history.json').write_text('{"historical":"原始证据"}', encoding='utf-8')
    return work, package


def eligible(tmp_path):
    work, package = job(tmp_path, 1)
    job(tmp_path, 2, days=2)
    job(tmp_path, 3, days=1)
    return work, package


def test_age_and_latest_success_retention(tmp_path):
    work, package = eligible(tmp_path)
    plan = m.staged_plan(tmp_path, NOW)
    assert [p['root'] for p in plan] == [str(package)]
    assert plan[0]['receipt'] == str(work / 'result.json')


@pytest.mark.parametrize('status', ['FAIL', 'PARTIAL_FAILURE', 'CANDIDATE', 'WAITING', 'UNKNOWN'])
def test_failure_and_unfinished_evidence_is_retained(tmp_path, status):
    work, package = job(tmp_path, 1, status=status)
    job(tmp_path, 2, days=2)
    job(tmp_path, 3, days=1)
    assert m.staged_plan(tmp_path, NOW) == []
    assert package.exists()


@pytest.mark.parametrize('status', ['PUBLISHED', 'SYNCED', 'NO_CHANGE'])
def test_terminal_success_states(tmp_path, status):
    job(tmp_path, 1, status=status)
    job(tmp_path, 2, days=2)
    job(tmp_path, 3, days=1)
    assert len(m.staged_plan(tmp_path, NOW)) == 1


def test_mounted_and_pinned_paths_retained(tmp_path):
    work, package = eligible(tmp_path)
    assert not m.staged_plan(tmp_path, NOW, busy=[work])
    assert not m.staged_plan(tmp_path, NOW, pins=[package / 'history.json'])


def test_rollback_business_and_unknown_roots_never_selected(tmp_path):
    eligible(tmp_path)
    for family in ['production-runtime', 'production-data-backups', 'runtime-validation']:
        p = tmp_path / family / 'history.json'
        p.parent.mkdir()
        p.write_text('retain')
    assert len(m.staged_plan(tmp_path, NOW)) == 1


def test_archive_restore_roundtrip_and_shared_objects(tmp_path):
    base = tmp_path / 'base'
    work, package = eligible(base)
    original = (package / 'history.json').read_bytes()
    state = tmp_path / 'state'
    state.mkdir()
    plan = m.staged_plan(base, NOW)
    index = Path(m.archive(plan[0], state))
    assert not package.exists()
    assert (work / 'result.json').exists()
    recovered = tmp_path / 'recovered'
    m.restore(index, state, recovered)
    assert (recovered / 'history.json').read_bytes() == original
    package.mkdir()
    (package / 'history.json').write_bytes(original)
    m.archive(m.staged_plan(base, NOW)[0], state)
    assert len(list((state / 'objects').iterdir())) == 1


@pytest.mark.parametrize('change', ['source', 'receipt', 'extra'])
def test_stale_plan_does_not_delete(tmp_path, change):
    work, package = eligible(tmp_path / 'base')
    item = m.staged_plan(tmp_path / 'base', NOW)[0]
    if change == 'source':
        (package / 'history.json').write_text('changed')
    elif change == 'receipt':
        (work / 'result.json').write_text('{}')
    else:
        (package / 'new.json').write_text('new')
    state = tmp_path / 'state'
    state.mkdir()
    with pytest.raises(ValueError):
        m.archive(item, state)
    assert (package / 'history.json').exists()


def test_corrupt_archive_cannot_authorize_deletion(tmp_path):
    work, package = eligible(tmp_path / 'base')
    item = m.staged_plan(tmp_path / 'base', NOW)[0]
    state = tmp_path / 'state'
    (state / 'objects').mkdir(parents=True)
    m.object_path(state, item['files'][0]['sha256']).write_bytes(b'corrupt')
    with pytest.raises((ValueError, OSError)):
        m.archive(item, state)
    assert (package / 'history.json').exists()


def test_secret_file_rejected(tmp_path):
    _, package = eligible(tmp_path)
    (package / '.env').write_text('private')
    with pytest.raises(ValueError, match='sensitive'):
        m.staged_plan(tmp_path, NOW)


def test_extra_hardlink_refuses_cleanup(tmp_path):
    _, package = eligible(tmp_path)
    os.link(package / 'history.json', tmp_path / 'other-consumer.json')
    with pytest.raises(ValueError, match='shared file'):
        m.staged_plan(tmp_path, NOW)


def test_restore_rejects_traversal_before_writing(tmp_path):
    index = tmp_path / 'index.json'
    index.write_text(json.dumps({'schema_version': 'staging-recovery-index/1',
                                'files': [{'path': '../escape', 'sha256': 'a'*64, 'bytes': 1}]}))
    target = tmp_path / 'restored'
    with pytest.raises(ValueError, match='unsafe recovery path'):
        m.restore(index, tmp_path, target)
    assert not target.exists()


def test_images_require_fresh_evidence_and_no_references(tmp_path):
    evidence = tmp_path / 'accepted.json'
    evidence.write_text('{"status":"PASS"}')
    image = 'sha256:' + 'a' * 64
    row = {'image_id': image, 'status': 'ACCEPTED_SUPERSEDED', 'reason': 'superseded by accepted unified release',
           'evidence': [{'path': str(evidence), 'sha256': m.sha(evidence)}]}
    assert m.images_to_retire([row], {image}, {image}) == []
    assert m.images_to_retire([row], set(), {image}) == [row]
    assert m.images_to_retire([row], set(), set()) == []
    evidence.write_text('changed')
    with pytest.raises(ValueError, match='evidence changed'):
        m.images_to_retire([row], set(), {image})


def test_collectors_and_publishers_pin_images_without_reading_credentials(tmp_path):
    image = 'sha256:' + 'a' * 64
    other = 'sha256:' + 'b' * 64
    scheduled = tmp_path / 'scheduled-server'
    scheduled.mkdir()
    (scheduled / 'job.json').write_text(json.dumps({'image_id': image}))
    policies = tmp_path / 'production-data-delivery'
    policies.mkdir()
    (policies / 'domain.json').write_text(json.dumps({'validation_image': {'image_id': other}}))
    warehouse = tmp_path / 'warehouse-server'
    warehouse.mkdir()
    (warehouse / 'credential.json').write_text('not JSON and must never be read')
    (scheduled / 'secret.json').write_text('must never be read')
    assert m.config_image_pins(tmp_path) == {image, other}


def test_release_identity_is_after_dependency_and_application_layers():
    dockerfile = (ROOT / '09_deploy/spread_runtime/Dockerfile.spread-runtime').read_text(encoding='utf-8')
    metadata = dockerfile.index('ARG MARKET_DATA_GIT_HEAD')
    assert metadata > dockerfile.rindex('COPY ')
    identity_run = dockerfile.index("RUN python -c 'import json,re,sys;")
    assert metadata > dockerfile.index('RUN python -B /app/04_scripts/runtime/precompress_streamlit_assets.py')
    assert metadata < identity_run < dockerfile.index('USER 65532:65532')
    for name in ('GIT_HEAD', 'GIT_TREE', 'RELEASE_ID', 'BUILD_TIME', 'SOURCE'):
        assert dockerfile.index('ARG MARKET_DATA_' + name) < identity_run
    assert dockerfile.count('USER ') == 1
    assert 'market-data.git.tree="${MARKET_DATA_GIT_TREE}"' in dockerfile
    assert 'ENV MARKET_DATA_GIT_HEAD="${MARKET_DATA_GIT_HEAD}"' in dockerfile
    assert 'ENV MARKET_DATA_GIT_TREE="${MARKET_DATA_GIT_TREE}"' in dockerfile


def test_restore_refuses_existing_destination(tmp_path):
    with pytest.raises(ValueError, match='already exists'):
        m.restore(tmp_path / 'index.json', tmp_path, tmp_path)


@pytest.mark.parametrize('days,count', [(0, 2), (3, 0), (2, 1)])
def test_minimum_retention_cannot_be_disabled(tmp_path, days, count):
    with pytest.raises(ValueError, match='minimum retention'):
        m.staged_plan(tmp_path, NOW, days, count)
