"""Pytest conftest — repo root を sys.path に追加"""
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


# logs/llm_calls.jsonl は本番の LLM コスト計上 *かつ* book-aware 監査台帳。
# ログパスを明示しないテストが実経路 (call_tier_analysis → call_book_aware_llm 等)
# を通ると、"contains_book": true の行が本番台帳に混入し「ポートフォリオを外部
# モデルへ送信した」という事実でない監査記録が残る。既定パスを常に tmp へ向けて
# 構造的に防ぐ。ログ内容を検証するテストは _append_llm_call_log を差し替えるか
# log_path を明示しているため、このリダイレクトの影響を受けない。
@pytest.fixture(autouse=True)
def _isolate_llm_call_log(tmp_path, monkeypatch):
    log = tmp_path / "llm_calls.jsonl"
    for module_name in ("almanac.llm_safety", "analyst.llm_client"):
        try:
            module = __import__(module_name, fromlist=["_DEFAULT_LOG_PATH"])
        except Exception:
            continue
        if hasattr(module, "_DEFAULT_LOG_PATH"):
            monkeypatch.setattr(module, "_DEFAULT_LOG_PATH", log)
    return log


@pytest.fixture(autouse=True)
def _isolate_yfinance_sqlite_cache(tmp_path, monkeypatch):
    """Keep yfinance SQLite/cache files out of production during tests."""
    cache_root = tmp_path / "yfinance-cache"
    monkeypatch.setenv("ALMANAC_YFINANCE_CACHE_DIR", str(cache_root))
    return cache_root


# ---------------------------------------------------------------------------
# 本番状態ファイルの保護
#
# テストは実データを読むことがあるため読み取りは許すが、書き込みは残さない。
# 実測で account.json (fx 取得時刻) と data/short_universe.json がフルラン時
# のみ書き換わっていた。個別実行では再現せず、テスト間の相互作用による。
# 内容は無害だったが、書けてしまう構造自体を塞ぐ。
# 同種の実例: logs/llm_calls.jsonl は偽の監査行を 678 行蓄積していた。
# ---------------------------------------------------------------------------
PROTECTED_STATE = (
    "account.json", "holdings.json", "nisa_portfolio.json", "tickers.json",
    "trade_history.csv", "action_state.json", "tunable_params.json",
    "action_executions.json", "decision_snapshot_state.json",
    "execution_invalidation_state.json", "execution_reconciliation_state.json",
    "feature_control_state.json",
    "macro_event_state.json",
    "market_regime_v2_state.json",
    "currency_policy_state.json", "hedge_target.json", "hedge_target_shadow.json",
    "fx_actual_hedge_state.json", "fx_instrument_master.json",
    "broker_position_snapshot_monex.json",
    "broker_position_snapshot_rakuten.json",
    "broker_position_snapshot_sbi.json",
    "factor_attribution.json",
    "models/ginn_model.pt", "models/ginn_meta.json", "models/ginn/current.json",
    "data/tax_harvest_reports.jsonl",
    "data/short_universe.json", "beliefs/agent_beliefs.json",
)


def _state_snapshot() -> dict[Path, bytes | None]:
    snapshot: dict[Path, bytes | None] = {}
    for rel in PROTECTED_STATE:
        path = ROOT / rel
        try:
            snapshot[path] = path.read_bytes() if path.is_file() else None
        except OSError:
            snapshot[path] = None
    return snapshot


def _restore_changed_state(snapshot: dict[Path, bytes | None]) -> list[str]:
    restored: list[str] = []
    for path, original in snapshot.items():
        try:
            current = path.read_bytes() if path.is_file() else None
            if current == original:
                continue
            if original is None:
                if path.exists():
                    path.unlink()
            else:
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_bytes(original)
            restored.append(str(path.relative_to(ROOT)))
        except OSError:
            continue
    return restored


@pytest.fixture(autouse=True)
def _fail_test_that_mutates_production_state():
    """Detect the exact test that touched repository state, including subprocesses.

    A session-only restore allowed later tests to observe contaminated state and
    only emitted one warning at the end. Per-test comparison both restores before
    the next test and turns the unsafe write into an actionable failure.
    """
    snapshot = _state_snapshot()
    yield
    restored = _restore_changed_state(snapshot)
    if restored:
        pytest.fail(
            "テストが本番状態ファイルを書き換えました（復元済み）: "
            + ", ".join(sorted(restored))
        )


@pytest.fixture(scope="session", autouse=True)
def _protect_production_state():
    snapshot = _state_snapshot()
    yield
    restored = _restore_changed_state(snapshot)
    if restored:
        import warnings
        warnings.warn(
            "テストが本番状態ファイルを書き換えたため復元しました: "
            + ", ".join(sorted(restored)),
            stacklevel=1,
        )
