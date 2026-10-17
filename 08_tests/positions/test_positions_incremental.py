"""Real append transport, source replay, byte preservation and zero-full-copy no-op."""
from copy import deepcopy
from datetime import date
from pathlib import Path

import pytest

from agri_research_agent.positions import delivery as codec
from agri_research_agent.automation import positions_scheduled_collection as collection
from test_positions_delivery import archive, ROOT
from test_server_collection import Source


def increment(baseline, changed=True):
    builder = codec.IncrementBuilder(baseline, ROOT)
    if changed:
        rows, raw, url = Source().holdings(date(2026, 10, 9), ('M', 'Y', 'P'))
        for domain, varieties in (('soybean', {'M', 'Y'}), ('palm', {'P'})):
            fresh = [r for r in rows if r['scope'].rstrip('0123456789') in varieties]
            builder.publish(Path(domain), [], fresh, [('stockapi_DCE_20261009', raw, url, 'json')], [])
    document, semantic = builder.finish(ROOT)
    return document, codec.resolve_increment(document, baseline)


def test_increment_contains_new_rows_once_and_deduplicated_evidence():
    baseline = archive(day='2026-10-08')
    original = codec.canonical(baseline)
    document, candidate = increment(baseline)
    old = codec.validate_archive(baseline, ROOT)[0]
    new = codec.validate_archive(candidate, ROOT)[0]
    assert len(document['objects']) == 1  # DCE raw shared by soybean and palm.
    assert set(document['updates']) == {'soybean', 'palm'}
    for domain in codec.DOMAINS:
        assert new[domain]['foreign'] == old[domain]['foreign']
        assert all(r in new[domain]['domestic'] for r in old[domain]['domestic'])
    assert all(r['report_date']=='2026-10-09' for u in document['updates'].values() for r in u['domestic'])
    assert len(codec.canonical(document)) < len(codec.canonical(candidate))
    assert codec.canonical(baseline) == original
    assert not codec.observations(candidate, baseline, ROOT)['revised_partitions']


def test_noop_proof_has_no_objects_or_rows_and_reconstructs_exact_baseline():
    baseline = archive()
    document, resolved = increment(baseline, changed=False)
    assert document['updates'] == document['objects'] == {}
    assert len(codec.canonical(document)) < 256
    assert codec.canonical(resolved) == codec.canonical(baseline)


@pytest.mark.parametrize('mutation', ['stale', 'missing_evidence', 'hash', 'extra_object', 'extra_field', 'overwrite', 'traversal'])
def test_increment_fail_closed(mutation):
    baseline = archive(day='2026-10-08')
    document, candidate = increment(baseline)
    update = document['updates']['palm']
    if mutation == 'stale': document['baseline_sha256'] = '0'*64
    if mutation == 'missing_evidence': document['objects'] = {}
    if mutation == 'hash': next(iter(document['objects'].values()))['sha256'] = '0'*64
    if mutation == 'extra_object':
        document['objects']['0'*64] = deepcopy(next(iter(document['objects'].values())))
    if mutation == 'extra_field': document['delete'] = ['history']
    if mutation == 'overwrite': update['domestic'] = codec.validate_archive(baseline, ROOT)[0]['palm']['domestic']
    if mutation == 'traversal': next(iter(update['sources'].values()))['raw_file'] = '../outside'
    with pytest.raises(ValueError): codec.resolve_increment(document, baseline)


def test_rehashed_business_mutation_still_requires_offline_source_replay():
    baseline = archive(day='2026-10-08')
    document, _ = increment(baseline)
    document['updates']['palm']['domestic'][0]['positions'] += 1
    with pytest.raises(ValueError):
        codec.validate_archive(codec.resolve_increment(document, baseline), ROOT)


def test_collection_nochange_never_materializes_history_or_bundle(tmp_path, monkeypatch):
    monkeypatch.setattr(collection.delivery, '_module', lambda *a: type('Collector', (), {
        'collect': staticmethod(lambda *a, **k: ([], [], [], [{'status':'not_published'}]))}))
    monkeypatch.setattr(codec, 'materialize', lambda *a: pytest.fail('full history written'))
    document, attempts = collection.collect_bundle(archive(), tmp_path, markets='domestic',
        today=date(2026,10,9), incremental=True)
    assert document['updates'] == {}
    assert not (tmp_path/'collection').exists() and not (tmp_path/'bundle').exists()
    assert sum(p.stat().st_size for p in tmp_path.rglob('*') if p.is_file()) < 4096


def test_second_domestic_check_skips_complete_dce_day_and_writes_no_full_copy(tmp_path, monkeypatch):
    _, baseline = increment(archive(day='2026-10-08'))
    monkeypatch.setattr(collection.delivery, '_module', lambda *a: type('Collector', (), {
        'collect': staticmethod(lambda *a, **k: ([], [], [], []))}))
    class MustNotFetch:
        def holdings(self,*a,**k): pytest.fail('already complete day was downloaded again')
    document, attempts = collection.collect_bundle(baseline, tmp_path, markets='domestic',
        today=date(2026,10,9), stock_source=MustNotFetch(), incremental=True)
    assert document['updates'] == {} and len(attempts) == 2
    assert not (tmp_path/'collection').exists() and not (tmp_path/'bundle').exists()


def test_changed_collection_uses_delta_only(tmp_path, monkeypatch):
    monkeypatch.setattr(collection.delivery, '_module', lambda *a: type('Collector', (), {
        'collect': staticmethod(lambda *a, **k: ([], [], [], []))}))
    document, attempts = collection.collect_bundle(archive(day='2026-10-08'), tmp_path, markets='domestic',
        today=date(2026,10,9), stock_source=Source(), incremental=True)
    assert set(document['updates']) == {'soybean','palm'}
    assert len(document['objects']) == 1
    assert not (tmp_path/'collection').exists() and not (tmp_path/'bundle').exists()


@pytest.mark.parametrize('today,start_year',[(date(2026,10,17),2026),(date(2027,1,10),2026)])
def test_annual_window_avoids_closed_year_downloads_but_keeps_rollover(tmp_path,monkeypatch,today,start_year):
    from agri_research_agent.positions.cftc_annual import AnnualSources
    seen=[]
    def collect(*a,**k):
        assert k['start_year']==start_year
        assert k['include_cftc'] is False
        return [],[],[],[]
    monkeypatch.setattr(collection.delivery,'_module',lambda *a:type('Collector',(),{'collect':staticmethod(collect)}))
    def annual(self,domain,spec,previous,year):
        seen.append(year)
        return [],[],[]
    monkeypatch.setattr(AnnualSources,'collect',annual)
    document,_=collection.collect_bundle(archive(),tmp_path,markets='foreign',today=today,cftc_annual=True,incremental=True)
    assert seen==[start_year]*3 and document['updates']=={}


@pytest.mark.parametrize('changed',[False,True])
def test_offline_worker_validates_increment_against_real_materialized_baseline(tmp_path, monkeypatch, changed):
    from test_positions_delivery import producer, delta, write
    host=producer.delivery._host_contract()
    baseline=archive(day='2026-10-08')
    document,candidate_archive=increment(baseline,changed=changed)
    candidate,allocation=tmp_path/'candidate',tmp_path/'allocation'
    write(allocation/host.POSITIONS_STABLE,baseline)
    write(allocation/host.POSITIONS_STATUS,{'existing':True})
    codec.materialize(baseline,allocation/host.DOMAIN_CONTRACTS['commodity_positions']['domain_dir'])
    manifest=delta(candidate_archive,baseline)
    manifest['baseline']['files']={p:host._actual_identity(allocation/p) for p in manifest['baseline']['files']}
    manifest['payloads']['positions_archive.json']=host._actual_identity(write(candidate/'positions_archive.json',document))
    write(candidate/host.MANIFEST_NAME,manifest)
    original_read=host.read_json
    monkeypatch.setattr(host,'Path',lambda p:candidate if p=='/candidate' else allocation if p=='/allocation' else ROOT if p=='/app' else Path(p))
    monkeypatch.setattr(host,'read_json',lambda p:{'git_commit':'a'*40,'git_tree':'b'*40} if str(p)=='/app/RELEASE.json' else original_read(p))
    monkeypatch.setenv('MARKET_DATA_GIT_HEAD','a'*40)
    monkeypatch.setenv('MARKET_DATA_GIT_TREE','b'*40)
    assert host.worker_validate('commodity_positions','a'*40,'b'*40)['status']=='PASS'
    document['baseline_sha256']='0'*64
    manifest['payloads']['positions_archive.json']=host._actual_identity(write(candidate/'positions_archive.json',document))
    write(candidate/host.MANIFEST_NAME,manifest)
    with pytest.raises(ValueError,match='baseline differs'):
        host.worker_validate('commodity_positions','a'*40,'b'*40)
