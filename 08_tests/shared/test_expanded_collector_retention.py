"""Shared collector retention must preserve every archived source and consumer."""
from datetime import datetime, timezone, timedelta
import importlib.util
import json
import os
from pathlib import Path
import pytest

ROOT = Path(__file__).resolve().parents[2]
spec = importlib.util.spec_from_file_location('expanded_collector_retention', ROOT / '04_scripts/maintenance/runtime_retention.py')
m = importlib.util.module_from_spec(spec)
spec.loader.exec_module(m)
NOW = datetime(2026, 10, 15, tzinfo=timezone.utc)


def job(base, number, *, status='PUBLISHED', days=10, job_name='canola_exports'):
    run = f'{job_name.replace("_", "-")}-server-20261001t100000-{number:012x}'
    work = base / 'production-data-automation/scheduled' / run
    work.mkdir(parents=True)
    result = {'run_id': run, 'job': job_name, 'status': status,
              'published': status in {'PUBLISHED', 'SYNCED'}, 'collection': {'status': 'PASS'},
              'checked_at': (NOW - timedelta(days=days)).isoformat()}
    (work / 'result.json').write_text(json.dumps(result))
    package = work / 'collection/package'
    package.mkdir(parents=True)
    (package / 'history.json').write_text('{"historical":"原始证据"}', encoding='utf-8')
    return work, package


@pytest.mark.parametrize('job_name,relative', [
    ('canola_exports', 'collection/01_data'),
    ('soybean_crop_progress', 'collection/project'),
    ('soybean_export_sales', 'collection/project'),
])
@pytest.mark.parametrize('status', ['PUBLISHED', 'NO_CHANGE'])
def test_expanded_collector_sources_archive_restore_without_touching_formal_history(
    tmp_path, job_name, relative, status,
):
    base = tmp_path / 'base'
    work, _ = job(base, 1, status=status, job_name=job_name)
    job(base, 2, days=2, job_name=job_name)
    job(base, 3, days=1, job_name=job_name)
    expanded = work / relative
    raw = expanded / 'raw/provider/2025-2026/source.csv'
    raw.parent.mkdir(parents=True)
    original = 'date,value\n2026-10-04,真实来源\n'.encode('utf-8')
    raw.write_bytes(original)
    formal = base / 'production-runtime/active/01_data/raw/provider/history.csv'
    formal.parent.mkdir(parents=True)
    formal.write_bytes(original)
    item = next(p for p in m.staged_plan(base, NOW) if p['root'] == str(expanded))
    assert item['reason'] == 'expired_success'
    state = tmp_path / 'state'
    state.mkdir()
    index = Path(m.archive(item, state))
    assert not expanded.exists() and (work / 'result.json').exists()
    recovered = tmp_path / 'restored'
    m.restore(index, state, recovered)
    assert (recovered / raw.relative_to(expanded)).read_bytes() == original
    assert formal.read_bytes() == original


@pytest.mark.parametrize('protection', ['young', 'latest', 'mounted', 'pinned', 'failed', 'unknown', 'wrong_prefix'])
def test_expanded_source_scope_retains_protected_and_unrecognized_work(tmp_path, protection):
    name = 'canola_exports' if protection != 'unknown' else 'unknown_collector'
    work, _ = job(tmp_path, 1, days=2.5 if protection == 'young' else 10,
                  status='FAIL' if protection == 'failed' else 'NO_CHANGE', job_name=name)
    if protection != 'latest':
        job(tmp_path, 2, days=2, job_name=name)
        job(tmp_path, 3, days=1, job_name=name)
    expanded = work / 'collection/01_data'
    expanded.mkdir(parents=True)
    raw = expanded / 'source.csv'
    raw.write_bytes(b'original source')
    if protection == 'wrong_prefix':
        receipt = work / 'result.json'
        value = json.loads(receipt.read_text())
        value['job'] = 'soybean_export_sales'
        receipt.write_text(json.dumps(value))
        job(tmp_path, 4, days=2, job_name='soybean_export_sales')
        job(tmp_path, 5, days=1, job_name='soybean_export_sales')
        # A known job label does not authorize another collector's directory.
        expanded.rename(work / 'collection/project')
        expanded = work / 'collection/project'
    plan = m.staged_plan(tmp_path, NOW, busy=[raw] if protection == 'mounted' else (),
                         pins=[raw] if protection == 'pinned' else ())
    assert str(expanded) not in {p['root'] for p in plan}
    assert expanded.exists()


def test_expanded_source_hardlink_cannot_archive_external_history(tmp_path):
    work, _ = job(tmp_path, 1, job_name='canola_exports')
    job(tmp_path, 2, days=2, job_name='canola_exports')
    job(tmp_path, 3, days=1, job_name='canola_exports')
    expanded = work / 'collection/01_data'
    expanded.mkdir(parents=True)
    external = tmp_path / 'formal-source.csv'
    external.write_bytes(b'protected formal evidence')
    # Directory junction/symlink privileges vary on Windows; an external hard
    # link exercises the same fail-closed inventory gate on both platforms.
    os.link(external, expanded / 'source.csv')
    with pytest.raises(ValueError, match='shared file'):
        m.staged_plan(tmp_path, NOW)
    assert external.read_bytes() == b'protected formal evidence'
