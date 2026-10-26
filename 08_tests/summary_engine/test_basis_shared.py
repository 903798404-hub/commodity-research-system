from __future__ import annotations

from datetime import date
import os
from pathlib import Path
import sys
from types import SimpleNamespace

import pandas as pd
import pytest
import yaml
from streamlit.testing.v1 import AppTest

from agri_research_agent.market_data.public_basis_current import (
    PublicBasisCurrentError, PublicBasisCurrentErrorCode,
)
from agri_research_agent.summary_engine import basis_cache as shared
from agri_research_agent.summary_engine.basis import build_basis_summary
from agri_research_agent.summary_engine.rules import DEFAULT_CONFIG, load_summary_rules

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / '05_apps'))
import basis_page
import research_overview_page as overview


@pytest.fixture(autouse=True)
def isolated_shared_cache():
    shared.clear_shared_basis_cache()
    yield
    shared.clear_shared_basis_cache()


@pytest.fixture
def records():
    return pd.DataFrame([
        {'date': pd.Timestamp(day), 'commodity': commodity, 'region': '华东',
         'quote_type': '基差报价', 'delivery_month': '现货', 'futures_contract': contract,
         'cash_price': None, 'futures_price': None, 'basis': value,
         'source_sheet': 'basis_price:' + commodity}
        for commodity in ['一豆', '24度', '三菜', '豆粕', '菜粕']
        for day, value, contract in [('2026-08-17', -10., '2609'),
                                      ('2026-08-19', 0., '2609'),
                                      ('2026-08-20', 20., '2701')]
    ])


@pytest.fixture
def identity():
    return {'release_id': 'test-current', 'manifest_sha256': 'a' * 64,
            'schema_version': 'domestic-basis-current/4', 'source_max_date': '2026-08-20'}


def fake_current(monkeypatch, records, identity):
    calls = []
    def resolve(_root):
        return SimpleNamespace(release_id=identity['release_id'], manifest_sha256=identity['manifest_sha256'])
    def load(root, *, expected_release_id, expected_manifest_sha256):
        calls.append((root, expected_release_id, expected_manifest_sha256))
        assert (expected_release_id, expected_manifest_sha256) == (identity['release_id'], identity['manifest_sha256'])
        return SimpleNamespace(records=records.copy(deep=True), source_identity=dict(identity))
    monkeypatch.setattr(shared, 'resolve_public_basis_current_identity', resolve)
    monkeypatch.setattr(shared, 'load_public_basis_current', load)
    return calls


def test_page_and_overview_share_one_verified_current_read(monkeypatch, tmp_path, records, identity):
    calls = fake_current(monkeypatch, records, identity)
    monkeypatch.setattr(overview, '_public_basis_current_root', lambda: tmp_path)
    actual, source = basis_page.load_basis_page_data(tmp_path)
    summary = overview.load_basis_overview_summary()
    pd.testing.assert_frame_equal(actual, records)
    assert source == identity
    assert len(calls) == 1
    assert calls[0][1:] == (identity['release_id'], identity['manifest_sha256'])
    assert summary.to_dict() == build_basis_summary(
        records, source_identity=identity, source_dataset='public_domestic_basis_current',
    ).to_dict()


@pytest.mark.parametrize('field,value', [('release_id','next-current'), ('manifest_sha256','b' * 64)])
def test_each_public_identity_component_invalidates_shared_read(monkeypatch, tmp_path, records, identity, field, value):
    calls = fake_current(monkeypatch, records, identity)
    shared.load_shared_basis_current(tmp_path)
    identity[field] = value
    _, source = shared.load_shared_basis_current(tmp_path)
    assert len(calls) == 2 and source[field] == value


def test_removed_pointer_never_falls_back_to_warm_cache(monkeypatch, tmp_path, records, identity):
    fake_current(monkeypatch, records, identity)
    shared.load_shared_basis_current(tmp_path)
    def missing(_root):
        raise PublicBasisCurrentError(PublicBasisCurrentErrorCode.PUBLIC_CURRENT_UNAVAILABLE, 'fixture missing')
    monkeypatch.setattr(shared, 'resolve_public_basis_current_identity', missing)
    with pytest.raises(PublicBasisCurrentError, match='fixture missing'):
        shared.load_shared_basis_current(tmp_path)


def test_pointer_switch_during_cache_hit_fails_closed(monkeypatch, tmp_path, records, identity):
    calls = fake_current(monkeypatch, records, identity)
    shared.load_shared_basis_current(tmp_path)
    versions = iter([SimpleNamespace(**{k:identity[k] for k in ['release_id','manifest_sha256']}),
                     SimpleNamespace(release_id='next-current', manifest_sha256='b' * 64)])
    monkeypatch.setattr(shared, 'resolve_public_basis_current_identity', lambda _: next(versions))
    with pytest.raises(PublicBasisCurrentError, match='CURRENT_IDENTITY_MISMATCH'):
        shared.load_shared_basis_current(tmp_path)
    assert len(calls) == 1


def test_callers_cannot_mutate_another_consumers_cached_records(monkeypatch, tmp_path, records, identity):
    fake_current(monkeypatch, records, identity)
    first, source = shared.load_shared_basis_current(tmp_path)
    first.loc[0, 'basis'] = 9999.
    source['release_id'] = 'caller-edit'
    second, other = shared.load_shared_basis_current(tmp_path)
    pd.testing.assert_frame_equal(second, records)
    assert other == identity


def count_calculations(monkeypatch):
    calls=[]
    original=shared.build_basis_summary
    def build(*args, **kwargs):
        calls.append(1)
        return original(*args, **kwargs)
    monkeypatch.setattr(shared, 'build_basis_summary', build)
    return calls


def test_same_scope_reuses_calculation_with_original_zero_null_and_contract_change(monkeypatch, records, identity):
    calls=count_calculations(monkeypatch)
    options=dict(source_identity=identity, today=date(2026,8,20))
    first=shared.build_shared_basis_summary(records, **options)
    second=shared.build_shared_basis_summary(records.copy(deep=True), **options)
    reference=build_basis_summary(records, source_identity=identity,
        source_dataset='public_domestic_basis_current', rules=load_summary_rules(), today=options['today'])
    assert first.to_dict()==second.to_dict()==reference.to_dict() and len(calls)==1
    assert all(q['cash_price'] is None and q['futures_price'] is None for q in first.facts['quotes'])
    assert all(q['missing_reason']=='换月' and q['change'] is None for q in first.facts['quotes'])
    # Historical revision of the previous quote must also invalidate the key.
    changed=records.copy();changed.loc[changed.date.eq('2026-08-19'),'basis']=1.
    shared.build_shared_basis_summary(changed, **options)
    assert len(calls)==2


def test_full_and_existing_display_scopes_never_share_results(monkeypatch, records, identity):
    calls=count_calculations(monkeypatch)
    extra=records.iloc[[0]].copy();extra['commodity']='一葵';extra['date']=pd.Timestamp('2026-08-21')
    full=pd.concat([records, extra], ignore_index=True)
    display=basis_page.filter_display_data(full)
    page=shared.build_shared_basis_summary(display, source_identity=identity)
    whole=shared.build_shared_basis_summary(full, source_identity=identity)
    assert page.source_date=='2026-08-20' and whole.source_date=='2026-08-21'
    assert page.to_dict()==build_basis_summary(display, source_identity=identity,
        source_dataset='public_domestic_basis_current').to_dict()
    assert whole.to_dict()==build_basis_summary(full, source_identity=identity,
        source_dataset='public_domestic_basis_current').to_dict()
    assert len(calls)==2


def test_observation_outside_large_dataframe_sample_changes_identity():
    frame=pd.DataFrame({'date':pd.date_range('1900-01-01',periods=60_001),'basis':range(60_001)})
    original=shared._records_sha256(frame)
    for index in [0, 12345, 60000]:
        changed=frame.copy();changed.loc[index,'basis']=-1
        assert shared._records_sha256(changed)!=original


def test_rule_content_revision_invalidates_even_with_preserved_stat(monkeypatch, tmp_path, records, identity):
    calls=count_calculations(monkeypatch);path=tmp_path/'rules.yaml'
    rules=yaml.safe_load(DEFAULT_CONFIG.read_text(encoding='utf-8'));rules['freshness_days']['basis']=7
    path.write_text(yaml.safe_dump(rules,allow_unicode=True),encoding='utf-8');stat=path.stat()
    options=dict(source_identity=identity,rules_path=path,today=date(2026,8,27))
    first=shared.build_shared_basis_summary(records,**options)
    assert first.freshness_status=='fresh'
    rules['freshness_days']['basis']=6
    path.write_text(yaml.safe_dump(rules,allow_unicode=True),encoding='utf-8')
    os.utime(path,ns=(stat.st_atime_ns,stat.st_mtime_ns))
    second=shared.build_shared_basis_summary(records,**options)
    assert second.freshness_status=='stale' and len(calls)==2


def test_utc_freshness_day_invalidates_without_changing_dates_or_rules(monkeypatch, records, identity):
    calls=count_calculations(monkeypatch)
    first=shared.build_shared_basis_summary(records,source_identity=identity,today=date(2026,8,27))
    second=shared.build_shared_basis_summary(records,source_identity=identity,today=date(2026,8,28))
    assert first.freshness_status=='fresh' and second.freshness_status=='stale'
    assert first.source_date==second.source_date=='2026-08-20' and len(calls)==2


def test_source_identity_and_dataset_keep_distinct_calculation_results(monkeypatch, records, identity):
    calls=count_calculations(monkeypatch)
    shared.build_shared_basis_summary(records,source_identity=identity)
    changed=dict(identity,manifest_sha256='b'*64)
    second=shared.build_shared_basis_summary(records,source_identity=changed)
    other=shared.build_shared_basis_summary(records,source_identity=identity,source_dataset='fixture_other_scope')
    assert second.source_identity==changed and other.source_dataset=='fixture_other_scope' and len(calls)==3


def test_cached_summary_does_not_share_mutable_facts(records,identity):
    first=shared.build_shared_basis_summary(records,source_identity=identity)
    first.facts['quotes'][0]['current_basis']=9999.
    second=shared.build_shared_basis_summary(records,source_identity=identity)
    assert second.facts['quotes'][0]['current_basis']==20.


def test_invalid_summary_rules_never_return_a_warm_result(tmp_path,records,identity):
    path=tmp_path/'rules.yaml';path.write_bytes(DEFAULT_CONFIG.read_bytes())
    shared.build_shared_basis_summary(records,source_identity=identity,rules_path=path)
    path.write_text('schema_version: 99\nrule_version: invalid\n',encoding='utf-8')
    with pytest.raises(ValueError,match='规则配置版本无效'):
        shared.build_shared_basis_summary(records,source_identity=identity,rules_path=path)


def test_basis_page_filter_rerun_reuses_summary_and_preserves_display(monkeypatch,records,identity):
    calls=count_calculations(monkeypatch)
    monkeypatch.setattr(basis_page,'load_basis_page_data',lambda _: (records.copy(),dict(identity)))
    app=AppTest.from_string("from pathlib import Path\nfrom basis_page import render_basis_page\nrender_basis_page(Path('fixture-shared-current'))",default_timeout=30).run()
    assert not app.exception and len(calls)==1
    before=app.dataframe[0].value.copy(deep=True)
    app.selectbox(key='basis_matrix_commodity').set_value('豆油').run()
    assert not app.exception and len(calls)==1
    pd.testing.assert_frame_equal(app.dataframe[0].value,before)
