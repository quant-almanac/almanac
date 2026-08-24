"""holdings/cash 鮮度アンカーを CSV 無しで前進させる経路のテスト。

2026-08-05 の自己ロック (holdings は楽天CSV取込でしか更新されないのに 96h で
失効 → 全候補 review → 発注も約定記録も起きず holdings が永久に古いまま) を
再現し、attestation と roll-forward がそれを解くことを固定する。
"""
import json
from datetime import datetime, timedelta

import pytest

import holdings_freshness as hf


def _write(path, payload):
    path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")


@pytest.fixture
def base(tmp_path):
    _write(tmp_path / "holdings.json", {
        "XLF": {"ticker": "XLF", "shares": 80.0, "account": "特定",
                "owner": "husband", "broker": "楽天証券", "currency": "USD"},
    })
    _write(tmp_path / "account.json", {"balance": 9000, "last_updated": "2026-07-28"})
    return tmp_path


def test_attestation_advances_as_of_without_csv(base):
    """CSV を出さずに鮮度アンカーを進められる (利用者の運用負荷を下げる本体)。"""
    stale = datetime(2026, 7, 29, 19, 26)
    now = datetime(2026, 8, 5, 6, 15)

    before, origin = hf.effective_source_as_of(
        scope="holdings", file_as_of=stale, base_dir=base,
    )
    assert before == stale and origin == "file"

    hf.record_attestation(scope="holdings", note="保有に変更なし", base_dir=base, now=now)

    after, origin = hf.effective_source_as_of(
        scope="holdings", file_as_of=stale, base_dir=base,
    )
    assert after == now and origin == "attestation"


def test_attestation_is_void_once_content_changes(base):
    """表明は『その内容』への保証。中身が変われば自動的に効力を失う。"""
    now = datetime(2026, 8, 5, 6, 15)
    hf.record_attestation(scope="holdings", base_dir=base, now=now)
    assert hf.latest_valid_attestation(scope="holdings", base_dir=base) is not None

    # 楽天CSV取込や roll-forward で holdings が書き換わった状況
    _write(base / "holdings.json", {
        "XLF": {"ticker": "XLF", "shares": 25.0, "account": "特定",
                "owner": "husband", "broker": "楽天証券", "currency": "USD"},
    })

    assert hf.latest_valid_attestation(scope="holdings", base_dir=base) is None
    stale = datetime(2026, 7, 29, 19, 26)
    as_of, origin = hf.effective_source_as_of(
        scope="holdings", file_as_of=stale, base_dir=base,
    )
    assert as_of == stale and origin == "file"


def test_attestation_never_moves_as_of_backwards(base):
    """ファイル側の方が新しければ、古い表明で巻き戻さない。"""
    attested_at = datetime(2026, 8, 1, 9, 0)
    hf.record_attestation(scope="holdings", base_dir=base, now=attested_at)
    newer_file = datetime(2026, 8, 4, 12, 0)

    as_of, origin = hf.effective_source_as_of(
        scope="holdings", file_as_of=newer_file, base_dir=base,
    )
    assert as_of == newer_file and origin == "file"


def test_attestation_scopes_are_independent(base):
    """holdings を表明しても cash の鮮度は動かない (別々の根拠)。"""
    now = datetime(2026, 8, 5, 6, 15)
    hf.record_attestation(scope="holdings", base_dir=base, now=now)

    cash_as_of, origin = hf.effective_source_as_of(
        scope="cash", file_as_of=datetime(2026, 7, 28), base_dir=base,
    )
    assert origin == "file"
    assert cash_as_of == datetime(2026, 7, 28)


def test_attestation_log_is_append_only(base):
    """監査のため過去の表明を上書きしない。"""
    hf.record_attestation(scope="holdings", note="1回目", base_dir=base,
                          now=datetime(2026, 8, 1, 9, 0))
    hf.record_attestation(scope="holdings", note="2回目", base_dir=base,
                          now=datetime(2026, 8, 5, 9, 0))
    rows = hf.load_attestations(base_dir=base)
    assert [r["note"] for r in rows] == ["1回目", "2回目"]
    # 有効なのは最新のものだけ
    assert hf.latest_valid_attestation(scope="holdings", base_dir=base)["note"] == "2回目"


def test_unknown_scope_is_rejected(base):
    with pytest.raises(ValueError, match="unknown attestation scope"):
        hf.record_attestation(scope="prices", base_dir=base)


def test_attesting_a_missing_file_is_rejected(tmp_path):
    """存在しないファイルに『一致している』とは表明できない。"""
    with pytest.raises(FileNotFoundError):
        hf.record_attestation(scope="holdings", base_dir=tmp_path)


# --- roll-forward ---------------------------------------------------------


def _confirmed_fill(**overrides):
    row = {
        "execution_id": "exec-1",
        "ticker": "XLF",
        "side": "sell",
        "account": "特定",
        "owner": "husband",
        "broker": "楽天証券",
        "filled_quantity": 55.0,
        "filled_price": 57.95,
        "broker_confirmed_filled": True,
        "broker_reported_at": "2026-08-05T01:00:00",
        "reconciled_at": "2026-08-05T01:05:00",
        "portfolio_applied": False,
    }
    row.update(overrides)
    return row


def test_rollforward_plan_is_side_effect_free(base):
    _write(base / "action_executions.json", [_confirmed_fill()])
    plan = hf.plan_rollforward(base_dir=base)

    holdings = json.loads((base / "holdings.json").read_text(encoding="utf-8"))
    assert holdings["XLF"]["shares"] == 80.0, "dry-run が holdings を書き換えている"
    assert plan["planned_count"] in (0, 1)


def test_rollforward_skips_already_applied_fills(base):
    """二重計上しない (再実行しても数量が二度引かれない)。"""
    _write(base / "action_executions.json", [_confirmed_fill(portfolio_applied=True)])
    plan = hf.plan_rollforward(base_dir=base)
    assert plan["planned_count"] == 0


def test_rollforward_skips_unconfirmed_fills(base):
    """broker 確認が無い約定は自動適用しない (証拠境界を弱めない)。"""
    _write(base / "action_executions.json", [_confirmed_fill(broker_confirmed_filled=False)])
    plan = hf.plan_rollforward(base_dir=base)
    assert plan["planned_count"] == 0


def test_rollforward_refuses_to_drive_shares_negative(base):
    """holdings と台帳が食い違うときは自動で触らず人間の再照合に回す。"""
    _write(base / "action_executions.json", [_confirmed_fill(filled_quantity=999.0)])
    plan = hf.plan_rollforward(base_dir=base)
    assert plan["planned_count"] == 0
    reasons = {row["reason"] for row in plan["skipped"]}
    assert not reasons or "would_go_negative" in reasons or "holding_key_unresolved" in reasons


# --- screening カテゴリの生産者周期ミスマッチ ---------------------------


def test_long_term_screening_uses_its_own_cadence_policy():
    """週2回の生産者に日次の閾値を課さない。

    long_term_screener の cron は日・木 (0 7 * * 0,4) で最大96h空くため、
    72h を課すと毎サイクル必ず stale になり、日次の2ファイルが新鮮でも
    screening カテゴリ全体を巻き添えにしていた (2026-08-05)。
    """
    from freshness_policy import get_freshness_policy, stale_after_hours

    daily = stale_after_hours("screening")
    long_term = stale_after_hours("screening_long_term")
    assert long_term > 96.0, "日曜→木曜の96h間隔を上回っていない"
    assert long_term > daily

    # refresh < stale の契約が明示的に成立していること
    policy = get_freshness_policy("screening_long_term")
    assert policy.refresh_after_hours == 96.0
    policy.validate("screening_long_term")


def test_screening_status_takes_the_worst_per_file_status(tmp_path):
    """日次ファイルが古ければ、長期側が新鮮でも stale になる (保守性の維持)。"""
    import json as _json
    from datetime import datetime, timedelta
    import analysis_snapshot as asn

    now = datetime(2026, 8, 5, 8, 0)
    old = (now - timedelta(hours=100)).isoformat()   # 日次72hを超過
    recent = (now - timedelta(hours=1)).isoformat()

    (tmp_path / "short_candidates.json").write_text(
        _json.dumps({"as_of": old}), encoding="utf-8")
    (tmp_path / "margin_long_candidates.json").write_text(
        _json.dumps({"generated_at": recent}), encoding="utf-8")
    (tmp_path / "long_term_screen_results.json").write_text(
        _json.dumps({"as_of": recent}), encoding="utf-8")

    snap = asn.build_base_snapshot(base_dir=tmp_path, now=now)
    assert snap.screening.freshness_status == "stale"


def test_the_freshness_score_honours_an_attestation(tmp_path, monkeypatch):
    """本題: attest したのに鮮度スコアが VERY_STALE のままにならないこと。

    2026-08-24 の実測: 本人が holdings_freshness.py attest を実行し、
    analysis_snapshot の provenance は holdings(attested)/fresh になったのに、
    analyst._compute_data_freshness は holdings.json の mtime を直読みしていた
    ため "❌ VERY_STALE holdings: 127h前" を出し続け、その文字列が LLM の
    プロンプトへ入って urgency を不必要に抑制していた。
    権威が2経路で食い違っていたのが原因。
    """
    import os
    from pathlib import Path

    import analyst

    stale = datetime.now() - timedelta(hours=127)
    holdings = tmp_path / "holdings.json"
    _write(holdings, {"AAPL": {"ticker": "AAPL", "shares": 1}})
    os.utime(holdings, (stale.timestamp(), stale.timestamp()))
    _write(tmp_path / "account.json", {"last_updated": stale.isoformat()})
    monkeypatch.setattr(analyst, "BASE_DIR", Path(tmp_path))

    before = analyst._compute_data_freshness()
    assert "VERY_STALE holdings" in before

    hf.record_attestation(scope="holdings", base_dir=tmp_path, actor="user")
    hf.record_attestation(scope="cash", base_dir=tmp_path, actor="user")
    after = analyst._compute_data_freshness()

    assert "VERY_STALE holdings" not in after
    assert "holdings(attested)" in after
    assert "account_cash(attested)" in after


def test_the_freshness_score_falls_back_to_the_file_without_an_attestation(tmp_path, monkeypatch):
    """attestation が無ければ従来どおりファイル基準のままであること。"""
    import os
    from pathlib import Path

    import analyst

    stale = datetime.now() - timedelta(hours=127)
    holdings = tmp_path / "holdings.json"
    _write(holdings, {"AAPL": {"ticker": "AAPL", "shares": 1}})
    os.utime(holdings, (stale.timestamp(), stale.timestamp()))
    monkeypatch.setattr(analyst, "BASE_DIR", Path(tmp_path))

    text = analyst._compute_data_freshness()

    assert "VERY_STALE holdings" in text
    assert "attested" not in text


def test_an_attestation_does_not_survive_an_unapplied_execution(tmp_path, monkeypatch):
    """本題: attest 済みでも未反映の約定があれば fresh を名乗らないこと。

    表明は「表明した時点の内容」しか保証しない。壁時計で止めない設計にした
    代わりに置いた唯一の停止条件が divergence なので、attestation を見る
    経路は必ず divergence も見なければならない。
    analysis_snapshot は _provenance_for_file(diverged=...) で持っているが、
    鮮度スコア側に同じ条件が無いと、そこだけ fail-open になる。
    """
    import os
    from pathlib import Path

    import analyst

    stale = datetime.now() - timedelta(hours=127)
    holdings = tmp_path / "holdings.json"
    _write(holdings, {"AAPL": {"ticker": "AAPL", "shares": 10}})
    os.utime(holdings, (stale.timestamp(), stale.timestamp()))
    monkeypatch.setattr(analyst, "BASE_DIR", Path(tmp_path))
    hf.record_attestation(scope="holdings", base_dir=tmp_path, actor="user")

    monkeypatch.setattr(
        hf, "holdings_divergence",
        lambda **kwargs: {"diverged": True, "unapplied": [{"ticker": "AAPL"}], "unresolved": []},
    )
    diverged_text = analyst._compute_data_freshness()

    assert "holdings(diverged)" in diverged_text
    assert "VERY_STALE holdings" in diverged_text
    assert "holdings(attested)" not in diverged_text

    monkeypatch.setattr(
        hf, "holdings_divergence",
        lambda **kwargs: {"diverged": False, "unapplied": [], "unresolved": []},
    )
    clean_text = analyst._compute_data_freshness()

    assert "holdings(attested)" in clean_text
    assert "VERY_STALE holdings" not in clean_text


def test_divergence_forces_very_stale_even_without_any_attestation(tmp_path, monkeypatch):
    """Codex レビュー Case A: attestation が無くても divergence を見ること。

    旧実装は divergence チェックを attestation ブロックの内側に置いていた
    ので、attestation が無いファイルは wall-clock だけで判定され、
    1時間前で未反映の約定があっても "✅ FRESH holdings: 1h前" になっていた
    (再現: Codex レビュー 2026-08-24)。mtime の新しさは中身の正しさを
    何も保証しない。
    """
    import os
    from pathlib import Path

    import analyst

    recent = datetime.now() - timedelta(hours=1)
    holdings = tmp_path / "holdings.json"
    _write(holdings, {"AAPL": {"ticker": "AAPL", "shares": 10}})
    os.utime(holdings, (recent.timestamp(), recent.timestamp()))
    monkeypatch.setattr(analyst, "BASE_DIR", Path(tmp_path))
    monkeypatch.setattr(
        hf, "holdings_divergence",
        lambda **kwargs: {"diverged": True, "unapplied": [{"ticker": "AAPL"}], "unresolved": []},
    )

    text = analyst._compute_data_freshness()

    assert "VERY_STALE holdings(diverged): 1h前" in text


def test_an_unresolvable_divergence_check_fails_closed_even_with_attestation(tmp_path, monkeypatch):
    """Codex レビュー Case B: 台帳読込エラー時に attestation だけで fresh にしないこと。

    再現: 800時間前の holdings + 有効な attestation + 乖離台帳の読込エラー。
    divergence_or_unresolved は判定不能を True (乖離あり扱い) にするので、
    ここが例外を投げても attested という理由だけで fresh を騙らない。
    """
    import os
    from pathlib import Path

    import analyst

    ancient = datetime.now() - timedelta(hours=800)
    holdings = tmp_path / "holdings.json"
    _write(holdings, {"AAPL": {"ticker": "AAPL", "shares": 10}})
    os.utime(holdings, (ancient.timestamp(), ancient.timestamp()))
    monkeypatch.setattr(analyst, "BASE_DIR", Path(tmp_path))
    hf.record_attestation(scope="holdings", base_dir=tmp_path, actor="user")

    def boom(**kwargs):
        raise RuntimeError("ledger unreadable")

    monkeypatch.setattr(hf, "holdings_divergence", boom)

    text = analyst._compute_data_freshness()

    assert "VERY_STALE holdings(diverged)" in text
    assert "holdings(attested)" not in text


def test_the_freshness_score_and_the_snapshot_agree_on_divergence(tmp_path, monkeypatch):
    """2経路が同じ停止条件を持つこと（権威の一致）。"""
    import os
    from pathlib import Path

    import analysis_snapshot as asn
    import analyst

    stale = datetime.now() - timedelta(hours=127)
    holdings = tmp_path / "holdings.json"
    _write(holdings, {"AAPL": {"ticker": "AAPL", "shares": 10}})
    os.utime(holdings, (stale.timestamp(), stale.timestamp()))
    _write(tmp_path / "account.json", {"last_updated": stale.isoformat()})
    monkeypatch.setattr(analyst, "BASE_DIR", Path(tmp_path))
    hf.record_attestation(scope="holdings", base_dir=tmp_path, actor="user")

    diverged = {"diverged": True, "unapplied": [{"ticker": "AAPL"}], "unresolved": []}
    # 両経路とも呼び出し時に holdings_freshness から import するので、
    # 元モジュール側を差し替えれば双方に効く。
    monkeypatch.setattr(hf, "holdings_divergence", lambda **kwargs: diverged)

    snap = asn.build_base_snapshot(base_dir=tmp_path, now=datetime.now())
    score_text = analyst._compute_data_freshness()

    assert snap.holdings.freshness_status == "stale"
    assert "(diverged)" in snap.holdings.source
    assert "VERY_STALE holdings" in score_text
