from __future__ import annotations

import unittest
from pathlib import Path

import yaml


REPOSITORY = Path(__file__).resolve().parents[1]
USDA_ROOT = REPOSITORY / "11_独立应用" / "USDA平衡表"


class ProductionPackagingTests(unittest.TestCase):
    def test_windows_local_docker_is_not_a_release_gate(self) -> None:
        required_rules = (
            "Windows 本地没有 Docker、Podman 或 WSL 属于正常状态",
            "本地不负责生产镜像构建",
            "不得再建议用户安装 Docker Desktop、Podman 或 WSL",
            "正式部署直接使用同一个镜像 ID",
        )
        for relative_path in ("AGENTS.md", "07_docs/开发与部署工作流.md"):
            content = (REPOSITORY / relative_path).read_text(encoding="utf-8")
            for rule in required_rules:
                self.assertIn(rule, content)

    def test_spread_image_build_context_includes_home_and_catalog(self) -> None:
        dockerfile = (REPOSITORY / "Dockerfile").read_text(encoding="utf-8")

        self.assertIn("COPY 02_configs /app/02_configs", dockerfile)
        self.assertIn("COPY 05_apps /app/05_apps", dockerfile)
        self.assertTrue((REPOSITORY / "02_configs" / "report_catalog.yaml").is_file())
        self.assertTrue((REPOSITORY / "05_apps" / "home.py").is_file())

    def test_spread_compose_requires_an_explicit_release_image(self) -> None:
        compose = yaml.safe_load(
            (REPOSITORY / "docker-compose.yml").read_text(encoding="utf-8")
        )
        spread = compose["services"]["spread-dashboard"]

        self.assertEqual(compose["name"], "market-data")
        self.assertEqual(
            spread["image"],
            "${SPREAD_IMAGE:?SPREAD_IMAGE must be set to an immutable release tag}",
        )
        self.assertIn("build", spread)

    def test_spread_image_requires_explicit_dashboard_urls(self) -> None:
        compose = yaml.safe_load(
            (REPOSITORY / "docker-compose.yml").read_text(encoding="utf-8")
        )
        environment = compose["services"]["spread-dashboard"]["environment"]

        self.assertEqual(
            environment["USDA_DASHBOARD_URL"],
            "${USDA_DASHBOARD_URL:?USDA_DASHBOARD_URL must be explicitly set}",
        )
        self.assertEqual(
            environment["OIL_WORLD_DASHBOARD_URL"],
            "${OIL_WORLD_DASHBOARD_URL:?OIL_WORLD_DASHBOARD_URL must be explicitly set}",
        )

    def test_spread_config_is_the_only_excel_allowed_into_main_image(self) -> None:
        config_path = REPOSITORY / "02_configs" / "historical_spread_config.xlsx"
        self.assertTrue(config_path.is_file())

        rules = [
            line.strip()
            for line in (REPOSITORY / ".dockerignore").read_text(encoding="utf-8").splitlines()
            if line.strip() and not line.lstrip().startswith("#")
        ]
        self.assertIn("*.xlsx", rules)
        self.assertIn("!02_configs/historical_spread_config.xlsx", rules)
        self.assertGreater(
            rules.index("!02_configs/historical_spread_config.xlsx"),
            rules.index("*.xlsx"),
        )
        self.assertEqual(
            [rule for rule in rules if rule.startswith("!") and rule.endswith(".xlsx")],
            ["!02_configs/historical_spread_config.xlsx"],
        )

    def test_spread_image_declares_immutable_release_identity(self) -> None:
        dockerfile = (REPOSITORY / "Dockerfile").read_text(encoding="utf-8")
        for argument in (
            "MARKET_DATA_GIT_HEAD",
            "MARKET_DATA_RELEASE_ID",
            "MARKET_DATA_BUILD_TIME",
            "MARKET_DATA_SOURCE",
        ):
            self.assertIn(f"ARG {argument}", dockerfile)
        for label in (
            "org.opencontainers.image.revision",
            "org.opencontainers.image.version",
            "org.opencontainers.image.created",
            "org.opencontainers.image.source",
        ):
            self.assertIn(label, dockerfile)
        self.assertIn("/app/RELEASE.json", dockerfile)
        self.assertIn("chmod 0444 /app/RELEASE.json", dockerfile)

    def test_dynamic_data_and_sensitive_files_are_not_packaged(self) -> None:
        dockerfile = (REPOSITORY / "Dockerfile").read_text(encoding="utf-8")
        rules = {
            line.strip()
            for line in (REPOSITORY / ".dockerignore")
            .read_text(encoding="utf-8")
            .splitlines()
            if line.strip() and not line.lstrip().startswith("#")
        }

        self.assertNotIn("COPY 01_data /app/01_data", dockerfile)
        self.assertNotIn("COPY 06_outputs /app/06_outputs", dockerfile)
        self.assertIn("mkdir -p /app/01_data /app/06_outputs /app/10_logs", dockerfile)
        for rule in (
            ".env",
            ".env.*",
            "**/.env",
            "**/.env.*",
            "**/*.pem",
            "**/*.key",
            "**/id_rsa*",
            "**/id_ed25519*",
            "01_data",
            "06_outputs",
            "10_logs",
        ):
            self.assertIn(rule, rules)

    def test_usda_root_redirect_remains_relative(self) -> None:
        nginx = (USDA_ROOT / "deploy" / "nginx.conf").read_text(encoding="utf-8")
        self.assertIn("absolute_redirect off;", nginx)
        self.assertIn("location = / {\n        return 302 /usda/;\n    }", nginx)
        self.assertIn("try_files $uri $uri/ /usda/index.html;", nginx)

    def test_usda_image_declares_traceability_labels(self) -> None:
        dockerfile = (USDA_ROOT / "Dockerfile").read_text(encoding="utf-8")
        for argument in ("OCI_REVISION", "OCI_SOURCE", "OCI_CREATED"):
            self.assertIn(f"ARG {argument}=", dockerfile)
        for label in (
            "org.opencontainers.image.revision=${OCI_REVISION}",
            "org.opencontainers.image.source=${OCI_SOURCE}",
            "org.opencontainers.image.created=${OCI_CREATED}",
        ):
            self.assertIn(label, dockerfile)


if __name__ == "__main__":
    unittest.main()
