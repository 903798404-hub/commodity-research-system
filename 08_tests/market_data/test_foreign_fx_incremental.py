from copy import deepcopy
from datetime import date
import json

import pytest

from agri_research_agent.market_data import foreign_fx_delivery as fx
from agri_research_agent.market_data.foreign_fx import BY_CODE, FxDataError


def feed(*, latest="2024-01-05", revision=False, omit=False):
    calls = []
    def xml(days):
        currencies = set(BY_CODE) - {"BRL"}
        return ("<Envelope>" + "".join('<Cube time="' + d + '"><Cube currency="USD" rate="1.25"/>' +
            "".join(f'<Cube currency="{c}" rate="2.5"/>' for c in sorted(currencies)) + "</Cube>" for d in days) + "</Envelope>").encode()
    days = ["2024-01-02", "2024-01-03", "2024-01-05"]
    days = [d for d in days if d <= latest]
    if latest not in days: days.append(latest)
    def fetch(url):
        calls.append(url)
        if url == fx.BCB_LATEST_URL:
            return json.dumps({"conteudo": [dict(moeda="Dólar", tipoCotacao="Fechamento", valorVenda=5.0,
                dataIndicador=latest + "T16:00:00Z")]}).encode()
        if url == fx.ECB_LATEST_URL: return xml([latest])
        if url in {fx.ECB_HISTORY_URL, fx.ECB_RECENT_URL}: return xml(days)
        selected = [d for d in days if not (omit and d == "2024-01-03")]
        return json.dumps([dict(data=date.fromisoformat(d).strftime("%d/%m/%Y"),
            valor="5.1" if revision and d == "2024-01-02" else "5.0") for d in selected]).encode()
    return fetch, calls


def baseline(tmp_path):
    fetch, _ = feed(latest="2024-01-03")
    return fx.collect(date(2024, 1, 4), tmp_path / "initial", fetcher=fetch)


def test_increment_reads_recent_endpoint_preserves_old_history_and_replays_v1_v2(tmp_path):
    old, evidence = baseline(tmp_path); original = deepcopy((old, evidence))
    fetch, calls = feed()
    candidate, proof = fx.collect_incremental(date(2024, 1, 6), tmp_path / "daily", baseline=old,
        baseline_evidence=evidence, fetcher=fetch)
    assert (old, evidence) == original
    assert fx.ECB_RECENT_URL in calls and fx.ECB_HISTORY_URL not in calls
    assert "dataInicial=20/12/2023" in next(u for u in calls if "api.bcb" in u)
    changes = fx.observations(candidate, old, proof)
    assert changes["added"] == 8 and changes["revised"] == 0
    assert proof["schema_version"] == fx.WINDOW_EVIDENCE_SCHEMA and len(proof["segments"]) == 2
    assert old["observations"] == [r for r in candidate["observations"] if r["date"] <= "2024-01-03"]
    repeated, repeated_proof = fx.collect_incremental(date(2024, 1, 6), tmp_path / "repeat", baseline=candidate,
        baseline_evidence=proof, fetcher=fetch)
    assert not fx.observations(repeated, candidate, repeated_proof)["business_changed"]


def test_month_change_runs_full_reconciliation_and_resets_window_archive(tmp_path):
    old, evidence = baseline(tmp_path)
    fetch, calls = feed(latest="2024-02-02", revision=True)
    candidate, proof = fx.collect_incremental(date(2024, 2, 3), tmp_path / "month", baseline=old,
        baseline_evidence=evidence, fetcher=fetch)
    assert proof["schema_version"] == fx.EVIDENCE_SCHEMA
    assert fx.ECB_HISTORY_URL in calls and fx.ECB_RECENT_URL not in calls
    assert fx.observations(candidate, old, proof)["revised"] == 1


@pytest.mark.parametrize("corrupt", ["drop", "tamper", "gap", "method"])
def test_incremental_proofs_refuse_missing_rows_or_tampered_history(tmp_path, corrupt):
    old, evidence = baseline(tmp_path); fetch, _ = feed(omit=corrupt == "drop")
    if corrupt == "drop":
        with pytest.raises(FxDataError, match="removes published"):
            fx.collect_incremental(date(2024, 1, 6), tmp_path / "bad", baseline=old, baseline_evidence=evidence, fetcher=fetch)
        return
    candidate, proof = fx.collect_incremental(date(2024, 1, 6), tmp_path / "good", baseline=old, baseline_evidence=evidence, fetcher=fetch)
    if corrupt == "tamper": proof["segments"][0]["raw"]["bcb_history"]["sha256"] = "0" * 64
    elif corrupt == "gap": proof["segments"][1]["start"] = "2024-01-06"
    else: candidate["sources"][0]["method"] = "altered"
    with pytest.raises(FxDataError): fx.replay(candidate, proof)


def test_failed_request_does_not_mutate_baseline_or_create_new_stable(tmp_path):
    old, evidence = baseline(tmp_path); before = deepcopy((old, evidence))
    def failure(url): raise TimeoutError("fixture request failed")
    with pytest.raises(TimeoutError):
        fx.collect_incremental(date(2024, 1, 6), tmp_path / "failure", baseline=old, baseline_evidence=evidence, fetcher=failure)
    assert (old, evidence) == before and not (tmp_path / "failure/daily.json").exists()
