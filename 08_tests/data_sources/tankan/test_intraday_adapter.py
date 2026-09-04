"""Adapter tests bind the approved public query, proofs and full contract identity."""
from dataclasses import replace
from datetime import datetime, timezone
from types import MappingProxyType
import pytest

from agri_research_agent.data_sources.tankan.client import LiveReadResult
from agri_research_agent.data_sources.tankan.models import ConnectionProof, QueryPlanProof
from agri_research_agent.data_sources.tankan.intraday_adapter import ExactRequest, read_intraday
from agri_research_agent.market_data.intraday import IntradaySnapshotValidationError

STAMP=datetime(2026,8,31,7,29,tzinfo=timezone.utc)
REQUESTS=(ExactRequest("CBOT:SOYBEAN:2027-01","2701","CBOT","SOYBEAN"),
          ExactRequest("DCE:SOYMEAL:2027-01","M2701","DCE","SOYMEAL"),
          ExactRequest("DCE:SOYOIL:2027-01","Y2701","DCE","SOYOIL"),
          ExactRequest("FX:USD/CNH:SPOT","","OTC","USD/CNH"))


class Client:
    def __init__(self,missing=()):
        self.missing=missing;self.calls=[]

    def read_live(self,query,identities):
        self.calls.append((query,identities))
        available=tuple(MappingProxyType(dict(requested_identity=key,price=6.8 if key=="USD/CNH:SPOT" else 3000,
            source_updated_at=STAMP,update_time=STAMP)) for key in identities if key not in self.missing)
        absent=tuple(MappingProxyType(dict(requested_identity=key,price=None,reason="NOT_FOUND")) for key in identities if key in self.missing)
        return LiveReadResult(identities,available,absent,query,QueryPlanProof(query.sha256,1,1,128,10000),
            ConnectionProof("quanyong","fixture","Asia/Shanghai","on","on"),STAMP)


def test_four_categories_use_public_read_live_only():
    client=Client();result=read_intraday(client,REQUESTS)
    assert len(client.calls)==4 and len(result.available)==4 and not result.unavailable
    assert {r.request.key for r in result.available}=={r.key for r in REQUESTS}
    assert all(q.source_updated_at is STAMP and q.retrieved_at is STAMP for q in result.available)
    assert all(e["connection"]["transaction_read_only"]=="on" for e in result.query_evidence)
    assert result.available[-1].source_trade_date is None


def test_missing_far_month_never_substitutes():
    far=ExactRequest("DCE:SOYMEAL:2028-01","M2801","DCE","SOYMEAL")
    client=Client(("M2801",));result=read_intraday(client,(REQUESTS[1],far))
    assert [r.request.contract_code for r in result.available]==["M2701"]
    assert result.unavailable[0].contract_code=="M2801"
    assert result.unavailable[0].reason=="NOT_FOUND"
    assert client.calls[0][1]==("M2701","M2801")


@pytest.mark.parametrize("code",["M01","M0","M2713","Y2701","M2801"])
def test_exact_identity_mismatch(code):
    with pytest.raises(IntradaySnapshotValidationError):
        replace(REQUESTS[1],contract_code=code)


def test_duplicate_requests_rejected():
    client=Client()
    with pytest.raises(IntradaySnapshotValidationError):
        read_intraday(client,(REQUESTS[1],REQUESTS[1]))
    assert not client.calls


@pytest.mark.parametrize("mutation",["missing_accounting","duplicate","unrequested","timestamp","proof"])
def test_invalid_public_response_fails_closed(mutation):
    class Broken(Client):
        def read_live(self,query,identities):
            r=super().read_live(query,identities)
            row=dict(r.available[0])
            if mutation=="missing_accounting": return replace(r,available=())
            if mutation=="duplicate": return replace(r,available=r.available+r.available)
            if mutation=="proof": return replace(r,requested=("M9999",))
            if mutation=="unrequested": row["requested_identity"]="M2801"
            if mutation=="timestamp": row["update_time"]=None
            return replace(r,available=(row,))
    with pytest.raises(IntradaySnapshotValidationError):
        read_intraday(Broken(),(REQUESTS[1],))
