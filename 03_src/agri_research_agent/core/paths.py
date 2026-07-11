from pathlib import Path

# 项目根目录。
PROJECT_ROOT = Path(__file__).resolve().parents[3]

# 配置与各阶段数据目录。
CONFIG_DIR = PROJECT_ROOT / "02_configs"
DATA_DIR = PROJECT_ROOT / "01_data"
RAW_DATA_DIR = DATA_DIR / "raw"
MANUAL_DATA_DIR = DATA_DIR / "manual"
INTERIM_DATA_DIR = DATA_DIR / "interim"
PROCESSED_DATA_DIR = DATA_DIR / "processed"
CACHE_DIR = DATA_DIR / "cache"

# 人工维护的国内现货基差目录。
BASIS_MANUAL_DIR = DATA_DIR / "manual" / "basis"
# 固定指向 01_data/manual/basis/国内现货基差.xlsx，程序只读取该文件。
BASIS_EXCEL_FILE = BASIS_MANUAL_DIR / "国内现货基差.xlsx"
# 指向 01_data/processed/basis_spread/，存放基差标准化数据。
BASIS_PROCESSED_DIR = DATA_DIR / "processed" / "basis_spread"
BASIS_DATABASE_DIR = DATA_DIR / "database" / "basis"
BASIS_DATABASE_FILE = BASIS_DATABASE_DIR / "basis_quotes.parquet"
BASIS_SAMPLE_DATABASE_FILE = BASIS_DATABASE_DIR / "basis_quotes_sample.parquet"

# OUTPUT_DIR 指向新 Agent 输出目录 `06_outputs/`。
OUTPUT_DIR = PROJECT_ROOT / "06_outputs"
# 新 Agent 的 Markdown、Excel、图表和推送日志输出目录。
MARKDOWN_OUTPUT_DIR = OUTPUT_DIR / "markdown"
EXCEL_OUTPUT_DIR = OUTPUT_DIR / "excel"
CHART_OUTPUT_DIR = OUTPUT_DIR / "charts"
PUSH_LOG_DIR = OUTPUT_DIR / "push_logs"

# 程序日志与项目说明文档目录。
LOG_DIR = PROJECT_ROOT / "10_logs"
DOCS_DIR = PROJECT_ROOT / "07_docs"
