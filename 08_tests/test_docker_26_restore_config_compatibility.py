"""Replay the bounded host restore delta; never rewrite signed raw identity."""
import copy
import json
from pathlib import Path
import sys

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / '09_deploy'))
from runtime_identity import host_authorization as host

PLATFORM = dict(ServerVersion='26.1.3', CgroupVersion='2', CgroupDriver='systemd')
DNS = ('Dns', 'DnsOptions', 'DnsSearch')

def payloads():
    # Derived regression payloads, not the original production inspect/receipt.
    before = json.loads((Path(__file__).parent / 'fixtures/runtime_config/production-e42-running.json').read_bytes())
    before['host_config']['OomKillDisable'] = False
    after = copy.deepcopy(before)
    after['host_config']['OomKillDisable'] = None
    for key in DNS:
        assert before['host_config'][key] is None
        after['host_config'][key] = []
    return before, after

def observed(after):
    return dict(after, actual_config_sha256=host._digest(after), container_id='a'*64,
                state=dict(Status='running', Running=True))

def test_exact_restore_preserves_both_raw_payloads_and_signed_hash(monkeypatch):
    before, after = payloads(); retained = copy.deepcopy((before, after))
    monkeypatch.setattr(host, '_run_docker', lambda args: host._canonical(PLATFORM))
    evidence = host.compare_observed_config(observed(after), host._digest(before))
    assert evidence['policy_raw_sha256'] == host._digest(before)
    assert evidence['actual_raw_sha256'] == host._digest(after)
    assert evidence['raw_hash_match'] is False and evidence['semantic_config_match'] is True
    assert evidence['compatibility_rule'] == 'DOCKER26_RESTORE_OOM_DNS_DEFAULTS'
    assert evidence['normalized_fields'] == ['HostConfig.OomKillDisable', *('HostConfig.'+k for k in DNS)]
    assert (before, after) == retained

@pytest.mark.parametrize('field', DNS)
@pytest.mark.parametrize('value', [None, '[]', ['8.8.8.8'], 0, False])
def test_dns_group_requires_explicit_empty_arrays(field, value, monkeypatch):
    before, after = payloads(); after['host_config'][field] = value
    monkeypatch.setattr(host, '_run_docker', lambda args: host._canonical(PLATFORM))
    with pytest.raises(host.HostAuthorizationError):
        host.compare_observed_config(observed(after), host._digest(before))

@pytest.mark.parametrize('section,field,value', [
    ('config', 'User', '0:0'), ('config', 'Hostname', 'other'),
    ('config', 'Env', ['SECRET=changed']), ('host_config', 'Memory', 123),
    ('host_config', 'NetworkMode', 'other'), ('host_config', 'ReadonlyRootfs', False),
])
def test_additional_change_cannot_be_hidden(section, field, value, monkeypatch):
    before, after = payloads(); after[section][field] = value
    monkeypatch.setattr(host, '_run_docker', lambda args: pytest.fail('changed config must fail before platform query'))
    with pytest.raises(host.HostAuthorizationError):
        host.compare_observed_config(observed(after), host._digest(before))

@pytest.mark.parametrize('platform', [dict(ServerVersion='28.0.4', CgroupVersion='2'),
    dict(ServerVersion='26.1.4', CgroupVersion='2'), dict(ServerVersion='26.1.3', CgroupVersion='1')])
def test_restore_is_not_a_cross_platform_exemption(platform, monkeypatch):
    before, after = payloads()
    monkeypatch.setattr(host, '_run_docker', lambda args: host._canonical(platform))
    with pytest.raises(host.HostAuthorizationError):
        host.compare_observed_config(observed(after), host._digest(before))

@pytest.mark.parametrize('state,cid', [(dict(Status='created', Running=False), 'a'*64),
    (dict(Status='running', Running=1), 'a'*64), ({}, 'a'*64),
    (dict(Status='running', Running=True), 'invalid')])
def test_restore_requires_real_running_instance(state, cid, monkeypatch):
    before, after = payloads(); obs = observed(after); obs.update(state=state, container_id=cid)
    monkeypatch.setattr(host, '_run_docker', lambda args: host._canonical(PLATFORM))
    with pytest.raises(host.HostAuthorizationError): host.compare_observed_config(obs, host._digest(before))

@pytest.mark.parametrize('field', DNS)
def test_missing_dns_is_not_explicit_null(field):
    before, after = payloads(); before['host_config'].pop(field)
    with pytest.raises(host.HostAuthorizationError):
        host.compare_config_payloads(before, after, policy_raw_sha256=host._digest(before),
            actual_raw_sha256=host._digest(after), platform=PLATFORM)

def test_reverse_transition_and_forged_binding_are_rejected(monkeypatch):
    before, after = payloads()
    monkeypatch.setattr(host, '_run_docker', lambda args: host._canonical(PLATFORM))
    with pytest.raises(host.HostAuthorizationError): host.compare_observed_config(observed(before), host._digest(after))
    with pytest.raises(host.HostAuthorizationError): host.compare_observed_config(observed(after), '0'*64)
