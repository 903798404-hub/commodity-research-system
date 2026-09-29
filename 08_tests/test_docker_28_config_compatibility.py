from __future__ import annotations

import copy
import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / '09_deploy'))
from runtime_identity import host_authorization as host

FIXTURE = Path(__file__).parent / 'fixtures/runtime_config/hosted-28.0.4-transition.json'
PLATFORM = dict(ServerVersion='28.0.4', CgroupVersion='2', CgroupDriver='systemd')


def payloads():
    fixture = json.loads(FIXTURE.read_bytes())
    return fixture, fixture['created'], fixture['running']


def compare(before, after, platform=PLATFORM, **hashes):
    return host.compare_config_payloads(before, after, platform=platform,
        policy_raw_sha256=hashes.get('hash_before', host._digest(before)),
        actual_raw_sha256=hashes.get('hash_after', host._digest(after)))


def test_real_hosted_28_transition_keeps_raw_hashes_and_exact_direction(monkeypatch):
    fixture, before, after = payloads()
    unchanged = copy.deepcopy((before, after))
    assert host._digest(before) == fixture['created_raw_sha256']
    assert host._digest(after) == fixture['running_raw_sha256']
    assert fixture['platform'] == PLATFORM
    normalized = copy.deepcopy(before)
    normalized['host_config']['OomKillDisable'] = None
    assert host._canonical(normalized) == host._canonical(after)
    monkeypatch.setattr(host, '_run_docker', lambda args: host._canonical(PLATFORM))
    observed = dict(after, actual_config_sha256=fixture['running_raw_sha256'],
        container_id=fixture['container_id'], state=dict(Status='running', Running=True))
    evidence = host.compare_observed_config(observed, fixture['created_raw_sha256'])
    assert evidence['raw_hash_match'] is False
    assert evidence['semantic_config_match'] is True
    assert evidence['compatibility_rule'] == 'OOM_KILL_DISABLE_FALSE_NULL_EQUIVALENCE'
    assert evidence['matched_platform'] == 'Docker 28.0.4 / cgroup 2'
    assert evidence['normalized_field'] == 'HostConfig.OomKillDisable'
    assert (before, after) == unchanged


@pytest.mark.parametrize('left,right', [(None, False), (False, True), (True, None),
    (True, False), (0, None), ('false', None), (False, 0), (False, 'false')])
def test_28_direction_and_types_not_general_false_null_equality(left, right):
    _, before, after = payloads()
    before['host_config']['OomKillDisable'] = left
    after['host_config']['OomKillDisable'] = right
    with pytest.raises(host.HostAuthorizationError):
        compare(before, after)


@pytest.mark.parametrize('side', ['created', 'running'])
def test_missing_oom_is_not_explicit_null(side):
    fixture, before, after = payloads()
    fixture[side]['host_config'].pop('OomKillDisable')
    with pytest.raises(host.HostAuthorizationError):
        compare(before, after)


@pytest.mark.parametrize('section,field,value', [
    ('config', 'User', '65533:65533'), ('config', 'User', '65532:65533'),
    ('config', 'Image', 'sha256:'+'a'*64),
    ('host_config', 'CapAdd', ['SYS_ADMIN']), ('host_config', 'CapDrop', []),
    ('host_config', 'SecurityOpt', []), ('host_config', 'ReadonlyRootfs', False),
    ('host_config', 'Memory', 123456), ('host_config', 'MemorySwap', 987654),
    ('host_config', 'OomScoreAdj', 123), ('config', 'Hostname', 'another-instance'),
])
def test_28_rejects_additional_configuration_change(section, field, value):
    _, before, after = payloads()
    assert after[section].get(field) != value
    after[section][field] = value
    with pytest.raises(host.HostAuthorizationError):
        compare(before, after)


@pytest.mark.parametrize('field,value', [('Source', '/different/source'),
    ('Target', '/different/target'), ('ReadOnly', False)])
def test_28_mount_source_target_access_remain_exact(field, value):
    _, before, after = payloads()
    mount = next(m for m in after['host_config']['Mounts'] if m['ReadOnly'] is True)
    mount[field] = value
    with pytest.raises(host.HostAuthorizationError):
        compare(before, after)


@pytest.mark.parametrize('side', ['before', 'after'])
def test_28_original_payload_integrity_precedes_normalization(side):
    _, before, after = payloads()
    with pytest.raises(host.HostAuthorizationError, match='raw hash'):
        compare(before, after, **{'hash_' + side: '0'*64})


@pytest.mark.parametrize('platform', [dict(ServerVersion='28.0.5', CgroupVersion='2'),
    dict(ServerVersion='28.0.4', CgroupVersion='1'),
    dict(ServerVersion='28.0.4', CgroupVersion=2),
    dict(ServerVersion='28.1.0', CgroupVersion='2'), None])
def test_28_platform_is_exact_not_a_version_range(platform):
    _, before, after = payloads()
    with pytest.raises(host.HostAuthorizationError):
        compare(before, after, platform)
    assert compare(after, after, platform)['raw_hash_match'] is True


@pytest.mark.parametrize('state,cid', [(dict(Status='created', Running=False), 'a'*64),
    (dict(Status='running', Running=1), 'a'*64),
    (dict(Status='running', Running=True), 'wrong'), ({}, 'a'*64)])
def test_28_observation_requires_bound_running_instance(monkeypatch, state, cid):
    _, before, after = payloads()
    monkeypatch.setattr(host, '_run_docker', lambda args: host._canonical(PLATFORM))
    with pytest.raises(host.HostAuthorizationError):
        host.compare_observed_config(dict(after, actual_config_sha256=host._digest(after),
            container_id=cid, state=state), host._digest(before))
