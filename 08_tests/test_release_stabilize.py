"""Preparation/pre-stop/issuance ordering. Real Linux evidence owns POSIX IO."""
import contextlib
import copy
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import stat
import sys
from types import SimpleNamespace

import pytest

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location('_stabilize_test_entry', ROOT/'09_deploy/spread_release/high_risk_execution.py')
execution = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = execution
spec.loader.exec_module(execution)
base = execution.load(ROOT, '08_tests/test_high_risk_execution.py', '_stabilize_original_tests')


def fixture(tmp_path, monkeypatch, *, mode=0o700, fault=None):
    root = tmp_path/'allocation';root.mkdir()
    parent = root/'deployment-secret';parent.mkdir()
    source = parent/'service.json';source.write_bytes(b'')
    states = {}
    for p in [parent, source]:
        real = p.lstat()
        states[str(p)] = SimpleNamespace(st_mode=(stat.S_IFDIR|mode) if p==parent else (stat.S_IFREG|0o440),
            st_dev=real.st_dev,st_ino=real.st_ino,st_uid=0,st_gid=0 if p==parent else 65532,st_size=0)
    if fault == 'owner':states[str(parent)].st_uid=1
    if fault == 'group':states[str(parent)].st_gid=1
    if fault == 'symlink':states[str(parent)].st_mode=stat.S_IFLNK|0o700
    if fault == 'nonempty':states[str(source)].st_size=10
    original = Path.lstat
    def lstat(p):return states.get(str(p)) or original(p)
    monkeypatch.setattr(Path,'lstat',lstat)
    def protected(p, **kw):
        if p.resolve()!=p or states.get(str(p),SimpleNamespace(st_mode=0)).st_mode & stat.S_IFLNK == stat.S_IFLNK:
            raise ValueError('aliased')
        return p
    issuer=SimpleNamespace(_protected_path=protected,_application_service_credential_path=lambda p,policy:protected(p),
        _application_service_secret_identity=lambda *a:None,_digest=lambda p:hashlib.sha256(json.dumps(p,sort_keys=True).encode()).hexdigest())
    policy=dict(production_storage_root=str(root),application_service={},mounts=[dict(target='/run/secrets/market-data-service.json',source=str(source),read_only=True)])
    return issuer,policy,states,parent,source


@pytest.mark.parametrize('fault',['mode','owner','group','symlink','nonempty','writable','outside','runtime-parent'])
def test_bad_bound_credential_actual_pre_stop_preserves_source(tmp_path,monkeypatch,fault):
    issuer,policy,states,parent,source=fixture(tmp_path,monkeypatch,mode=0o755 if fault=='mode' else 0o700,fault=fault)
    if fault=='writable':policy['mounts'][0]['read_only']=False
    if fault=='outside':policy['mounts'][0]['source']=str(tmp_path/'outside'/'service.json')
    if fault=='runtime-parent':policy['mounts'][0]['source']=str(parent.parent/'service.json')
    class ActualCredentialCheck(base.Backend):
        def preconditions(self, plan):
            self.hit('preconditions')
            execution.credential_preparation(issuer,policy,'65532:65532')
    backend=ActualCredentialCheck()
    result=base.run(base.sealed(tmp_path),backend)
    assert result['result']=='FAIL' and result['source']=='PRESERVED_NOT_STOPPED'
    assert 'stop_source' not in backend.calls and not any(c.startswith('create_') for c in backend.calls)
    assert source.read_bytes()==b''


@pytest.mark.parametrize('drift',['mode','parent-inode','file-inode','policy'])
def test_prepared_credential_drift_refused_before_formal_issuer(tmp_path,monkeypatch,drift):
    issuer,policy,states,parent,source=fixture(tmp_path,monkeypatch)
    initial=execution.credential_preparation(issuer,policy,'65532:65532')
    if drift=='mode':states[str(parent)].st_mode=stat.S_IFDIR|0o755
    elif drift=='parent-inode':states[str(parent)].st_ino+=1
    elif drift=='file-inode':states[str(source)].st_ino+=1
    else:policy['mounts'][0]['source']=str(parent.parent/'other.json')
    backend=execution.HostBackend.__new__(execution.HostBackend)
    calls=[]
    backend.sessions={'target':SimpleNamespace(host=issuer,prepared_policy=policy,prepared_user='65532:65532',
        credential_preparation=initial,prepare_authorization=lambda *a:calls.append('issuer'))}
    with pytest.raises((ValueError,FileNotFoundError)):
        backend.authorize(base.plan(),'target')
    assert calls==[] and source.read_bytes()==b''


def test_formal_before_stop_rechecks_parent_before_record_or_stop(tmp_path,monkeypatch):
    issuer,policy,states,parent,source=fixture(tmp_path,monkeypatch)
    initial=execution.credential_preparation(issuer,policy,'65532:65532')
    backend=execution.HostBackend.__new__(execution.HostBackend)
    backend.sessions={'target':SimpleNamespace(host=issuer,prepared_policy=policy,prepared_user='65532:65532',credential_preparation=initial)}
    states[str(parent)].st_mode=stat.S_IFDIR|0o755
    backend._browser_preflight=lambda *a:pytest.fail('must reject directory first')
    with pytest.raises(execution.ExecutionError,match='ROOT_PRIVATE'):backend.before_stop(base.plan())


@pytest.mark.parametrize('failure',['package','binary','launch','access'])
def test_browser_runner_failure_refused_by_formal_execution_before_stop(tmp_path,failure):
    class BrowserFailure(base.Backend):
        def before_stop(self,value):
            self.hit('record_window')
            raise execution.ExecutionError('BROWSER_'+failure.upper()+'_UNAVAILABLE')
    backend=BrowserFailure()
    result=base.run(base.sealed(tmp_path),backend)
    assert result['source']=='PRESERVED_NOT_STOPPED' and 'stop_source' not in backend.calls
    assert result['rollback']=='NOT_EXECUTED'


def test_timeline_preserves_actual_failed_stage_and_original_timeout(tmp_path):
    class TimeoutAfterRestoration(base.Backend):
        def accept(self,plan,role):
            self.hit('accept_'+role)
            if role=='target':raise RuntimeError('target failed')
            self.source_restored=True
            raise execution.ExecutionError('ROLLBACK_TIMEOUT')
    backend=TimeoutAfterRestoration()
    result=base.run(base.sealed(tmp_path),backend)
    assert result['result']=='FAIL' and result['rollback']=='FAIL'
    assert result['rollback_failure']=='ExecutionError: ROLLBACK_TIMEOUT'
    assert backend.source_restored and 'cleanup' not in backend.calls
    assert result['resources']=='RETAINED_FOR_RECOVERY'
    stages={i['step']:i for i in result['timeline']}
    assert stages['rollback_start']['status']=='PASS' and stages['rollback_accept']['status']=='FAIL'
    assert all(i['finished_monotonic_ns']>=i['started_monotonic_ns'] for i in result['timeline'])
    assert execution.record_consumption_windows(base.plan())==base.execution.record_consumption_windows(base.plan())


def test_no_identity_or_deadline_policy_replacement():
    source=(ROOT/'09_deploy/spread_release/high_risk_execution.py').read_text(encoding='utf-8')
    assert "signal.setitimer(signal.ITIMER_REAL, seconds)" in source
    assert "credential_issued=False" in source
    assert "phase='HTTP_HEALTH_RESTORED'" in source and "phase='COMPLETE_ACCEPTANCE'" in source
