import json
from pathlib import Path

from agri_research_agent.sugar_positions.model import foreign_metrics


def config(project_root):
    value = json.loads((Path(project_root) / "02_configs/oilseed_positions.json").read_text(encoding="utf-8"))
    if value.get("schema_version") != 1:
        raise ValueError("油脂持仓配置版本无效")
    return value["domains"]


def preview_root(project_root, domain):
    root = Path(project_root).resolve()
    if not (root / ".git").is_file():
        raise ValueError("采集必须运行于独立 linked worktree")
    if domain not in config(root):
        raise ValueError("持仓板块无效")
    target = root / "01_data/oilseed_positions_preview" / domain
    if target.resolve() != target:
        raise ValueError("预览数据目录不能指向其他位置")
    return target


def metrics(rows):
    result = foreign_metrics(rows)
    # Euronext's combined positions are delta-equivalent lots with two decimals.
    for row in result:
        for field in ("net", "net_change"):
            if row[field] is not None:
                row[field] = round(row[field], 2)
    return result
