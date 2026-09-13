"""First consumer of generic candidate hooks; never updates a store.

Fixture checks run the exact container's presentation functions using in-memory
inputs. They are distinct from the real page browser acceptance and real data.
"""
import json
import re
import subprocess
import sys
from datetime import datetime, timezone


FIXTURES = r'''
import json, sys
from datetime import date
from pathlib import Path
from types import SimpleNamespace
sys.path.insert(0, '/app/05_apps')
from agri_research_agent.import_profit import load_soybean_config
from agri_research_agent.market_data.intraday import MarketSession
from import_profit_intraday_page import _result_profit_rows
config = load_soybean_config(Path('/app/02_configs/import_profit_soybean.yaml'))
day = date(2026, 8, 28)
observations = []
for scenario in ('missing', 'am-only', 'pm-only', 'partial', 'cnf-none', 'cnf-zero', 'cbot-missing', 'dce-missing', 'fx-missing', 'origin', 'date', 'cross-year'):
    counts = []
    for session in (MarketSession.AM, MarketSession.PM):
        row = dict(business_date=day.isoformat(), session=session.value, commodity='soybean', origin='brazil', shipment_year=2026, shipment_month=12, shipment_period='2026-12', cnf_cents_per_bushel=0.0, cbot_price_cents_per_bushel=1200.0, soymeal_price_cny_per_tonne=3200.0, soyoil_price_cny_per_tonne=8000.0, fx_value=7.2, net_crush_margin_cny_per_tonne=None)
        if scenario == 'cnf-none': row['cnf_cents_per_bushel'] = None
        if scenario == 'cbot-missing': row['cbot_price_cents_per_bushel'] = None
        if scenario == 'dce-missing': row.update(soymeal_price_cny_per_tonne=None, soyoil_price_cny_per_tonne=None)
        if scenario == 'fx-missing': row['fx_value'] = None
        if scenario == 'origin': row['origin'] = 'argentina'
        if scenario == 'date': row['business_date'] = '2026-08-27'
        if scenario == 'cross-year': row.update(shipment_year=2027, shipment_period='2027-12')
        missing = scenario == 'missing' or (scenario == 'am-only' and session == MarketSession.PM) or (scenario == 'pm-only' and session == MarketSession.AM)
        release = None if missing else SimpleNamespace(business_date=day, session=session, rows=(row,))
        rows = _result_profit_rows(release, business_date=day, session=session, origin='brazil', config=config)
        assert len(rows) == 12
        assert [r['船期'][-2:] for r in rows] == [f'{m:02d}' for m in range(1,13)]
        assert all(r['CNF（美分/蒲）'] is None for r in rows[:11])
        expected = None if missing or scenario in ('origin', 'date', 'cross-year', 'cnf-none') else 0.0
        assert rows[11]['CNF（美分/蒲）'] == expected
        for src, display in [('cbot_price_cents_per_bushel','CBOT价格'), ('soymeal_price_cny_per_tonne','豆粕盘面'), ('soyoil_price_cny_per_tonne','豆油盘面'), ('fx_value','汇率')]:
            assert rows[11][display] == (None if missing or scenario in ('origin','date','cross-year') else row[src])
        assert all(r['盘面榨利（元/吨）'] is None for r in rows)
        counts.append(len(rows))
    observations.append(dict(scenario=scenario, am_rows=counts[0], pm_rows=counts[1]))
print(json.dumps(observations))
'''


def main():
    request = json.load(sys.stdin)
    result = dict(identity=request["identity"], context="CANDIDATE_FIXTURE", status="FAIL",
                  timestamp=datetime.now(timezone.utc).isoformat(), observations=[])
    try:
        container = request["identity"]["container_id"]
        if not re.fullmatch(r"[0-9a-f]{64}", container):
            raise ValueError("invalid candidate ID")
        actual = json.loads(subprocess.check_output(["docker", "inspect", container], timeout=20))[0]
        if actual["Image"] != request["identity"]["image_id"] or actual["Config"]["Hostname"] != request["identity"]["nonce"]:
            raise ValueError("candidate instance changed")
        command = (["python", "-B", "-c", FIXTURES] if request["category"] == "fixture" else
                   ["python", "-B", "/app/04_scripts/runtime/spread_runtime_preflight.py", "--identity-kind", "oci_container"])
        completed = subprocess.run(["docker", "exec", container, *command], capture_output=True, timeout=120)
        if completed.returncode:
            raise ValueError("container acceptance failed")
        observations = json.loads(completed.stdout)
        if request["category"] == "consumer" and observations.get("status") != "PASS":
            raise ValueError("consumer acceptance failed")
        result.update(status="PASS", observations=observations)
    except Exception as exc:
        result["observations"] = [{"error_type": type(exc).__name__}]
    print(json.dumps(result, ensure_ascii=False))
    return 0 if result["status"] == "PASS" else 1


if __name__ == "__main__":
    raise SystemExit(main())
