import importlib.util
from pathlib import Path

import pandas as pd
import pytest

from agri_research_agent.shared import table_increment as codec

SHA = "a" * 64
OPTIONS = dict(key_columns=("year", "week"), scope_column="year", scope_values=[2026],
               refresh_columns=("batch", "retrieved_at"))


def baseline():
    return pd.DataFrame({
        "year": pd.Series([2025, 2026, 2026], dtype="Int64"),
        "week": pd.to_datetime(["2025-07-24", "2026-07-23", "2026-07-30"]),
        "value": pd.Series([7, pd.NA, 10], dtype="Int64"),
        "batch": pd.Series(["historical", "old", "old"], dtype="string"),
        "retrieved_at": pd.to_datetime(["2025-08-01T00:00:00Z",
                                        "2026-08-01T00:00:00Z", "2026-08-01T00:00:00Z"]),
    })


def fresh():
    result = baseline()
    selected = result.year.eq(2026)
    result.loc[selected, "batch"] = "new"
    result.loc[selected, "retrieved_at"] = pd.Timestamp("2026-08-07T00:00:00Z")
    return result


def test_arrow_null_roundtrip_preserves_missing_values_and_does_not_fill_them():
    original = pd.DataFrame({"year": [2026, 2026, 2026], "week": [1, 2, 3],
                             "value": pd.Series([None, pd.NA, ""], dtype=object)})
    payload = codec.build(original, None, baseline_sha256=None, key_columns=["year", "week"],
                          scope_column="year", scope_values=[2026])
    restored = codec.resolve(payload, None, baseline_sha256=None, key_columns=["year", "week"],
                             scope_column="year", scope_values=[2026])
    assert codec.equivalent(restored, original, ["year", "week"])
    assert restored.value.isna().tolist() == [True, True, False]
    filled = restored.copy()
    filled.loc[0, "value"] = ""
    assert not codec.equivalent(filled, original, ["year", "week"])


def test_no_business_change_projects_fresh_provenance_without_serializing_history():
    old, new = baseline(), fresh()
    encoded = codec.build(new, old, baseline_sha256=SHA, **OPTIONS)
    assert encoded["updates"] is None and not encoded["removals"]
    assert len(codec.canonical(encoded)) < 8000
    restored = codec.resolve(encoded, old, baseline_sha256=SHA, **OPTIONS)
    pd.testing.assert_frame_equal(restored, new)
    pd.testing.assert_frame_equal(old, baseline())


def test_unchanged_transport_size_does_not_grow_with_retained_history():
    old = baseline()
    history = pd.concat([old.iloc[[0]]] * 10000, ignore_index=True)
    history["week"] = pd.date_range("1990-01-01", periods=len(history))
    old = pd.concat([history, old.loc[old.year.eq(2026)]], ignore_index=True)
    new = old.copy()
    new.loc[new.year.eq(2026), "batch"] = "new"
    new.loc[new.year.eq(2026), "retrieved_at"] = pd.Timestamp("2026-08-07T00:00:00Z")
    encoded = codec.build(new, old, baseline_sha256=SHA, **OPTIONS)
    assert encoded["updates"] is None and not encoded["removals"]
    assert len(codec.canonical(encoded)) < 8000
    restored = codec.resolve(encoded, old, baseline_sha256=SHA, **OPTIONS)
    pd.testing.assert_frame_equal(restored, new.sort_values(["year", "week"]).reset_index(drop=True))


def test_actual_added_revised_and_removed_keys_preserve_nulls_and_old_scope():
    old, new = baseline(), fresh()
    new = new.drop(index=1).reset_index(drop=True)
    new.loc[new.year.eq(2026), "value"] = 11
    extra = new.iloc[[-1]].copy()
    extra["week"] = pd.Timestamp("2026-08-06")
    extra["value"] = pd.Series([pd.NA], index=extra.index, dtype="Int64")
    new = pd.concat([new, extra], ignore_index=True)
    encoded = codec.build(new, old, baseline_sha256=SHA, **OPTIONS)
    restored = codec.resolve(encoded, old, baseline_sha256=SHA, **OPTIONS)
    pd.testing.assert_frame_equal(restored, new)
    assert restored.loc[restored.week.eq(pd.Timestamp("2026-08-06")), "value"].isna().all()
    assert len(encoded["removals"]) == 1


def test_initialization_requires_every_row_inside_declared_scope():
    new = fresh().loc[lambda frame: frame.year.eq(2026)].reset_index(drop=True)
    encoded = codec.build(new, None, baseline_sha256=None, **OPTIONS)
    pd.testing.assert_frame_equal(codec.resolve(encoded, None, baseline_sha256=None, **OPTIONS), new)
    with pytest.raises(ValueError, match="outside fetched scopes"):
        codec.build(fresh(), None, baseline_sha256=None, **OPTIONS)


@pytest.mark.parametrize("mutation", ["old_scope_edit", "old_scope_delete", "duplicate_key",
                                     "mixed_projection", "schema_drift"])
def test_collector_refuses_history_rewrite_or_ambiguous_projection(mutation):
    old, new = baseline(), fresh()
    if mutation == "old_scope_edit":
        new.loc[new.year.eq(2025), "value"] = 123
    elif mutation == "old_scope_delete":
        new = new.loc[new.year.eq(2026)]
    elif mutation == "duplicate_key":
        new = pd.concat([new, new.iloc[[0]]], ignore_index=True)
    elif mutation == "mixed_projection":
        new.loc[new.index[-1], "batch"] = "different"
    else:
        new["unexpected"] = 1
    with pytest.raises(ValueError):
        codec.build(new, old, baseline_sha256=SHA, **OPTIONS)
    pd.testing.assert_frame_equal(old, baseline())


@pytest.mark.parametrize("mutation", ["baseline", "scope", "key", "count", "unknown_field",
                                     "duplicate_removal", "history_removal", "trailing_ipc", "trailing_schema"])
def test_offline_resolver_refuses_untrusted_transport(mutation):
    old = baseline()
    payload = codec.build(fresh(), old, baseline_sha256=SHA, **OPTIONS)
    if mutation == "baseline":
        payload["baseline_sha256"] = "b" * 64
    elif mutation == "scope":
        payload["scope_values"] = [2025, 2026]
    elif mutation == "key":
        payload["key_columns"] = ["week"]
    elif mutation == "count":
        payload["row_count"] += 1
    elif mutation == "unknown_field":
        payload["unbound"] = True
    elif mutation in {"duplicate_removal", "history_removal"}:
        year, week = (2025, "2025-07-24T00:00:00") if mutation == "history_removal" else (2026, "2026-07-23T00:00:00")
        payload["removals"] = [[year, week]] * (2 if mutation == "duplicate_removal" else 1)
    else:
        import base64
        name = "refresh" if mutation == "trailing_ipc" else "schema"
        payload[name] = base64.b64encode(base64.b64decode(payload[name]) + b"trailing").decode()
    with pytest.raises(ValueError):
        codec.resolve(payload, old, baseline_sha256=SHA, **OPTIONS)
    pd.testing.assert_frame_equal(old, baseline())


def test_fas_real_normalization_and_increment_validator_accept_sparse_projection():
    from agri_research_agent.soybean_exports import fas
    fixture = Path(__file__).resolve().parents[1] / "soybean_exports/test_fas_data_layer.py"
    spec = importlib.util.spec_from_file_location("table_increment_fas_fixture", fixture)
    helpers = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(helpers)
    rows = [helpers.fas_row(report_year=2024, week="2024-07-25T00:00:00"),
            helpers.fas_row(report_year=2025, week="2025-07-31T00:00:00"), helpers.fas_row()]
    old = helpers.normalize(rows, "old")
    latest = rows[1:] + [helpers.fas_row(week="2026-08-06T00:00:00")]
    normalized = helpers.normalize(latest, "new")
    new = fas.merge_fas_years(old, normalized, [2025, 2026])
    options = dict(key_columns=fas.FAS_KEY, scope_column="report_market_year_end",
                   scope_values=[2025, 2026], refresh_columns=("source_fetch_id", "batch_id",
                       "raw_snapshot_id", "raw_snapshot_sha256", "fetch_time_utc",
                       "source_release_time_raw", "source_release_timezone"))
    encoded = codec.build(new, old, baseline_sha256=SHA, **options)
    assert codec._decode(encoded["updates"]).num_rows == 1
    restored = codec.resolve(encoded, old, baseline_sha256=SHA, **options)
    pd.testing.assert_frame_equal(restored, new)
    metadata = dict(batch_id="new", raw_snapshot_sha256="d" * 64,
                    fetch_scope=dict(report_market_years=[2025, 2026]))
    fas.validate_fas_increment(restored, old, metadata)
    pd.testing.assert_frame_equal(restored.loc[restored.report_market_year_end.eq(2024)].reset_index(drop=True),
                                  old.loc[old.report_market_year_end.eq(2024)].reset_index(drop=True))
