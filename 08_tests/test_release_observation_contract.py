from __future__ import annotations

import copy
import ast
import base64
from datetime import datetime
import hashlib
import importlib.util
import json
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]


def load(path, name):
    spec = importlib.util.spec_from_file_location(name, ROOT / path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


host = load('09_deploy/runtime_identity/host_authorization.py', 'observation_host')
engine = load('04_scripts/runtime/validate_target_runtime.py', 'observation_engine')
obs = host._observation()
IMAGE = 'sha256:' + 'b' * 64
CID = 'a' * 64


def test_hosted_fixture_prepares_nested_readonly_parent_mountpoints(tmp_path):
    fixture = load('08_tests/shared/release_canonicalization_docker_e2e.py', 'hosted_mount_fixture')
    manifest = json.loads((ROOT / '02_configs/runtime_contracts/spread-production-runtime.json').read_bytes())
    identity = tmp_path / 'identity'
    identity.mkdir()
    sources = {manifest['identity_root_role']: identity}
    fixture.prepare_nested_mountpoints(manifest, sources)
    fixture.prepare_nested_mountpoints(manifest, sources)  # idempotent setup
    assert (identity / 'weather').is_dir()
    assert (identity / 'import-profit/operational/cnf').is_dir()
    assert not list(identity.rglob('*.*'))


def test_hosted_recovery_render_uses_formal_sandbox_sources_and_keeps_access():
    fixture = load('08_tests/shared/release_canonicalization_docker_e2e.py', 'hosted_recovery_fixture')
    old = [dict(source='/baseline/state', target='/runtime/snapshot', read_only=True),
           dict(source='/baseline/state', target='/runtime/capture', read_only=False)]
    projected = [dict(item, source='/sandbox/state') for item in old]
    values = dict(SNAPSHOT='/baseline/state', CAPTURE='/baseline/state', SECRET='/test/secret')
    assert fixture.recovery_environment(values, old, projected) == dict(
        SNAPSHOT='/sandbox/state', CAPTURE='/sandbox/state', SECRET='/test/secret')
    assert values['SNAPSHOT'] == '/baseline/state'
    wrong_access = copy.deepcopy(projected)
    wrong_access[0]['read_only'] = False
    with pytest.raises(AssertionError):
        fixture.recovery_environment(values, old, wrong_access)


@pytest.mark.parametrize('kind,identity', [('image', IMAGE), ('container', CID)])
@pytest.mark.parametrize('raw', [b'[]', b'[{},{}]', b'[null]', b'{}', b'[{"Id":"wrong"}]',
    b'[{"Id":"a","Id":"b"}]', b'[{"Id":NaN}]', b'[{"Id":Infinity}]', b'[{"Id":1e999}]'])
def test_formal_inspect_boundary_rejects_bad_cli_stdout(monkeypatch, kind, identity, raw):
    monkeypatch.setattr(host, '_run_docker', lambda *args, **kwargs: raw)
    adapter = host.docker_image_inspect if kind == 'image' else host.docker_inspect
    with pytest.raises(host.HostAuthorizationError):
        adapter(identity)
    monkeypatch.setattr(engine, '_docker', lambda *a, **k: type('CLI', (), {'stdout': raw})())
    with pytest.raises(engine.ValidationError):
        engine.inspect_one(kind, identity)


@pytest.mark.parametrize('kind,identity', [('image', IMAGE), ('container', CID)])
def test_adapters_preserve_complete_raw_object(monkeypatch, kind, identity):
    value = dict(Id=identity, Config={'User': '65532:65532'}, Unprojected={'kept': True})
    raw = json.dumps([value]).encode()
    monkeypatch.setattr(host, '_run_docker', lambda *a, **k: raw)
    adapter = host.docker_image_inspect if kind == 'image' else host.docker_inspect
    assert adapter(identity) == value
    with pytest.raises(host.HostAuthorizationError, match='object'):
        host._json(raw)


def test_mount_projection_is_digest_compatible_and_not_a_source_substitution():
    raw = [dict(Type='bind', Source='/host/z', Destination='/runtime/z', RW=True),
           dict(Type='bind', Source='/host/a', Destination='/runtime/a', RW=False)]
    expected = [dict(source='/host/a', target='/runtime/a', read_only=True),
                dict(source='/host/z', target='/runtime/z', read_only=False)]
    assert host._mounts({'Mounts': raw}) == expected
    assert host._digest(host._mounts({'Mounts': raw})) == host._digest(expected)
    manifest = dict(runtime_roots=[dict(role='a', container_path='/runtime/a', access='ro'),
                                   dict(role='z', container_path='/runtime/z', access='rw')],
        required_mounts=[dict(role='a', container_path='/runtime/a', read_only=True),
                         dict(role='z', container_path='/runtime/z', read_only=False)])
    policy = dict(grant_container_directory='/run/grants', mounts=expected)
    assert obs.resolve_mount_interpretation(manifest, policy, expected, [])['runtime'][0]['role'] == 'a'
    changed = copy.deepcopy(expected); changed[0]['source'] = '/sandbox/not-approved'
    with pytest.raises(obs.ObservationError, match='deployment contract'):
        obs.resolve_mount_interpretation(manifest, policy, changed, [])


@pytest.mark.parametrize('value', ['false', 0, None, []])
def test_compose_permissions_are_not_coerced(value):
    with pytest.raises(obs.ObservationError):
        obs.compose_mounts([dict(type='bind', source='/a', target='/b', read_only=value)])


def test_fixed_old_signed_materials_keep_canonical_bytes_and_verification():
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey
    old = json.loads((ROOT / '08_tests/fixtures/runtime_config/legacy-signed-materials.json').read_bytes())
    record = load('09_deploy/runtime_identity/candidate_validation_record.py', 'fixed_old_record')
    raw = record.canonical(old['record'])
    assert hashlib.sha256(raw).hexdigest() == old['record_canonical_sha256']
    assert record.verify_record(raw, old['record_trust'], now=datetime.fromisoformat(old['record_now'])) == old['record']['payload']
    grant = load('03_src/agri_research_agent/shared/production_grant.py', 'fixed_old_grant')
    grant.validate_execution_grant_envelope(old['grant'])
    payload = host._canonical(old['grant']['payload'])
    assert hashlib.sha256(payload).hexdigest() == old['grant_payload_canonical_sha256']
    Ed25519PublicKey.from_public_bytes(base64.b64decode(old['public_key_base64'])).verify(
        base64.b64decode(old['grant']['signature']), payload)


@pytest.mark.parametrize('path,functions,shared_entry', [
    ('09_deploy/runtime_identity/host_authorization.py', ['docker_inspect', 'docker_image_inspect', '_json', '_mounts'], '_observe'),
    ('04_scripts/runtime/validate_target_runtime.py', ['inspect_one', '_strict_json', 'validate_source_compose'], '_interpret'),
    ('04_scripts/runtime/pre_release_runtime.py', ['assess_release', 'verify_rollback_assets'], 'docker_image_inspect'),
    ('04_scripts/runtime/routine_release.py', ['_check_mounts'], '_observe'),
    ('09_deploy/spread_release/release_contract.py', ['_load_docker_array', '_evidence_mounts'], 'interpret_observation'),
    ('09_deploy/spread_release/wait_for_service_ready.py', ['inspect'], 'interpret_observation'),
    ('09_deploy/runtime_identity/recovery_namespace.py', ['validate_instance'], '_observe'),
])
def test_core_boundaries_cannot_reintroduce_private_inspect_decoders(path, functions, shared_entry):
    source = (ROOT / path).read_text(encoding='utf-8')
    tree = ast.parse(source)
    concrete = [n for n in ast.walk(tree) if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))
                and n.name in functions and not (len(n.body) == 1 and isinstance(n.body[0], ast.Expr)
                    and isinstance(n.body[0].value, ast.Constant) and n.body[0].value.value is Ellipsis)]
    assert concrete
    for function in concrete:
        text = ast.get_source_segment(source, function)
        assert shared_entry in text
        assert not any(isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute)
                       and isinstance(n.func.value, ast.Name) and n.func.value.id == 'json'
                       and n.func.attr == 'loads' for n in ast.walk(function))


def test_assessment_tests_do_not_replace_the_formal_parser_with_json_loads():
    tree = ast.parse((ROOT / '08_tests/test_pre_release_runtime.py').read_text(encoding='utf-8'))
    for node in ast.walk(tree):
        if isinstance(node, ast.keyword) and node.arg == '_json':
            assert not (isinstance(node.value, ast.Attribute) and isinstance(node.value.value, ast.Name)
                        and node.value.value.id == 'json' and node.value.attr == 'loads')
        if isinstance(node, ast.Assign) and any(isinstance(t, ast.Attribute) and t.attr == '_json' for t in node.targets):
            assert not (isinstance(node.value, ast.Attribute) and isinstance(node.value.value, ast.Name)
                        and node.value.value.id == 'json' and node.value.attr == 'loads')
