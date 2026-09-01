"""Build a local-only Tankan soybean CNF cache for FINAL_UI_PREVIEW.

This command opens a PostgreSQL session with read-only transaction settings and
writes only the explicitly supplied local preview output.  It never touches a
Public, Runtime, Snapshot, Candidate, Current, or Release path.
"""

from __future__ import annotations

import argparse
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import sys

import pandas as pd
import psycopg
from psycopg.rows import dict_row


ROOT = Path(__file__).resolve().parents[2]
SRC = ROOT / "03_src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from agri_research_agent.data_sources.tankan.client import (  # noqa: E402
    TankanConnectionSettings,
)


SOURCE_IDENTITY = "tankan:quanyong.market.soybean_param:cnf"
ORIGINS = {
    "e5b7b4e8a5bf": ("巴西", "brazil"),
    "e7be8ee6b9be": ("美湾", "us_gulf"),
    "e7be8ee8a5bf": ("美西", "us_pnw"),
    "e998bfe6a0b9e5bbb7": ("阿根廷", "argentina"),
}


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--environment", required=True)
    parser.add_argument("--secret-file", required=True, type=Path)
    parser.add_argument("--output", required=True, type=Path)
    args = parser.parse_args()
    if args.environment != "TEST_ISOLATED_NON_PRODUCTION":
        parser.error("preview cache requires TEST_ISOLATED_NON_PRODUCTION")

    settings = TankanConnectionSettings.from_secret_file(args.secret_file)
    try:
        with psycopg.connect(
            host=settings.host,
            port=settings.port,
            dbname=settings.database,
            user=settings.user,
            password=settings.password,
            autocommit=False,
            row_factory=dict_row,
            options=(
                "-c default_transaction_read_only=on "
                "-c statement_timeout=120000 -c lock_timeout=3000"
            ),
        ) as connection:
            connection.read_only = True
            with connection.cursor() as cursor:
                cursor.execute("SHOW transaction_read_only")
                proof = next(iter(cursor.fetchone().values()))
                if proof != "on":
                    raise RuntimeError("Tankan transaction is not read-only")
                cursor.execute(
                    """
SELECT trade_date, encode(region::bytea, 'hex') AS region_hex,
       month, cnf, updated_at
FROM market.soybean_param
ORDER BY trade_date, region, month
"""
                )
                rows = cursor.fetchall()
            connection.rollback()
    finally:
        settings.clear_password()

    frame = pd.DataFrame(rows)
    frame["region"] = frame["region_hex"].map(
        {key: value[0] for key, value in ORIGINS.items()}
    )
    frame["origin"] = frame["region_hex"].map(
        {key: value[1] for key, value in ORIGINS.items()}
    )
    if frame["origin"].isna().any():
        raise RuntimeError("unexpected Tankan soybean origin")
    frame["source_identity"] = SOURCE_IDENTITY
    output = args.output.resolve()
    output.parent.mkdir(parents=True, exist_ok=True)
    frame.to_parquet(output, index=False)
    digest = hashlib.sha256(output.read_bytes()).hexdigest()
    manifest = {
        "schema_version": "final-ui-preview-cnf-cache-v1",
        "environment": args.environment,
        "source_identity": SOURCE_IDENTITY,
        "database": settings.database,
        "schema": "market",
        "table": "soybean_param",
        "price_field": "cnf",
        "record_count": len(frame),
        "first_date": frame["trade_date"].min().isoformat(),
        "latest_date": frame["trade_date"].max().isoformat(),
        "cache_sha256": digest,
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "transaction_read_only": "on",
    }
    output.with_suffix(".manifest.json").write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    print(json.dumps(manifest, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
