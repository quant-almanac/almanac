"""テストスイートが本番の LLM 台帳 (logs/llm_calls.jsonl) を汚染しないことの回帰テスト。

背景: tests/test_tier_timeout_controls.py が実物の call_tier_analysis を
role="tier_analysis_margin_long" で呼んでおり、call_book_aware_llm が log_path
未指定時に既定の本番パスへ追記していた。結果として本番台帳に 198 行の
"contains_book": true 行 —— 実際には送信していないのに「ポートフォリオを外部
モデルへ送信した」と主張する監査記録 —— が蓄積していた。

conftest の autouse fixture でこれを構造的に防いでいる。
"""
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
PROD_LOG = ROOT / "logs" / "llm_calls.jsonl"


def test_tier_analysis_never_appends_to_production_log(
    monkeypatch, _isolate_llm_call_log
):
    """book-aware tier 経路を実際に通しても本番台帳は 1 バイトも増えない。"""
    from analyst.llm_client import call_tier_analysis

    before = PROD_LOG.read_bytes() if PROD_LOG.exists() else b""

    monkeypatch.setenv("ALMANAC_PRIVACY_MODE", "multi_provider_book_aware")
    monkeypatch.setattr(
        "llm_adapters.call_by_role",
        lambda **kw: {"content": '{"health":"good","priority_actions":[]}'},
    )

    result = call_tier_analysis(
        "system", "user", role="tier_analysis_margin_long", max_tokens=1000
    )
    assert result.get("health") == "good", result

    after = PROD_LOG.read_bytes() if PROD_LOG.exists() else b""
    assert after == before, (
        "テストが本番 LLM 台帳へ追記した。conftest の _isolate_llm_call_log "
        "が効いていないか、新しいログ生成箇所が既定パスを直書きしている。"
    )

    # 素通りで受かる (どこにも書かれていない) テストにならないよう、
    # リダイレクト先には実際に行が書かれたことを確認する。
    assert _isolate_llm_call_log.exists(), "監査行がどこにも記録されていない"
    assert _isolate_llm_call_log.read_text(encoding="utf-8").strip(), "台帳行が空"
