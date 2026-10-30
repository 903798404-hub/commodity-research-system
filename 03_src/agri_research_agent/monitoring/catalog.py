"""All navigation destinations map to explicit, independently dated inputs."""
from .freshness import Rule

JOBS = {
    "spreads": "market-data-akshare.service",
    "foreign_fx": "market-data-foreign-fx.service",
    "crop": "market-data-soybean-crop-progress.service",
    "fas": "market-data-soybean-export-sales.service",
    "fgis": "market-data-fgis.service",
    "brazil": "market-data-brazil-soy.service",
    "canola_exports": "market-data-canola-exports.service",
    "positions_domestic": "market-data-positions-domestic.service",
    "positions_foreign": "market-data-positions-foreign.service",
    "warehouse": "market-data-warehouse-receipts.service",
    "guangxi": "guangxi-weather-update.service",
    "yunnan": "yunnan-weather-update.service",
    "thailand": "thailand-sugarcane-weather-update.service",
}

PROFIT_PAGES = ("import_profit", "canola_import_profit", "palm_import_profit", "sugar_import_profit")
RULES = [
    Rule("navigation", "首页导航", ("home",), "静态导航", cadence="static"),
    Rule("monitor", "监测进程", ("status",), "服务器监测回执", cadence="static"),
    Rule("domestic_spread", "国内月差及跨品种价差", ("spreads_dashboard",), "原正式价差及18个合约", cadence="china_daily", deadline="19:00", job="spreads"),
    Rule("basis", "国内基差与一口价", ("basis_domestic", "research_overview"), "坚果云原人工报价 / Formal Public Basis Current", cadence="china_daily", deadline="21:00", manual=True),
    Rule("sugar_basis", "白糖柳州现货与基差", ("sugar_basis",), "柳州历史与较新三地快照；人工截图核验交付", cadence="china_daily", deadline="21:00", manual=True),
    Rule("fgis", "美豆出口检验", ("soybean_weekly", "research_overview"), "USDA FGIS", cadence="weekly", deadline="09:00", publish_weekday=1, data_lag_days=5, job="fgis"),
    Rule("fas", "美豆出口销售", ("soybean_weekly", "research_overview"), "USDA FAS", cadence="weekly", deadline="07:00", publish_weekday=4, data_lag_days=8, job="fas"),
    Rule("canola_exports", "加拿大菜籽周度出口", ("canada_canola",), "CGC 原当前作物年来源", cadence="weekly", deadline="10:00", publish_weekday=4, data_lag_days=5, job="canola_exports"),
    Rule("brazil_PLANTED", "巴西大豆播种", ("brazil_soy",), "CONAB 全国原值", cadence="weekly", deadline="10:00", publish_weekday=5, data_lag_days=1, job="brazil", active_months=(8,9,10,11,12,1,2)),
    Rule("brazil_HARVESTED", "巴西大豆收割", ("brazil_soy",), "CONAB 全国原值", cadence="weekly", deadline="10:00", publish_weekday=5, data_lag_days=1, job="brazil", active_months=(1,2,3,4,5,6,7,8,9)),
    Rule("brazil_growth", "巴西大豆全国生长阶段", ("brazil_soy",), "CONAB 全国原值", cadence="weekly", deadline="10:00", publish_weekday=5, data_lag_days=1, job="brazil", active_months=(9,10,11,12,1,2,3,4,5,6)),
    Rule("public_market", "美豆榨利盘面输入", ("import_profit",), "页面原 Tankan Public Current", cadence="weekday_daily", deadline="12:00",business_lag_days=1,source_paused=True),
    Rule("public_fx", "美豆榨利汇率输入", ("import_profit",), "页面原 Tankan FX", cadence="weekday_daily", deadline="12:00",source_paused=True),
    Rule("usda", "USDA报告版次", ("usda_dashboard",), "原 USDA 人工报告交付", cadence="monthly", period_kind="month", manual=True, deadline="00:00"),
    Rule("oil_world", "Oil World季度版次", ("oil_world_dashboard",), "原 Oil World 人工资料交付", cadence="quarterly", period_kind="month", manual=True, deadline="00:00"),
]

for code in ("AUD", "BRL", "CAD", "CNY", "THB", "IDR", "INR", "MYR"):
    RULES.append(Rule("fx_"+code, code+"日度汇率", ("foreign_fx",), "原 ECB / BCB 汇率序列；早间核对前一来源工作日", cadence="weekday_daily", deadline="10:00", business_lag_days=1, job="foreign_fx"))

for metric, label, months in [
    ("PLANTED","播种",(4,5,6,7)), ("EMERGED","出苗",(5,6,7)),
    ("BLOOMING","开花",(6,7,8,9)), ("SETTING_PODS","结荚",(7,8,9)),
    ("DROPPING_LEAVES","落叶",(8,9,10)), ("HARVESTED","收割",(9,10,11)),
    ("GOOD_EXCELLENT","优良率",(5,6,7,8,9,10)),
]:
    RULES.append(Rule("nass_"+metric, "美豆"+label, ("soybean_weekly","research_overview"), "USDA US TOTAL", cadence="weekly", deadline="08:00", publish_weekday=1, data_lag_days=2, job="crop", active_months=months))

for province, name, weekday, lag in [("SK","萨省",4,4),("AB","阿省",5,4),("MB","曼省",2,1)]:
    for metric,label,months in [("PLANTED","播种",(5,6,7)),("HARVESTED","收获",(8,9,10,11)),("GOOD_EXCELLENT","优良率",(5,6,7,8,9))]:
        RULES.append(Rule(f"canada_{province}_{metric}", name+"菜籽"+label, ("canada_canola",), "原省级官方报告 / 人工复核交付", cadence="weekly", deadline="10:00", publish_weekday=weekday, data_lag_days=lag, manual=True, active_months=months))

for symbol in ("M","Y","P","RM","OI","SR"):
    RULES.append(Rule("warehouse_"+symbol, symbol+"仓单", ("spreads_dashboard",), "原交易所仓单", cadence="china_daily", deadline="21:00", job="warehouse"))
for symbol in ("M","Y","P","RM","OI","RS","SR"):
    RULES.append(Rule("positions_"+symbol, symbol+"国内持仓", ("foreign_seats",), "原 DCE / CZCE 持仓", cadence="china_daily", deadline="19:00", job="positions_domestic"))
for board in ("soybean","rapeseed","sugar"):
    RULES.append(Rule("foreign_positions_"+board, board+"外盘持仓", ("foreign_seats",), "各原持仓来源；按独立报告日", cadence="weekly", deadline="10:00", publish_weekday=5, data_lag_days=4, job="positions_foreign"))
RULES.append(Rule("foreign_positions_euronext_rapeseed", "Euronext 菜籽持仓", ("foreign_seats",), "原 Euronext 周报（周五截止，随后周三发布）", cadence="weekly", deadline="10:00", publish_weekday=3, data_lag_days=6, job="positions_foreign"))
RULES.append(Rule("foreign_positions_palm", "棕榈油FCPO持仓（原预留入口）", ("foreign_seats",), "原入口尚无正式报告或已接入更新器", manual=True, deadline="10:00"))

for oil, label in [("palm","棕榈油"),("soy","豆油"),("rape","菜油")]:
    RULES.append(Rule("international_"+oil, label+"国际价差输入", ("international_spread",), "原 Public Current 国际现货及关联价格", cadence="weekday_daily", deadline="12:00",source_paused=True))
for commodity,page in zip(("soybean","canola","palm","sugar"),PROFIT_PAGES):
    RULES.append(Rule("manual_"+commodity, commodity+("自算保存记录" if commodity=='sugar' else "人工录价"), (page,), "原人工CNF/报价；不改用户输入", cadence="on_demand" if commodity=='sugar' else "weekday_daily", deadline="21:00", manual=True))
for commodity,page in [("canola","canola_import_profit"),("palm","palm_import_profit")]:
    RULES.append(Rule("margin_market_"+commodity, commodity+"已保存市场输入", (page,), "原CNF触发的CFETS汇率与Sina精确合约", cadence="on_demand", deadline="21:00", manual=True))
    RULES.append(Rule("margin_history_"+commodity, commodity+"Excel历史", (page,), "原人工Excel历史基线交付", cadence="on_demand", deadline="21:00", manual=True))
RULES.append(Rule("sugar_history", "白糖历史利润", ("sugar_import_profit",), "原Steel三来源历史交付", cadence="on_demand", deadline="21:00", manual=True))

WEATHER = [
    ("USA","美国大豆","soybean_weather",True), ("BRA","巴西大豆","soybean_weather",True),
    ("ARG","阿根廷大豆","soybean_weather",False), ("CAN","加拿大菜籽","rapeseed_weather",True),
    ("AUS","澳大利亚菜籽","rapeseed_weather",False), ("EU","欧盟菜籽","rapeseed_weather",False),
    ("RUS","俄罗斯菜籽","rapeseed_weather",False), ("UKR","乌克兰菜籽","rapeseed_weather",False),
    ("IDN","印尼棕榈油","palm_oil_weather",True), ("MYS","马来西亚棕榈油","palm_oil_weather",True),
    ("IND_COTTON","印度棉花","india_crop_weather",False), ("IND_SUGARCANE","印度甘蔗","india_crop_weather",False),
]
for country,label,page,excluded in WEATHER:
    pages=(page,"research_overview") if country=="USA" else (page,)
    RULES.append(Rule("weather_"+country,label+"天气",pages,"原 Public Weather Current",cadence="weather",deadline="10:00",excluded=excluded,source_paused=not excluded))
for identity,label,page in [("guangxi","广西甘蔗","guangxi_weather"),("yunnan","云南甘蔗","yunnan_weather"),("thailand","泰国甘蔗","thailand_sugarcane_weather")]:
    RULES.append(Rule(identity,label+"天气",(page,),"原独立历史与共同起报快照",cadence="weather",deadline="09:30",job=identity))

RULES = tuple(RULES)
BY_ID = {rule.identity:rule for rule in RULES}
assert len(BY_ID)==len(RULES)
