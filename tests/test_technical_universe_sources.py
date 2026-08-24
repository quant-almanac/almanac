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
import time
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

    def test_a_continuing_proposal_keeps_last_seen_current(self, tmp_path):
        """Codex レビュー再現: last_seen が「最後に新規取得できた日」のまま
        止まり、「最後に提案された日」を表さなくなっていた。

        2日目以降、その銘柄は既に technical row を持つので
        ensure_technical_coverage が取得をスキップし、resolved には
        二度と入らない。resolved だけで last_seen を判定すると、
        毎日提案され続けても初日の日付のまま TTL_DAYS 後に消える。
        """
        day1 = datetime(2026, 8, 1, tzinfo=timezone.utc)
        # continuing は「今も technical_state.json に行がある」ことが前提
        # (Codex レビュー: レジストリに載っているだけの過去の実在確認では
        # 不十分 — 現在の実在を technical_state.json 自身で確認する)。
        (tmp_path / "technical_state.json").write_text(
            json.dumps({"tickers": {"VT": {"data_quality_status": "ok"}}}),
            encoding="utf-8",
        )

        proposed_ticker_registry.record(
            ["VT"], resolved={"VT"}, base_dir=tmp_path, now=day1)
        before = proposed_ticker_registry.load_registered(tmp_path)["VT"]
        assert before["last_seen"] == "2026-08-01"
        assert before["seen_count"] == 1

        # 2日目以降、VT は毎日提案され続けるが行は既にあるので resolved には
        # 入らない (ensure_technical_coverage が取得をスキップするため)。
        for offset in range(1, 21):
            day = day1 + timedelta(days=offset)
            report = proposed_ticker_registry.record(
                ["VT"], resolved=set(), base_dir=tmp_path, now=day)
            assert report["continuing"] == ["VT"]

        after = proposed_ticker_registry.load_registered(tmp_path)["VT"]
        assert after["last_seen"] == "2026-08-21"
        assert after["seen_count"] == 21

        # TTL_DAYS(21) は「最後に提案された日」からの猶予であるべき。
        # 提案が21日連続で続いた後、22日目に登録済みのまま生き残ること。
        day22 = day1 + timedelta(days=21)
        proposed_ticker_registry.record(
            ["VT"], resolved=set(), base_dir=tmp_path, now=day22)
        assert "VT" in proposed_ticker_registry.load_registered(tmp_path)

    def test_continuing_does_not_reset_the_grace_counter_without_a_live_row(self, tmp_path):
        """Codex レビュー再現: レジストリに載っているだけで technical_state.json
        に行が無い銘柄が、continuing 扱いで missed_rebuilds を 0 に戻してしまう。

        再現した状態: MDB は technical_state に不在・直近の topup も
        失敗・missed_rebuilds=2。この状態で MDB が resolved 無しで
        再提案されると、旧実装では「レジストリにある」というだけで
        continuing 扱いになり missed_rebuilds が 0 へリセットされていた —
        ポジティブキャッシュ制約に反し、欠落銘柄が追い出されず
        強制再計算を繰り返し得る。
        """
        proposed_ticker_registry.record(["MDB"], resolved={"MDB"}, base_dir=tmp_path)
        for _ in range(2):
            proposed_ticker_registry.evict_unresolved(
                ["MDB"], base_dir=tmp_path, rebuild_coverage=0.99)
        before = proposed_ticker_registry.load_registered(tmp_path)["MDB"]
        assert before["missed_rebuilds"] == 2

        # technical_state.json に MDB の行は無い (topup も直近の全再計算も
        # 解決できなかった)。それでも再提案された。
        (tmp_path / "technical_state.json").write_text(
            json.dumps({"tickers": {}}), encoding="utf-8")
        report = proposed_ticker_registry.record(
            ["MDB"], resolved=set(), base_dir=tmp_path)

        assert report.get("continuing") != ["MDB"]
        after = proposed_ticker_registry.load_registered(tmp_path)["MDB"]
        assert after["missed_rebuilds"] == 2, "実在しない行で猶予がリセットされた"

    def test_a_continuing_proposal_for_an_unregistered_ticker_is_ignored(self, tmp_path):
        """continuing は「既に登録済み」が前提。未登録銘柄を resolved 無しで
        提案しても新規登録してはならない (ポジティブキャッシュ制約は不変)。
        """
        report = proposed_ticker_registry.record(
            ["ZZQQXX"], resolved=set(), base_dir=tmp_path)

        assert report["status"] == "noop"
        assert proposed_ticker_registry.load_registered(tmp_path) == {}

    def test_a_single_miss_does_not_evict(self, tmp_path):
        """Codex レビュー再現: coverage99% でMDBだけ1回欠けても即追い出さない。

        一時的な Yahoo 障害と本当の上場廃止を区別できないまま即時削除する
        と、翌日また resolved で再登録されるだけの往復が起きる。
        """
        proposed_ticker_registry.record(
            ["JPM", "MDB"], resolved={"JPM", "MDB"}, base_dir=tmp_path)

        report = proposed_ticker_registry.evict_unresolved(
            ["MDB"], base_dir=tmp_path, rebuild_coverage=0.99)

        assert report["status"] == "ok"
        assert report["evicted"] == []
        registered = proposed_ticker_registry.load_registered(tmp_path)
        assert set(registered) == {"JPM", "MDB"}
        assert registered["MDB"]["missed_rebuilds"] == 1

    def test_a_ticker_is_evicted_only_after_consecutive_misses(self, tmp_path):
        """MISSED_REBUILDS_BEFORE_EVICTION 回連続で欠けて初めて追い出す。"""
        proposed_ticker_registry.record(
            ["MDB"], resolved={"MDB"}, base_dir=tmp_path)

        for i in range(proposed_ticker_registry.MISSED_REBUILDS_BEFORE_EVICTION - 1):
            proposed_ticker_registry.evict_unresolved(
                ["MDB"], base_dir=tmp_path, rebuild_coverage=0.99)
            assert "MDB" in proposed_ticker_registry.load_registered(tmp_path), (
                f"{i + 1}回目の欠落で追い出された (閾値未満のはず)"
            )

        report = proposed_ticker_registry.evict_unresolved(
            ["MDB"], base_dir=tmp_path, rebuild_coverage=0.99)

        assert report["evicted"] == ["MDB"]
        assert proposed_ticker_registry.load_registered(tmp_path) == {}

    def test_a_success_between_misses_resets_the_grace_counter(self, tmp_path):
        """猶予の途中で1回でも解決できれば、カウントは積み上がらない。"""
        proposed_ticker_registry.record(
            ["MDB"], resolved={"MDB"}, base_dir=tmp_path)

        proposed_ticker_registry.evict_unresolved(
            ["MDB"], base_dir=tmp_path, rebuild_coverage=0.99)
        assert proposed_ticker_registry.load_registered(tmp_path)["MDB"]["missed_rebuilds"] == 1

        # 次の再計算では解決できた (継続提案としての record() 呼び出し)。
        # continuing は「今も technical_state.json に行がある」ことが前提。
        (tmp_path / "technical_state.json").write_text(
            json.dumps({"tickers": {"MDB": {"data_quality_status": "ok"}}}),
            encoding="utf-8",
        )
        proposed_ticker_registry.record(["MDB"], resolved=set(), base_dir=tmp_path)
        assert proposed_ticker_registry.load_registered(tmp_path)["MDB"]["missed_rebuilds"] == 0

        # 再び欠けても、猶予は 1 からやり直しになる (2 からの続きではない)。
        for _ in range(proposed_ticker_registry.MISSED_REBUILDS_BEFORE_EVICTION - 1):
            proposed_ticker_registry.evict_unresolved(
                ["MDB"], base_dir=tmp_path, rebuild_coverage=0.99)
        assert "MDB" in proposed_ticker_registry.load_registered(tmp_path)

    def test_a_ticker_the_rebuild_could_not_resolve_is_evicted(self, tmp_path):
        """繰り返し欠け続ければ、猶予を使い切って最終的には追い出されること。"""
        proposed_ticker_registry.record(
            ["JPM", "MDB"], resolved={"JPM", "MDB"}, base_dir=tmp_path)

        for _ in range(proposed_ticker_registry.MISSED_REBUILDS_BEFORE_EVICTION):
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

    2026-08-24 の Codex レビューで、旧実装 (書く直前にもう一度読んで比較する
    check-then-replace) は「厳密には CAS ではない」と指摘された: 確認読みと
    実際の os.replace の間にも窓が残り、隔離再現で実際に巻き戻った。
    今は utils.process_lock (fcntl.flock、プロセス間で有効) で
    「読む→合成する→書く」全体を直列化する。以下はモックではなく、実際に
    ロックを取り合うスレッドで真の排他を検証する。
    """

    def test_a_concurrent_writer_holding_the_lock_is_not_clobbered(
        self, tmp_path, loader
    ):
        """真の直列化: ロック保持中の書き込みは、解放後まで待って合成される。

        再現していた最悪順序 (check-then-replace 時代):
          a) topup が古い state を読む (cached_at=旧, XLF=旧値)
          b) cron が新しい全再計算結果を書く (cached_at=新, XLF=999, NEW追加)
          c) topup が (a) の古い内容 + MDB行 を書く
        → (b) の新しい cached_at・XLF・NEW行が消える。
        process_lock を使えば、cron 側がロックを保持している間 topup は
        本当にブロックされ、解放後の最新内容の上で合成するしかない。
        """
        _write_base(tmp_path, rows={
            "XLF": {"price": 50.0, "freshness_status": "fresh",
                    "data_quality_status": "ok", "data_as_of": "2026-08-21"},
        })
        loader({"MDB": _frame()})

        import threading

        from utils import process_lock

        writer_holds_lock = threading.Event()
        release_writer = threading.Event()

        def _simulated_rebuild_writer():
            with process_lock(technical_signals.TECHNICAL_STATE_LOCK_NAME, timeout=5.0):
                writer_holds_lock.set()
                # topup 側がロック待ちで確実にブロックされる時間を作る。
                release_writer.wait(timeout=5.0)
                rebuilt = {
                    "tickers": {
                        "XLF": {"price": 999.0, "freshness_status": "fresh",
                                "data_quality_status": "ok", "data_as_of": "2026-08-24"},
                        "NEW": {"price": 1.0, "freshness_status": "fresh",
                                "data_quality_status": "ok"},
                    },
                    "market_breadth": {"pct_above_ma50": 0.9},
                    "source_health": _source_health(2),
                    "cached_at": "2026-08-24T12:00:00+00:00",
                }
                (tmp_path / "technical_state.json").write_text(
                    json.dumps(rebuilt), encoding="utf-8")

        writer = threading.Thread(target=_simulated_rebuild_writer)
        writer.start()
        assert writer_holds_lock.wait(timeout=5.0), "writer がロックを取得できなかった"

        def _release_shortly():
            # ensure_technical_coverage は手順1〜6 (フェッチ相当・daemon
            # スレッド生成含む) を通ってから手順9のロック区間へ入るので、
            # 「本当に待たされているか」を確実に見るには、そのフェッチ相当の
            # オーバーヘッドより明確に長く保持する。
            time.sleep(0.6)
            release_writer.set()

        threading.Thread(target=_release_shortly).start()

        report = technical_signals.ensure_technical_coverage(["MDB"], base_dir=tmp_path)
        writer.join(timeout=5.0)
        assert not writer.is_alive(), "simulated writer が終了しなかった"

        assert report["status"] == "ok"
        assert report["added"] == ["MDB"]

        state = _read_state(tmp_path)
        # cron (writer) が書いた新しい内容が生きていて、その上に MDB が
        # 追加されていること。旧内容への巻き戻りではない。
        assert state["cached_at"] == "2026-08-24T12:00:00+00:00"
        assert state["tickers"]["XLF"]["price"] == 999.0
        assert "NEW" in state["tickers"]
        assert "MDB" in state["tickers"]

    def test_a_row_already_present_when_the_lock_is_acquired_needs_no_write(
        self, tmp_path, loader
    ):
        """ロック取得時点で既に行があれば (他の書き手が先に済ませていた)、
        そもそも書かない — 既存行を上書きしないという不変条件の一部。
        """
        rebuilt_row = {"price": 999.0, "freshness_status": "fresh",
                       "data_quality_status": "ok", "data_as_of": "2026-08-24"}
        _write_base(tmp_path, rows={"JPM": dict(rebuilt_row)})
        loader({"JPM": _frame()})
        before = (tmp_path / "technical_state.json").read_bytes()

        report = technical_signals.ensure_technical_coverage(["JPM"], base_dir=tmp_path)

        assert report["added"] == []
        assert (tmp_path / "technical_state.json").read_bytes() == before
        state = _read_state(tmp_path)
        assert "coverage_source" not in state["tickers"]["JPM"]
        assert state["tickers"]["JPM"]["price"] == 999.0

    def test_a_lock_held_beyond_the_timeout_gives_up_without_writing(
        self, tmp_path, loader, monkeypatch
    ):
        """ロックが解放されなければ、待った末に書かず諦めること。

        get_technical_context 側の書き込みは JSON の直列化+置換だけなので
        通常ミリ秒で終わる。それでも埋まっているのは異常系で、無限に
        待たず安全側に倒す。
        """
        _write_base(tmp_path)
        loader({"MDB": _frame()})
        monkeypatch.setattr(technical_signals, "TECHNICAL_STATE_LOCK_TIMEOUT_SECONDS", 0.3)

        import threading

        from utils import process_lock

        holder_ready = threading.Event()
        release_holder = threading.Event()

        def _hold_lock():
            with process_lock(technical_signals.TECHNICAL_STATE_LOCK_NAME, timeout=5.0):
                holder_ready.set()
                release_holder.wait(timeout=5.0)

        holder = threading.Thread(target=_hold_lock)
        holder.start()
        assert holder_ready.wait(timeout=5.0), "holder がロックを取得できなかった"

        try:
            before = (tmp_path / "technical_state.json").read_bytes()
            report = technical_signals.ensure_technical_coverage(["MDB"], base_dir=tmp_path)
            after = (tmp_path / "technical_state.json").read_bytes()
        finally:
            release_holder.set()
            holder.join(timeout=5.0)

        assert before == after
        assert report.get("added") != ["MDB"]

    def test_a_topup_landing_mid_rebuild_is_not_erased_by_the_rebuild_write(
        self, tmp_path, monkeypatch
    ):
        """compute_technical_state はロックの外で計算するため、計算が始まった
        「後」に着地した topup 行を知らずに書き込む。process_lock は書き込み
        の排他を保証するだけで、「知らない内容で丸ごと置換する」こと自体は
        防げない (Codex レビュー 2026-08-24 で再現: topup added=['MDB'] の
        直後、続く全再計算の書き込みで MDB が消えた:
        present_after_topup=True, present_after_rebuild=False)。
        get_technical_context は書く直前にもう一度読み、まだ現役の登録銘柄
        (evict_unresolved 未到達) である topup 行を引き継がねばならない。
        """
        _write_base(tmp_path, rows={
            "XLF": {"price": 50.0, "freshness_status": "fresh",
                    "data_quality_status": "ok", "data_as_of": "2026-08-21"},
        })
        (tmp_path / "holdings.json").write_text("{}", encoding="utf-8")
        (tmp_path / "scenario_playbook.json").write_text("{}", encoding="utf-8")

        monkeypatch.setattr(technical_signals, "BASE_DIR", tmp_path)
        monkeypatch.setattr(technical_signals, "CACHE_FILE", tmp_path / "technical_state.json")

        import threading

        rebuild_fetch_started = threading.Event()
        release_rebuild_fetch = threading.Event()

        def _fake_load_ohlcv(tickers):
            if "MDB" in tickers:
                # ensure_technical_coverage 側の取得。即座に返す。
                return {t: _frame() for t in tickers if t == "MDB"}
            # 全再計算側の取得 (XLF 等)。ここで待たせて topup を割り込ませる。
            rebuild_fetch_started.set()
            release_rebuild_fetch.wait(timeout=5.0)
            return {t: _frame() for t in tickers if t == "XLF"}

        monkeypatch.setattr(technical_signals, "_load_ohlcv", _fake_load_ohlcv)

        result: dict = {}

        def _run_rebuild():
            result["state"] = technical_signals.get_technical_context(force=True)

        rebuild_thread = threading.Thread(target=_run_rebuild)
        rebuild_thread.start()
        assert rebuild_fetch_started.wait(timeout=5.0), "全再計算のfetchが開始しなかった"

        # 全再計算がまだ (ロックの外で) fetch 中の間に、提案銘柄が登録され
        # テクニカル行が topup される — レビューが再現した衝突順序そのもの。
        proposed_ticker_registry.record(["MDB"], resolved={"MDB"}, base_dir=tmp_path)
        report = technical_signals.ensure_technical_coverage(["MDB"], base_dir=tmp_path)
        assert report["status"] == "ok"
        assert report["added"] == ["MDB"]
        assert _read_state(tmp_path)["tickers"]["MDB"]["coverage_source"] == (
            technical_signals.COVERAGE_SOURCE_TOPUP
        )

        release_rebuild_fetch.set()
        rebuild_thread.join(timeout=5.0)
        assert not rebuild_thread.is_alive(), "全再計算スレッドが終了しなかった"

        final = _read_state(tmp_path)
        assert "MDB" in final["tickers"], "競合窓のtopup行が全再計算の書き込みで消えた"
        assert final["tickers"]["MDB"]["coverage_source"] == technical_signals.COVERAGE_SOURCE_TOPUP
        assert "XLF" in final["tickers"], "全再計算自身が計算した行も生きているべき"

    def test_an_evicted_topup_ticker_is_not_resurrected_by_the_rebuild(
        self, tmp_path, monkeypatch
    ):
        """引き継ぎはレジストリに現役の銘柄限定。追い出し済み(もしくは一度も
        登録されなかった)行まで救うと、evict_unresolved が本来落とすはずの
        行が全再計算のたびに復活し、technical_state.json が無限に肥大化する。
        """
        _write_base(tmp_path, rows={
            "XLF": {"price": 50.0, "freshness_status": "fresh",
                    "data_quality_status": "ok", "data_as_of": "2026-08-21"},
            "GHOST": {"price": 1.0, "freshness_status": "fresh",
                      "data_quality_status": "ok", "data_as_of": "2026-08-01",
                      "coverage_source": technical_signals.COVERAGE_SOURCE_TOPUP},
        })
        (tmp_path / "holdings.json").write_text("{}", encoding="utf-8")
        (tmp_path / "scenario_playbook.json").write_text("{}", encoding="utf-8")
        (tmp_path / "proposed_ticker_candidates.json").write_text(
            json.dumps({"version": 1, "candidates": []}), encoding="utf-8")

        monkeypatch.setattr(technical_signals, "BASE_DIR", tmp_path)
        monkeypatch.setattr(technical_signals, "CACHE_FILE", tmp_path / "technical_state.json")
        monkeypatch.setattr(
            technical_signals, "_load_ohlcv",
            lambda tickers: {t: _frame() for t in tickers if t == "XLF"},
        )

        technical_signals.get_technical_context(force=True)

        final = _read_state(tmp_path)
        assert "GHOST" not in final["tickers"], "追い出し済みのtopup行が復活した"
        assert "XLF" in final["tickers"]
