from __future__ import annotations

import argparse
import datetime as dt
import json
import logging
import os
import shutil
import subprocess
import sys
from pathlib import Path

import pandas as pd
from filelock import FileLock, Timeout


DATABASE_NAME = "historical_spread_database.xlsx"
PARQUET_NAME = "historical_spread_database.parquet"
PRICE_LONG_NAME = "historical_price_long.xlsx"
CONFIG_NAME = "historical_spread_config.xlsx"
STATUS_NAME = "update_status.json"
LOCK_NAME = ".server_update.lock"
LOCK_TIMEOUT_SECONDS = 60


def project_root() -> Path:
    return Path(__file__).resolve().parents[1]


def now_text() -> str:
    return dt.datetime.now().astimezone().isoformat(timespec="seconds")


def setup_logger(log_file: Path) -> logging.Logger:
    logger = logging.getLogger("server_update_spreads")
    logger.setLevel(logging.INFO)
    logger.handlers.clear()
    formatter = logging.Formatter("%(asctime)s %(levelname)s %(message)s")
    file_handler = logging.FileHandler(log_file, encoding="utf-8")
    file_handler.setFormatter(formatter)
    logger.addHandler(file_handler)
    console_handler = logging.StreamHandler()
    console_handler.setFormatter(formatter)
    logger.addHandler(console_handler)
    return logger


def atomic_write_json(path: Path, payload: dict[str, object]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp_path = path.with_name(f"{path.name}.tmp")
    tmp_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    os.replace(tmp_path, path)


def initial_status(run_mode: str) -> dict[str, object]:
    return {
        "status": "running",
        "started_at": now_text(),
        "finished_at": "",
        "server_date": dt.date.today().isoformat(),
        "latest_date": "",
        "success_contracts": 0,
        "failure_contracts": 0,
        "required_contracts": 0,
        "failed_contracts": [],
        "price_long_backup": "",
        "spread_database_backup": "",
        "parquet_backup": "",
        "excel_exists": False,
        "parquet_exists": False,
        "excel_latest_date": "",
        "parquet_latest_date": "",
        "error_message": "",
        "run_mode": run_mode,
        "source": "akshare_futures_zh_daily_sina" if run_mode == "update_from_akshare" else "existing_local_data",
    }


def read_database_summary(database_file: Path) -> dict[str, object]:
    if database_file.suffix.lower() == ".parquet":
        spread_long = pd.read_parquet(database_file)
    else:
        spread_long = pd.read_excel(database_file, sheet_name="spread_long")
    spread_long["date"] = pd.to_datetime(spread_long["date"], errors="coerce")
    success_rows = spread_long[spread_long["status"] == "success"] if "status" in spread_long.columns else spread_long
    latest_date = success_rows["date"].max() if not success_rows.empty else pd.NaT
    return {
        "rows": int(len(spread_long)),
        "success_rows": int(len(success_rows)),
        "latest_date": "" if pd.isna(latest_date) else latest_date.strftime("%Y-%m-%d"),
    }


def backup_file(source: Path, backups_dir: Path, timestamp: str) -> Path | None:
    if not source.exists():
        return None
    backups_dir.mkdir(parents=True, exist_ok=True)
    backup = backups_dir / f"{source.stem}_{timestamp}{source.suffix}"
    shutil.copy2(source, backup)
    return backup


def restore_file(
    *,
    backup: Path | None,
    target: Path,
    existed_before: bool,
    logger: logging.Logger,
) -> None:
    if backup and backup.exists():
        shutil.copy2(backup, target)
        logger.info("restore_completed target=%s backup=%s", target, backup)
    elif not existed_before and target.exists():
        target.unlink()
        logger.info("restore_removed_new_file target=%s", target)
    else:
        logger.info("restore_skipped target=%s backup=%s existed_before=%s", target, backup, existed_before)


def run_script_args(script_args: list[str], root: Path, logger: logging.Logger) -> subprocess.CompletedProcess[str]:
    script_name = script_args[0]
    script_path = root / "04_scripts" / script_name
    command = [sys.executable, str(script_path), *script_args[1:]]
    logger.info("running_update_step=%s", " ".join(command))
    result = subprocess.run(command, cwd=root, text=True, capture_output=True, check=False)
    if result.stdout:
        logger.info("stdout_from_%s:\n%s", script_name, result.stdout)
    if result.stderr:
        logger.warning("stderr_from_%s:\n%s", script_name, result.stderr)
    logger.info("step_finished script=%s returncode=%s", script_name, result.returncode)
    return result


def validate_database_outputs(database_file: Path, parquet_file: Path, logger: logging.Logger) -> dict[str, object]:
    if not database_file.exists():
        raise FileNotFoundError(f"database Excel not found after update: {database_file}")
    if not parquet_file.exists():
        raise FileNotFoundError(f"database Parquet not found after update: {parquet_file}")
    excel_summary = read_database_summary(database_file)
    parquet_summary = read_database_summary(parquet_file)
    logger.info("excel_latest_date=%s", excel_summary["latest_date"])
    logger.info("parquet_latest_date=%s", parquet_summary["latest_date"])
    logger.info("excel_rows=%s", excel_summary["rows"])
    logger.info("parquet_rows=%s", parquet_summary["rows"])
    if excel_summary["latest_date"] != parquet_summary["latest_date"]:
        raise ValueError(
            f"Excel and Parquet latest_date mismatch: "
            f"{excel_summary['latest_date']} != {parquet_summary['latest_date']}"
        )
    if excel_summary["rows"] != parquet_summary["rows"]:
        raise ValueError(f"Excel and Parquet row count mismatch: {excel_summary['rows']} != {parquet_summary['rows']}")
    logger.info("excel_parquet_validation_completed=true")
    return {
        "excel": excel_summary,
        "parquet": parquet_summary,
    }


def read_json(path: Path) -> dict[str, object]:
    return json.loads(path.read_text(encoding="utf-8"))


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Safely update the spread database on a server.")
    parser.add_argument(
        "--update-from-akshare",
        action="store_true",
        help="Backfill missing exchange-dated futures closes with AkShare, then recalculate spreads.",
    )
    parser.add_argument(
        "--recalculate-from-existing-price-long",
        action="store_true",
        help="Recalculate spread outputs from the existing historical_price_long.xlsx.",
    )
    parser.add_argument("--dry-run", action="store_true", help="Preview AkShare updates without writing or recalculating.")
    parser.add_argument("--lock-timeout", type=int, default=LOCK_TIMEOUT_SECONDS)
    return parser.parse_args()


def run_mode_for_args(args: argparse.Namespace) -> str:
    if args.update_from_akshare:
        return "update_from_akshare"
    if args.recalculate_from_existing_price_long:
        return "recalculate_existing_price_long"
    return "safety_check"


def main() -> int:
    args = parse_args()
    root = project_root()
    data_dir = root / "01_data"
    logs_dir = root / "10_logs"
    backups_dir = data_dir / "backups"
    database_file = data_dir / DATABASE_NAME
    parquet_file = data_dir / PARQUET_NAME
    price_file = data_dir / PRICE_LONG_NAME
    config_file = root / "02_configs" / CONFIG_NAME
    status_file = data_dir / STATUS_NAME
    lock_file = data_dir / LOCK_NAME
    run_mode = run_mode_for_args(args)
    timestamp = dt.datetime.now().strftime("%Y%m%d_%H%M%S")
    log_file = logs_dir / f"server_update_spreads_{dt.date.today().strftime('%Y%m%d')}.log"
    result_file = data_dir / f".akshare_update_result_{timestamp}.json"
    logs_dir.mkdir(parents=True, exist_ok=True)
    data_dir.mkdir(parents=True, exist_ok=True)
    logger = setup_logger(log_file)
    status = initial_status(run_mode)
    lock = FileLock(str(lock_file))

    try:
        lock.acquire(timeout=max(0, args.lock_timeout))
        logger.info("lock_acquired=true lock_file=%s", lock_file)
    except Timeout:
        status.update(
            {
                "status": "skipped_locked",
                "finished_at": now_text(),
                "error_message": f"another update task holds {lock_file}",
            }
        )
        atomic_write_json(status_file, status)
        logger.error("lock_acquired=false lock_file=%s", lock_file)
        logger.info("status_file_written=true status=%s", status["status"])
        return 2

    price_existed = price_file.exists()
    excel_existed = database_file.exists()
    parquet_existed = parquet_file.exists()
    price_backup: Path | None = None
    excel_backup: Path | None = None
    parquet_backup: Path | None = None
    before_summary: dict[str, object] = {}

    try:
        atomic_write_json(status_file, status)
        logger.info("status_file_written=true status=running")
        logger.info("start_time=%s", status["started_at"])
        logger.info("project_root=%s", root)
        logger.info("run_mode=%s dry_run=%s", run_mode, args.dry_run)
        logger.info("server_date=%s source=%s", status["server_date"], status["source"])

        for required in [price_file, config_file, database_file]:
            if not required.exists():
                raise FileNotFoundError(f"required file not found: {required}")

        before_summary = read_database_summary(database_file)
        status["latest_date"] = before_summary["latest_date"]
        logger.info("before_rows=%s before_latest_date=%s", before_summary["rows"], before_summary["latest_date"])

        if not args.dry_run:
            price_backup = backup_file(price_file, backups_dir, timestamp)
            excel_backup = backup_file(database_file, backups_dir, timestamp)
            parquet_backup = backup_file(parquet_file, backups_dir, timestamp)
            status["price_long_backup"] = str(price_backup or "")
            status["spread_database_backup"] = str(excel_backup or "")
            status["parquet_backup"] = str(parquet_backup or "")
            logger.info("price_long_backup=%s", price_backup)
            logger.info("spread_database_backup=%s", excel_backup)
            logger.info("parquet_backup=%s", parquet_backup)

        if args.update_from_akshare:
            akshare_args = [
                "update_price_long_from_akshare.py",
                "--result-json",
                str(result_file),
                "--skip-backup",
            ]
            if args.dry_run:
                akshare_args.append("--dry-run")
            result = run_script_args(akshare_args, root, logger)
            if result_file.exists():
                update_result = read_json(result_file)
                for field in [
                    "success_contracts",
                    "failure_contracts",
                    "required_contracts",
                    "failed_contracts",
                    "latest_date",
                ]:
                    status[field] = update_result.get(field, status[field])
                logger.info(
                    "contract_summary required=%s success=%s failures=%s failed_contracts=%s",
                    status["required_contracts"],
                    status["success_contracts"],
                    status["failure_contracts"],
                    status["failed_contracts"],
                )
                logger.info("price_long_written=%s", update_result.get("price_long_written"))
            if result.returncode != 0:
                error_detail = update_result.get("error_message", "") if "update_result" in locals() else ""
                raise RuntimeError(f"AkShare price update failed with code {result.returncode}: {error_detail}")
            if status["required_contracts"] != status["success_contracts"] or int(status["failure_contracts"]) != 0:
                raise RuntimeError("contract completeness gate rejected the update")
            if args.dry_run:
                logger.info("dry_run_completed=true spread_recalculation_started=false")
            else:
                logger.info("spread_recalculation_started=true")
                result = run_script_args(["calculate_historical_spreads.py"], root, logger)
                if result.returncode != 0:
                    raise RuntimeError(f"calculate_historical_spreads.py failed with code {result.returncode}")
        elif args.recalculate_from_existing_price_long:
            logger.info("spread_recalculation_started=true")
            result = run_script_args(["calculate_historical_spreads.py"], root, logger)
            if result.returncode != 0:
                raise RuntimeError(f"calculate_historical_spreads.py failed with code {result.returncode}")
        else:
            logger.info("safety_check_only=true spread_recalculation_started=false")

        if not args.dry_run and run_mode != "safety_check":
            summaries = validate_database_outputs(database_file, parquet_file, logger)
            status["excel_latest_date"] = summaries["excel"]["latest_date"]
            status["parquet_latest_date"] = summaries["parquet"]["latest_date"]
            status["latest_date"] = summaries["excel"]["latest_date"]
        else:
            status["excel_latest_date"] = before_summary.get("latest_date", "")
            status["parquet_latest_date"] = (
                read_database_summary(parquet_file)["latest_date"] if parquet_file.exists() else ""
            )

        status.update(
            {
                "status": "success",
                "finished_at": now_text(),
                "excel_exists": database_file.exists(),
                "parquet_exists": parquet_file.exists(),
            }
        )
        atomic_write_json(status_file, status)
        logger.info("update_success=true latest_date=%s", status["latest_date"])
        logger.info("status_file_written=true status=success")
        print(f"server_update_spreads completed: mode={run_mode} dry_run={args.dry_run}")
        print(f"status_file: {status_file}")
        print(f"latest_date: {status['latest_date']}")
        return 0

    except Exception as exc:  # noqa: BLE001
        logger.exception("update_failed=%s", exc)
        if not args.dry_run:
            restore_file(backup=price_backup, target=price_file, existed_before=price_existed, logger=logger)
            restore_file(backup=excel_backup, target=database_file, existed_before=excel_existed, logger=logger)
            restore_file(backup=parquet_backup, target=parquet_file, existed_before=parquet_existed, logger=logger)
        status.update(
            {
                "status": "failed",
                "finished_at": now_text(),
                "error_message": f"{type(exc).__name__}: {exc}",
                "excel_exists": database_file.exists(),
                "parquet_exists": parquet_file.exists(),
            }
        )
        try:
            if database_file.exists():
                status["excel_latest_date"] = read_database_summary(database_file)["latest_date"]
            if parquet_file.exists():
                status["parquet_latest_date"] = read_database_summary(parquet_file)["latest_date"]
            status["latest_date"] = status["excel_latest_date"] or status["latest_date"]
        except Exception as summary_exc:  # noqa: BLE001
            logger.exception("status_summary_failed=%s", summary_exc)
        atomic_write_json(status_file, status)
        logger.info("status_file_written=true status=failed")
        print(f"server_update_spreads failed: {type(exc).__name__}: {exc}")
        print(f"status_file: {status_file}")
        return 1
    finally:
        if result_file.exists():
            result_file.unlink()
        if lock.is_locked:
            lock.release()
            logger.info("lock_released=true")


if __name__ == "__main__":
    raise SystemExit(main())

