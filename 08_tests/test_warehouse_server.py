from __future__ import annotations

import datetime as dt
import importlib.util
import json
import subprocess
from pathlib import Path

import pytest

from agri_research_agent.warehouse_receipts.model import FIELDS, aggregate_daily, normalize_rows, sha256, write_snapshot

ROOT=Path(__file__).resolve().parents[1]


def host_module():
    spec=importlib.util.spec_from_file_location("warehouse_host_test",ROOT/"09_deploy/warehouse_server/run.py")
    module=importlib.util.module_from_spec(spec); spec.loader.exec_module(module)
    return module


def snapshot(root,vol):
    row=dict.fromkeys(FIELDS.split(","))
    row.update(symbol="M",trade_date="20261013",fut_name="豆粕",warehouse="A",wh_id="1",unit="手",exchange="DCE",vol=vol,vol_chg=0)
    raw=normalize_rows([row]); return write_snapshot(root,raw,aggregate_daily(raw),{})


def test_host_publication_preserves_history_and_rejects_stale_base(tmp_path):
    host=host_module(); store=tmp_path/"store"; candidate=tmp_path/"candidate"
    old=snapshot(store,10); new=snapshot(candidate,20)
    before=(store/"current.json").read_bytes(); digest=sha256(store/"current.json")
    with pytest.raises(ValueError,match="STALE"):
        host.publish_candidate(store,candidate,"0"*64)
    assert (store/"current.json").read_bytes()==before
    result=host.publish_candidate(store,candidate,digest)
    assert result==new["snapshot_id"]
    assert (store/"snapshots"/old["snapshot_id"]/"raw.csv").is_file()
    assert json.loads((store/"current.json").read_text())["snapshot_id"]==new["snapshot_id"]


def test_corrupt_candidate_cannot_change_pointer(tmp_path):
    host=host_module(); store=tmp_path/"store"; candidate=tmp_path/"candidate"
    snapshot(store,10); new=snapshot(candidate,20)
    before=(store/"current.json").read_bytes()
    (candidate/"snapshots"/new["snapshot_id"]/"daily.csv").write_text("corrupt",encoding="utf-8")
    with pytest.raises(ValueError,match="identity"):
        host.publish_candidate(store,candidate,sha256(store/"current.json"))
    assert (store/"current.json").read_bytes()==before


def test_collector_mounts_production_only_readonly_and_keeps_token_out_of_arguments():
    host=host_module()
    args=host.collector_command(dict(store_root="/var/lib/market-data/production-runtime/test/01_data/processed/warehouse_receipts",image_id="sha256:"+"0"*64),Path("/var/lib/market-data/production-data-automation/warehouse-receipts/run"),"warehouse-test")
    baseline=next(value for value in args if "dst=/baseline" in value)
    assert baseline.endswith(",readonly")
    assert "--read-only" in args and "--pull" in args and "never" in args
    assert "65532:65532" in args and "no-new-privileges" in args
    assert not any("token=" in value.lower() for value in args)


def test_conditional_retries_only_skip_fully_current_same_day_checks():
    host=host_module(); now=dt.datetime(2026,10,14,18,30,tzinfo=dt.timezone(dt.timedelta(hours=8)))
    status=dict(status="NO_CHANGE",checked_at=now.isoformat(),pending_products=[])
    assert not host.retry_needed(status,now)
    assert host.retry_needed({**status,"status":"WAITING_FOR_SOURCE"},now)
    assert host.retry_needed({**status,"pending_products":["SR"]},now)
    assert host.retry_needed({**status,"checked_at":(now-dt.timedelta(days=1)).isoformat()},now)
    assert host.retry_needed({**status,"checked_at":(now+dt.timedelta(hours=1)).isoformat()},now)


def test_canonical_origin_survives_the_readonly_ssh_transport_rewrite(tmp_path):
    repository=tmp_path/"approved-source"
    canonical="https://github.com/903798404-hub/commodity-research-system.git"
    rewritten="git@github.com:903798404-hub/commodity-research-system.git"
    subprocess.run(["git","init",str(repository)],check=True,capture_output=True)
    def git(*args):
        return subprocess.check_output(["git","-C",str(repository),*args],text=True).strip()
    git("remote","add","origin",canonical)
    git("config",f"url.{rewritten}.insteadOf",canonical)
    assert git("remote","get-url","origin")==rewritten
    assert git("config","--get","remote.origin.url")==canonical
    host=host_module()
    host.verify_source_origin(repository)
    git("remote","set-url","origin","https://github.com/example/unapproved.git")
    with pytest.raises(ValueError,match="source origin differs"):
        host.verify_source_origin(repository)
