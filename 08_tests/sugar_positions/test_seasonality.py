from copy import deepcopy

import pytest

from agri_research_agent.shared.chart_style import CURRENT_YEAR_COLOR
from agri_research_agent.sugar_positions.charts import seasonality
from agri_research_agent.sugar_positions.model import COFCO_MEMBER, member_metrics


def observation(day, **kw):
    return dict(report_date=day, market="sugar11", report_type="futures_only",
                group="managed_money", scope="all_expiries", unit="contracts", **kw)


def test_overlay_aligns_month_day_including_leap_day_and_keeps_real_dates_and_zeros():
    rows = [observation(d, net=n) for d, n in [
        ("2024-02-29", 4), ("2024-03-01", 0), ("2025-03-01", -2), ("2026-03-01", 3)]]
    original = deepcopy(rows)
    fig = seasonality(rows, "季节性", current_year=2026)
    assert list(fig.data[0].x) == ["2000-02-29", "2000-03-01"]
    assert list(fig.data[0].y) == [4, 0]
    assert fig.data[1].x[0] == fig.data[2].x[0] == "2000-03-01"
    assert fig.data[0].customdata[0][0] == "2024年2月29日"
    assert fig.data[-1].line.color == CURRENT_YEAR_COLOR and "当年" in fig.data[-1].name
    assert fig.layout.showlegend is True and len(fig.layout.xaxis.ticktext) == 12
    assert rows == original


def test_missing_and_missing_report_gaps_are_not_connected_or_extended():
    rows = [observation(d, net=n) for d, n in [
        ("2026-01-06", 2), ("2026-01-13", None), ("2026-01-20", 0), ("2026-02-10", 5)]]
    trace = seasonality(rows, "季节性", current_year=2026).data[0]
    assert list(trace.y) == [2, None, 0, None, 5]
    assert trace.connectgaps is False and trace.x[-1] == "2000-02-10"


def test_delta_equivalent_unit_and_decimal_sides_are_preserved():
    row = observation("2025-01-03", long=11.25, short=5.1, net=6.15)
    row["unit"] = "delta_equivalent_contracts"
    for field, expected in [("long", 11.25), ("short", 5.1), ("net", 6.15)]:
        fig = seasonality([row], "欧洲菜籽", field=field, current_year=2026)
        assert fig.data[0].y[0] == expected
        assert "Delta等价手" in fig.layout.yaxis.title.text
        assert ".2f" in fig.data[0].hovertemplate and "当年" not in fig.data[0].name


@pytest.mark.parametrize("field,value", [("market", "white_sugar"), ("report_type", "combined"),
    ("group", "producer"), ("scope", "SR701"), ("account", "自营"), ("unit", "delta_equivalent_contracts")])
def test_overlay_rejects_mixed_series(field, value):
    rows = [observation("2026-01-06", net=1), observation("2026-01-13", net=2)]
    rows[1][field] = value
    with pytest.raises(ValueError, match="不能混合"):
        seasonality(rows, "错误")


def test_duplicate_dates_rejected_and_source_change_breaks_line():
    row = observation("2026-01-06", net=1)
    with pytest.raises(ValueError, match="重复"):
        seasonality([row, row], "错误")
    rows = [dict(row, source_provider="old"),
            observation("2026-01-13", net=2, source_provider="new")]
    assert list(seasonality(rows, "更换来源").data[0].y) == [1, None, 2]


def ranking(day, member="中粮期货", side="long", positions=10, account="代客", scope="SR"):
    return dict(report_date=day, member=member, raw_member=member, side=side,
                positions=positions, account=account, scope=scope, rank=1)


def test_cofco_matches_exact_seat_account_scope_and_preserves_undisclosed_sides():
    rows = [ranking("2025-01-06"), ranking("2025-01-06", side="short", positions=3),
            ranking("2025-01-07", member="中粮", side="short", positions=5),
            ranking("2025-01-08", member="其他期货"),
            ranking("2025-01-08", positions=100, account="自营"),
            ranking("2025-01-08", positions=200, scope="SR701"),
            ranking("2025-01-08", member="中粮集团", positions=300)]
    original = deepcopy(rows)
    metrics = member_metrics(rows, COFCO_MEMBER)
    sr = [r for r in metrics if r["scope"] == "SR"]
    assert [r["net"] for r in sr] == [7, -5, 0]
    assert sr[1]["long"] is None and sr[1]["net_change"] == -12
    assert sr[2]["long"] is None and sr[2]["short"] is None
    assert list(seasonality(sr, "中粮").data[0].y) == [7, -5, None]
    assert list(seasonality(sr, "多仓", field="long").data[0].y) == [10, None, None]
    assert rows == original
    with pytest.raises(ValueError, match="多条名称映射"):
        member_metrics([ranking("2025-01-06"), ranking("2025-01-06", member="中粮")], COFCO_MEMBER)
