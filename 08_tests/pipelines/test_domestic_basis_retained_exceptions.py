"""Non-sensitive reproduction of 9 + 6 historical NULL factory quote rows."""
from dataclasses import replace
from datetime import date
from decimal import Decimal
import json

import pyarrow as pa
import pyarrow.parquet as pq
import pytest

from agri_research_agent.data_sources.lutou.domestic_basis import (
    DomesticBasisSourceRow, load_domestic_basis_catalog,
)
from agri_research_agent.pipelines import lutou_domestic_basis as basis
from test_domestic_basis_async_update import (
    AS_OF, OLD, PREVIOUS, START, NEW, MISSING, extraction, sources,
    FakeAdapter, mapping_file, runtime,
)

EAST = 'market.basis.domestic.china.rapeseed_oil_3.east_china.spot'
SOUTH = 'market.basis.domestic.china.rapeseed_oil_3.south_china.spot'


def retained_fixture(*, advance=False):
    catalog = load_domestic_basis_catalog()
    old = tuple(replace(row, basis_value=Decimal('680' if row.region == '华东' else '410'))
                if row.source_product == '菜籽油' and row.region in {'华东', '华南'} else row
                for row in sources(catalog))
    seed = extraction(catalog, old, old, start=OLD, end=PREVIOUS)
    current, _ = basis.build_canonical_table(basis.build_standard_table(seed, catalog), catalog)
    nulls = tuple(DomesticBasisSourceRow(
        business_date=date(2026, 8, 6), source_product='菜籽油', region=region,
        source_quote_type='现货基差', source_contract_code=None, basis_value=None,
        raw_basis_value=None, row_type='工厂', factory=f'synthetic-factory-{region}-{i}',
        article_id='synthetic-null-article', source_row_identity=f'synthetic-null-{region}-{i}',
    ) for region, count in [('华东', 9), ('华南', 6)] for i in range(count))
    later = sources(catalog, increment=True) if advance else ()
    all_rows = old + later + nulls
    rows = tuple(row for row in all_rows if START <= row.business_date <= NEW)
    return catalog, current, extraction(catalog, rows, all_rows), nulls


def assemble(catalog, current, source):
    return basis.assemble_domestic_basis_candidate(
        current=current, extraction=source, catalog=catalog, as_of_date=AS_OF)


@pytest.mark.parametrize('advance', [False, True])
def test_historical_retained_null_fixture(advance):
    catalog, current, source, nulls = retained_fixture(advance=advance)
    state = assemble(catalog, current, source)
    diag = state.update_report['quality_diagnostics']
    assert diag['retained_exception_rows'] == 15
    assert diag['retained_exception_identities'] == [EAST, SOUTH]
    assert diag['blocking_normalization_errors'] == 0
    assert diag['not_evaluated_due_to_batch_abort'] == 0
    null_ids = {row.source_row_identity for row in nulls}
    retained = [row for row in state.standard.to_pylist() if row['source_row_identity'] in null_ids]
    assert len(retained) == 15
    assert all(row['basis_value'] is None and row['raw_contract_code'] is None
               and not row['is_usable'] and row['canonical_selection_status'] == 'NOT_ELIGIBLE_FOR_CANONICAL'
               and row['quality_status'] == 'RETAINED_EXCEPTION' for row in retained)
    assert not any(row['business_date'] == date(2026, 8, 6) for row in state.next_state.to_pylist())
    assert len(set(state.next_state['series_id'].to_pylist())) == 21
    assert state.next_state.num_rows == current.num_rows + (20 if advance else 0)
    for ident, value in [(EAST, '680.0000'), (SOUTH, '410.0000')]:
        history = [r for r in state.next_state.to_pylist() if r['series_id'] == ident and r['business_date'] == PREVIOUS]
        assert len(history) == 1 and history[0]['value'] == Decimal(value)
    report = state.update_report
    assert report['promotion_allowed']
    assert report['summary']['coverage'] == {'PRESENT': 21, 'MISSING': 0, 'ERROR': 0}
    assert report['summary']['updates'] == {'UPDATED': 20 if advance else 0, 'NO_CHANGE': 1 if advance else 21, 'ERROR': 0}
    assert report['summary']['freshness'] == {'FRESH': 0, 'STALE': 0, 'UNASSESSED': 21}
    central = next(row for row in report['series'] if row['identity'] == MISSING)
    assert central['update_status'] == 'NO_CHANGE' and central['next_latest_date'] == OLD.isoformat()


@pytest.mark.parametrize('changes', [
    {'raw_basis_value': 'not-a-number'}, {'raw_basis_value': ''},
    {'raw_basis_value': 'NaN'}, {'source_contract_code': '2701'},
    {'raw_price_text': 'unparsed quote'}, {'cash_price': Decimal('100')},
    {'futures_price': Decimal('100')}, {'volume': Decimal('0')},
    {'row_type': 'unknown'}, {'source_product': '大豆油'}, {'region': '华北'},
])
def test_unapproved_null_shapes_still_block(changes):
    catalog, current, source, nulls = retained_fixture()
    replaced = replace(nulls[0], **changes)
    all_rows = tuple(replaced if r.source_row_identity == nulls[0].source_row_identity else r for r in source.records)
    source = extraction(catalog, all_rows, (*sources(catalog), *all_rows))
    # Inventory must count exactly the rows queried, not duplicate baseline rows.
    source = replace(source, source_inventory=tuple(replace(item, window_row_count=sum(
        (r.source_product, r.region) == (item.source_product, item.region) for r in all_rows))
        for item in source.source_inventory))
    with pytest.raises(basis.DomesticBasisPipelineError) as caught:
        assemble(catalog, current, source)
    assert not caught.value.update_report['promotion_allowed']
    assert caught.value.update_report['summary']['updates']['ERROR'] > 0


def test_unknown_nonnumeric_source_value_is_rejected():
    with pytest.raises(ValueError, match='basis_value must be finite'):
        DomesticBasisSourceRow(date(2026, 8, 6), '菜籽油', '华东', '现货基差', None,
                               'not-a-number', 'synthetic-illegal')


@pytest.mark.parametrize('initial_seed', [False, True])
def test_unapproved_new_null_or_missing_prior_state_still_blocks(initial_seed):
    catalog, current, source, nulls = retained_fixture()
    if initial_seed:
        rows = sources(catalog) + nulls
        source = extraction(catalog, rows, rows, start=OLD, end=PREVIOUS)
        current = None
    else:
        rows = tuple(replace(r, business_date=NEW) if r.source_row_identity == nulls[0].source_row_identity else r
                     for r in source.records)
        source = extraction(catalog, rows, (*rows, *[r for r in sources(catalog) if r.business_date < START]))
    with pytest.raises(basis.DomesticBasisPipelineError, match='normalization failed'):
        assemble(catalog, current, source)


@pytest.mark.parametrize('fault', ['standard_schema', 'raw_text_mask', 'eligible_null', 'canonical_add', 'identity_loss'])
def test_retained_policy_does_not_hide_corruption(monkeypatch, fault):
    catalog, current, source, _ = retained_fixture()
    if fault in {'standard_schema', 'raw_text_mask', 'eligible_null'}:
        original = basis.build_standard_table
        if fault == 'raw_text_mask':
            source = replace(source, records=tuple(replace(r, raw_basis_value='invalid') if r.source_row_identity.startswith('synthetic-null') else r for r in source.records))
        def corrupt(*args, **kwargs):
            table = original(*args, **kwargs)
            if fault == 'standard_schema':
                return table.drop(['unit'])
            rows = table.to_pylist()
            for row in rows:
                if row['source_row_identity'].startswith('synthetic-null'):
                    if fault == 'raw_text_mask': row['raw_basis_value'] = None
                    else: row['canonical_selection_status'] = 'SELECTED_BY_CANONICAL_POLICY'
            return pa.Table.from_pylist(rows, schema=basis.STANDARD_SCHEMA)
        monkeypatch.setattr(basis, 'build_standard_table', corrupt)
    else:
        original = basis._merge_current
        def corrupt(*args):
            table = original(*args)
            rows = table.to_pylist()
            if fault == 'identity_loss': rows = [r for r in rows if r['series_id'] != EAST]
            else: rows.append({**rows[0], 'business_date': date(2026, 8, 6)})
            return pa.Table.from_pylist(rows, schema=basis.CANONICAL_SCHEMA)
        monkeypatch.setattr(basis, '_merge_current', corrupt)
    with pytest.raises(basis.DomesticBasisPipelineError) as caught:
        assemble(catalog, current, source)
    assert not caught.value.update_report['promotion_allowed']


def test_batch_abort_diagnostics_distinguish_actual_errors():
    catalog, current, source, nulls = retained_fixture()
    source = replace(source, records=tuple(replace(r, raw_basis_value='invalid') if r.source_row_identity == nulls[0].source_row_identity else r for r in source.records))
    with pytest.raises(basis.DomesticBasisPipelineError) as caught:
        assemble(catalog, current, source)
    diag = caught.value.update_report['quality_diagnostics']
    assert diag['retained_exception_rows'] == 14
    assert diag['blocking_normalization_errors'] == 1
    assert diag['not_evaluated_due_to_batch_abort'] == 20
    assert caught.value.update_report['summary']['updates']['ERROR'] == 21


def test_fixture_audit_keeps_null_rows_and_quality_without_promoting(runtime, tmp_path):
    catalog, _, _, nulls = retained_fixture()
    mapping = mapping_file(tmp_path, approved=False)
    old = sources(catalog)
    basis.run_domestic_basis_live(runtime=runtime, run_id='synthetic-seed',
                                  adapter=FakeAdapter(old), mapping_path=mapping)
    root = runtime.runtime_root / 'public-market-data/lutou-domestic-basis'
    before = (root / 'current.json').read_bytes()
    result = basis.run_domestic_basis_live(runtime=runtime, run_id='synthetic-retained',
        adapter=FakeAdapter(old + nulls), mapping_path=mapping, candidate_only=True)
    saved = json.loads((root / 'async-update-reports/synthetic-retained/manifest.json').read_text(encoding='utf-8'))
    assert saved['quality_diagnostics']['retained_exception_rows'] == 15
    assert saved['quality_diagnostics']['blocking_normalization_errors'] == 0
    assert saved['summary']['updates']['NO_CHANGE'] == 21
    standard = pq.read_table(result.candidate_directory / 'standard.parquet').to_pylist()
    assert sum(row['exception_reason'] == 'BASIS_NULL_OR_NONNUMERIC' for row in standard) == 15
    following = pq.read_table(result.candidate_directory / 'next-state.parquet').to_pylist()
    assert not any(row['business_date'] == date(2026, 8, 6) for row in following)
    assert not result.promoted and (root / 'current.json').read_bytes() == before
