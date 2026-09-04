"""Generic runtime manifest contract tests; no business service assumptions."""
from __future__ import annotations

from dataclasses import FrozenInstanceError
import json
from pathlib import Path

import pytest
from jsonschema import Draft202012Validator

from agri_research_agent.shared import runtime_manifest as manifest


PROBES = [
    "entrypoint_initialization", "runtime_identity", "dependencies", "runtime_paths",
    "mount_permissions", "missing_grant_rejected", "wrong_commit_rejected",
    "wrong_tree_rejected", "wrong_image_rejected", "wrong_service_rejected",
    "wrong_manifest_rejected", "preview_write_rejected", "release_mismatch_rejected",
]


def fixture(version: str = "runtime-manifest/1") -> dict:
    value = {
        "schema_version": version, "project_id": "fixture-project", "module_id": "fixture-module",
        "service_id": "fixture-service", "runtime_target": "production_container",
        "identity_kind": "oci_container", "build": {"dockerfile": "Dockerfile", "dockerignore": ".dockerignore",
            "dependency_contracts": ["requirements.txt"], "compose_sources": ["compose.yml"]},
        "entrypoint": ["python", "app.py"], "working_directory": "/app",
        "runtime_roots": [{"role": "marker", "container_path": "/runtime", "access": "ro"},
                          {"role": "data", "container_path": "/runtime/data", "access": "rw"}],
        "required_mounts": [{"role": "marker", "container_path": "/runtime", "read_only": True},
                             {"role": "data", "container_path": "/runtime/data", "read_only": False}],
        "required_environment": ["PATH"], "secret_references": [], "required_executables": ["python"],
        "required_python_modules": [],
        "production_policy": {"deployment_role": "production", "write_grant_required": True},
        "preview_policy": {"production_write": False, "production_rw_mounts": False},
        "validation_probes": PROBES,
    }
    if version == "runtime-manifest/2":
        value.update({"identity_root_role": "marker",
                      "initialization_commands": [{"name": "initialize", "argv": ["python", "init.py"]}],
                      "source_inputs": [{"path": "app.py", "role": "entrypoint"},
                                        {"path": "init.py", "role": "initialization"}]})
    return value


@pytest.mark.parametrize("version", ["runtime-manifest/1", "runtime-manifest/2"])
def test_parse_and_roundtrip_both_manifest_versions(version: str):
    parsed = manifest.parse_runtime_manifest(fixture(version))
    assert parsed.schema_version == version
    assert parsed.to_dict() == fixture(version)
    result = parsed.to_dict()
    result["project_id"] = "mutated"
    result["runtime_roots"][0]["access"] = "rw"
    assert parsed.to_dict()["project_id"] == "fixture-project"
    assert parsed.to_dict()["runtime_roots"][0]["access"] == "ro"


@pytest.mark.parametrize("version", ["runtime-manifest/1", "runtime-manifest/2"])
def test_manifest_schema_draft202012_accepts_fixture(version: str):
    schema = json.loads((Path(__file__).resolve().parents[2] / "02_configs" / "runtime_manifest.schema.json").read_text(encoding="utf-8"))
    Draft202012Validator.check_schema(schema)
    Draft202012Validator(schema).validate(fixture(version))


def test_loader_rejects_duplicate_json_keys(tmp_path: Path):
    path = tmp_path / "manifest.json"
    path.write_text('{"schema_version":"runtime-manifest/1","schema_version":"runtime-manifest/1"}', encoding="utf-8")
    with pytest.raises(manifest.ManifestValidationError):
        manifest.load_runtime_manifest(path)


@pytest.mark.parametrize(("field", "value"), [
    ("schema_version", "runtime-manifest/3"), ("entrypoint", "python"),
    ("working_directory", "/"),
    ("production_policy", {"deployment_role": "production", "write_grant_required": False}),
    ("preview_policy", {"production_write": True, "production_rw_mounts": False}),
    ("validation_probes", PROBES[:-1]),
])
def test_v1_structural_contract_rejects_invalid_values(field, value):
    data = fixture()
    data[field] = value
    with pytest.raises(manifest.ManifestValidationError):
        manifest.parse_runtime_manifest(data)


@pytest.mark.parametrize(("field", "value"), [
    ("identity_root_role", "data"),
    ("initialization_commands", []),
    ("initialization_commands", [{"name": "bad", "argv": "python init.py"}]),
    ("initialization_commands", [{"name": "x", "argv": ["python"]}, {"name": "x", "argv": ["sh"]}]),
    ("source_inputs", []),
    ("source_inputs", [{"path": "app.py", "role": "unknown"}]),
    ("source_inputs", [{"path": "../app.py", "role": "entrypoint"}]),
    ("source_inputs", [{"path": "app.py", "role": "initialization"}]),
    ("source_inputs", [{"path": "app.py", "role": "entrypoint"}, {"path": "APP.py", "role": "runtime_configuration"}]),
])
def test_v2_extensions_reject_invalid_values(field, value):
    data = fixture("runtime-manifest/2")
    data[field] = value
    with pytest.raises(manifest.ManifestValidationError):
        manifest.parse_runtime_manifest(data)


def test_v2_source_inputs_allow_runtime_configuration_and_require_entrypoint():
    data = fixture("runtime-manifest/2")
    data["source_inputs"].append({"path": "config.json", "role": "runtime_configuration"})
    assert manifest.parse_runtime_manifest(data).to_dict() == data
    data["source_inputs"] = [{"path": "init.py", "role": "initialization"}]
    with pytest.raises(manifest.ManifestValidationError):
        manifest.parse_runtime_manifest(data)


def test_v2_rejects_build_overlap_and_casefold_duplicates():
    data = fixture("runtime-manifest/2")
    data["source_inputs"] = [{"path": "Dockerfile", "role": "runtime_configuration"},
                             {"path": "APP.py", "role": "entrypoint"}, {"path": "app.py", "role": "initialization"}]
    with pytest.raises(manifest.ManifestValidationError):
        manifest.parse_runtime_manifest(data)


def test_v1_empty_roots_and_mounts_remain_structurally_compatible():
    data = fixture()
    data["runtime_roots"] = []
    data["required_mounts"] = []
    assert manifest.parse_runtime_manifest(data).to_dict() == data


@pytest.mark.parametrize("version", ["runtime-manifest/1", "runtime-manifest/2"])
def test_duplicate_json_keys_rejected_for_both_versions(tmp_path: Path, version: str):
    path = tmp_path / f"{version[-1]}.json"
    path.write_text('{"schema_version":"' + version + '","schema_version":"' + version + '"}', encoding="utf-8")
    with pytest.raises(manifest.ManifestValidationError):
        manifest.load_runtime_manifest(path)


def test_v2_identity_root_and_rw_descendant_rules():
    data = fixture("runtime-manifest/2")
    data["identity_root_role"] = "data"
    with pytest.raises(manifest.ManifestValidationError):
        manifest.parse_runtime_manifest(data)
    data = fixture("runtime-manifest/2")
    data["runtime_roots"][1]["container_path"] = "/other/data"
    data["required_mounts"][1]["container_path"] = "/other/data"
    with pytest.raises(manifest.ManifestValidationError):
        manifest.parse_runtime_manifest(data)


def test_v2_rejects_host_instance_fields_and_control_characters():
    data = fixture("runtime-manifest/2")
    data["image_id"] = "sha256:" + "a" * 64
    with pytest.raises(manifest.ManifestValidationError):
        manifest.parse_runtime_manifest(data)
    data = fixture("runtime-manifest/2")
    data["initialization_commands"][0]["argv"] = ["python\n"]
    with pytest.raises(manifest.ManifestValidationError):
        manifest.parse_runtime_manifest(data)


def test_v2_build_overlap_is_checked_independently():
    data = fixture("runtime-manifest/2")
    data["source_inputs"] = [{"path": "Dockerfile", "role": "entrypoint"}]
    with pytest.raises(manifest.ManifestValidationError):
        manifest.parse_runtime_manifest(data)


def test_parsed_contract_is_deeply_immutable_and_does_not_alias_input():
    data = fixture("runtime-manifest/2")
    parsed = manifest.parse_runtime_manifest(data)
    data["runtime_roots"][0]["access"] = "rw"
    data["initialization_commands"][0]["argv"].append("changed")
    with pytest.raises(FrozenInstanceError):
        parsed.runtime_roots[0].access = "rw"
    with pytest.raises(TypeError):
        parsed.production_policy["write_grant_required"] = False
    result = parsed.to_dict()
    result["build"]["dependency_contracts"].append("untracked.txt")
    result["initialization_commands"][0]["argv"].append("changed")
    assert parsed.to_dict() == fixture("runtime-manifest/2")


def test_command_syntax_is_not_an_initialization_or_authorization_claim():
    data = fixture("runtime-manifest/2")
    data["initialization_commands"][0]["argv"] = ["python", "--help"]
    parsed = manifest.parse_runtime_manifest(data)
    assert parsed.to_dict() == data
    assert not hasattr(parsed, "production_write_grant")


@pytest.mark.parametrize("path", ["/host/source.py", "../source.py", "a//b.py", "a/./b.py",
                                  "a\\b.py", "C:/source.py", "a*.py", "a b.py", "a\nb.py",
                                  "a./b.py", "NUL.py"])
def test_source_inputs_reject_cross_platform_aliases(path):
    data = fixture("runtime-manifest/2")
    data["source_inputs"][0]["path"] = path
    with pytest.raises(manifest.ManifestValidationError):
        manifest.parse_runtime_manifest(data)


@pytest.mark.parametrize("field,value", [("schema_version", []), ("production_policy", []),
                                        ("preview_policy", None), ("initialization_commands", True)])
def test_invalid_json_types_fail_with_contract_error(field, value):
    data = fixture("runtime-manifest/2")
    data[field] = value
    with pytest.raises(manifest.ManifestValidationError):
        manifest.parse_runtime_manifest(data)


def test_missing_fields_cross_version_fields_and_build_aliases_rejected():
    data = fixture("runtime-manifest/2")
    del data["source_inputs"]
    with pytest.raises(manifest.ManifestValidationError):
        manifest.parse_runtime_manifest(data)
    data = fixture()
    data["identity_root_role"] = "marker"
    with pytest.raises(manifest.ManifestValidationError):
        manifest.parse_runtime_manifest(data)
    data = fixture("runtime-manifest/2")
    data["build"]["dependency_contracts"] = ["DOCKERFILE"]
    with pytest.raises(manifest.ManifestValidationError):
        manifest.parse_runtime_manifest(data)


def test_non_json_values_and_recursive_input_rejected(tmp_path):
    data = fixture("runtime-manifest/2")
    data["entrypoint"] = ("python",)
    with pytest.raises(manifest.ManifestValidationError):
        manifest.parse_runtime_manifest(data)
    data = fixture("runtime-manifest/2")
    data["loop"] = data
    with pytest.raises(manifest.ManifestValidationError):
        manifest.parse_runtime_manifest(data)
    path = tmp_path / "manifest.json"
    path.write_text('{"unknown": NaN}', encoding="utf-8")
    with pytest.raises(manifest.ManifestValidationError):
        manifest.load_runtime_manifest(path)
