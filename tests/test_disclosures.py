from datetime import date

import pandas as pd
import pytest

from src import disclosures
from src.datasources.tdnet import Disclosure

FCFG = {
    "window_days": 90,
    "decay": "linear",
    "count_kinds": ["forecast_revision", "forecast_dividend_revision"],
    "exclude_corrections": True,
    "exclude_same_day_as_earnings": True,
}


def _df(rows):
    base = {c: "" for c in disclosures.COLUMNS}
    return pd.DataFrame([{**base, "is_correction": "False", **r} for r in rows])


def test_no_revision_returns_zero():
    df = _df([{"code": "4617", "kind": "earnings_report", "disclosed_at": "2026-08-05T15:00"}])
    r = disclosures.revision_recency("4617", date(2026, 10, 30), df, FCFG, "2026-07-01")
    assert r["value"] == 0.0
    assert r["n_revisions"] == 0
    assert r["coverage_ok"] is True


def test_linear_decay_and_latest_wins():
    df = _df([
        {"code": "4617", "kind": "forecast_revision", "disclosed_at": "2026-08-20T15:30",
         "direction": "up", "change_pct": "10.0"},
        {"code": "4617", "kind": "forecast_revision", "disclosed_at": "2026-10-01T15:30",
         "direction": "up", "change_pct": "18.1"},
        {"code": "9999", "kind": "forecast_revision", "disclosed_at": "2026-10-28T15:30"},
    ])
    r = disclosures.revision_recency("4617", date(2026, 10, 31), df, FCFG, "2026-07-01")
    assert r["n_revisions"] == 2
    assert r["days_since"] == 30
    assert r["value"] == pytest.approx(1 - 30 / 90, abs=1e-4)
    assert r["last_direction"] == "up"
    assert r["last_change_pct"] == 18.1


def test_flat_decay():
    df = _df([{"code": "4617", "kind": "forecast_revision", "disclosed_at": "2026-10-01T15:30"}])
    r = disclosures.revision_recency("4617", date(2026, 10, 31), df, {**FCFG, "decay": "flat"})
    assert r["value"] == 1.0
    assert r["coverage_ok"] is False   # coverage_start 不明


def test_window_boundary_and_future_excluded():
    df = _df([
        {"code": "4617", "kind": "forecast_revision", "disclosed_at": "2026-08-02T15:30"},  # 90日前ちょうど → 窓外
        {"code": "4617", "kind": "forecast_revision", "disclosed_at": "2026-11-01T15:30"},  # 基準日より後
    ])
    r = disclosures.revision_recency("4617", date(2026, 10, 31), df, FCFG, "2026-07-01")
    assert r["n_revisions"] == 0


def test_excludes_corrections_same_day_earnings_and_dividend_only():
    df = _df([
        {"code": "4617", "kind": "forecast_revision", "disclosed_at": "2026-10-01T15:30", "is_correction": "True"},
        {"code": "4617", "kind": "earnings_report", "disclosed_at": "2026-08-05T15:00"},
        {"code": "4617", "kind": "forecast_revision", "disclosed_at": "2026-08-05T15:00"},
        {"code": "4617", "kind": "dividend_revision", "disclosed_at": "2026-10-10T15:00"},
    ])
    r = disclosures.revision_recency("4617", date(2026, 10, 31), df, FCFG, "2026-07-01")
    assert r["n_revisions"] == 0
    r2 = disclosures.revision_recency("4617", date(2026, 10, 31), df,
                                      {**FCFG, "exclude_same_day_as_earnings": False}, "2026-07-01")
    assert r2["n_revisions"] == 1


class FakeSession:
    pass


def test_ingest_idempotent(tmp_path, monkeypatch):
    items = [
        Disclosure("140120261008547896", "2026-10-08T16:30", "2687", "テスト商事",
                   "業績予想の修正に関するお知らせ", "https://x/a.pdf", "https://x/a.zip", "東"),
        Disclosure("140120261008500002", "2026-10-08T15:00", "7203", "テスト自動車",
                   "自己株式の取得状況に関するお知らせ", "https://x/b.pdf", "", "東"),
        Disclosure("140120261008500003", "2026-10-08T15:00", "1111", "対象外",
                   "業績予想の修正に関するお知らせ", "https://x/c.pdf", "", "東"),
    ]
    xbrl_calls = []
    monkeypatch.setattr(disclosures.tdnet, "fetch_day",
                        lambda s, d: items if d == date(2026, 10, 8) else None)

    def fake_xbrl(s, url):
        xbrl_calls.append(url)
        return {"period": "CurrentYearDuration", "metric": "OperatingIncome", "change_pct": -25.4,
                "direction": "down", "changes": {"OperatingIncome": -25.4}}
    monkeypatch.setattr(disclosures.tdnet, "fetch_forecast_revision", fake_xbrl)
    (tmp_path / "universe.csv").write_text("code,name\n2687,テスト商事\n7203,テスト自動車\n", encoding="utf-8")

    cfg = {"disclosures": {"lookback_days": 1, "backfill_max_days": 31,
                           "store_kinds": ["forecast_revision", "earnings_report"],
                           "filter_by_universe": True,
                           "parse_xbrl_kinds": ["forecast_revision"]}}
    s1 = disclosures.ingest(cfg, data_dir=tmp_path, session=FakeSession(), today=date(2026, 10, 9))
    s2 = disclosures.ingest(cfg, data_dir=tmp_path, session=FakeSession(), today=date(2026, 10, 9))

    assert s1["new_rows"] == 1 and s2["new_rows"] == 0
    assert len(xbrl_calls) == 1                     # 2回目はXBRLを取りに行かない
    df = disclosures.load(tmp_path)
    assert df["code"].tolist() == ["2687"]          # other と universe外 は保存しない
    row = df.iloc[0]
    assert (row["direction"], row["change_pct"], row["metric"]) == ("down", "-25.4", "OperatingIncome")
    assert disclosures.load_meta(tmp_path)["coverage_start"] == "2026-10-08"


def test_ingest_fills_gap_since_last_run(tmp_path, monkeypatch):
    seen = []
    monkeypatch.setattr(disclosures.tdnet, "fetch_day", lambda s, d: seen.append(d) or [])
    (tmp_path / "disclosures_meta.json").write_text(
        '{"coverage_start": "2026-09-20", "last_date": "2026-10-03"}', encoding="utf-8")
    cfg = {"disclosures": {"lookback_days": 1, "backfill_max_days": 31, "store_kinds": [],
                           "filter_by_universe": False, "parse_xbrl_kinds": []}}
    disclosures.ingest(cfg, data_dir=tmp_path, session=FakeSession(), today=date(2026, 10, 9))
    assert seen[0] == date(2026, 10, 3) and seen[-1] == date(2026, 10, 9)
    meta = disclosures.load_meta(tmp_path)
    assert meta == {"coverage_start": "2026-09-20", "last_date": "2026-10-09"}
