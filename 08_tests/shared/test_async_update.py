from datetime import date, timedelta

import pytest

from agri_research_agent.shared.async_update import FreshnessPolicy, SeriesUpdate, evaluate_update, validate_update_summary


AS_OF = date(2026, 9, 4)


def report(evidence, *, policy=None, identities=None):
    return evaluate_update(dataset_id="fixture", required={"a"}, series={"a": evidence},
                           next_identities={"a"} if identities is None else identities,
                           as_of_date=AS_OF, policy=policy or FreshnessPolicy("test-only", 1, False, True))


@pytest.mark.parametrize("update,freshness,previous,following,new_rows", [
    ("UPDATED", "FRESH", AS_OF - timedelta(days=2), AS_OF, 1),
    ("NO_CHANGE", "FRESH", AS_OF, AS_OF, 0),
    ("NO_CHANGE", "STALE", AS_OF - timedelta(days=2), AS_OF - timedelta(days=2), 0),
    ("UPDATED", "STALE", AS_OF - timedelta(days=3), AS_OF - timedelta(days=2), 1),
])
def test_independent_statuses_and_counts(update, freshness, previous, following, new_rows):
    value = report(SeriesUpdate(previous, following, following, new_rows))
    row = value["series"][0]
    assert row["coverage_status"] == "PRESENT"
    assert row["update_status"] == update
    assert row["freshness_status"] == freshness
    assert "status" not in row
    assert value["summary"]["updates"][update] == 1
    assert value["summary"]["freshness"][freshness] == 1
    for dimension in ("coverage", "updates", "freshness"):
        assert sum(value["summary"][dimension].values()) == value["summary"]["TOTAL_REQUIRED"]
    assert value["promotion_allowed"]
    validate_update_summary(value, required={"a"})


@pytest.mark.parametrize("blocking,expected", [(False, "WARNING"), (True, "FAILED")])
def test_stale_action_is_dataset_policy(blocking, expected):
    old = AS_OF - timedelta(days=2)
    value = report(SeriesUpdate(old, old, old), policy=FreshnessPolicy("test-only", 1, blocking, True))
    assert value["dataset_status"] == expected
    assert value["series"][0]["next_latest_date"] == old.isoformat()


@pytest.mark.parametrize("threshold", [None, 1])
@pytest.mark.parametrize("blocking", [True, False])
def test_unapproved_threshold_is_unassessed_not_blocking(threshold, blocking):
    old = AS_OF - timedelta(days=38)
    value = report(SeriesUpdate(old, old, old), policy=FreshnessPolicy("pending", threshold, blocking, False))
    row = value["series"][0]
    assert row["coverage_status"] == "PRESENT" and row["update_status"] == "NO_CHANGE"
    assert row["freshness_status"] == "UNASSESSED"
    assert row["latest_date"] == old.isoformat() and row["age_days"] == 38
    assert row["threshold"] is None and row["age_business_days"] is None
    assert value["dataset_status"] == "NO_CHANGE"
    assert value["blocking_reasons"] == [] and value["promotion_allowed"]


@pytest.mark.parametrize("evidence,identities", [
    (SeriesUpdate(AS_OF, AS_OF, None), set()),
    (SeriesUpdate(AS_OF, None, AS_OF), {"a"}),
    (SeriesUpdate(AS_OF, AS_OF, AS_OF, errors=("MAPPING_MISSING",)), {"a"}),
    (SeriesUpdate(AS_OF, AS_OF, AS_OF + timedelta(days=1), 1), {"a"}),
    (SeriesUpdate(AS_OF, AS_OF, AS_OF - timedelta(days=1)), {"a"}),
    (SeriesUpdate(AS_OF, AS_OF, AS_OF, -1), {"a"}),
])
def test_errors_fail_closed(evidence, identities):
    value = report(evidence, identities=identities)
    assert value["summary"]["updates"]["ERROR"] == 1
    assert value["series"][0]["coverage_status"] in {"MISSING", "ERROR"}
    assert value["series"][0]["freshness_status"] == "UNASSESSED"
    assert not value["promotion_allowed"]


def test_unexpected_identity_fails_coverage():
    value = report(SeriesUpdate(AS_OF, AS_OF, AS_OF), identities={"a", "extra"})
    assert value["identity_coverage"]["unexpected"] == ["extra"]
    assert not value["promotion_allowed"]


@pytest.mark.parametrize("kwargs", [
    {"threshold_approved": True}, {"freshness_threshold": -1}, {"freshness_threshold": True},
    {"stale_is_blocking": "false"}, {"age_basis": "business_days"}, {"threshold_approved": "true"},
])
def test_invalid_or_unimplemented_policy_is_rejected(kwargs):
    with pytest.raises(ValueError):
        FreshnessPolicy("fixture", **kwargs)


def test_same_date_revision_is_not_falsely_reported_as_new_observation():
    value = report(SeriesUpdate(AS_OF, AS_OF, AS_OF, revision_row_count=1))
    assert value["series"][0]["update_status"] == "NO_CHANGE"
    assert value["series"][0]["new_row_count"] == 0
    assert value["dataset_status"] == "UPDATED"


@pytest.mark.parametrize("fault", ["coverage", "updates", "freshness", "total", "duplicate", "missing", "unknown_status", "boolean", "missing_dimension"])
def test_summary_reconciliation_rejects_corruption(fault):
    value = report(SeriesUpdate(AS_OF, AS_OF, AS_OF))
    if fault in {"coverage", "updates", "freshness"}:
        counts = value["summary"][fault]
        present = next(key for key, count in counts.items() if count)
        other = next(key for key in counts if key != present)
        counts[present], counts[other] = 0, 1
    elif fault == "total":
        value["summary"]["TOTAL_REQUIRED"] = 2
    elif fault == "duplicate":
        value["series"].append(dict(value["series"][0]))
    elif fault == "missing":
        value["series"] = []
    elif fault == "unknown_status":
        value["series"][0]["update_status"] = "STALE"
    elif fault == "boolean":
        value["summary"]["coverage"]["PRESENT"] = True
    else:
        del value["summary"]["freshness"]
    with pytest.raises(ValueError, match="ASYNC_SUMMARY_SERIES_MISMATCH"):
        validate_update_summary(value, required={"a"})
