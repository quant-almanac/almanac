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
    "data/short_universe.json", "beliefs/agent_beliefs.json",
)


@pytest.fixture(scope="session", autouse=True)
def _protect_production_state():
    snapshot = {}
    for rel in PROTECTED_STATE:
        f = ROOT / rel
        if f.is_file():
            try:
                snapshot[f] = f.read_bytes()
            except OSError:
                pass
    yield
    restored = []
    for f, data in snapshot.items():
        try:
            if f.read_bytes() != data:
                f.write_bytes(data)
                restored.append(str(f.relative_to(ROOT)))
        except OSError:
            continue
    if restored:
        import warnings
        warnings.warn(
            "テストが本番状態ファイルを書き換えたため復元しました: "
            + ", ".join(sorted(restored)),
            stacklevel=1,
        )
