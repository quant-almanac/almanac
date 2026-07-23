"""
契約テスト: AI 動的外貨比率方針 (currency_policy.py) と rebalance への注入。

設計 (Codex/Claude レビュー 2026-07):
  - AI は外貨比率を判断するが自動発注はしない。Policy/人間実行は不変。
  - rebalance に適用するのは basis="long_tier" の方針のみ。
  - 壊れ/古い/自信不足/合計不一致/basis不一致 → static CURRENCY_TARGETS に fail-closed。
  - 全 ingest は log.jsonl に append (採否問わず監査可能)。state は採用時のみ atomic write。
  - 目標変化は1回 ±MAX_DELTA_PCT まで、horizon は ±MAX_HORIZON_DAYS までクランプ。

本テストは currency_policy の公開契約と、rebalance_engine が注入された
currency_targets を使う/デフォルトでは現行 static 挙動を保つことを固定する。
"""
import json
from datetime import date, timedelta

import pytest

import currency_policy as cp
import rebalance_engine as re


# ── フィクスチャ ─────────────────────────────────────────────────────────

STATIC = re.CURRENCY_TARGETS  # {'USD': {min,max,ideal}, 'JPY': {...}} (fraction)
TODAY = date(2026, 7, 1)


def _valid_rec(**over):
    rec = {
        "basis": "long_tier",
        "usd_target_pct": 70,
        "jpy_target_pct": 30,
        "confidence_pct": 75,
        "horizon_days": 14,
        "valid_until": "2026-07-12",
        "reason": "円安進行で USD 比率引き上げが妥当",
        "review_triggers": ["USDJPY < 150", "VIX > 30"],
    }
    rec.update(over)
    return rec


@pytest.fixture
def paths(tmp_path):
    return {
        "state_path": tmp_path / "currency_policy_state.json",
        "log_path": tmp_path / "currency_policy_log.jsonl",
    }


def _log_lines(p):
    if not p.exists():
        return []
    return [json.loads(ln) for ln in p.read_text(encoding="utf-8").splitlines() if ln.strip()]


# ── 定数の契約 ───────────────────────────────────────────────────────────

def test_module_constants():
    assert cp.APPLICABLE_BASIS == "long_tier"
    assert cp.MAX_DELTA_PCT == 10
    assert cp.MAX_HORIZON_DAYS == 30
    assert cp.MIN_CONFIDENCE_PCT == 60


# ── ingest: valid は state+log 保存 ──────────────────────────────────────

def test_valid_recommendation_saves_state_and_logs(paths):
    res = cp.ingest(_valid_rec(), current_targets=STATIC, now=TODAY, **paths)

    assert res["actionable"] is True
    assert res["verdict"] == "accepted"
    assert res["clamped"] is False

    assert paths["state_path"].exists()
    state = json.loads(paths["state_path"].read_text(encoding="utf-8"))
    assert state["basis"] == "long_tier"
    assert state["usd_target_pct"] == 70
    assert state["jpy_target_pct"] == 30
    assert state["schema_version"] >= 1

    lines = _log_lines(paths["log_path"])
    assert len(lines) == 1
    assert lines[0]["verdict"] == "accepted"
    assert lines[0]["actionable"] is True


def test_log_is_append_only(paths):
    cp.ingest(_valid_rec(), current_targets=STATIC, now=TODAY, **paths)
    cp.ingest(_valid_rec(usd_target_pct=68, jpy_target_pct=32),
              current_targets=STATIC, now=TODAY, **paths)
    lines = _log_lines(paths["log_path"])
    assert len(lines) == 2


# ── ingest: invalid は log のみ・state 更新しない ────────────────────────

def test_sum_not_100_rejected_no_state(paths):
    res = cp.ingest(_valid_rec(usd_target_pct=70, jpy_target_pct=40),
                    current_targets=STATIC, now=TODAY, **paths)
    assert res["actionable"] is False
    assert res["verdict"] == "rejected"
    assert not paths["state_path"].exists()
    lines = _log_lines(paths["log_path"])
    assert len(lines) == 1 and lines[0]["verdict"] == "rejected"


def test_missing_confidence_fails_closed(paths):
    rec = _valid_rec()
    rec.pop("confidence_pct")
    res = cp.ingest(rec, current_targets=STATIC, now=TODAY, **paths)
    assert res["actionable"] is False
    assert not paths["state_path"].exists()


def test_low_confidence_rejected(paths):
    res = cp.ingest(_valid_rec(confidence_pct=50),
                    current_targets=STATIC, now=TODAY, **paths)
    assert res["actionable"] is False
    assert not paths["state_path"].exists()


def test_missing_valid_until_fails_closed(paths):
    rec = _valid_rec()
    rec.pop("valid_until")
    res = cp.ingest(rec, current_targets=STATIC, now=TODAY, **paths)
    assert res["actionable"] is False
    assert not paths["state_path"].exists()


def test_basis_not_long_tier_not_applied(paths):
    res = cp.ingest(_valid_rec(basis="whole_portfolio"),
                    current_targets=STATIC, now=TODAY, **paths)
    assert res["actionable"] is False
    assert not paths["state_path"].exists()
    lines = _log_lines(paths["log_path"])
    assert len(lines) == 1 and lines[0]["verdict"] == "rejected"


def test_out_of_range_pct_rejected(paths):
    res = cp.ingest(_valid_rec(usd_target_pct=130, jpy_target_pct=-30),
                    current_targets=STATIC, now=TODAY, **paths)
    assert res["actionable"] is False
    assert not paths["state_path"].exists()


# ── クランプ ─────────────────────────────────────────────────────────────

def test_delta_clamped_to_max(paths):
    # current ideal USD 65 → rec 80 は +15pt。±10pt にクランプ → 75/25。
    res = cp.ingest(_valid_rec(usd_target_pct=80, jpy_target_pct=20),
                    current_targets=STATIC, now=TODAY, **paths)
    assert res["actionable"] is True
    assert res["verdict"] == "clamped"
    assert res["clamped"] is True
    state = json.loads(paths["state_path"].read_text(encoding="utf-8"))
    assert state["usd_target_pct"] == 75
    assert state["jpy_target_pct"] == 25
    assert state["source_usd_target_pct"] == 80


def test_horizon_clamped_to_max(paths):
    res = cp.ingest(_valid_rec(horizon_days=90, valid_until="2026-12-31"),
                    current_targets=STATIC, now=TODAY, **paths)
    assert res["actionable"] is True
    state = json.loads(paths["state_path"].read_text(encoding="utf-8"))
    assert state["horizon_days"] == cp.MAX_HORIZON_DAYS
    # valid_until は now + MAX_HORIZON_DAYS を超えない
    vu = date.fromisoformat(state["valid_until"])
    assert vu <= TODAY + timedelta(days=cp.MAX_HORIZON_DAYS)


# ── resolve_effective_targets ────────────────────────────────────────────

def test_resolve_no_state_is_static(paths):
    targets, meta = cp.resolve_effective_targets(
        static=STATIC, state_path=paths["state_path"], now=TODAY)
    assert targets == STATIC
    assert meta["source"] == "static_fallback"


def test_resolve_corrupt_state_is_static(paths):
    paths["state_path"].write_text("{ this is not json", encoding="utf-8")
    targets, meta = cp.resolve_effective_targets(
        static=STATIC, state_path=paths["state_path"], now=TODAY)
    assert targets == STATIC
    assert meta["source"] == "static_fallback"


def test_resolve_valid_state_returns_ai_targets(paths):
    cp.ingest(_valid_rec(usd_target_pct=70, jpy_target_pct=30),
              current_targets=STATIC, now=TODAY, **paths)
    targets, meta = cp.resolve_effective_targets(
        static=STATIC, state_path=paths["state_path"], now=TODAY)
    assert meta["source"] == "ai_policy"
    assert targets["USD"]["ideal"] == pytest.approx(0.70)
    assert targets["JPY"]["ideal"] == pytest.approx(0.30)
    # band は static と同じ ±0.05
    assert targets["USD"]["min"] == pytest.approx(0.65)
    assert targets["USD"]["max"] == pytest.approx(0.75)
    # rebalance が直接消費できる形 (同じキー集合)
    assert set(targets["USD"]) == set(STATIC["USD"])


def test_resolve_expired_state_is_static(paths):
    cp.ingest(_valid_rec(valid_until="2026-07-03"),
              current_targets=STATIC, now=TODAY, **paths)
    # valid_until を過ぎた日に resolve → static fallback
    later = date(2026, 7, 10)
    targets, meta = cp.resolve_effective_targets(
        static=STATIC, state_path=paths["state_path"], now=later)
    assert targets == STATIC
    assert meta["source"] == "static_fallback"
    assert "expire" in meta["reason"].lower() or "期限" in meta["reason"]


# ── rebalance への注入 ───────────────────────────────────────────────────

def _long_snap(usd_jpy_split):
    """long tier のみ: USD/JPY を指定比率で。"""
    usd_val, jpy_val = usd_jpy_split
    positions = [
        {"ticker": "USDX", "key": "USDX", "name": "USDX", "currency": "USD",
         "sector": "Technology", "investment_type": "long", "value_jpy": usd_val, "account": "特定"},
        {"ticker": "JPYX", "key": "JPYX", "name": "JPYX", "currency": "JPY",
         "sector": "Healthcare", "investment_type": "long", "value_jpy": jpy_val, "account": "特定"},
    ]
    total = usd_val + jpy_val
    return {
        "positions": positions,
        "total_jpy": total,
        "currency_breakdown": {
            "USD": {"value_jpy": usd_val, "ratio": usd_val / total},
            "JPY": {"value_jpy": jpy_val, "ratio": jpy_val / total},
        },
        "sector_breakdown": {},
    }


def test_rebalance_default_targets_match_explicit_static():
    snap = _long_snap((7_500_000, 2_500_000))  # USD 75% / JPY 25%
    default_res = re.calculate_rebalance_actions(snap)
    explicit_res = re.calculate_rebalance_actions(snap, currency_targets=re.CURRENCY_TARGETS)
    assert default_res["currency_result"] == explicit_res["currency_result"]


def test_rebalance_uses_injected_targets():
    snap = _long_snap((7_500_000, 2_500_000))  # USD 75% / JPY 25%

    # static では JPY 25% < min 30% → action_needed
    static_res = re.calculate_rebalance_actions(snap)
    assert static_res["currency_result"]["status"] == "action_needed"

    # AI 方針 USD 75/JPY 25 (band ±5) を注入 → 75/25 はレンジ内 → ok
    ai_targets = {
        "USD": {"min": 0.70, "max": 0.80, "ideal": 0.75},
        "JPY": {"min": 0.20, "max": 0.30, "ideal": 0.25},
    }
    ai_res = re.calculate_rebalance_actions(snap, currency_targets=ai_targets)
    assert ai_res["currency_result"]["status"] == "ok"
    assert ai_res["currency_result"]["currencies"]["JPY"]["level"] == "ok"


def test_analyze_currency_balance_accepts_targets():
    snap = _long_snap((7_500_000, 2_500_000))
    core = re.build_core_snapshot(snap)
    ai_targets = {
        "USD": {"min": 0.70, "max": 0.80, "ideal": 0.75},
        "JPY": {"min": 0.20, "max": 0.30, "ideal": 0.25},
    }
    out = re.analyze_currency_balance(core, targets=ai_targets)
    assert out["status"] == "ok"


# ── Codex 採用前修正 1: state 保存失敗時は fail-closed ────────────────────

def test_state_write_failure_is_fail_closed(paths, monkeypatch):
    """valid な方針でも atomic_write_json が失敗したら actionable=False。
    state は作られず、log に state_write_failed が残ること。"""
    def _boom(*a, **k):
        raise OSError("disk full")

    monkeypatch.setattr(cp, "atomic_write_json", _boom)

    res = cp.ingest(_valid_rec(), current_targets=STATIC, now=TODAY, **paths)

    assert res["actionable"] is False
    assert res["normalized"] is None
    assert not paths["state_path"].exists()

    lines = _log_lines(paths["log_path"])
    assert any(ln.get("state_write_failed") for ln in lines), \
        "log に state_write_failed の監査エントリが必要"


# ── Codex 採用前修正 2: decision_support ケースE が動的目標を使う ────────

def test_case_e_uses_dynamic_currency_targets(monkeypatch):
    import decision_support as ds

    snap = _long_snap((7_200_000, 2_800_000))  # USD 72% / JPY 28%
    monkeypatch.setattr("portfolio_manager.build_portfolio_snapshot", lambda: snap)

    # 有効な AI policy が解決されたと仮定 (USD ideal 0.72 → band 0.67〜0.77)
    ai_targets = {
        "USD": {"min": 0.67, "max": 0.77, "ideal": 0.72},
        "JPY": {"min": 0.23, "max": 0.33, "ideal": 0.28},
    }
    monkeypatch.setattr(
        cp, "resolve_effective_targets",
        lambda *, static: (ai_targets, {"source": "ai_policy"}),
    )

    ctx = ds._build_case_e()

    # AI policy 側の band が表示され、static (60〜70%) は出ないこと
    assert "67〜77%" in ctx
    assert "60〜70%" not in ctx


# ── Codex 採用前修正 3: backup_manager に state/log を含める ─────────────

def test_currency_policy_files_are_backed_up():
    import backup_manager
    assert "currency_policy_state.json" in backup_manager.TARGETS
    assert "currency_policy_log.jsonl" in backup_manager.TARGETS
