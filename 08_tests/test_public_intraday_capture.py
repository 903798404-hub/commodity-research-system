"""CLI wiring exercises a real isolated seal; no provider network or formal writes."""
from datetime import datetime,timezone
import importlib.util
import json
from pathlib import Path

import pytest

from agri_research_agent.data_sources.tankan.client import LiveReadResult
from agri_research_agent.data_sources.tankan.models import ConnectionProof,QueryPlanProof
from agri_research_agent.shared.file_identity import identify_file

ROOT=Path(__file__).resolve().parents[1]
spec=importlib.util.spec_from_file_location("capture_public_intraday_cli",ROOT/"04_scripts/capture_public_intraday.py")
cli=importlib.util.module_from_spec(spec);spec.loader.exec_module(cli)
STAMP=datetime(2026,8,31,7,30,tzinfo=timezone.utc)


class Client:
    def __enter__(self): return self
    def __exit__(self,*args): return False
    def read_live(self,query,identities):
        return LiveReadResult(identities,tuple(dict(requested_identity=key,price=3000,source_updated_at=STAMP,update_time=STAMP)
                             for key in identities),(),query,QueryPlanProof(query.sha256,1,1,128,10000),
                             ConnectionProof("quanyong","fixture","Asia/Shanghai","on","on"),STAMP)


def arguments(tmp_path):
    marker=tmp_path/".market-data-runtime.json"
    marker.write_text(json.dumps(dict(schema_version=1,runtime_id="test-cli",classification="fixture",
        module_id="shared-intraday",created_at=STAMP.isoformat())),encoding="utf-8")
    request=tmp_path/"request.json"
    request.write_text(json.dumps(dict(business_date="2026-08-31",session="PM",requested=[dict(instrument_id="DCE:SOYMEAL:2027-01",
        contract_code="M2701",exchange="DCE",product="SOYMEAL")],source_not_before={"DCE:SOYMEAL:2027-01":STAMP.isoformat()})),encoding="utf-8")
    calendar=tmp_path/"calendar.json";calendar.write_text('{"business_days":["2026-08-31"]}',encoding="utf-8")
    return ["--business-date","2026-08-31","--session","PM","--request-file",str(request),"--calendar-file",str(calendar),
            "--runtime-root",str(tmp_path),"--runtime-mode","FIXTURE","--expected-runtime-id","test-cli",
            "--expected-marker-sha256",identify_file(marker).sha256,"--secret-file",str(tmp_path/"never-read-secret")]


def test_cli_real_isolated_seal(tmp_path,capsys):
    assert cli.main(arguments(tmp_path),client_factory=Client,clock=lambda:STAMP)==0
    output=json.loads(capsys.readouterr().out)
    assert output["run_id"]=="2026-08-31-PM" and output["sealed_result"]=="SEALED"
    assert output["requested_count"]==output["available_count"]==1
    assert (tmp_path/"snapshots/releases/2026-08-31-PM/manifest.json").is_file()


@pytest.mark.parametrize("flag,value",[("--session","PREVIEW"),("--runtime-mode","PREVIEW"),("--business-date","invalid")])
def test_cli_parser_rejects_preview_and_invalid_values(tmp_path,flag,value):
    args=arguments(tmp_path);args[args.index(flag)+1]=value
    with pytest.raises(SystemExit) as exc:
        cli.main(args)
    assert exc.value.code==2


def test_wrong_runtime_fails_before_secret_access(tmp_path,capsys):
    args=arguments(tmp_path);args[args.index("--expected-runtime-id")+1]="wrong"
    def forbidden(): pytest.fail("Provider must not be reached")
    assert cli.main(args,client_factory=forbidden)==1
    result=json.loads(capsys.readouterr().out)
    assert result["status"]=="FAILED" and not (tmp_path/"snapshots").exists()


def test_cli_conflict_evidence_and_no_preview(tmp_path,capsys):
    args=arguments(tmp_path)
    assert cli.main(args,client_factory=Client,clock=lambda:STAMP)==0
    capsys.readouterr()
    later=STAMP.replace(minute=31)
    assert cli.main(args,client_factory=Client,clock=lambda:later)==1
    result=json.loads(capsys.readouterr().out)
    assert result["immutable_conflict"] is True
    assert result["reason"]=="IMMUTABLE_CONFLICT"
