"""OpenCode provider and reported accounting remain separate from API value."""

from __future__ import annotations

from datetime import UTC, datetime

import pytest
import server_fixture as sf
from fastapi.testclient import TestClient

from ccreport import exchange, pricing, protocol
from ccreport.server import db, reports
from ccreport.server.factory import create_app


@pytest.fixture
def app(tmp_path, monkeypatch):
    monkeypatch.setattr(exchange, "_store", exchange._store)
    monkeypatch.setattr(exchange, "load_rates", lambda dates, prefetch=None: {})
    return create_app(sf.config(tmp_path))


def push(app, *, provider="github-copilot", reported=None, path="/p/a", dk="one", source="opencode"):
    token = sf.mint_for(app, "opencode-machine")
    record = sf.record(source=source, provider=provider, reported_cost_usd=reported,
                       model="claude-opus-4.5", ts=1_792_000_000.0, dk=dk)
    response = TestClient(app).post("/v1/ingest", json=sf.batch([record], path=path), headers=sf.auth(token))
    assert response.status_code == 200
    assert response.json()["files"][0]["status"] == "accepted"


def test_ingest_preserves_raw_id_provider_and_zero_cost(app):
    push(app, reported=0)
    rec = sf.stored(app, "opencode-machine", "/p/a")[0]
    assert (rec["source"], rec["model"], rec["provider"]) == ("opencode", "claude-opus-4.5", "github-copilot")
    assert rec["reported_cost_usd"] == 0
    assert rec["cost"] > 0
    loaded = reports.load(app.state.db.connect())[0].record
    assert loaded.provider == "github-copilot"
    assert loaded.reported_cost_usd == 0


@pytest.mark.parametrize("reported", [None, 0, .25])
def test_grouped_and_bucketed_preserve_missing_vs_zero(app, reported):
    push(app, reported=reported)
    conn = app.state.db.connect()
    for rows in (reports.load_grouped(conn), reports.load_bucketed(conn, origin=0, step=86400)):
        assert len(rows) == 1
        assert rows[0].record.reported_cost_usd == reported
        assert rows[0].record.provider == "github-copilot"


def test_providers_stay_separate_and_reported_values_sum(app):
    push(app, provider="github-copilot", reported=.25, path="a", dk="a")
    push(app, provider="github-copilot", reported=.5, path="b", dk="b")
    push(app, provider="anthropic", reported=0, path="c", dk="c")
    rows = reports.load_grouped(app.state.db.connect())
    assert {r.record.provider: r.record.reported_cost_usd for r in rows} == {
        "github-copilot": .75, "anthropic": 0,
    }


def test_tools_are_isolated_even_with_matching_dedup_keys(app):
    push(app, source="opencode", path="a")
    push(app, source="claude", path="b")
    conn = app.state.db.connect()
    assert {m.record.source for m in reports.load_grouped(conn)} == {"claude", "opencode"}
    selected = reports.load_grouped(conn, reports.Filters(tool="opencode"))
    assert [m.record.source for m in selected] == ["opencode"]


@pytest.mark.parametrize("model", [
    "claude-opus-4.5", "claude-opus-4.6", "claude-haiku-4.5", "claude-sonnet-4.5", "claude-sonnet-4-0",
])
def test_exact_documented_anthropic_aliases_have_api_value(model):
    assert pricing.has_pricing(model, datetime(2026, 10, 15, tzinfo=UTC), source="opencode")


@pytest.mark.parametrize("model", [
    "other:claude-opus-4.5", "claude-opus-4.5-unreleased", "claude-opus", "deepseek-v4-flash-free",
])
def test_unknown_models_do_not_borrow_a_provider_price(model):
    assert not pricing.has_pricing(model, source="opencode")


def test_schema_12_migrates_without_inventing_reported_cost(tmp_path, monkeypatch):
    path = tmp_path / "old.db"
    with monkeypatch.context() as old:
        old.setattr(db, "MIGRATION_CHAIN", db.MIGRATION_CHAIN[:-2])
        old.setattr(db, "SCHEMA_VERSION", 12)
        start = db._SCHEMA_SQL.index(",\n    provider      TEXT,")
        end = db._SCHEMA_SQL.index("fast          INTEGER NOT NULL DEFAULT 0") + len(
            "fast          INTEGER NOT NULL DEFAULT 0")
        old.setattr(db, "_SCHEMA_SQL", db._SCHEMA_SQL[:start] + db._SCHEMA_SQL[end:])
        conn = db.connect(path)
        conn.execute("INSERT INTO machines VALUES ('m', 'M', 1, 1, NULL)")
        conn.execute(
            "INSERT INTO server_records(machine_id,file_path,account_uuid,model,ts,day,oslo_date,"
            "cost,input_tokens,output_tokens,cache_create,cache_read) "
            "VALUES ('m','f','a','claude-opus-5',1,'2026-01-01','2026-01-01',1,1,1,0,0)",
        )
        conn.commit()
        conn.close()
    conn = db.connect(path)
    assert conn.execute("PRAGMA user_version").fetchone()[0] == 14
    assert conn.execute("SELECT provider,reported_cost_usd FROM server_records").fetchone() == (None, None)
    assert protocol.PROTOCOL_VERSION == 6
    conn.close()


@pytest.mark.parametrize(("model", "date", "rates"), [
    ("gpt-5-mini", "2025-08-07", (.25e-6, .025e-6, 2e-6)),
    ("gpt-5.6-luna", "2026-07-30", (.2e-6, .02e-6, 1.2e-6)),
    ("grok-4.5", "2026-07-08", (2e-6, .3e-6, 6e-6)),
])
def test_documented_other_models_and_effective_dates(model, date, rates):
    from datetime import timedelta

    start = datetime.fromisoformat(date).replace(tzinfo=UTC)
    assert not pricing.has_pricing(model, start - timedelta(seconds=1), source="opencode")
    assert pricing.has_pricing(model, start, source="opencode")
    prices = pricing.find_pricing(model, start)
    assert prices is not None
    assert tuple(prices[k] for k in ("input", "cache_read", "output")) == pytest.approx(rates)


def test_grok_long_context_boundary_is_inclusive():
    short = pricing.calc_cost(100_000, 100, 0, 99_999, "grok-4.5")
    long = pricing.calc_cost(100_000, 100, 0, 100_000, "grok-4.5")
    assert short == pytest.approx(.2 + .0006 + .0299997)
    assert long == pytest.approx(.4 + .0012 + .06)


def test_luna_long_context_includes_cached_input():
    assert pricing.calc_cost(72_001, 100, 0, 200_000, "gpt-5.6-luna") == pytest.approx(
        72_001 * .4e-6 + 100 * 1.8e-6 + 200_000 * .04e-6,
    )


@pytest.mark.parametrize(("model", "date", "rates"), [
    ("codestral-latest", "2025-07-30", (.3e-6, .03e-6, .9e-6)),
    ("mistral-large-latest", "2025-12-02", (.5e-6, .05e-6, 1.5e-6)),
    ("pixtral-large-latest", "2024-11-18", (2e-6, .2e-6, 6e-6)),
    ("kimi-k3", "2026-07-16", (3e-6, .3e-6, 15e-6)),
])
def test_verified_provider_model_prices(model, date, rates):
    when = datetime.fromisoformat(date).replace(tzinfo=UTC)
    prices = pricing.find_pricing(model, when)
    assert prices is not None
    assert pricing.has_pricing(model, when, source="opencode")
    assert tuple(prices[k] for k in ("input", "cache_read", "output")) == pytest.approx(rates)


def test_devstral_free_launch_and_paid_transition():
    from datetime import timedelta

    paid = datetime(2026, 1, 27, tzinfo=UTC)
    assert pricing.calc_cost(1000, 1000, 0, 1000, "devstral-2512", paid - timedelta(seconds=1)) == 0
    assert pricing.calc_cost(1000, 1000, 0, 1000, "devstral-2512", paid) == pytest.approx(.00244)


def test_kimi_default_cache_write_ttl_is_five_minutes():
    assert pricing.calc_cost(100, 200, 300, 400, "kimi-k3") == pytest.approx(.00432)
