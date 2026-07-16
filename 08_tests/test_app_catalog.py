from __future__ import annotations

import json
import re
import unittest
from pathlib import Path, PurePosixPath
from typing import Any
from urllib.parse import urlparse

import yaml


ROOT = Path(__file__).resolve().parents[1]
CATALOG_PATH = ROOT / "02_configs" / "app_catalog.yaml"
HANDBOOK_PATH = ROOT / "07_docs" / "农产品研究系统更新手册.md"
WORKFLOW_PATH = ROOT / "07_docs" / "开发与部署工作流.md"

EXPECTED_APP_IDS = {"main_dashboard", "usda_dashboard", "oil_world_dashboard"}
ALLOWED_STATUSES = {"production", "pending_verification", "retired"}
REQUIRED_FIELDS = {
    "app_id",
    "display_name",
    "status",
    "project_path",
    "application_type",
    "production_url",
    "container_name",
    "host_port",
    "container_port",
    "healthcheck",
    "data_inputs",
    "runtime_data",
    "release_data",
    "logs",
    "update_method",
    "update_command",
    "deploy_method",
    "source_revision_policy",
    "retention_policy",
    "dependencies",
    "notes",
}
FORBIDDEN_KEY_PARTS = {
    "password",
    "token",
    "cookie",
    "private_key",
    "secret",
    "api_key",
    "credential",
}
DOCUMENTED_LOCAL_PATHS = (
    "02_configs/app_catalog.yaml",
    "docker-compose.yml",
    "04_scripts/update_basis_data.py",
    "04_scripts/server_update_spreads.py",
    "07_docs/开发与部署工作流.md",
    "07_docs/农产品研究系统更新手册.md",
    "11_独立应用/USDA平衡表/configs/usda_report_version.json",
    "11_独立应用/USDA平衡表/scripts/buildData.ts",
    "11_独立应用/USDA平衡表/scripts/checkData.ts",
    "11_独立应用/USDA平衡表/scripts/compareSnapshots.ts",
    "11_独立应用/USDA平衡表/public/data/report_version.json",
    "11_独立应用/OilWorld平衡表/04_scripts/update_oil_world.py",
    "11_独立应用/OilWorld平衡表/public/data/oil_world/latest.json",
    "11_独立应用/OilWorld平衡表/public/data/oil_world/releases.json",
    "11_独立应用/OilWorld平衡表/deploy/compose.yml",
    "11_独立应用/OilWorld平衡表/deploy/healthcheck.sh",
)


def iter_keys(value: Any):
    if isinstance(value, dict):
        for key, child in value.items():
            yield str(key)
            yield from iter_keys(child)
    elif isinstance(value, list):
        for child in value:
            yield from iter_keys(child)


class ApplicationCatalogTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.catalog_text = CATALOG_PATH.read_text(encoding="utf-8")
        cls.catalog = yaml.safe_load(cls.catalog_text)
        cls.applications = cls.catalog["applications"]
        cls.handbook = HANDBOOK_PATH.read_text(encoding="utf-8")
        cls.workflow = WORKFLOW_PATH.read_text(encoding="utf-8")

    def test_yaml_parses_and_contains_exactly_three_formal_apps(self) -> None:
        self.assertIsInstance(self.catalog, dict)
        self.assertEqual(set(self.catalog["status_values"]), ALLOWED_STATUSES)
        app_ids = [app["app_id"] for app in self.applications]
        self.assertEqual(set(app_ids), EXPECTED_APP_IDS)
        self.assertEqual(len(app_ids), len(set(app_ids)))

    def test_required_fields_status_ports_urls_and_container_names(self) -> None:
        containers: list[str] = []
        for app in self.applications:
            self.assertFalse(REQUIRED_FIELDS - set(app), app["app_id"])
            self.assertIn(app["status"], ALLOWED_STATUSES)
            for field in ("host_port", "container_port"):
                self.assertIsInstance(app[field], int)
                self.assertGreaterEqual(app[field], 1)
                self.assertLessEqual(app[field], 65535)
            parsed = urlparse(app["production_url"])
            self.assertIn(parsed.scheme, {"http", "https"})
            self.assertTrue(parsed.hostname)
            self.assertIsNone(parsed.username)
            self.assertIsNone(parsed.password)
            self.assertEqual(parsed.port, app["host_port"])
            containers.append(app["container_name"])
        self.assertEqual(len(containers), len(set(containers)))

    def test_project_paths_are_existing_repository_relative_paths(self) -> None:
        root = ROOT.resolve()
        for app in self.applications:
            project_path = PurePosixPath(app["project_path"])
            self.assertFalse(project_path.is_absolute())
            self.assertNotIn("..", project_path.parts)
            resolved = (ROOT / Path(*project_path.parts)).resolve()
            self.assertTrue(resolved.is_relative_to(root))
            self.assertTrue(resolved.is_dir(), app["project_path"])
            self.assertTrue(PurePosixPath(app["server_project_path"]).is_absolute())

            repository_paths: list[str] = []
            repository_paths.extend(
                item["path"]
                for item in app["data_inputs"]
                if str(item.get("type", "")).startswith("repository") and "path" in item
            )
            repository_paths.extend(
                item["repository_path"] for item in app["runtime_data"] if "repository_path" in item
            )
            repository_paths.extend(item for item in app["release_data"] if isinstance(item, str))
            if "command_path" in app["healthcheck"]:
                repository_paths.append(app["healthcheck"]["command_path"])
            for repository_path in repository_paths:
                pure_path = PurePosixPath(repository_path)
                self.assertFalse(pure_path.is_absolute(), repository_path)
                self.assertNotIn("..", pure_path.parts, repository_path)

    def test_catalog_contains_no_secret_fields_or_user_specific_windows_path(self) -> None:
        for key in iter_keys(self.catalog):
            normalized = key.casefold()
            self.assertFalse(any(part in normalized for part in FORBIDDEN_KEY_PARTS), key)
        self.assertNotRegex(self.catalog_text, re.compile(r"[A-Za-z]:[\\/]Users[\\/]", re.I))

    def test_documented_local_paths_and_scripts_exist(self) -> None:
        for relative in DOCUMENTED_LOCAL_PATHS:
            path = ROOT / Path(*PurePosixPath(relative).parts)
            self.assertTrue(path.exists(), relative)

    def test_update_commands_reference_existing_scripts(self) -> None:
        apps = {app["app_id"]: app for app in self.applications}
        for command in apps["main_dashboard"]["update_command"].values():
            script = re.search(r"python\s+(/app/\S+\.py)", command)
            self.assertIsNotNone(script)
            self.assertTrue((ROOT / script.group(1).removeprefix("/app/")).is_file())

        oil_root = ROOT / apps["oil_world_dashboard"]["project_path"]
        for command in apps["oil_world_dashboard"]["update_command"].values():
            script = re.search(r"python\s+(\S+\.py)", command)
            self.assertIsNotNone(script)
            self.assertTrue((oil_root / script.group(1)).is_file())

        package = json.loads(
            (ROOT / apps["usda_dashboard"]["project_path"] / "package.json").read_text(encoding="utf-8")
        )
        for command in apps["usda_dashboard"]["update_command"].values():
            script_name = command.removeprefix("pnpm run ")
            self.assertIn(script_name, package["scripts"])

    def test_handbook_matches_catalog_and_documents_have_mutual_links(self) -> None:
        for app in self.applications:
            self.assertIn(app["display_name"], self.handbook)
            self.assertIn(app["production_url"], self.handbook)
            self.assertIn(f"`{app['container_name']}`", self.handbook)
            self.assertIn(f"| {app['host_port']} |", self.handbook)
        self.assertIn("开发与部署工作流.md", self.handbook)
        self.assertIn("农产品研究系统更新手册.md", self.workflow)


if __name__ == "__main__":
    unittest.main()
