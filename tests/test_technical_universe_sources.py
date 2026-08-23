"""テクニカル取得対象の候補ソース配線の検証。

2026-08-21: technical_signals は "screen_results_us.json" を読もうとしていたが、
screener.py はそんなファイルを書かない (US は screen_results.json / 朝バッチは
screen_results_morning.json)。load_json が既定値 {} を返すだけなので例外も
警告も出ず、米国スクリーナー候補が丸ごとテクニカル取得対象から漏れ続けていた。
結果として、その候補が発注候補に上がると technical_data_missing で止まる。
"""
from __future__ import annotations

import json

import technical_signals


class TestCandidateFileWiring:
    def test_the_phantom_us_filename_is_gone(self):
        # 誰も書かないファイル名。参照が残っていると欠落に気づけない。
        assert "screen_results_us.json" not in technical_signals.CANDIDATE_UNIVERSE_FILES

    def test_the_us_screener_outputs_are_actually_read(self):
        # screener.py が実際に書く2つの US 出力先。
        for name in ("screen_results.json", "screen_results_morning.json"):
            assert name in technical_signals.CANDIDATE_UNIVERSE_FILES

    def test_the_jp_screener_output_is_still_read(self):
        assert "screen_results_jp.json" in technical_signals.CANDIDATE_UNIVERSE_FILES

    def test_every_configured_filename_is_one_a_producer_writes(self):
        """設定名が実在の書き手を持つこと。

        screen_results_us.json が長期間放置されたのは、名前と書き手の対応を
        誰も検査していなかったため。
        """
        from pathlib import Path

        producers = "\n".join(
            Path(p).read_text(encoding="utf-8")
            for p in ("screener.py", "short_screener.py", "margin_long_screener.py")
        )
        # ペアトレ/スクイーズは別レーンで書き手が分かれるため対象外。
        checked = {
            "screen_results.json", "screen_results_morning.json",
            "screen_results_jp.json", "short_candidates.json",
            "margin_long_candidates.json",
        }
        for name in checked:
            assert name in technical_signals.CANDIDATE_UNIVERSE_FILES
            assert name in producers, f"{name} を書くコードが見当たらない"


class TestUniverseAssembly:
    def _universe(self, tmp_path, monkeypatch, candidates):
        monkeypatch.setattr(technical_signals, "BASE_DIR", tmp_path)
        (tmp_path / "holdings.json").write_text(
            json.dumps({"AAPL": {"ticker": "AAPL"}}), encoding="utf-8")
        (tmp_path / "scenario_playbook.json").write_text("{}", encoding="utf-8")
        for name, rows in candidates.items():
            (tmp_path / name).write_text(
                json.dumps({"candidates": rows}), encoding="utf-8")
        return technical_signals._build_ticker_universe()

    def test_a_us_screener_candidate_reaches_the_universe(self, tmp_path, monkeypatch):
        universe = self._universe(
            tmp_path, monkeypatch, {"screen_results.json": [{"ticker": "MRNA"}]})
        assert "MRNA" in universe

    def test_a_morning_batch_candidate_reaches_the_universe(self, tmp_path, monkeypatch):
        universe = self._universe(
            tmp_path, monkeypatch, {"screen_results_morning.json": [{"ticker": "TJX"}]})
        assert "TJX" in universe

    def test_holdings_are_still_included(self, tmp_path, monkeypatch):
        assert "AAPL" in self._universe(tmp_path, monkeypatch, {})

    def test_a_missing_candidate_file_is_not_fatal(self, tmp_path, monkeypatch):
        # その日走らなかったレーンがあるのは正常。
        assert self._universe(tmp_path, monkeypatch, {})

    def test_a_completely_absent_candidate_set_is_warned_about(self, tmp_path, monkeypatch, caplog):
        # 個々の不在は正常でも、全滅は配線が壊れている証拠。
        with caplog.at_level("WARNING"):
            self._universe(tmp_path, monkeypatch, {})
        assert any("候補ユニバース" in r.message for r in caplog.records)


# ---------------------------------------------------------------------------
# 実行内テクニカル補完 (ensure_technical_coverage) と提案銘柄レジストリ
#
# AI は保有・セクターETF・主要指数・シナリオ playbook・スクリーナー候補の
# どこにも属さない銘柄を名指しできる。朝の再計算はそれを値付けしていないので
# execution_readiness は technical_data_missing で必ず止める。ゲートは正しい
# ので緩めず、取得の側を補完する — ただし fail-closed の不変条件を1つも
# 崩さずに。以下はその不変条件そのもののテスト。
# ---------------------------------------------------------------------------
import math
from datetime import datetime, timedelta, timezone

import pandas as pd
import pytest

import proposed_ticker_registry


FRESH_CACHED_AT = "2026-08-21T08:06:12.829714+00:00"


def _frame(rows: int = 260, *, end=None, jump: bool = False) -> pd.DataFrame:
    """解析可能な OHLCV。単調増加だと RSI が退化するので軽く振動させる。"""
    index = pd.bdate_range(end=end or pd.Timestamp.today().normalize(), periods=rows)
    close = [100.0 + i * 0.15 + math.sin(i / 3.0) * 2.0 for i in range(rows)]
    if jump:
        # 未調整の分割・併合候補 (>50% の日次変化) を1本だけ仕込む。
        close[-1] = close[-2] * 3.0
    return pd.DataFrame(
        {
            "Open": close,
            "High": [c * 1.01 for c in close],
            "Low": [c * 0.99 for c in close],
            "Close": close,
            "Volume": [1_000_000 + i for i in range(rows)],
        },
        index=index,
    )


def _source_health(analyzed: int) -> dict:
    return {
        "max_lag_sessions": 0,
        "freshness_counts": {"fresh": analyzed},
        "data_quality_counts": {"ok": analyzed},
        "requested_count": analyzed,
        "analyzed_count": analyzed,
        "missing_count": 0,
        "missing_tickers": [],
    }


def _write_base(tmp_path, *, rows=None, cached_at=FRESH_CACHED_AT):
    rows = {"XLF": {"price": 50.0, "freshness_status": "fresh",
                    "data_quality_status": "ok", "data_as_of": "2026-08-21"}} if rows is None else rows
    state = {
        "tickers": rows,
        "market_breadth": {"pct_above_ma50": 0.7, "avg_rsi": 55.0, "bearish_divergences": []},
        "source_health": _source_health(len(rows)),
        "cached_at": cached_at,
    }
    (tmp_path / "technical_state.json").write_text(json.dumps(state), encoding="utf-8")
    return state


def _read_state(tmp_path) -> dict:
    return json.loads((tmp_path / "technical_state.json").read_text(encoding="utf-8"))


class _Loader:
    """_load_ohlcv の差し替え。要求された銘柄を記録する。"""

    def __init__(self, frames=None, *, raises=None, delay=None):
        self.frames = frames or {}
        self.raises = raises
        self.delay = delay
        self.calls: list[list[str]] = []

    def __call__(self, tickers):
        self.calls.append(list(tickers))
        if self.delay:
            import time
            time.sleep(self.delay)
        if self.raises:
            raise self.raises
        return {t: self.frames[t] for t in tickers if t in self.frames}

    @property
    def requested(self) -> set[str]:
        return {t for call in self.calls for t in call}


@pytest.fixture
def loader(monkeypatch):
    def _install(frames=None, **kwargs):
        fake = _Loader(frames, **kwargs)
        monkeypatch.setattr(technical_signals, "_load_ohlcv", fake)
        return fake
    return _install


class TestCoverageTopup:
    def test_a_new_proposal_gets_a_technical_row(self, tmp_path, loader):
        _write_base(tmp_path)
        loader({"JPM": _frame()})

        report = technical_signals.ensure_technical_coverage(["JPM"], base_dir=tmp_path)

        assert report["status"] == "ok"
        assert report["added"] == ["JPM"]
        row = _read_state(tmp_path)["tickers"]["JPM"]
        assert row["composite_score"] is not None
        assert row["freshness_status"] == "fresh"
        assert row["coverage_source"] == technical_signals.COVERAGE_SOURCE_TOPUP

    def test_an_unresolvable_symbol_is_reported_but_gets_no_row(self, tmp_path, loader):
        _write_base(tmp_path)
        loader({})  # 幻覚・上場廃止銘柄は _load_ohlcv が黙って落とす

        report = technical_signals.ensure_technical_coverage(["ZZQQXX"], base_dir=tmp_path)

        assert report["unavailable"] == ["ZZQQXX"]
        assert report["added"] == []
        assert "ZZQQXX" not in _read_state(tmp_path)["tickers"]

    def test_a_short_history_is_refused_at_the_analysis_seam(self, tmp_path, loader):
        """20〜29本は「ローダ通過・解析拒否」になる継ぎ目。

        _load_ohlcv は20本以上を通すが _analyze_ticker は30本未満を None に
        する。行が生まれない = ゲートは今まで通り blocked。
        """
        _write_base(tmp_path)
        loader({"IPOX": _frame(rows=25)})

        report = technical_signals.ensure_technical_coverage(["IPOX"], base_dir=tmp_path)

        assert report["added"] == []
        assert report["unavailable"] == ["IPOX"]
        assert "IPOX" not in _read_state(tmp_path)["tickers"]

    def test_an_existing_row_is_never_overwritten(self, tmp_path, loader):
        """これが無いと fail-closed が ready に化ける。

        朝の再計算が正しく stale と判定した保有銘柄を、実行内の数銘柄取得が
        「stale解除」してしまう経路を塞ぐ。
        """
        stale = {"price": 50.0, "freshness_status": "stale",
                 "data_quality_status": "ok", "data_as_of": "2026-07-01"}
        _write_base(tmp_path, rows={"XLF": dict(stale)})
        fake = loader({"XLF": _frame(), "JPM": _frame()})

        technical_signals.ensure_technical_coverage(["XLF", "JPM"], base_dir=tmp_path)

        assert "XLF" not in fake.requested, "既存行の銘柄は取得すらしてはならない"
        assert _read_state(tmp_path)["tickers"]["XLF"] == stale

    def test_funds_pseudo_and_skip_tickers_are_never_fetched(self, tmp_path, loader):
        _write_base(tmp_path)
        fake = loader({})

        report = technical_signals.ensure_technical_coverage(
            ["SLIM_SP500", "GS_MMF_USD", "CASH_JPY", "MNXACT"], base_dir=tmp_path)

        assert report["requested"] == []
        assert report["status"] == "noop"
        assert fake.calls == []

    def test_a_non_canonical_request_is_not_topped_up(self, tmp_path, loader):
        """裸JPXコードで行を作ると cron の再計算が再現できない。

        行キーは要求文字列でなければゲートが引けないが、_build_ticker_universe
        → _load_ohlcv は正規形でないと解決できない。両立しないので、
        非正規形は今まで通り blocked のまま残す。
        """
        _write_base(tmp_path)
        fake = loader({"7203.T": _frame()})

        report = technical_signals.ensure_technical_coverage(
            ["7203", "7203.T"], base_dir=tmp_path)

        assert report["requested"] == ["7203.T"]
        assert "7203" not in fake.requested
        assert "7203" not in _read_state(tmp_path)["tickers"]

    def test_cached_at_and_source_health_survive_the_merge(self, tmp_path, loader):
        """cached_at を進めると 12:00 の cron が TTL でキャッシュを返し、
        定時の全再計算を握り潰す。source_health は再計算の報告であって
        本関数の報告ではない。
        """
        base = _write_base(tmp_path)
        loader({"JPM": _frame()})

        technical_signals.ensure_technical_coverage(["JPM"], base_dir=tmp_path)

        after = _read_state(tmp_path)
        assert after["cached_at"] == base["cached_at"]
        assert after["source_health"] == base["source_health"]
        assert after["market_breadth"] == base["market_breadth"]

    def test_rows_outside_rebuild_is_derived_not_accumulated(self, tmp_path, loader):
        """再計算を挟まず2回 topup しても突き合わせ式が成立すること。

        当回の len(added) を入れると2回目で壊れる。
        """
        _write_base(tmp_path)
        loader({"JPM": _frame(), "MDB": _frame()})

        technical_signals.ensure_technical_coverage(["JPM"], base_dir=tmp_path)
        technical_signals.ensure_technical_coverage(["MDB"], base_dir=tmp_path)

        state = _read_state(tmp_path)
        assert state["coverage_topup"]["rows_outside_rebuild"] == 2
        assert len(state["tickers"]) == (
            state["source_health"]["analyzed_count"]
            + state["coverage_topup"]["rows_outside_rebuild"]
        )

    def test_every_topup_row_carries_a_recognised_quality_status(self, tmp_path, loader):
        """_ensure_technical_state_fresh の quality_schema_current 契約。

        cached_rows の**全行**が {"ok","blocked"} を持たないと、分析のたびに
        全銘柄の強制再計算が無警告で走る。topup 行も例外ではない。
        """
        _write_base(tmp_path)
        loader({"JPM": _frame(), "SPLT": _frame(jump=True)})

        technical_signals.ensure_technical_coverage(["JPM", "SPLT"], base_dir=tmp_path)

        rows = _read_state(tmp_path)["tickers"]
        assert {"JPM", "SPLT"} <= set(rows)
        assert all(r.get("data_quality_status") in {"ok", "blocked"} for r in rows.values())
        assert rows["SPLT"]["data_quality_status"] == "blocked"

    def test_nothing_is_written_when_no_row_can_be_added(self, tmp_path, loader):
        _write_base(tmp_path)
        loader({})
        path = tmp_path / "technical_state.json"
        before, before_mtime = path.read_bytes(), path.stat().st_mtime

        technical_signals.ensure_technical_coverage(["ZZQQXX"], base_dir=tmp_path)

        assert path.read_bytes() == before
        assert path.stat().st_mtime == before_mtime

    def test_a_missing_base_state_is_a_no_op(self, tmp_path, loader):
        """補完は再計算の補助であって代替ではない。"""
        fake = loader({"JPM": _frame()})

        report = technical_signals.ensure_technical_coverage(["JPM"], base_dir=tmp_path)

        assert report["status"] == "no_base_state"
        assert fake.calls == []
        assert not (tmp_path / "technical_state.json").exists()

    def test_a_timeout_writes_nothing(self, tmp_path, loader):
        _write_base(tmp_path)
        loader({"JPM": _frame()}, delay=0.5)
        path = tmp_path / "technical_state.json"
        before = path.read_bytes()

        report = technical_signals.ensure_technical_coverage(
            ["JPM"], base_dir=tmp_path, timeout_seconds=0.01)

        assert report["status"] == "timeout"
        assert path.read_bytes() == before

    def test_a_loader_exception_is_distinguished_from_unavailable(self, tmp_path, loader):
        """バッチ単位の失敗は実在銘柄まで巻き込む。報告上区別すること。"""
        _write_base(tmp_path)
        loader({}, raises=RuntimeError("yfinance down"))
        path = tmp_path / "technical_state.json"
        before = path.read_bytes()

        report = technical_signals.ensure_technical_coverage(
            ["JPM", "MDB"], base_dir=tmp_path)

        assert report["status"] == "error"
        assert report["added"] == []
        assert path.read_bytes() == before

    def test_a_hung_fetch_does_not_block_process_exit(self, tmp_path):
        """タイムアウトはワーカーを本当に放棄できていること。

        ThreadPoolExecutor だと concurrent.futures.thread の atexit フックが
        ワーカーを join するので、shutdown(wait=False) を呼んでもプロセスは
        取得が終わるまで終了できない。yfinance が固まると
        portfolio_analyst.py が exit で固まり、process_lock を握ったまま次の
        cron に衝突する。in-process では観測できないので子プロセスで測る。
        """
        import subprocess
        import sys
        import time
        from pathlib import Path

        root = Path(__file__).resolve().parent.parent
        (tmp_path / "technical_state.json").write_text(
            json.dumps({"tickers": {"XLF": {"data_quality_status": "ok"}},
                        "source_health": {"analyzed_count": 1},
                        "cached_at": FRESH_CACHED_AT}), encoding="utf-8")
        script = (
            "import sys, time\n"
            f"sys.path.insert(0, {str(root)!r})\n"
            "import technical_signals as ts\n"
            "ts._load_ohlcv = lambda tickers: time.sleep(60)\n"
            "from pathlib import Path\n"
            f"r = ts.ensure_technical_coverage(['JPM'], base_dir=Path({str(tmp_path)!r}),"
            " timeout_seconds=0.2)\n"
            "print(r['status'])\n"
        )
        started = time.monotonic()
        proc = subprocess.run([sys.executable, "-c", script],
                              capture_output=True, text=True, timeout=60)
        elapsed = time.monotonic() - started

        assert proc.returncode == 0, proc.stderr
        assert proc.stdout.strip().endswith("timeout")
        assert elapsed < 30, (
            f"放棄したはずのワーカーがプロセス終了を {elapsed:.1f}s ブロックした"
        )

    def test_the_coverage_marker_is_read_by_no_execution_path(self):
        """coverage_source は監査専用。方針①の実行可能な形。

        ゲート・フィルタ・サイジング・UI のどれかが分岐に使い始めたら、
        「AI提案であること自体にペナルティを設けない」が壊れる。
        """
        from pathlib import Path

        # cwd ではなくリポジトリルートに固定する。相対パスのままだと、
        # pytest をルート以外から起動したとき FileNotFoundError になるだけで
        # 不変条件を検査しなくなる (runbook の起動方法がまさにそれ)。
        root = Path(__file__).resolve().parent.parent
        for name in ("execution_readiness.py", "analyst/__init__.py",
                     "analyst/order_strategy.py", "policy_engine.py"):
            text = (root / name).read_text(encoding="utf-8")
            assert "coverage_source" not in text, f"{name} が監査専用マーカーを読んでいる"


class TestProposedTickerRegistry:
    def _universe_with_registry(self, tmp_path, monkeypatch, rows):
        monkeypatch.setattr(technical_signals, "BASE_DIR", tmp_path)
        (tmp_path / "holdings.json").write_text(
            json.dumps({"AAPL": {"ticker": "AAPL"}}), encoding="utf-8")
        (tmp_path / "scenario_playbook.json").write_text("{}", encoding="utf-8")
        (tmp_path / proposed_ticker_registry.REGISTRY_FILENAME).write_text(
            json.dumps({"version": 1, "candidates": rows}), encoding="utf-8")
        return technical_signals._build_ticker_universe()

    def test_a_registered_ticker_reaches_the_universe(self, tmp_path, monkeypatch):
        """ピースB の本体。これが届かないと cron の再計算で行が消える。"""
        universe = self._universe_with_registry(tmp_path, monkeypatch, [
            {"ticker": "MDB", "first_seen": "2026-08-24",
             "last_seen": "2026-08-24", "seen_count": 1, "source": "ai_proposal"},
        ])
        assert "MDB" in universe

    def test_the_newest_entries_survive_the_consumer_slice(self, tmp_path, monkeypatch):
        """消費側は rows[:30] で切る。古い順のままだと最新の提案が黙って落ちる
        — screen_results_us.json 事件 (508e948) と同じ静かな取りこぼし。
        """
        now = datetime(2026, 8, 24, tzinfo=timezone.utc)
        for i in range(40):
            proposed_ticker_registry.record(
                [f"T{i:02d}"], resolved={f"T{i:02d}"}, base_dir=tmp_path,
                now=now - timedelta(days=39 - i))
        rows = json.loads(
            (tmp_path / proposed_ticker_registry.REGISTRY_FILENAME).read_text(encoding="utf-8")
        )["candidates"]

        assert len(rows) <= technical_signals.CANDIDATE_TICKERS_PER_FILE
        assert rows[0]["ticker"] == "T39", "最新が先頭に来ていない"
        universe = self._universe_with_registry(tmp_path, monkeypatch, rows)
        assert "T39" in universe

    def test_entries_older_than_the_ttl_are_dropped(self, tmp_path):
        now = datetime(2026, 8, 24, tzinfo=timezone.utc)
        proposed_ticker_registry.record(
            ["OLD"], resolved={"OLD"}, base_dir=tmp_path,
            now=now - timedelta(days=proposed_ticker_registry.TTL_DAYS + 1))

        proposed_ticker_registry.record(["NEW"], resolved={"NEW"}, base_dir=tmp_path, now=now)

        assert set(proposed_ticker_registry.load_registered(tmp_path)) == {"NEW"}

    def test_expired_entries_are_pruned_on_a_day_with_no_new_symbol(self, tmp_path):
        """TTL は追加のあった日にしか効かない、では不十分。

        _build_ticker_universe はこのファイルを直接読むので、prune を
        「追加があった日」に限ると、二度と提案されない銘柄が期限を過ぎても
        ユニバースに残り、毎回の再計算で無駄にダウンロードされ続ける。
        """
        now = datetime(2026, 8, 24, tzinfo=timezone.utc)
        proposed_ticker_registry.record(
            ["OLD"], resolved={"OLD"}, base_dir=tmp_path,
            now=now - timedelta(days=proposed_ticker_registry.TTL_DAYS + 1))
        assert set(proposed_ticker_registry.load_registered(tmp_path)) == {"OLD"}

        # 解決した新規銘柄がゼロの日 (= 提案が全部既存銘柄だった日)。
        proposed_ticker_registry.record(
            ["XLF"], resolved=set(), base_dir=tmp_path, now=now)

        assert proposed_ticker_registry.load_registered(tmp_path) == {}

    def test_an_unchanged_registry_is_not_rewritten(self, tmp_path):
        now = datetime(2026, 8, 24, tzinfo=timezone.utc)
        proposed_ticker_registry.record(["JPM"], resolved={"JPM"}, base_dir=tmp_path, now=now)
        path = tmp_path / proposed_ticker_registry.REGISTRY_FILENAME
        before, before_mtime = path.read_bytes(), path.stat().st_mtime

        proposed_ticker_registry.record(["XLF"], resolved=set(), base_dir=tmp_path, now=now)

        assert path.read_bytes() == before
        assert path.stat().st_mtime == before_mtime

    def test_only_resolved_tickers_are_registered(self, tmp_path):
        """ポジティブキャッシュ制約。

        解決できない銘柄をユニバースへ入れると universe_is_complete が恒久的に
        false になり、分析のたびに全銘柄の強制再計算が無警告で走り続ける。
        """
        report = proposed_ticker_registry.record(
            ["JPM", "ZZQQXX"], resolved={"JPM"}, base_dir=tmp_path)

        assert report["registered"] == ["JPM"]
        assert set(proposed_ticker_registry.load_registered(tmp_path)) == {"JPM"}

    def test_a_ticker_the_rebuild_could_not_resolve_is_evicted(self, tmp_path):
        proposed_ticker_registry.record(
            ["JPM", "MDB"], resolved={"JPM", "MDB"}, base_dir=tmp_path)

        proposed_ticker_registry.evict_unresolved(
            ["MDB"], base_dir=tmp_path, rebuild_coverage=0.95)

        assert set(proposed_ticker_registry.load_registered(tmp_path)) == {"JPM"}

    def test_a_broad_outage_does_not_empty_the_registry(self, tmp_path):
        """yfinance 全面障害の日に追い出すと、翌日復活する銘柄まで消える。"""
        proposed_ticker_registry.record(
            ["JPM", "MDB"], resolved={"JPM", "MDB"}, base_dir=tmp_path)

        report = proposed_ticker_registry.evict_unresolved(
            ["JPM", "MDB"], base_dir=tmp_path, rebuild_coverage=0.1)

        assert report["status"] == "skipped_low_coverage"
        assert set(proposed_ticker_registry.load_registered(tmp_path)) == {"JPM", "MDB"}

    def test_the_registry_capacity_matches_the_consumer_slice(self):
        assert (proposed_ticker_registry.MAX_ENTRIES
                <= technical_signals.CANDIDATE_TICKERS_PER_FILE)


class TestConcurrentRebuildMerge:
    """cron の再計算と衝突したときの read-modify-write。

    ensure_technical_coverage は取得前に「既に行がある銘柄」を除外するが、
    それは手順2で読んだスナップショットの話。取得の最中に 12:00 の cron が
    再計算を終えていると、マージ直前の読み直しでは行が既に存在しうる。
    その行は再計算がネイティブに作った新しい行なので、数銘柄の取得結果で
    上書きしてはならない。
    """

    def test_a_row_created_by_a_concurrent_rebuild_is_not_clobbered(
        self, tmp_path, loader, monkeypatch
    ):
        _write_base(tmp_path)
        loader({"JPM": _frame()})

        rebuilt_row = {"price": 999.0, "freshness_status": "fresh",
                       "data_quality_status": "ok", "data_as_of": "2026-08-24"}
        real_load_json = technical_signals.load_json
        seen = {"n": 0}

        def _staggered(path, default=None):
            if str(path).endswith("technical_state.json"):
                seen["n"] += 1
                if seen["n"] >= 2:
                    # 取得中に 12:00 の cron が再計算を終え、ファイルを丸ごと
                    # 置換した状態を再現する。以降の読み直しは新しい行を見る。
                    rebuilt = real_load_json(path, default)
                    rebuilt["tickers"] = {**rebuilt["tickers"], "JPM": dict(rebuilt_row)}
                    (tmp_path / "technical_state.json").write_text(
                        json.dumps(rebuilt), encoding="utf-8")
            return real_load_json(path, default)

        monkeypatch.setattr(technical_signals, "load_json", _staggered)

        report = technical_signals.ensure_technical_coverage(["JPM"], base_dir=tmp_path)

        assert report["added"] == []
        assert seen["n"] >= 2, "マージ直前の読み直しが行われていない"
        state = _read_state(tmp_path)
        assert "coverage_source" not in state["tickers"]["JPM"]
        assert state["tickers"]["JPM"]["price"] == 999.0
