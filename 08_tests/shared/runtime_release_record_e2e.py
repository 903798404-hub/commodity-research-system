"""Hosted fixture signing of the real engine result with the formal record verifier.

This record tests the signing/verification contract. Its run-local candidate
fixture key is NOT a production trust anchor or a production release record.
The production pre_release_runtime issuer continues to use its fixed host key.
"""
from __future__ import annotations

import argparse
import base64
from datetime import datetime, timedelta, timezone
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import sys

from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from cryptography.hazmat.primitives.serialization import Encoding, PublicFormat


ROOT = Path(__file__).resolve().parents[2]


def verify_actual_engine_record(evidence: dict, output: Path) -> dict:
    path = ROOT / '09_deploy/runtime_identity/candidate_validation_record.py'
    spec = importlib.util.spec_from_file_location('hosted_release_record', path)
    record = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(record)
    key = Ed25519PrivateKey.generate()  # host-only memory; never persisted/mounted
    now = datetime.now(timezone.utc)
    payload = dict(record_id=os.urandom(16).hex(), purpose='target-runtime-validation',
        authorization_role='candidate_validation', issued_at=now.isoformat(),
        expires_at=(now+timedelta(hours=1)).isoformat(), evidence=evidence,
        evidence_sha256=hashlib.sha256(record.canonical(evidence)).hexdigest())
    record.validate_payload(payload)
    envelope = dict(schema_version='candidate-validation-record/1',algorithm='ed25519',
                    key_id='hosted-record-fixture',payload=payload)
    envelope['signature']=base64.b64encode(key.sign(record.canonical(envelope))).decode('ascii')
    public=base64.b64encode(key.public_key().public_bytes(Encoding.Raw,PublicFormat.Raw)).decode('ascii')
    trust=dict(schema_version='production-runtime-trust/1',keys=[dict(key_id=envelope['key_id'],
        domain='candidate_validation',algorithm='ed25519',public_key_base64=public)],
        revoked_key_ids=[],revoked_grant_ids=[])
    raw=record.canonical(envelope)
    assert record.verify_record(raw,trust,now=now)==payload
    malformed=json.loads(raw);malformed['payload']['evidence']['unknown']=True
    malformed['payload']['evidence_sha256']=hashlib.sha256(record.canonical(malformed['payload']['evidence'])).hexdigest()
    unsigned={k:v for k,v in malformed.items() if k!='signature'}
    malformed['signature']=base64.b64encode(key.sign(record.canonical(unsigned))).decode('ascii')
    try:
        record.verify_record(record.canonical(malformed),trust,now=now)
    except record.CandidateValidationRecordError:
        pass
    else:
        raise AssertionError('unknown signed evidence field accepted')
    output.mkdir(mode=0o700)
    for name,value in [('signed-record.json',envelope),('fixture-public-trust.json',trust)]:
        with (output/name).open('xb') as f:f.write(record.canonical(value))
    receipt=dict(CANDIDATE_VALIDATION_RECORD='PASS',UNKNOWN_RECORD_FIELD_REJECTED='PASS',
        record_id=payload['record_id'],record_sha256=hashlib.sha256(raw).hexdigest(),
        evidence_sha256=payload['evidence_sha256'],candidate=evidence['binding'],
        image_id=evidence['image_id'],trust_scope='HOSTED_RECORD_CONTRACT_FIXTURE_ONLY',
        production_trust_changed=False,production_approval=False,private_key_persisted=False)
    with (output/'receipt.json').open('x',encoding='utf-8') as f:json.dump(receipt,f,sort_keys=True)
    return receipt


def main():
    parser=argparse.ArgumentParser();parser.add_argument('--evidence',type=Path,required=True)
    parser.add_argument('--output',type=Path,required=True);args=parser.parse_args()
    if sys.platform!='linux' or os.geteuid()!=0 or os.environ.get('RUNNER_ENVIRONMENT')!='github-hosted':
        raise RuntimeError('this evidence test requires the hosted Linux root fixture')
    evidence=json.loads(args.evidence.read_bytes())
    assert evidence['spread_runtime_packaging']['identity_kind']=='EPHEMERAL_CI_CANDIDATE_VALIDATION_ROOT'
    assert set(evidence['spread_runtime_packaging']['service_credential_mounts'].values())=={'PASS'}
    print(json.dumps(verify_actual_engine_record(evidence,args.output),sort_keys=True))


if __name__=='__main__':
    main()
