"""Retired legacy collector: do not recreate the old positioning dataset."""
import sys


def main():
    print("旧持仓采集已停用。请使用 04_scripts/positions/update_positions.py；定时任务需按新持仓契约重新配置。", file=sys.stderr)
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
