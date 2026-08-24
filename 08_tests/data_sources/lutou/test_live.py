from __future__ import annotations

import json
import reprlib
from datetime import date

import pytest

from agri_research_agent.data_sources.lutou.live import (
    LutouClient,
    LutouConnectionError,
    LutouConnectionSettings,
    LutouPlanRejectedError,
    LutouQuery,
    LutouReadOnlyError,
    LutouSchemaError,
)


class FakeCursor:
    def __init__(self, connection: "FakeConnection") -> None:
        self.connection = connection
        self.statement = ""
        self._rows: list[dict[str, object]] = []

    def __enter__(self) -> "FakeCursor":
        return self

    def __exit__(self, *_: object) -> None:
        pass

    def execute(self, statement: str, parameters=()) -> None:  # type: ignore[no-untyped-def]
        self.statement = statement
        self.connection.statements.append((statement, tuple(parameters)))
        if statement.startswith("SHOW GRANTS"):
            self._rows = [{"Grants": item} for item in self.connection.grants]
        elif statement.startswith("SELECT VERSION"):
            self._rows = [
                {
                    "engine_version": "8.0.37",
                    "engine_comment": "MySQL Community Server - GPL",
                    "global_timezone": "SYSTEM",
                    "session_timezone": "SYSTEM",
                    "global_read_only": 0,
                    "transaction_read_only": self.connection.transaction_read_only,
                }
            ]
        elif statement == "SELECT 1 AS read_probe":
            self._rows = [{"read_probe": 1}]
        elif statement.startswith("EXPLAIN FORMAT=JSON"):
            self._rows = [
                {
                    "EXPLAIN": json.dumps(
                        {"query_block": {"table": {"rows_examined_per_scan": self.connection.plan_rows}}}
                    )
                }
            ]
        elif statement.startswith("SELECT COLUMN_NAME"):
            names = tuple(parameters[2:]) or ("Date", "value")
            self._rows = [
                {
                    "COLUMN_NAME": name,
                    "DATA_TYPE": "date" if index == 0 else "decimal",
                    "IS_NULLABLE": "YES",
                    "ORDINAL_POSITION": index + 1,
                    "COLUMN_COMMENT": "",
                }
                for index, name in enumerate(names)
            ]
        elif statement.startswith("SELECT TABLE_NAME"):
            self._rows = [
                {
                    "TABLE_NAME": "weather",
                    "TABLE_TYPE": "BASE TABLE",
                    "TABLE_ROWS": 10,
                    "TABLE_COMMENT": "",
                    "UPDATE_TIME": None,
                }
            ]
        elif statement.startswith("SELECT MIN("):
            self._rows = [{"min_date": date(2026, 8, 1), "max_date": date(2026, 8, 18)}]
        elif statement.startswith("SELECT `Date`"):
            self._rows = list(self.connection.source_rows)

    def fetchone(self) -> dict[str, object]:
        return self._rows[0]

    def fetchall(self) -> list[dict[str, object]]:
        return list(self._rows)

    def fetchmany(self, size: int) -> list[dict[str, object]]:
        result = self._rows[:size]
        self._rows = self._rows[size:]
        return result


class FakeConnection:
    def __init__(
        self,
        *,
        grants: tuple[str, ...] = (
            "GRANT USAGE ON *.* TO reader",
            "GRANT SELECT ON `油脂油料价格`.* TO reader",
        ),
        transaction_read_only: int = 1,
        plan_rows: int = 10,
        source_rows: tuple[dict[str, object], ...] = (),
    ) -> None:
        self.grants = grants
        self.transaction_read_only = transaction_read_only
        self.plan_rows = plan_rows
        self.source_rows = source_rows
        self.statements: list[tuple[str, tuple[object, ...]]] = []
        self.closed = False
        self.ping_count = 0

    def cursor(self, *_args, **_kwargs) -> FakeCursor:
        return FakeCursor(self)

    def rollback(self) -> None:
        pass

    def ping(self, reconnect: bool = True) -> None:
        assert reconnect is True
        self.ping_count += 1

    def close(self) -> None:
        self.closed = True


def settings(password: str = "fixture-password") -> LutouConnectionSettings:
    return LutouConnectionSettings("fixture.invalid", 3306, "reader", password)


def query(**overrides: object) -> LutouQuery:
    values = {
        "schema": "油脂油料价格",
        "table": "oil_world_prices",
        "date_column": "Date",
        "value_columns": ("Soybean oil,Dutch, fob ex-mill",),
    }
    values.update(overrides)
    return LutouQuery(**values)  # type: ignore[arg-type]


def test_client_proves_account_and_transaction_are_read_only() -> None:
    connection = FakeConnection()
    values = settings()
    with LutouClient(values, connector=lambda **_: connection) as client:
        assert client.proof.transaction_read_only is True
        assert client.proof.global_read_only is False
        assert client.proof.write_privileges == ()
        assert values.password == ""
    assert connection.closed
    assert any(item[0] == "SET SESSION TRANSACTION READ ONLY" for item in connection.statements)
    assert any(item[0] == "START TRANSACTION READ ONLY" for item in connection.statements)


@pytest.mark.parametrize(
    ("grants", "transaction_read_only"),
    [
        (("GRANT SELECT, INSERT ON *.* TO reader",), 1),
        (("GRANT ALL PRIVILEGES ON *.* TO reader",), 1),
        (("GRANT SELECT ON *.* TO reader WITH GRANT OPTION",), 1),
        (("GRANT application_password_admin TO reader",), 1),
        (("GRANT SELECT ON *.* TO reader",), 0),
    ],
)
def test_client_rejects_write_privilege_or_non_read_only_transaction(
    grants: tuple[str, ...], transaction_read_only: int
) -> None:
    values = settings()
    connection = FakeConnection(grants=grants, transaction_read_only=transaction_read_only)
    with pytest.raises(LutouReadOnlyError):
        with LutouClient(values, connector=lambda **_: connection):
            pass
    assert values.password == "" and connection.closed


def test_query_is_bounded_parameterized_and_planned_before_read() -> None:
    day = date(2026, 8, 18)
    connection = FakeConnection(source_rows=({"Date": day, "Soybean oil,Dutch, fob ex-mill": 1000},))
    with LutouClient(settings(), connector=lambda **_: connection) as client:
        plan, batches = client.plan_stream(query(), day, day)
        rows = [row for batch in batches for row in batch.rows]
    assert plan.estimated_rows == 10
    assert rows[0]["Date"] == day
    operational = [item for item in connection.statements if item[0].startswith(("EXPLAIN", "SELECT `Date`"))]
    assert operational[0][0].startswith("EXPLAIN FORMAT=JSON SELECT")
    assert "ANALYZE" not in operational[0][0]
    assert operational[0][1] == (day, day)
    assert operational[1][1] == (day, day)


def test_latest_date_inspects_approved_columns_and_reads_one_row() -> None:
    day = date(2026, 8, 18)
    connection = FakeConnection(source_rows=({"Date": day},))
    with LutouClient(settings(), connector=lambda **_: connection) as client:
        assert client.latest_date(query()) == day
    latest = [statement for statement, _ in connection.statements if "ORDER BY `Date` DESC" in statement]
    assert latest == [
        "SELECT `Date` FROM `油脂油料价格`.`oil_world_prices` ORDER BY `Date` DESC LIMIT 1"
    ]


def test_required_query_probe_executes_newest_window_and_validates_shape() -> None:
    day = date(2026, 8, 18)
    connection = FakeConnection(
        source_rows=({"Date": day, "Soybean oil,Dutch, fob ex-mill": 1000},)
    )
    with LutouClient(settings(), connector=lambda **_: connection) as client:
        proof = client.probe_query(query())
    assert proof["latest_date"] == day.isoformat()
    assert proof["row_count"] == 1
    assert proof["column_count"] == 2


def test_required_query_probe_rejects_result_shape_mismatch() -> None:
    day = date(2026, 8, 18)
    connection = FakeConnection(source_rows=({"Date": day, "unexpected": 1000},))
    with LutouClient(settings(), connector=lambda **_: connection) as client:
        with pytest.raises(LutouSchemaError, match="result shape"):
            client.probe_query(query())


def test_idle_preflight_connection_is_pinged_and_read_only_state_is_reproved() -> None:
    connection = FakeConnection()
    with LutouClient(settings(), connector=lambda **_: connection) as client:
        initial_proof_count = sum(
            statement.startswith("START TRANSACTION READ ONLY")
            for statement, _ in connection.statements
        )
        proof = client.ensure_connected()
        final_proof_count = sum(
            statement.startswith("START TRANSACTION READ ONLY")
            for statement, _ in connection.statements
        )
    assert connection.ping_count == 1
    assert proof.transaction_read_only is True
    assert final_proof_count == initial_proof_count + 1


def test_metadata_inventory_and_date_bounds_are_read_only() -> None:
    connection = FakeConnection()
    with LutouClient(settings(), connector=lambda **_: connection) as client:
        relation = client.inspect_relation("天气2.0", "weather")
        inventory = client.inspect_schema_inventory("天气2.0")
        bounds = client.date_bounds(query())
    assert [item["COLUMN_NAME"] for item in relation] == ["Date", "value"]
    assert inventory[0]["TABLE_TYPE"] == "BASE TABLE"
    assert bounds == (date(2026, 8, 1), date(2026, 8, 18))
    assert all(
        not statement.lstrip().upper().startswith(
            ("INSERT", "UPDATE", "DELETE", "REPLACE", "CREATE", "ALTER", "DROP")
        )
        for statement, _ in connection.statements
    )


def test_plan_bound_and_identifier_validation_fail_closed() -> None:
    day = date(2026, 8, 18)
    limited = query(max_plan_rows=5)
    with LutouClient(settings(), connector=lambda **_: FakeConnection(plan_rows=6)) as client:
        with pytest.raises(LutouPlanRejectedError):
            client.plan(limited, day, day)
    with pytest.raises(ValueError, match="identifier"):
        query(table="unsafe`; DROP TABLE x")


def test_connection_error_does_not_expose_settings() -> None:
    values = settings()

    def fail(**_: object) -> object:
        raise RuntimeError("fixture-password at fixture.invalid")

    with pytest.raises(LutouConnectionError) as captured:
        with LutouClient(values, connector=fail):
            pass
    assert "fixture-password" not in str(captured.value)
    assert "fixture.invalid" not in str(captured.value)
    assert "fixture-password" not in reprlib.repr(values)
    assert values.password == ""
