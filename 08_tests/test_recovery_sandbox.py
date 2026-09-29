"""Permanent isolated-recovery contract regressions; no production inputs."""
import copy
import importlib.util
import os
from pathlib import Path
import stat
from types import SimpleNamespace

import pytest

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location('sandbox_recovery', ROOT/'09_deploy/runtime_identity/recovery_namespace.py')
m = importlib.util.module_from_spec(spec); spec.loader.exec_module(m)


class Rejected(ValueError): pass


@pytest.fixture
def case(tmp_path, monkeypatch):
    parent = tmp_path/'sandboxes'; parent.mkdir()
    live = tmp_path/'production'; live.mkdir()
    rw = live/'state'; rw.mkdir(); (rw/'current').write_bytes(b'production')
    ro = live/'data'; ro.mkdir()
    nonce = 'a'*32; root = parent/nonce; root.mkdir()
    isolated = root/'root-0'; isolated.mkdir(); (isolated/'current').write_bytes(b'production')
    monkeypatch.setattr(m, 'SANDBOX_PARENT', parent)
    h = SimpleNamespace(HostAuthorizationError=Rejected,
        _absolute=lambda x:x, _within=lambda a,b:Path(a)==Path(b) or Path(b) in Path(a).parents,
        _protected_path=lambda p,**kw:p)
    old={'mounts':[{'source':str(rw),'target':'/runtime/write','read_only':False},
        {'source':str(rw),'target':'/runtime/read','read_only':True},
        {'source':str(ro),'target':'/runtime/data','read_only':True}]}
    p={'role':'production','recovery':{'nonce':nonce,'sandbox':{'mode':'isolated-rehearsal','root':str(root),
        'roots':[{'production_source':str(rw),'sandbox_source':str(isolated),
            'initialization':'COPY_CURRENT_STATE_REQUIRED','uid':65532,'gid':65532,'mode':0o700}]}}}
    # Filesystem-free structural tests remain genuine scope/mount checks;
    # physical tree safety has independent link/inode tests below.
    return h,p,old,root,rw,isolated


@pytest.mark.parametrize('fault',[None,'live-source','outside','missing-root','extra-root',
    'wrong-mode','wrong-purpose','candidate-role','duplicate','unknown-field'])
def test_only_explicit_complete_sandbox_scope(case,fault):
    h,p,old,root,rw,dst=case; s=p['recovery']['sandbox']; i=s['roots'][0]
    if fault=='live-source':i['sandbox_source']=str(rw)
    elif fault=='outside':s['root']=str(root.parent.parent/'escape')
    elif fault=='missing-root':s['roots']=[]
    elif fault=='extra-root':s['roots'].append(dict(i,production_source='/extra',sandbox_source=str(root/'root-1')))
    elif fault=='wrong-mode':i['mode']=0o777
    elif fault=='wrong-purpose':s['mode']='test-bypass'
    elif fault=='candidate-role':p['role']='candidate_validation'
    elif fault=='duplicate':s['roots'].append(copy.deepcopy(i))
    elif fault=='unknown-field':i['skip_source_check']=True
    if fault:
        with pytest.raises(Rejected):
            m.sandbox_mounts(h,p,old,check_files=False)
    else:
        mounts=m.sandbox_mounts(h,p,old,check_files=False)
        assert [v['target'] for v in mounts]==[v['target'] for v in old['mounts']]
        assert [v['read_only'] for v in mounts]==[v['read_only'] for v in old['mounts']]
        assert mounts[0]['source']==mounts[1]['source']==str(dst)
        assert mounts[2]==old['mounts'][2]


@pytest.mark.parametrize('fault',['symlink','hardlink','special'])
def test_tree_rejects_physical_aliases(case,fault):
    h,p,old,root,rw,dst=case
    if fault=='symlink':
        # Windows development does not require symlink creation privileges.
        # Exercise the same rejection with an actual hardlink when unavailable.
        try:(dst/'alias').symlink_to(rw/'current')
        except OSError:os.link(rw/'current',dst/'alias')
    elif fault=='hardlink':os.link(rw/'current',dst/'alias')
    else:
        # Non-regular mode observed by lstat, without creating a host device.
        original=Path.lstat
        def lstat(path,*a,**kw):
            if path==dst/'current':return SimpleNamespace(st_mode=stat.S_IFIFO)
            return original(path,*a,**kw)
        from unittest.mock import patch
        with patch.object(Path,'lstat',lstat),pytest.raises(Rejected):m.tree_identity(h,dst)
        return
    with pytest.raises(Rejected):m.tree_identity(h,dst)


def test_byte_owned_sandbox_writes_leave_production_unchanged(case):
    h,p,old,root,rw,dst=case
    before=m.tree_identity(h,rw)
    (dst/'current').write_bytes(b'sandbox normal runtime write')
    (dst/'new').write_bytes(b'isolated')
    assert m.tree_identity(h,rw)==before
    assert (dst/'current').stat().st_ino!=(rw/'current').stat().st_ino


@pytest.mark.parametrize('fault',['rw-to-ro','ro-to-rw','target','missing','extra','arbitrary-source'])
def test_exact_projected_mounts_reject_all_other_deltas(case,fault):
    h,p,old,root,rw,dst=case
    expected=m.sandbox_mounts(h,p,old,check_files=False)
    actual=copy.deepcopy(expected)
    if fault=='rw-to-ro':actual[0]['read_only']=True
    elif fault=='ro-to-rw':actual[1]['read_only']=False
    elif fault=='target':actual[0]['target']='/different'
    elif fault=='missing':actual.pop()
    elif fault=='extra':actual.append(dict(actual[0],target='/extra'))
    else:actual[0]['source']='/arbitrary'
    with pytest.raises(Rejected):m.validate_mount_projection(h,actual,expected,'/run/market-data-grants')


@pytest.mark.parametrize('fault',['scope-hash','outside-write','candidate-role','production-changed','coverage'])
def test_signed_grant_scope_and_preservation_facts_cannot_be_substituted(case,monkeypatch,fault):
    import hashlib,json
    h,p,old,root,rw,dst=case
    h._digest=lambda v:hashlib.sha256(json.dumps(v,sort_keys=True).encode()).hexdigest()
    h._container_id=lambda v:v; h._HEX64=__import__('re').compile('[0-9a-f]{64}')
    p['mounts']=m.sandbox_mounts(h,p,old,check_files=False)
    p.update(actual_config_sha256='config',rendered_compose_sha256='compose')
    monkeypatch.setattr(m,'retained',lambda *a,**k:(old,{}))
    monkeypatch.setattr(m,'grant_id',lambda *a:'bound-scope')
    grant={'role':'production','authorization_mode':'production','container_id':'c'*64,
        'grant_id':'bound-scope','mount_contract_sha256':h._digest(p['mounts']),
        'actual_config_sha256':'config','rendered_compose_sha256':'compose','writable_roots':['/runtime/write']}
    state=m.tree_identity(h,rw); isolated=m.tree_identity(h,dst)
    data={'production_before':{str(rw):state},'production_after':{str(rw):state},
        'sandbox_before':{str(dst):isolated},'sandbox_after':{str(dst):isolated}}
    if fault=='scope-hash':grant['mount_contract_sha256']='tampered'
    elif fault=='outside-write':grant['writable_roots'].append('/public-current')
    elif fault=='candidate-role':grant['role']='candidate_validation'
    elif fault=='production-changed':data['production_before']={str(rw):{'changed':True}}
    else:data['sandbox_after']={}
    with pytest.raises(Rejected):m.verify_sandbox_evidence(h,p,grant,data)
