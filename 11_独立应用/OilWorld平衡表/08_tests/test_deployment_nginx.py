import json
import re
import unittest
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[1]
NGINX_CONFIG = PROJECT_ROOT / "deploy" / "nginx.conf"


def location_block(config: str, location: str) -> str:
    pattern = re.compile(
        rf"location\s+{re.escape(location)}\s*\{{(?P<body>.*?)\n\s*\}}",
        re.DOTALL,
    )
    match = pattern.search(config)
    if not match:
        raise AssertionError(f"missing Nginx location: {location}")
    return match.group("body")


class OilWorldNginxDeploymentTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.config = NGINX_CONFIG.read_text(encoding="utf-8")

    def test_all_data_locations_use_dedicated_json_404(self) -> None:
        locations = (
            "= /oil-world/data/oil_world/latest.json",
            "= /oil-world/data/oil_world/releases.json",
            "^~ /oil-world/data/oil_world/releases/",
            "^~ /oil-world/data/oil_world/comparisons/",
            "^~ /oil-world/data/oil_world/",
        )
        for location in locations:
            with self.subTest(location=location):
                block = location_block(self.config, location)
                self.assertIn(
                    "error_page 404 = @oil_world_json_not_found;", block
                )

    def test_json_404_handler_returns_valid_json_with_404_status(self) -> None:
        block = location_block(self.config, "@oil_world_json_not_found")
        self.assertIn("default_type application/json;", block)
        self.assertIn('add_header Cache-Control "no-store" always;', block)
        self.assertIn(
            'add_header X-Content-Type-Options "nosniff" always;', block
        )
        match = re.search(r"return\s+(\d+)\s+'([^']+)'\s*;", block)
        self.assertIsNotNone(match)
        self.assertEqual(match.group(1), "404")
        self.assertEqual(json.loads(match.group(2)), {"error": "not_found"})
        self.assertNotIn("index.html", block)

    def test_asset_404_does_not_use_json_handler(self) -> None:
        block = location_block(self.config, "^~ /oil-world/assets/")
        self.assertIn("try_files $uri =404;", block)
        self.assertNotIn("@oil_world_json_not_found", block)

    def test_spa_routes_still_fall_back_to_index(self) -> None:
        block = location_block(self.config, "/oil-world/")
        self.assertIn(
            "try_files $uri $uri/ /oil-world/index.html;", block
        )
        self.assertNotIn("@oil_world_json_not_found", block)

    def test_valid_json_cache_policies_remain_unchanged(self) -> None:
        expected = {
            "= /oil-world/data/oil_world/latest.json": "no-store",
            "= /oil-world/data/oil_world/releases.json": "no-cache",
            "^~ /oil-world/data/oil_world/releases/": (
                "public, max-age=31536000, immutable"
            ),
            "^~ /oil-world/data/oil_world/comparisons/": (
                "public, max-age=31536000, immutable"
            ),
        }
        for location, cache_control in expected.items():
            with self.subTest(location=location):
                block = location_block(self.config, location)
                self.assertIn(
                    f'add_header Cache-Control "{cache_control}" always;', block
                )


if __name__ == "__main__":
    unittest.main()
