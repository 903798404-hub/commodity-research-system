"""Pure navigation contract shared by the workspace sidebar and homepage."""

from __future__ import annotations

from dataclasses import dataclass


USDA_PAGE_TITLE = "USDA平衡表"
SOYBEAN_CROP_PAGE_TITLE = "美豆种植生长"
CANADA_CANOLA_PAGE_TITLE = "加拿大菜籽种植与生长"
BRAZIL_SOY_PAGE_TITLE = "巴西大豆种植与生长"
SOYBEAN_WEATHER_PAGE_TITLE = "大豆天气"
RAPESEED_WEATHER_PAGE_TITLE = "菜籽天气"
PALM_OIL_WEATHER_PAGE_TITLE = "棕榈油天气"
INDIA_CROP_WEATHER_PAGE_TITLE = "印度作物天气"
IMPORT_PROFIT_ROUTE_ID = "import_profit"
INTERNATIONAL_SPREAD_PAGE_TITLE = "国际价差"
RESEARCH_OVERVIEW_PAGE_TITLE = "研究快览 / 最新变化"


@dataclass(frozen=True)
class NavigationItem:
    """One stable workspace destination and its static homepage description."""

    key: str
    label: str
    target: str
    icon: str
    description: str
    external_env: str | None = None
    detail_label: str | None = None
    detail_text: str | None = None


@dataclass(frozen=True)
class NavigationGroup:
    """One research-task group shared by compact and expanded navigation."""

    title: str
    items: tuple[NavigationItem, ...]


NAVIGATION_GROUPS = (
    NavigationGroup(
        "工作台",
        (
            NavigationItem(
                "research_overview",
                RESEARCH_OVERVIEW_PAGE_TITLE,
                RESEARCH_OVERVIEW_PAGE_TITLE,
                "chart",
                "按各模块独立时间身份汇总确定性的最新变化",
            ),
            NavigationItem(
                "home",
                "工作台首页",
                "首页",
                "brand",
                "研究系统首页",
            ),
        ),
    ),
    NavigationGroup(
        "市场行情",
        (
            NavigationItem(
                "international_spread",
                INTERNATIONAL_SPREAD_PAGE_TITLE,
                INTERNATIONAL_SPREAD_PAGE_TITLE,
                "chart",
                "比较棕榈油、豆油与菜油的国际现货、能源与生柴相对价值",
            ),
            NavigationItem(
                "spreads_dashboard",
                "价差动态",
                "价差动态看板",
                "chart",
                "研究油脂油料跨期价差与跨品种套利的历史季节性结构",
            ),
            NavigationItem(
                "basis_domestic",
                "国内现货（基差与一口价）",
                "基差/一口价",
                "quote",
                "查看国内油脂油料现货基差、一口价与地区报价",
            ),
        ),
    ),
    NavigationGroup(
        "周度跟踪",
        (
            NavigationItem(
                "soybean_weekly",
                "美豆周度跟踪",
                SOYBEAN_CROP_PAGE_TITLE,
                "leaf",
                "跟踪美国大豆从田间生长到出口执行的周度变化",
                detail_label="包含",
                detail_text="种植进度 · 生长状况 · 出口销售 · 出口装船",
            ),
            NavigationItem(
                "canada_canola", CANADA_CANOLA_PAGE_TITLE, CANADA_CANOLA_PAGE_TITLE,
                "leaf", "比较加拿大三省菜籽播种、收割与优良率的历史同期差异",
            ),
            NavigationItem(
                "brazil_soy", BRAZIL_SOY_PAGE_TITLE, BRAZIL_SOY_PAGE_TITLE,
                "leaf", "比较巴西大豆全国和主要州播种、收割及全国生长阶段",
            ),
        ),
    ),
    NavigationGroup(
        "天气研究",
        (
            NavigationItem(
                "soybean_weather",
                "大豆天气",
                SOYBEAN_WEATHER_PAGE_TITLE,
                "leaf",
                "监测全球主要大豆产区的降雨、温度与土壤墒情",
                detail_label="覆盖",
                detail_text="美国 · 巴西 · 阿根廷",
            ),
            NavigationItem(
                "rapeseed_weather",
                "菜籽天气",
                RAPESEED_WEATHER_PAGE_TITLE,
                "leaf",
                "监测全球主要菜籽产区天气及历史同期变化",
                detail_label="覆盖",
                detail_text="加拿大 · 澳大利亚 · 欧盟 · 俄罗斯 · 乌克兰",
            ),
            NavigationItem(
                "palm_oil_weather",
                "棕榈油天气",
                PALM_OIL_WEATHER_PAGE_TITLE,
                "leaf",
                "跟踪东南亚主要棕榈油产区天气变化",
                detail_label="覆盖",
                detail_text="印度尼西亚 · 马来西亚",
            ),
            NavigationItem(
                "india_crop_weather",
                "印度作物天气",
                INDIA_CROP_WEATHER_PAGE_TITLE,
                "leaf",
                "跟踪印度主要农作物产区天气变化",
                detail_label="包含",
                detail_text="棉花 · 甘蔗",
            ),
        ),
    ),
    NavigationGroup(
        "国际供需",
        (
            NavigationItem(
                "usda_dashboard",
                "USDA供需平衡",
                USDA_PAGE_TITLE,
                "institution",
                "查看全球主要农产品供需平衡、库存与月度修正",
            ),
            NavigationItem(
                "oil_world_dashboard",
                "Oil World供需平衡",
                "",
                "globe",
                "查看油籽油脂季度供需平衡及相邻报告期变化",
                external_env="OIL_WORLD_DASHBOARD_URL",
            ),
        ),
    ),
    NavigationGroup(
        "研究工具",
        (
            NavigationItem(
                "import_profit",
                "进口大豆榨利",
                IMPORT_PROFIT_ROUTE_ID,
                "quote",
                "测算进口大豆盘面净榨利并拆解关键成本参数",
            ),
            NavigationItem(
                "foreign_seats",
                "外资与重点席位",
                "外资与重点席位",
                "chart",
                "跟踪外资及重点期货席位的持仓变化与资金方向",
            ),
            NavigationItem(
                "status",
                "运行监控",
                "运行监控",
                "monitor",
                "查看数据更新结果、任务状态与运行明细",
            ),
        ),
    ),
)


def research_navigation_groups() -> tuple[NavigationGroup, ...]:
    """Return expanded homepage groups without the homepage linking to itself."""

    return tuple(group for group in NAVIGATION_GROUPS if group.title != "工作台")


def internal_workspace_pages() -> tuple[str, ...]:
    """Return the unique internal route whitelist in navigation order."""

    return tuple(
        item.target
        for group in NAVIGATION_GROUPS
        for item in group.items
        if not item.external_env
    )
