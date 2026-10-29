"""The Hosted fixture must resume its own writer even when native copying fails."""
import importlib.util
from pathlib import Path
from types import SimpleNamespace

import pytest


def fixture_module():
    path = Path(__file__).with_name('release_canonicalization_docker_e2e.py')
    spec = importlib.util.spec_from_file_location('canonicalization_fixture_test', path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.mark.parametrize('failure', [None, 'copy', 'paused_observation'])
def test_quiescence_preserves_native_checks_and_always_resumes(monkeypatch, failure):
    module = fixture_module()
    paused, calls = [False], []
    policy = dict(approved_commit='synthetic', recovery=dict(
        production_container_id='owned-fixture', production_observation_sha256='stable'))
    receipt = dict(evidence_class='HOSTED_SYNTHETIC_TRUST_NOT_PRODUCTION_REHEARSAL',
        synthetic_commit='synthetic', image_adapter=dict(image_id='fixture-image'))

    def inspect(cid):
        assert cid == 'owned-fixture'
        if paused[0] and failure == 'paused_observation':
            raise RuntimeError('observation failed')
        return dict(Image='fixture-image', State=dict(Running=True, Paused=paused[0]))

    def command(*args):
        assert args in [('docker', 'pause', 'owned-fixture'),
                        ('docker', 'unpause', 'owned-fixture')]
        calls.append(args[1])
        paused[0] = args[1] == 'pause'

    def native_copy(host, actual_policy):
        assert actual_policy is policy and paused[0]
        calls.append('native-copy')
        if failure == 'copy':
            raise RuntimeError('production source changed during copy')
        return {'production_before': 'same', 'production_after': 'same'}

    host = SimpleNamespace(docker_inspect=inspect)
    recovery = SimpleNamespace(production_identity=lambda *_: 'stable', build_sandbox=native_copy)
    monkeypatch.setattr(module, 'run', command)
    if failure:
        with pytest.raises(RuntimeError):
            module.copy_quiesced_fixture(host, recovery, policy, receipt)
        assert 'synthetic_copy_quiescence' not in receipt
    else:
        assert module.copy_quiesced_fixture(host, recovery, policy, receipt)['production_before'] == 'same'
        assert receipt['synthetic_copy_quiescence']['resumed']
    assert calls == (['pause', 'unpause'] if failure == 'paused_observation'
                     else ['pause', 'native-copy', 'unpause'])
    assert paused[0] is False


def test_quiescence_rejects_non_synthetic_scope_before_docker(monkeypatch):
    module = fixture_module()
    monkeypatch.setattr(module, 'run', lambda *_: pytest.fail('must not touch Docker'))
    with pytest.raises(AssertionError):
        module.copy_quiesced_fixture(None, None, {}, dict(evidence_class='PRODUCTION'))
