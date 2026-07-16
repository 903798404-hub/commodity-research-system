from __future__ import annotations

import unittest
from pathlib import Path


REPOSITORY = Path(__file__).resolve().parents[1]
USDA_ROOT = REPOSITORY / "11_独立应用" / "USDA平衡表"


class ProductionPackagingTests(unittest.TestCase):
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
