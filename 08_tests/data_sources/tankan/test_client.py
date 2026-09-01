from __future__ import annotations

import reprlib
import math
from datetime import date
from pathlib import Path

import pytest

from agri_research_agent.data_sources.tankan.client import (
    TankanClient,
    TankanConnectionError,
    TankanConnectionSettings,
    TankanPlanRejectedError,
    TankanReadOnlyError,
)
from agri_research_agent.data_sources.tankan.queries import (
    CBOT_SOYBEAN_LIVE_QUERY,
    MARKET_WINDOW_QUERY,
    USD_CNH_SPOT_LIVE_QUERY,
)


class FakeCursor:
    def __init__(self, connection: "FakeConnection", *, named: bool = False) -> None:
        self.connection = connection
        self.named = named
        self.statement = ""
        self._batches = iter(connection.batches if named else ())

    def __enter__(self) -> "FakeCursor":
        return self

    def __exit__(self, *_: object) -> None:
        pass

    def execute(self, statement: str, parameters=()) -> None:  # type: ignore[no-untyped-def]
        self.statement = statement.strip()
        self.connection.statements.append((self.statement, tuple(parameters), self.named))

    def fetchone(self) -> dict[str, object]:
        values = {
            "SHOW default_transaction_read_only": {
                "default_transaction_read_only": self.connection.read_only_value
            },
            "SHOW transaction_read_only": {
                "transaction_read_only": self.connection.read_only_value
            },
            "SHOW server_version": {"server_version": "18.4"},
            "SHOW timezone": {"TimeZone": "Asia/Shanghai"},
            "SELECT current_database() AS database": {"database": "quanyong"},
        }
        if self.statement.startswith("EXPLAIN (FORMAT JSON)"):
            return {
                "QUERY PLAN": [
                    {
                        "Plan": {
                            "Node Type": "Sort",
                            "Plan Rows": 50,
                            "Total Cost": 120.5,
                            "Plans": [
                                {
                                    "Node Type": "Index Scan",
                                    "Plan Rows": self.connection.plan_rows,
                                    "Total Cost": self.connection.plan_cost,
                                }
                            ],
                        }
                    }
                ]
            }
        if self.statement.startswith("SELECT trade_date FROM market."):
            return {"trade_date": date(2026, 8, 18)}
        return values[self.statement]

    def fetchmany(self, _: int) -> list[dict[str, object]]:
        return next(self._batches, [])


class FakeConnection:
    def __init__(
        self,
        read_only_value: str = "on",
        *,
        plan_rows: int = 10,
        plan_cost: float = 20.0,
        batches: tuple[list[dict[str, object]], ...] = (),
    ) -> None:
        self.read_only_value = read_only_value
        self.plan_rows = plan_rows
        self.plan_cost = plan_cost
        self.batches = batches
        self.read_only = False
        self.autocommit = True
        self.closed = False
        self.statements: list[tuple[str, tuple[object, ...], bool]] = []

    def cursor(self, *_, name: str | None = None, **__) -> FakeCursor:  # type: ignore[no-untyped-def]
        return FakeCursor(self, named=name is not None)

    def rollback(self) -> None:
        pass

    def close(self) -> None:
        self.closed = True


def settings(password: str = "super-secret") -> TankanConnectionSettings:
    return TankanConnectionSettings("db.example", 5432, "quanyong", "reader", password)


def test_existing_tankan_machine_local_secret_loader_contract(tmp_path: Path) -> None:
    secret = tmp_path / "tankan.env"
    secret.write_text(
        "TANKAN_HOST=db.example\n"
        "TANKAN_PORT=5432\n"
        "TANKAN_DATABASE=quanyong\n"
        "TANKAN_USER=reader\n"
        "TANKAN_PASSWORD=super-secret\n",
        encoding="utf-8",
    )
    loaded = TankanConnectionSettings.from_secret_file(secret)
    assert (loaded.host, loaded.port, loaded.database, loaded.user) == (
        "db.example", 5432, "quanyong", "reader"
    )
    assert loaded.password == "super-secret"
    assert "super-secret" not in reprlib.repr(loaded)


def test_import_and_construction_do_not_connect() -> None:
    calls = 0

    def connector(**_: object) -> object:
        nonlocal calls
        calls += 1
        return FakeConnection()

    TankanClient(settings(), connector=connector)
    assert calls == 0


def test_client_requires_both_session_and_transaction_read_only() -> None:
    connection = FakeConnection("on")
    values = settings()
    with TankanClient(values, connector=lambda **_: connection) as client:
        assert client.proof.transaction_read_only == "on"
        assert client.proof.default_transaction_read_only == "on"
        assert values.password == ""
    assert connection.closed
    assert all(statement.startswith(("SHOW", "SELECT")) for statement, _, _ in connection.statements)


def test_stream_explains_without_analyze_before_bounded_select() -> None:
    rows = [{"trade_date": "2026-08-13", "close_price": "10.5"}]
    connection = FakeConnection(batches=(rows,))
    parameters = (date(2026, 8, 1), date(2026, 8, 13))
    with TankanClient(settings(), connector=lambda **_: connection) as client:
        batches = list(client.stream(MARKET_WINDOW_QUERY, parameters, batch_size=1))

    operational = [item for item in connection.statements if item[0].startswith(("EXPLAIN", "SELECT trade_date"))]
    assert operational[0][0].startswith("EXPLAIN (FORMAT JSON) SELECT")
    assert "ANALYZE" not in operational[0][0]
    assert operational[0][1] == parameters
    assert operational[1][2] is True
    assert batches[0].rows == tuple(rows)
    assert batches[0].plan.estimated_rows == 50
    assert batches[0].query.provider.dataset.origin_system.value == "tankan"


def test_stream_live_binds_only_exact_unique_contracts() -> None:
    rows = [{"contract": "2701", "last": 1200.0}]
    connection = FakeConnection(batches=(rows,))
    with TankanClient(settings(), connector=lambda **_: connection) as client:
        batches = list(
            client.stream_live(CBOT_SOYBEAN_LIVE_QUERY, ("2701",), batch_size=1)
        )
    operational = [
        item for item in connection.statements if item[0].startswith(("EXPLAIN", "SELECT exchange"))
    ]
    assert operational[0][1] == (["2701"],)
    assert operational[1][2] is True
    assert batches[0].rows == tuple(rows)


@pytest.mark.parametrize("contracts", [(), ("M2701",), ("270",), ("2701", "2701")])
def test_stream_live_rejects_unbounded_or_non_yymm_contracts(contracts) -> None:
    connection = FakeConnection()
    with TankanClient(settings(), connector=lambda **_: connection) as client:
        with pytest.raises(ValueError):
            list(client.stream_live(CBOT_SOYBEAN_LIVE_QUERY, contracts))
    assert not any(statement.startswith("EXPLAIN") for statement, _, _ in connection.statements)


def test_zero_parameter_live_spot_query_rejects_contracts() -> None:
    connection = FakeConnection()
    with TankanClient(settings(), connector=lambda **_: connection) as client:
        with pytest.raises(ValueError):
            list(client.stream_live(USD_CNH_SPOT_LIVE_QUERY, ("2701",)))
    assert not any(statement.startswith("EXPLAIN") for statement, _, _ in connection.statements)


def test_latest_source_dates_are_lightweight_allowlisted_reads() -> None:
    connection = FakeConnection()
    with TankanClient(settings(), connector=lambda **_: connection) as client:
        assert client.latest_source_dates() == {
            "market": date(2026, 8, 18),
            "fx": date(2026, 8, 18),
            "domestic_spread": date(2026, 8, 18),
        }
    probes = [statement for statement, _, _ in connection.statements if "ORDER BY trade_date DESC" in statement]
    assert len(probes) == 3
    assert all("LIMIT 1" in statement for statement in probes)


def test_plan_over_threshold_fails_before_named_cursor_execution() -> None:
    connection = FakeConnection(plan_rows=MARKET_WINDOW_QUERY.max_plan_rows + 1)
    with TankanClient(settings(), connector=lambda **_: connection) as client:
        with pytest.raises(TankanPlanRejectedError):
            list(client.stream(MARKET_WINDOW_QUERY, (date(2026, 8, 1), date(2026, 8, 13))))
    assert not any(named for _, _, named in connection.statements)


@pytest.mark.parametrize(
    "plan_cost",
    [MARKET_WINDOW_QUERY.max_total_cost + 1, math.inf, math.nan],
)
def test_invalid_or_excessive_plan_cost_fails_before_named_cursor(plan_cost: float) -> None:
    connection = FakeConnection(plan_cost=plan_cost)
    with TankanClient(settings(), connector=lambda **_: connection) as client:
        with pytest.raises(TankanPlanRejectedError):
            list(client.stream(MARKET_WINDOW_QUERY, (date(2026, 8, 1), date(2026, 8, 13))))
    assert not any(named for _, _, named in connection.statements)


def test_parameter_count_fails_before_explain() -> None:
    connection = FakeConnection()
    with TankanClient(settings(), connector=lambda **_: connection) as client:
        with pytest.raises(ValueError, match="parameter count"):
            list(client.stream(MARKET_WINDOW_QUERY, (date(2026, 8, 1),)))
    assert not any(statement.startswith("EXPLAIN") for statement, _, _ in connection.statements)


@pytest.mark.parametrize(
    "parameters",
    [
        ("2026-08-01", "2026-08-13"),
        (date(2026, 8, 13), date(2026, 8, 1)),
        (date(2025, 1, 1), date(2026, 8, 13)),
    ],
)
def test_invalid_or_excessive_windows_fail_before_explain(parameters: tuple[object, object]) -> None:
    connection = FakeConnection()
    with TankanClient(settings(), connector=lambda **_: connection) as client:
        with pytest.raises(ValueError, match="query window"):
            list(client.stream(MARKET_WINDOW_QUERY, parameters))
    assert not any(statement.startswith("EXPLAIN") for statement, _, _ in connection.statements)


def test_read_only_off_fails_closed_and_clears_password() -> None:
    values = settings()
    connection = FakeConnection("off")
    with pytest.raises(TankanReadOnlyError):
        with TankanClient(values, connector=lambda **_: connection):
            raise AssertionError("must not continue")
    assert values.password == ""
    assert connection.closed


@pytest.mark.parametrize(
    "error",
    [TimeoutError("network"), RuntimeError("authentication failed super-secret")],
)
def test_connection_failures_do_not_expose_credentials(error: Exception) -> None:
    values = settings()

    def fail(**_: object) -> object:
        raise error

    with pytest.raises(TankanConnectionError) as captured:
        with TankanClient(values, connector=fail):
            pass
    assert "super-secret" not in str(captured.value)
    assert "db.example" not in str(captured.value)
    assert values.password == ""
    assert "super-secret" not in reprlib.repr(values)
