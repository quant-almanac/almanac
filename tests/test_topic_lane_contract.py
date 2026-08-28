"""topic_lane_contract / news_topic / social_screener の実データ契約テスト。

背景 (2026-08-28 の監査):
  - news_topic は 20 銘柄一括で max_tokens に張り付き、44 回中 43 回で出力が
    切れて parse に失敗していたが、会計ログは全 44 回が status=ok だった
    (API が 200 を返したことしか見ていなかった)。
  - fallback の Qwen は OpenRouter 残高切れ (402) で、直近 10 営業日の
    分析結果は 0 件だった。
  - social_topic は選抜条件 message_count>200 が、上流の 1 ページ標本
    (実測 27-30) に対して到達不能で、4 ヶ月間ずっと 0 件だった。
  - 両レーンとも format_for_prompt() が generated_at を見ておらず、
    何日前の成果物でも最終分析へ注入され得た。

既存テストは message_count=280 という「上流が生成し得ない値」を fixture に
していたため、この producer/consumer の不一致を隠していた。ここでは
上流が実際に出す形で契約を固定する。
"""
from __future__ import annotations

import json
import sys
from datetime import datetime, timedelta
from pathlib import Path

import pytest

_REPO_ROOT = Path(__file__).resolve().parent.parent
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

import topic_lane_contract as tlc  # noqa: E402


# ---------------------------------------------------------------------------
# truncation / parse の区別
# ---------------------------------------------------------------------------

def test_truncated_output_is_detected_from_usage():
    """max_tokens に張り付いた出力を truncation として識別する。

    実測: news_topic_deepdive 44 回中 43 回が completion_tokens 2999-3000
    (max_tokens=3000)。原因が truncation か純粋な parse 失敗かで対策
    (バッチ縮小 vs プロンプト調整) が変わるため区別が要る。
    """
    assert tlc.looks_truncated({"completion_tokens": 3000}, 3000) is True
    assert tlc.looks_truncated({"completion_tokens": 2999}, 3000) is True
    assert tlc.looks_truncated({"completion_tokens": 1200}, 3000) is False
    assert tlc.looks_truncated(None, 3000) is False


def test_truncated_json_is_not_silently_repaired():
    """閉じ括弧の無い出力を「修復」して部分的な結果を通さない。

    以前の実装は末尾に `}` を足して再試行していたが、抽出正規表現
    `\\{[\\s\\S]*\\}` が閉じ括弧を要求する以上そもそもマッチせず、
    再試行に到達しなかった。半分だけ読めた分析を正常扱いしないこと。
    """
    truncated = '{"analyses": [{"ticker": "AAPL", "reason": "途中で切れ'
    assert tlc.extract_json(truncated) is None
    assert tlc.extract_json('{"analyses": [{"ticker": "AAPL"},]}') is None
    # 正常な JSON は当然読める
    assert tlc.extract_json('{"analyses": [{"ticker": "AAPL"}]}') == {
        "analyses": [{"ticker": "AAPL"}]}


def test_code_fence_wrapped_json_is_read():
    fenced = '```json\n{"analyses": [{"ticker": "NVDA"}]}\n```'
    assert tlc.extract_json(fenced) == {"analyses": [{"ticker": "NVDA"}]}


# ---------------------------------------------------------------------------
# quota (402) の分類と circuit breaker
# ---------------------------------------------------------------------------

def test_quota_errors_are_classified_separately_from_transport():
    """402 は待っても直らないので transport とは別扱いにする。

    実際の OpenRouter 応答 (news_topic_analysis.json に記録されていたもの) を
    模した文字列で確認する。
    """
    real_402 = ("Error code: 402 - {'error': {'message': 'This request requires "
                "more credits, or fewer max_tokens. You requested up to 3000 "
                "tokens, but can only afford 972'}}")
    assert tlc.classify_error(real_402) == tlc.ERROR_QUOTA
    assert tlc.is_quota_error(real_402) is True
    assert tlc.classify_error("Connection reset by peer") == tlc.ERROR_TRANSPORT
    assert tlc.is_quota_error("Connection reset by peer") is False


# ---------------------------------------------------------------------------
# スキーマ検証: 「JSON が読めた」と「使える分析がある」は別
# ---------------------------------------------------------------------------

def test_schema_validation_rejects_rows_missing_required_fields():
    parsed = {"analyses": [{"ticker": "AAPL"}]}   # catalyst_type 等が無い
    res = tlc.validate_rows(parsed, list_key="analyses",
                            required_fields=("ticker", "catalyst_type"))
    assert res.ok is False


def test_schema_validation_rejects_tickers_outside_the_prompt():
    """プロンプトに無い銘柄を LLM が創作しても通さない。"""
    parsed = {"analyses": [
        {"ticker": "FAKE", "catalyst_type": "earnings"},
    ]}
    res = tlc.validate_rows(parsed, list_key="analyses",
                            required_fields=("ticker", "catalyst_type"),
                            expected_tickers=["AAPL", "NVDA"])
    assert res.ok is False


def test_schema_validation_accepts_a_well_formed_row():
    parsed = {"analyses": [
        {"ticker": "AAPL", "catalyst_type": "earnings",
         "durability": "medium", "impact_magnitude": 60},
    ]}
    res = tlc.validate_rows(parsed, list_key="analyses",
                            required_fields=("ticker", "catalyst_type",
                                             "durability", "impact_magnitude"),
                            expected_tickers=["AAPL"])
    assert res.ok is True
    assert len(res.rows) == 1


# ---------------------------------------------------------------------------
# 注入ゲート (fail-closed)
# ---------------------------------------------------------------------------

def _artifact(**over) -> dict:
    base = {
        "run_status": tlc.RUN_STATUS_SUCCESS,
        "generated_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "analyses": [{"ticker": "AAPL"}],
    }
    base.update(over)
    return base


def test_fresh_successful_artifact_is_injectable():
    ok, reason = tlc.injection_gate(_artifact(), source="news_topic")
    assert ok is True, reason


def test_partial_run_is_not_injected():
    """部分成功は監査用に保存するが判断入力にはしない。

    20 銘柄中 3 銘柄だけ分析できた結果を「全体の所見」として扱わない。
    """
    ok, reason = tlc.injection_gate(
        _artifact(run_status=tlc.RUN_STATUS_PARTIAL), source="news_topic")
    assert ok is False
    assert "partial" in reason


def test_failed_run_is_not_injected():
    ok, _ = tlc.injection_gate(
        _artifact(run_status=tlc.RUN_STATUS_FAILED), source="news_topic")
    assert ok is False


def test_stale_artifact_is_not_injected():
    """鮮度切れを注入しない。72h 契約に対し 10 日前を拒否する。"""
    old = (datetime.now() - timedelta(days=10)).strftime("%Y-%m-%d %H:%M:%S")
    ok, reason = tlc.injection_gate(_artifact(generated_at=old), source="news_topic")
    assert ok is False
    assert "stale" in reason


def test_artifact_from_the_scheduled_weekend_gap_is_still_injectable():
    """金曜 18:25 生成 → 月曜 06:15 消費 (約 60h) は通す。

    単純な 12h 固定にすると週明けだけ材料コンテキストが必ず落ちる。
    実スケジュールから決めた 72h 契約であることを固定する。
    """
    friday_evening = datetime(2026, 8, 28, 18, 25, 0)
    monday_morning = datetime(2026, 8, 31, 6, 15, 0)
    ok, reason = tlc.injection_gate(
        _artifact(generated_at=friday_evening.strftime("%Y-%m-%d %H:%M:%S")),
        source="news_topic", now=monday_morning)
    assert ok is True, reason


def test_pre_contract_artifact_without_run_status_is_not_injected():
    """契約導入前に書かれた成果物は成功と断定できないので通さない。"""
    legacy = {"generated_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
              "analyses": [{"ticker": "AAPL"}]}
    ok, reason = tlc.injection_gate(legacy, source="news_topic")
    assert ok is False
    assert "run_status" in reason


def test_future_timestamp_is_not_injected():
    future = (datetime.now() + timedelta(days=3)).strftime("%Y-%m-%d %H:%M:%S")
    ok, reason = tlc.injection_gate(_artifact(generated_at=future), source="news_topic")
    assert ok is False
    assert "future" in reason


# ---------------------------------------------------------------------------
# producer/consumer 契約: 上流が実際に出す値で下流が動くか
# ---------------------------------------------------------------------------

def test_truncated_batch_is_split_and_retried(monkeypatch):
    """truncation したバッチは分割して retry する。

    このレーンは「全バッチ成功時のみ注入」なので、retry が無いと病的な
    1 バッチのせいで run 全体が永久に partial になり、結局一度も注入
    されない —— 元の「毎日走るが何も生まない」状態を別の形で再現する。

    隔離ライブの実測では 1 バッチあたりの出力が 591〜4000+ トークンと
    ばらつき、同じ 3 銘柄が試行によって切れたり切れなかったりした
    (V/MSFT/LRCX が 4000 で切れた次の試行では 2335 で成功)。
    固定サイズだけでは吸収できない性質なので分割 retry で収束させる。
    """
    import news_topic_analyzer as nt

    calls: list[list[str]] = []

    def _fake_run(batch, *, role, run_id, batch_id):
        tickers = [c["ticker"] for c in batch]
        calls.append(tickers)
        # 3 銘柄だと必ず切れ、2 銘柄以下なら通る、という状況を作る
        if len(batch) >= 3:
            return {"batch_id": batch_id, "role": role, "tickers": tickers,
                    "status": "error", "failure_kind": tlc.ERROR_TRUNCATION,
                    "rows": [], "usage": {"completion_tokens": 4000},
                    "error": None, "raw_response": None, "adapter": "x", "model": "m"}
        return {"batch_id": batch_id, "role": role, "tickers": tickers,
                "status": "ok", "failure_kind": None,
                "rows": [{"ticker": t, "catalyst_type": "macro"} for t in tickers],
                "usage": {"completion_tokens": 900},
                "error": None, "raw_response": None, "adapter": "x", "model": "m"}

    monkeypatch.setattr(nt, "_run_one_batch", _fake_run)
    monkeypatch.setattr(nt, "BATCH_SIZE", 3)
    monkeypatch.setattr(nt, "call_by_role", lambda *a, **k: None)
    monkeypatch.setattr(nt, "_load_candidates", lambda: (
        [{"ticker": t, "sentiment_score": 50, "top_headlines": []}
         for t in ("A", "B", "C")], 3, "2026-08-28 18:18"))

    out = nt.analyze(dry_run=True)

    assert calls[0] == ["A", "B", "C"], "最初は 3 銘柄でまとめて投げる"
    assert len(calls) > 1, "truncation したのに分割 retry していない"
    assert out["run_status"] == tlc.RUN_STATUS_SUCCESS, (
        f"分割 retry 後も success になっていない: {out['run_status']}")
    assert {a["ticker"] for a in out["analyses"]} == {"A", "B", "C"}


def test_split_retry_terminates_on_a_single_pathological_ticker(monkeypatch):
    """サイズ 1 でなお切れる銘柄では分割を止める (無限ループにしない)。"""
    import news_topic_analyzer as nt

    calls: list[list[str]] = []

    def _always_truncate(batch, *, role, run_id, batch_id):
        calls.append([c["ticker"] for c in batch])
        return {"batch_id": batch_id, "role": role,
                "tickers": [c["ticker"] for c in batch],
                "status": "error", "failure_kind": tlc.ERROR_TRUNCATION,
                "rows": [], "usage": {"completion_tokens": 4000},
                "error": None, "raw_response": None, "adapter": "x", "model": "m"}

    monkeypatch.setattr(nt, "_run_one_batch", _always_truncate)
    monkeypatch.setattr(nt, "BATCH_SIZE", 2)
    monkeypatch.setattr(nt, "call_by_role", lambda *a, **k: None)
    monkeypatch.setattr(nt, "_load_candidates", lambda: (
        [{"ticker": t, "sentiment_score": 50, "top_headlines": []}
         for t in ("A", "B")], 2, None))

    out = nt.analyze(dry_run=True)

    assert out["run_status"] == tlc.RUN_STATUS_FAILED
    assert out["error_code"] == tlc.ERROR_TRUNCATION
    # 2 銘柄 → 分割して 1 銘柄 ×2。サイズ 1 ではもう割らないので計 3 回で止まる。
    assert len(calls) == 3, f"分割が収束していない: {calls}"


def test_quota_error_opens_a_circuit_breaker(monkeypatch):
    """402 を掴んだら残りのバッチを叩かない (待っても直らないため)。"""
    import news_topic_analyzer as nt

    calls: list[str] = []

    def _quota(batch, *, role, run_id, batch_id):
        calls.append(batch_id)
        return {"batch_id": batch_id, "role": role,
                "tickers": [c["ticker"] for c in batch],
                "status": "error", "failure_kind": tlc.ERROR_QUOTA,
                "rows": [], "usage": None,
                "error": "Error code: 402", "raw_response": None,
                "adapter": "x", "model": "m"}

    monkeypatch.setattr(nt, "_run_one_batch", _quota)
    monkeypatch.setattr(nt, "BATCH_SIZE", 1)
    monkeypatch.setattr(nt, "call_by_role", lambda *a, **k: None)
    monkeypatch.setattr(nt, "_load_candidates", lambda: (
        [{"ticker": t, "sentiment_score": 50, "top_headlines": []}
         for t in ("A", "B", "C", "D")], 4, None))

    out = nt.analyze(dry_run=True)

    assert len(calls) == 1, f"402 の後も呼び出しを続けている: {calls}"
    assert out["run_status"] == tlc.RUN_STATUS_FAILED
    assert out["error_code"] == tlc.ERROR_QUOTA


def test_social_screener_message_count_is_a_page_sample_not_a_daily_volume():
    """上流の message_count は API 1 ページ分の長さであることを固定する。

    ⚠️ このテストが今回の中心。social_topic_analyzer は
    ``message_count > 200`` を要求していたが、上流は
    ``len(messages)`` (= StockTwits が 1 回のリクエストで返す 1 ページ、
    実測 27-30) を入れていた。producer が生成し得ない値を consumer が
    要求していたため、選抜が構造的に 0 件だった。

    既存の tests/test_social_topic.py は message_count=280 という
    「上流が出せない値」を fixture にしていたため、この不一致を隠していた。
    """
    import social_screener as ss

    # StockTwits の 1 ページ応答を模す (実測のページサイズは 30)
    page = [{"user": {"id": i}, "created_at": "2026-08-28T10:00:00Z",
             "entities": {"sentiment": {"basic": "Bullish"}}} for i in range(30)]
    quality = ss._sample_quality(page, 30, 0)

    assert quality["sample_message_count"] == 30
    assert quality["source_window"] == "api_page", (
        "1ページ標本を24時間の総量と誤解させる名前にしない")
    # 下流が使っていた閾値 200 は、この標本サイズでは到達不能
    assert quality["sample_message_count"] < 200


def test_social_quality_metrics_expose_the_denominator_behind_a_percentage():
    """bullish_pct の分母を復元できること。

    ラベル付き投稿だけが分母なので、「100% Bullish」が 2 件中 2 件なのか
    20 件中 20 件なのか、パーセントだけでは区別できなかった。
    """
    import social_screener as ss

    page = [
        {"user": {"id": 1}, "entities": {"sentiment": {"basic": "Bullish"}}},
        {"user": {"id": 1}, "entities": {"sentiment": {"basic": "Bullish"}}},
        {"user": {"id": 2}, "entities": {"sentiment": {}}},   # ラベル無し
    ]
    q = ss._sample_quality(page, 2, 0)
    assert q["labeled_message_count"] == 2      # 分母は 2 であって 3 ではない
    assert q["sample_message_count"] == 3
    assert q["unique_author_count"] == 2        # 同一著者の連投を区別できる


def test_social_shadow_history_is_append_only(tmp_path, monkeypatch):
    """日次上書きだけでは 20-40 営業日後の校正ができないので履歴を残す。"""
    import social_screener as ss

    hist = tmp_path / "shadow.jsonl"
    monkeypatch.setattr(ss, "SHADOW_HISTORY_FILE", hist)

    day1 = {"generated_at": "2026-08-27 18:47",
            "stocktwits": {"AAPL": {"sample_message_count": 30, "bullish_pct": 80.0}}}
    day2 = {"generated_at": "2026-08-28 18:47",
            "stocktwits": {"AAPL": {"sample_message_count": 28, "bullish_pct": 75.0}}}
    ss._append_shadow_history(day1)
    ss._append_shadow_history(day2)

    rows = [json.loads(line) for line in hist.read_text(encoding="utf-8").splitlines()]
    assert len(rows) == 2, "2 日目の追記が 1 日目を上書きしている"
    assert [r["as_of"] for r in rows] == ["2026-08-27 18:47", "2026-08-28 18:47"]


def test_social_shadow_history_stores_no_message_bodies_or_usernames():
    """個人情報・投稿本文を履歴へ残さない。"""
    import social_screener as ss

    page = [{"user": {"id": 7, "username": "someone", "name": "Real Name"},
             "body": "本文テキスト",
             "entities": {"sentiment": {"basic": "Bullish"}}}]
    q = ss._sample_quality(page, 1, 0)
    serialized = json.dumps(q, ensure_ascii=False)
    assert "someone" not in serialized
    assert "Real Name" not in serialized
    assert "本文テキスト" not in serialized
    assert q["unique_author_count"] == 1        # 件数だけは残る
