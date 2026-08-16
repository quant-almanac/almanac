"""Swingレーンの自己ロック回帰テスト。

背景 (2026-08-16): _analyze_short_positions は保有swingがゼロのとき関数の先頭で
早期 return しており、その25行下にある「スクリーニング由来の新規スイング候補抽出」に
到達しなかった。TXN/ANET を決済した 2026-07 以降、

    保有0 → 新規候補ゼロ → 新規に建たない → 保有0 のまま

という自己ロックに陥り、スタンスが moderately_aggressive でも swing 提案が
永久に出なくなっていた（8月に判明した鮮度デッドロックと同型の構造）。

ここで固定するのは「保有ゼロでも新規候補があれば LLM に評価させる」こと。
サイズ上限・安全ゲートは本レーンの責務外であり、変更していない。
"""
import analyst


def _fake_screen_candidate(ticker="NVDA", signal="BUY", conf=75, score=40):
    return {
        "ticker": ticker, "strategy": "momentum", "ai_signal": signal,
        "ai_confidence": conf, "score": score, "rsi": 55,
        "mom_1m": 8.0, "stop_loss_atr": 100.0, "ai_reason": "強いモメンタム",
    }


def _data(*, positions=None, screen_candidates=None):
    return {
        "positions": positions or [],
        "screen_candidates": screen_candidates or [],
        "screening": {},
        "news": {},
        "earnings": {},
        "technical_state": {},
        "social_sentiment": {},
    }


def _install_fakes(monkeypatch, captured):
    def fake_call(_system, prompt, **_kwargs):
        captured["prompt"] = prompt
        return {"health": "good", "summary": "ok", "priority_actions": [
            {"rank": 1, "type": "buy", "ticker": "NVDA", "amount_hint": "1株"},
        ]}

    monkeypatch.setattr(analyst, "call_tier_analysis", fake_call)
    monkeypatch.setattr(analyst, "_compute_ginn_vol", lambda _tickers: ("", {}, {}))


def test_zero_positions_with_screen_candidates_still_reaches_the_llm(monkeypatch):
    """本命: 保有ゼロ + スクリーニングBUYあり → 早期returnせず新規候補を評価させる。"""
    captured: dict = {}
    _install_fakes(monkeypatch, captured)

    result = analyst._analyze_short_positions(
        _data(screen_candidates=[_fake_screen_candidate()])
    )

    # 旧実装ならここで "swingポジションなし" が返り、prompt は組み立てられなかった
    assert "prompt" in captured, "保有ゼロだと LLM に到達しない（デッドロック再発）"
    assert "NVDA" in captured["prompt"]
    assert "新規スイングエントリー候補" in captured["prompt"]
    assert result["priority_actions"], "新規買い提案が返っていない"


def test_zero_positions_prompt_omits_stop_loss_warning(monkeypatch):
    """保有ゼロなら損切り警告節は出さない（保有していないものは切れない）。"""
    captured: dict = {}
    _install_fakes(monkeypatch, captured)

    analyst._analyze_short_positions(_data(screen_candidates=[_fake_screen_candidate()]))

    prompt = captured["prompt"]
    assert "現在 swing 保有はゼロ" in prompt
    assert "含み損が-20%超" not in prompt


def test_zero_positions_and_zero_candidates_short_circuits_without_llm(monkeypatch):
    """保有も候補も無いときは従来どおり LLM を呼ばずに返す（無駄なコストを出さない）。"""
    captured: dict = {}
    _install_fakes(monkeypatch, captured)

    result = analyst._analyze_short_positions(_data())

    assert "prompt" not in captured, "候補ゼロなのに LLM を呼んでいる"
    assert result["priority_actions"] == []
    assert "新規候補なし" in result["summary"]


def test_watch_signal_with_bullish_support_also_reaches_the_llm(monkeypatch):
    """WATCH でも強気支持あり score>=25 なら候補として扱う経路が生きていること。"""
    captured: dict = {}
    _install_fakes(monkeypatch, captured)
    monkeypatch.setattr(analyst, "_screen_candidate_has_bullish_support", lambda _c: True)

    analyst._analyze_short_positions(
        _data(screen_candidates=[_fake_screen_candidate(ticker="AMD", signal="WATCH", score=30)])
    )

    assert "prompt" in captured
    assert "AMD" in captured["prompt"]


def test_existing_positions_still_get_the_stop_loss_warning(monkeypatch):
    """既存の保有ありパスは従来どおり（損切り警告つき）で退行していないこと。"""
    captured: dict = {}
    _install_fakes(monkeypatch, captured)

    analyst._analyze_short_positions(_data(positions=[{
        "ticker": "CRWV", "name": "CoreWeave", "shares": 1, "value_jpy": 50_000,
        "current_price": 100.0, "unrealized_pct": -0.25, "unrealized_jpy": -10_000,
        "holding_days": 30, "entry_date": "2026-07-01",
        "stop_loss": 80.0, "stop_loss_source": "suggested",
        "investment_type": "swing",
    }]))

    prompt = captured["prompt"]
    assert "CRWV" in prompt
    assert "含み損が-20%超" in prompt
    assert "現在 swing 保有はゼロ" not in prompt
