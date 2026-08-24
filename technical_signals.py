"""
technical_signals.py — テクニカル指標計算エンジン

RSI, MACD, Bollinger Bands, 出来高分析をポートフォリオ全銘柄 + マーケットETFに対して計算し、
technical_state.json に書き出す。

Usage:
    python technical_signals.py
"""

from __future__ import annotations

import logging
import time
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

import numpy as np
import pandas as pd
import yfinance as yf

from utils import load_json, atomic_write_json, process_lock, LockBusy
from pseudo_tickers import is_pseudo_market_ticker

logger = logging.getLogger(__name__)
BASE_DIR = Path(__file__).parent

# --- 定数 ---
CACHE_FILE = BASE_DIR / "technical_state.json"
CACHE_TTL = 30 * 60  # 30分

# technical_state.json への書き込みを直列化するロック名。
# ensure_technical_coverage (実行内補完) と get_technical_context
# (cron の全再計算、別プロセス) の両方がこの名前で utils.process_lock を
# 取る。process_lock は fcntl.flock なのでプロセスをまたいで効く。
TECHNICAL_STATE_LOCK_NAME = "technical_state"
# JSON の直列化+置換だけの区間なので、通常はミリ秒で解放される。
# 10秒待っても取れない場合は異常系とみなし、安全側に倒して諦める。
TECHNICAL_STATE_LOCK_TIMEOUT_SECONDS = 10.0

# 価格データの無い投信・現金等はスキップ
SKIP_TICKERS = {
    "SLIM_SP500", "SLIM_ORCAN", "MNXACT",
    "IFREE_FANGPLUS", "NOMURA_SEMI",
    "AVGO_特定", "AVGO_一般",
    "CASH_JPY", "CASH_USD", "CASH_JPY_SBI", "CASH_JPY_SBI_WIFE",
    "GS_MMF_USD",
}

# セクターETF + 主要指数
SECTOR_ETFS = ["XLK", "XLE", "XLF", "XLV", "XLI", "XLP", "XLU"]
MARKET_INDICES = ["SPY", "QQQ", "SOXX", "TLT", "IWM", "EEM", "FXI", "ITA", "SMH", "EWJ"]

# Fresh screener artifacts can introduce a new action ticker that is neither a
# holding nor a scenario-playbook symbol.  If omitted here, execution readiness
# necessarily blocks every such candidate as technical_data_missing.
# screener.py の出力は US=screen_results.json (朝バッチは screen_results_morning.json)、
# JP=screen_results_jp.json。"screen_results_us.json" を書く経路は存在せず、
# load_json が既定値 {} を返すだけで米国スクリーナー候補が丸ごとテクニカル
# 取得対象から漏れていた (2026-08-21 に JPM/VT の technical_data_missing を
# 追って発覚)。欠落は例外にならないので、名前を間違えても誰も気づかない。
CANDIDATE_UNIVERSE_FILES = (
    "margin_long_candidates.json",
    "short_candidates.json",
    "screen_results.json",
    "screen_results_morning.json",
    "screen_results_jp.json",
    "pair_trade_candidates.json",
    "squeeze_candidates.json",
    # AI が保有・指数・playbook・スクリーナー成果物のどこにも無い銘柄を
    # 名指しした場合の受け皿 (proposed_ticker_registry が書く)。実行内の
    # ensure_technical_coverage が行を作れた銘柄だけがここへ登録され、
    # 以降の全再計算がネイティブに行を再生成する。これが無いと、
    # 08:30/12:00/17:05 の再計算がファイルを丸ごと置換するたびに補完行が
    # 消え、「朝 ready → 正午 technical_data_missing」の間欠障害になる。
    # 508e948 と同じ「静かな取りこぼし」を作らないよう、消費側スライス
    # (CANDIDATE_TICKERS_PER_FILE) と registry の MAX_ENTRIES を一致させて
    # あることに注意。
    "proposed_ticker_candidates.json",
)
CANDIDATE_TICKERS_PER_FILE = 30
PRICE_DISCONTINUITY_THRESHOLD = 0.50


def _market_calendar_name(ticker: str) -> str:
    return "JPX" if ticker.endswith((".T", ".JP")) else "NYSE"


def _latest_index_date(df: pd.DataFrame) -> date | None:
    if df is None or df.empty:
        return None
    try:
        value = pd.Timestamp(df.index[-1])
        return value.date()
    except Exception:
        return None


def _weekday_on_or_before(day: date) -> date:
    while day.weekday() >= 5:
        day -= timedelta(days=1)
    return day


def _last_completed_session(ticker: str, *, now: datetime | None = None) -> date:
    now = now or datetime.now(timezone.utc)
    if now.tzinfo is None:
        now = now.replace(tzinfo=timezone.utc)
    try:
        import pandas_market_calendars as mcal  # type: ignore
        calendar = mcal.get_calendar(_market_calendar_name(ticker))
        schedule = calendar.schedule(
            start_date=(now.date() - timedelta(days=12)).isoformat(),
            end_date=now.date().isoformat(),
        )
        completed = schedule[schedule["market_close"] <= now.astimezone(timezone.utc)]
        if not completed.empty:
            return pd.Timestamp(completed.index[-1]).date()
    except Exception:
        pass

    local_tz = ZoneInfo("Asia/Tokyo") if ticker.endswith((".T", ".JP")) else ZoneInfo("America/New_York")
    local = now.astimezone(local_tz)
    close_hour = 15 if ticker.endswith((".T", ".JP")) else 16
    candidate = local.date() if local.hour >= close_hour else local.date() - timedelta(days=1)
    return _weekday_on_or_before(candidate)


def _session_lag(ticker: str, data_as_of: date | None, *, expected: date | None = None) -> int | None:
    if data_as_of is None:
        return None
    expected = expected or _last_completed_session(ticker)
    if data_as_of >= expected:
        return 0
    try:
        import pandas_market_calendars as mcal  # type: ignore
        schedule = mcal.get_calendar(_market_calendar_name(ticker)).schedule(
            start_date=data_as_of.isoformat(),
            end_date=expected.isoformat(),
        )
        return max(0, len(schedule) - 1)
    except Exception:
        lag = 0
        cur = data_as_of
        while cur < expected:
            cur += timedelta(days=1)
            if cur.weekday() < 5:
                lag += 1
    return lag


def _price_discontinuity_reasons(
    close: pd.Series,
    *,
    threshold: float = PRICE_DISCONTINUITY_THRESHOLD,
) -> list[dict]:
    """Return split/merge candidates that make long-window indicators unsafe."""
    numeric = pd.to_numeric(close, errors="coerce").dropna()
    changes = numeric.pct_change().dropna()
    reasons = []
    for index, change in changes[changes.abs() > threshold].items():
        reasons.append({
            "code": "unadjusted_price_discontinuity",
            "date": pd.Timestamp(index).strftime("%Y-%m-%d"),
            "daily_change_pct": round(float(change) * 100.0, 4),
            "threshold_pct": threshold * 100.0,
        })
    return reasons


def _freshness_status(lag_sessions: int | None) -> str:
    if lag_sessions is None:
        return "unknown"
    if lag_sessions == 0:
        return "fresh"
    if lag_sessions == 1:
        return "degraded"
    return "stale"


# ====================================================================
# テクニカル指標の計算（pandas/numpy のみ、外部TAライブラリ不使用）
# ====================================================================

def calc_rsi(close: pd.Series, period: int = 14) -> pd.Series:
    """Wilder's smoothing による RSI を計算"""
    delta = close.diff()
    gain = delta.where(delta > 0, 0.0)
    loss = (-delta).where(delta < 0, 0.0)

    # 最初の period 日間は単純平均
    avg_gain = gain.rolling(window=period, min_periods=period).mean()
    avg_loss = loss.rolling(window=period, min_periods=period).mean()

    # Wilder's exponential smoothing（α = 1/period）
    for i in range(period, len(close)):
        avg_gain.iloc[i] = (avg_gain.iloc[i - 1] * (period - 1) + gain.iloc[i]) / period
        avg_loss.iloc[i] = (avg_loss.iloc[i - 1] * (period - 1) + loss.iloc[i]) / period

    rs = avg_gain / avg_loss
    rsi = 100 - (100 / (1 + rs))
    return rsi


def calc_macd(close: pd.Series, fast: int = 12, slow: int = 26, signal: int = 9):
    """MACD ライン、シグナルライン、ヒストグラムを返す"""
    ema_fast = close.ewm(span=fast, adjust=False).mean()
    ema_slow = close.ewm(span=slow, adjust=False).mean()
    macd_line = ema_fast - ema_slow
    signal_line = macd_line.ewm(span=signal, adjust=False).mean()
    histogram = macd_line - signal_line
    return macd_line, signal_line, histogram


def calc_bollinger(close: pd.Series, period: int = 20, num_std: float = 2.0):
    """ボリンジャーバンドの %B を返す"""
    sma = close.rolling(window=period).mean()
    std = close.rolling(window=period).std()
    upper = sma + num_std * std
    lower = sma - num_std * std
    # %B = (price - lower) / (upper - lower)
    pct_b = (close - lower) / (upper - lower)
    return pct_b, upper, lower, sma


def calc_ma(close: pd.Series, period: int = 50) -> pd.Series:
    """単純移動平均"""
    return close.rolling(window=period).mean()


# ====================================================================
# コンポジットスコア
# ====================================================================

def composite_score(rsi: float, macd_hist: float, bb_pct_b: float) -> int:
    """
    -100 〜 +100 の統合スコア
      RSI: 0-100 → -50..+50 （50中心）
      MACD histogram: 正 → +25, 負 → -25
      BB %B: 0-1 → -25..+25
    """
    # RSI 寄与: (rsi - 50) で -50..+50
    rsi_contrib = rsi - 50.0

    # MACD 寄与: 符号ベース ±25
    macd_contrib = 25.0 if macd_hist > 0 else (-25.0 if macd_hist < 0 else 0.0)

    # BB %B 寄与: %B を 0-1 → -25..+25 にマッピング
    bb_clamped = max(0.0, min(1.0, bb_pct_b))
    bb_contrib = (bb_clamped - 0.5) * 50.0  # -25..+25

    score = rsi_contrib + macd_contrib + bb_contrib
    return int(round(max(-100, min(100, score))))


def score_to_signal(score: int) -> str:
    """コンポジットスコアからシグナルラベルを返す"""
    if score < -50:
        return "strongly_bearish"
    elif score < -20:
        return "bearish"
    elif score < -5:
        return "mildly_bearish"
    elif score <= 5:
        return "neutral"
    elif score <= 20:
        return "mildly_bullish"
    elif score <= 50:
        return "bullish"
    else:
        return "strongly_bullish"


# ====================================================================
# データ取得
# ====================================================================

def _build_ticker_universe() -> list[str]:
    """保有・シナリオ・直近スクリーナー候補からテクニカル対象を構築する。"""
    holdings = load_json(BASE_DIR / "holdings.json", {})
    tickers = set()

    for key, h in holdings.items():
        ticker = h.get("ticker", key)
        # holdings.json のキー名もスキップ対象チェック
        if key in SKIP_TICKERS or ticker in SKIP_TICKERS:
            continue
        tickers.add(ticker)

    tickers.update(SECTOR_ETFS)
    tickers.update(MARKET_INDICES)
    playbook = load_json(BASE_DIR / "scenario_playbook.json", {})
    try:
        from scenario_invariants import scenario_action_tickers_from_playbook
        tickers.update(scenario_action_tickers_from_playbook(playbook))
    except Exception as exc:
        raise RuntimeError("failed to extract scenario action tickers for technical universe") from exc

    for filename in CANDIDATE_UNIVERSE_FILES:
        # 名前を間違えても load_json は {} を返して黙って続行する。それが
        # screen_results_us.json の欠落を長期間見えなくしていたので、
        # 「候補ファイルが1つも実在しない」ときだけは声を出す。個々の
        # 不在は正常 (その日スクリーナーが走っていないレーンもある)。
        payload = load_json(BASE_DIR / filename, {})
        rows = []
        if isinstance(payload, dict):
            for key in ("candidates", "all_candidates", "passed", "picks"):
                value = payload.get(key)
                if isinstance(value, list):
                    rows.extend(value)
        elif isinstance(payload, list):
            rows = payload
        for row in rows[:CANDIDATE_TICKERS_PER_FILE]:
            if not isinstance(row, dict):
                continue
            for key in ("ticker", "symbol", "long_ticker", "short_ticker"):
                ticker = str(row.get(key) or "").strip().upper()
                if ticker and ticker not in SKIP_TICKERS and not is_pseudo_market_ticker(ticker):
                    tickers.add(ticker)
    _present = [f for f in CANDIDATE_UNIVERSE_FILES if (BASE_DIR / f).exists()]
    if not _present:
        logger.warning(
            "候補ユニバースのファイルが1つも見つかりません (%s) — "
            "スクリーナー候補がテクニカル取得対象から漏れます",
            ", ".join(CANDIDATE_UNIVERSE_FILES),
        )
    return sorted(tickers)


def _load_ohlcv(tickers: list[str]) -> dict[str, pd.DataFrame]:
    """
    各ティッカーの OHLCV データを取得。
    1) data/ohlcv/{ticker}.parquet から読み込み（直近60営業日以上あれば採用）
    2) Parquet が無い or 古い場合は yfinance でまとめてダウンロード
    """
    ohlcv_dir = BASE_DIR / "data" / "ohlcv"
    result: dict[str, pd.DataFrame] = {}
    missing: list[str] = []
    stale_fallbacks: dict[str, pd.DataFrame] = {}

    for ticker in tickers:
        parquet_path = ohlcv_dir / f"{ticker}.parquet"
        if parquet_path.exists():
            try:
                df = pd.read_parquet(parquet_path)
                # MultiIndex の場合は展開
                if isinstance(df.columns, pd.MultiIndex):
                    df = df.droplevel(1, axis=1)
                # MA200 まで評価できるよう、可能なら約1年分を保持する。
                if len(df) >= 60:
                    df = df.tail(260).copy()
                    df.attrs["ohlcv_source"] = "parquet"
                    lag = _session_lag(ticker, _latest_index_date(df))
                    if lag == 0:
                        result[ticker] = df
                        continue
                    stale_fallbacks[ticker] = df
                    logger.warning("Parquet基準日遅延 %s: lag_sessions=%s", ticker, lag)
            except Exception as e:
                logger.warning("Parquet読み込み失敗 %s: %s", ticker, e)
        missing.append(ticker)

    # 不足分を yfinance で一括取得
    if missing:
        logger.info("yfinance で %d 銘柄をダウンロード: %s", len(missing), missing)
        try:
            data = yf.download(missing, period="1y", progress=False, threads=True)
            if isinstance(data.columns, pd.MultiIndex):
                # マルチティッカー: columns = (Price, Ticker)
                for ticker in missing:
                    try:
                        df_t = data.xs(ticker, level="Ticker", axis=1)
                        if len(df_t.dropna()) >= 20:
                            fresh = df_t.dropna().copy()
                            fresh.attrs["ohlcv_source"] = "yfinance"
                            result[ticker] = fresh
                    except (KeyError, Exception) as e:
                        logger.warning("yfinance データ抽出失敗 %s: %s", ticker, e)
            elif len(missing) == 1:
                # 単一ティッカー
                if len(data.dropna()) >= 20:
                    fresh = data.dropna().copy()
                    fresh.attrs["ohlcv_source"] = "yfinance"
                    result[missing[0]] = fresh
        except Exception as e:
            logger.warning("yfinance 一括ダウンロード失敗: %s", e)

    # Retrieval failure must not turn an old file into fresh data.  Keep the
    # data for analysis continuity with explicit stale/degraded provenance.
    for ticker, fallback in stale_fallbacks.items():
        result.setdefault(ticker, fallback)

    return result


# ====================================================================
# メイン解析
# ====================================================================

def _analyze_ticker(ticker: str, df: pd.DataFrame) -> dict | None:
    """1銘柄のテクニカル指標を計算して dict で返す"""
    try:
        close = df["Close"].astype(float)
        volume = df["Volume"].astype(float) if "Volume" in df.columns else None

        if len(close) < 30:
            return None

        price = float(close.iloc[-1])
        data_date = _latest_index_date(df)
        lag_sessions = _session_lag(ticker, data_date)
        data_quality_reasons = _price_discontinuity_reasons(close)
        if data_quality_reasons:
            return {
                "price": round(price, 2),
                "data_as_of": data_date.isoformat() if data_date else None,
                "source": df.attrs.get("ohlcv_source") or "unknown",
                "last_completed_session": _last_completed_session(ticker).isoformat(),
                "lag_sessions": lag_sessions,
                "freshness_status": _freshness_status(lag_sessions),
                "data_quality_status": "blocked",
                "data_quality_reasons": data_quality_reasons,
                "change_1d_pct": None,
                "change_5d_pct": None,
                "change_20d_pct": None,
                "ma200_diff_pct": None,
                "rsi": None,
                "rsi_signal": "unavailable",
                "macd_histogram": None,
                "macd_crossover": "unavailable",
                "bb_pct_b": None,
                "bb_signal": "unavailable",
                "volume_ratio": None,
                "volume_trend": "unavailable",
                "composite_score": None,
                "composite_signal": "unavailable",
            }

        # 変化率
        change_1d_pct = round((close.iloc[-1] / close.iloc[-2] - 1) * 100, 2) if len(close) >= 2 else None
        change_5d_pct = round((close.iloc[-1] / close.iloc[-6] - 1) * 100, 2) if len(close) >= 6 else None
        change_20d_pct = round((close.iloc[-1] / close.iloc[-21] - 1) * 100, 2) if len(close) >= 21 else None

        # RSI(14)
        rsi_series = calc_rsi(close, 14)
        rsi_val = float(rsi_series.iloc[-1]) if not np.isnan(rsi_series.iloc[-1]) else 50.0
        if rsi_val < 30:
            rsi_signal = "oversold"
        elif rsi_val > 70:
            rsi_signal = "overbought"
        else:
            rsi_signal = "neutral"

        # MACD(12, 26, 9)
        macd_line, signal_line, histogram = calc_macd(close)
        macd_hist_val = float(histogram.iloc[-1]) if not np.isnan(histogram.iloc[-1]) else 0.0

        # クロスオーバー判定: 直近2本のヒストグラム符号変化
        if len(histogram.dropna()) >= 2:
            prev_hist = float(histogram.dropna().iloc[-2])
            if prev_hist <= 0 < macd_hist_val:
                macd_cross = "bullish"
            elif prev_hist >= 0 > macd_hist_val:
                macd_cross = "bearish"
            else:
                macd_cross = "none"
        else:
            macd_cross = "none"

        # Bollinger Bands(20, 2)
        pct_b, _, _, _ = calc_bollinger(close)
        bb_pct_b_val = float(pct_b.iloc[-1]) if not np.isnan(pct_b.iloc[-1]) else 0.5
        if bb_pct_b_val < 0:
            bb_signal = "below_lower"
        elif bb_pct_b_val < 0.5:
            bb_signal = "lower_half"
        elif bb_pct_b_val < 1.0:
            bb_signal = "upper_half"
        else:
            bb_signal = "above_upper"

        # 出来高分析
        vol_ratio = None
        vol_trend = "unknown"
        if volume is not None and len(volume) >= 20:
            avg_vol_20 = float(volume.tail(20).mean())
            current_vol = float(volume.iloc[-1])
            if avg_vol_20 > 0:
                vol_ratio = round(current_vol / avg_vol_20, 2)
                if vol_ratio > 1.2:
                    vol_trend = "expanding"
                elif vol_ratio < 0.8:
                    vol_trend = "contracting"
                else:
                    vol_trend = "normal"

        ma200_diff_pct = None
        if len(close) >= 200:
            ma200 = float(close.rolling(200).mean().iloc[-1])
            if ma200 > 0:
                ma200_diff_pct = round((price / ma200 - 1.0) * 100.0, 2)

        # コンポジットスコア
        score = composite_score(rsi_val, macd_hist_val, bb_pct_b_val)
        signal = score_to_signal(score)

        return {
            "price": round(price, 2),
            "data_as_of": data_date.isoformat() if data_date else None,
            "source": df.attrs.get("ohlcv_source") or "unknown",
            "last_completed_session": _last_completed_session(ticker).isoformat(),
            "lag_sessions": lag_sessions,
            "freshness_status": _freshness_status(lag_sessions),
            "data_quality_status": "ok",
            "data_quality_reasons": [],
            "change_1d_pct": change_1d_pct,
            "change_5d_pct": change_5d_pct,
            "change_20d_pct": change_20d_pct,
            "ma200_diff_pct": ma200_diff_pct,
            "rsi": round(rsi_val, 1),
            "rsi_signal": rsi_signal,
            "macd_histogram": round(macd_hist_val, 4),
            "macd_crossover": macd_cross,
            "bb_pct_b": round(bb_pct_b_val, 2),
            "bb_signal": bb_signal,
            "volume_ratio": vol_ratio,
            "volume_trend": vol_trend,
            "composite_score": score,
            "composite_signal": signal,
        }

    except Exception as e:
        logger.warning("テクニカル分析失敗 %s: %s", ticker, e)
        return None


# ====================================================================
# 実行内テクニカル補完
# ====================================================================

# 壁時計キャップ。yf.download の timeout=10 は HTTP 1本あたりのソケット
# タイムアウトで、threads=True と内部リトライがあるため実時間は無制限。
COVERAGE_TOPUP_TIMEOUT_SECONDS = 90.0

# coverage_source は監査専用のマーカー。ゲート・フィルタ・サイジング・UI の
# どの実行経路も、これを読んで分岐してはならない。AI 提案であること自体に
# ペナルティを設けないという方針の、実行可能な形。用途は2つだけ:
#   1) 「この提案は朝の全再計算で ready になったのか、実行内の補完で ready に
#      なったのか」に答える唯一の永続的手段
#   2) proposed_ticker_registry の配線ミス検知器 — 登録済みなのにマーカーが
#      残り続けるなら、レジストリが _build_ticker_universe に届いていない
COVERAGE_SOURCE_TOPUP = "topup"


def ensure_technical_coverage(
    tickers,
    *,
    base_dir: Path,
    now: datetime | None = None,
    timeout_seconds: float | None = None,
) -> dict:
    """直近の再計算が拾えなかった銘柄のテクニカル行を追加する。

    追加専用: 既存行は絶対に書き換えない。cached_at は保持し、source_health
    (再計算の報告であって本関数の報告ではない) は触らない。
    {"status","requested","added","unavailable"} を返す。例外は投げない。

    cached_at を進めてはいけない理由は決定的で、
      - get_technical_context の30分TTLを駆動する。11:45 の補完で進めると
        12:00 の cron が「まだ15分」と判断してキャッシュを返し、定時の全
        再計算を握り潰す (cron は force 無し)
      - analysis_snapshot.py と analyst._compute_data_freshness が
        「ユニバースを値付けした時刻」として読む。数銘柄の取得は
        ユニバースを値付けしていない

    source_health を触ってはいけない理由も同様に、
      - max_lag_sessions は 2 以上で分析全体の鮮度スコアを 0 にする
      - missing_count / analyzed_count は _ensure_technical_state_fresh が
        「再計算は完全だった」の判定に使う

    **書き込みは process_lock によるプロセス間ロックで直列化する**
    (2026-08-24, レビュー起因で2段階の修正を経た)。当初は単純な
    os.replace のみで、「読む→合成する→書く」の間に cron の全再計算が
    割り込むと、新しい cached_at・breadth・新規行・既存行の更新を丸ごと
    巻き戻す実害が再現された。次に「書く直前にもう一度読んで比較する」
    check-then-replace を入れたが、その確認と実際の置換の間にも窓が残り、
    厳密には CAS ではなかった (Codex レビューで再現)。今は
    TECHNICAL_STATE_LOCK_NAME を fcntl.flock で取ってから読む→合成する→
    書くを行う。get_technical_context 側の書き込みも同じ名前でロックする
    ので、別プロセスであっても真に直列化される。
    """
    requested_raw = list(tickers or [])
    report: dict = {
        "status": "noop",
        "requested": [],
        "added": [],
        "unavailable": [],
    }
    try:
        from instrument_metadata import canonical_ticker
        # ゲート側と同じ定数を使う。execution_readiness は risk_increasing 分岐で
        # `if not ticker.startswith(FUND_PREFIXES)` としてテクニカル判定自体を
        # 飛ばすので、投信を補完しても発注可否は1ミリも動かない。
        # 遅延 import: execution_readiness は technical_signals を import しない
        # ので循環にはならないが、モジュール読み込み時の依存は増やさない。
        from execution_readiness import FUND_PREFIXES

        base_dir = Path(base_dir)
        now = now or datetime.now(timezone.utc)

        # 1) yfinance のグローバル session に timeout を設定する。
        #    ⚠️ これはプロセス全体の singleton を書き換え、本関数を抜けた後も
        #    残る。「短くするだけ」が成り立つのは、それ以前に誰もこれより
        #    短く設定していない場合だけ。
        try:
            from utils import init_yfinance_timeout
            init_yfinance_timeout()
        except Exception:
            pass

        # 2) ベース state が無ければ何もしない。補完は再計算の補助であって
        #    代替ではない。
        state = load_json(base_dir / "technical_state.json", {})
        if not isinstance(state, dict) or not isinstance(state.get("tickers"), dict):
            return {**report, "status": "no_base_state"}
        existing = set(state["tickers"].keys())

        # 3) 要求の正規化。
        targets: list[str] = []
        for value in requested_raw:
            if not value or not isinstance(value, str):
                continue
            ticker = value.strip()
            if not ticker or ticker in targets:
                continue
            # 非正規形 (裸JPXコード・小文字・保有キー等) は補完しない。
            # ここで取得すると「要求文字列でキーした行」を作ることになるが、
            # その行は cron の再計算が再現できない (_build_ticker_universe →
            # _load_ohlcv が正規形でないと解決できない)。結果は
            # 「朝 ready → 正午 technical_data_missing」という間欠障害で、
            # 恒常的に blocked な現状より悪い。正規形でない要求は今まで通り
            # blocked のまま残す。
            if canonical_ticker(ticker) != ticker:
                continue
            if ticker.startswith(FUND_PREFIXES):
                continue
            if ticker in SKIP_TICKERS or is_pseudo_market_ticker(ticker):
                continue
            if ticker in existing:
                continue
            targets.append(ticker)
        report["requested"] = list(targets)

        # 4) 空なら取得も書き込みもしない。
        if not targets:
            return report

        # 5) 壁時計キャップ付きで取得する。
        #    ワーカーを放棄して安全なのは _load_ohlcv が一切書き込みを
        #    しないから (parquet を読む・yfinance から読む、それだけ)。
        #    書き込みを行う関数へこのパターンを流用してはならない。
        #
        #    ⚠️ ThreadPoolExecutor は使えない。concurrent.futures.thread は
        #    atexit フック (_python_exit) で全ワーカーを join するので、
        #    shutdown(wait=False) を呼んでも「同期待ちを飛ばす」だけで、
        #    プロセス自体は取得が終わるまで終了できない (実測: 6秒のワーカーを
        #    0.2秒で放棄してもプロセスは6.07秒かかる)。yfinance が固まった
        #    ときに portfolio_analyst.py が exit で固まり process_lock を
        #    握ったまま次の cron に衝突する。daemon スレッドはインタプリタ
        #    終了時に落とせるので、本当に放棄できる。
        #
        #    ⚠️ _load_ohlcv が読む parquet は technical_signals.BASE_DIR/
        #    data/ohlcv (モジュールグローバル)。base_dir はサンドボックスに
        #    ならないので、テストは必ず _load_ohlcv を差し替えること。
        import threading

        limit = COVERAGE_TOPUP_TIMEOUT_SECONDS if timeout_seconds is None else float(timeout_seconds)
        outcome: dict = {}

        def _fetch() -> None:
            try:
                outcome["ohlcv"] = _load_ohlcv(list(targets))
            except Exception as exc:  # バッチ全体の失敗。呼び元で再送出する。
                outcome["error"] = exc

        worker = threading.Thread(
            target=_fetch, name="technical-coverage-topup", daemon=True)
        worker.start()
        worker.join(timeout=limit)
        if worker.is_alive():
            logger.warning("テクニカル補完タイムアウト (%.0fs): %s", limit, targets)
            return {**report, "status": "timeout", "unavailable": list(targets)}
        if "error" in outcome:
            raise outcome["error"]
        ohlcv = outcome.get("ohlcv")
        if not isinstance(ohlcv, dict):
            ohlcv = {}

        # 6) 解析。_analyze_ticker は再計算経路と完全に同一のまま呼び、
        #    マーカーは戻り値へ後から付ける。
        added_rows: dict[str, dict] = {}
        unavailable: list[str] = []
        for ticker in targets:
            df = ohlcv.get(ticker)
            if df is None:
                unavailable.append(ticker)
                continue
            row = _analyze_ticker(ticker, df)
            if not row:
                # 30本未満・解析失敗。_load_ohlcv は20本以上を通すので
                # 20〜29本は「ローダ通過・解析拒否」になる。
                unavailable.append(ticker)
                continue
            row = dict(row)
            row["coverage_source"] = COVERAGE_SOURCE_TOPUP
            row["coverage_added_at"] = now.isoformat()
            added_rows[ticker] = row

            # 7) blocked 品質の行は再計算経路 (compute_technical_state) と
            #    同じく sanity 台帳へ記録し、一貫性を保つ。
            if row.get("data_quality_status") == "blocked":
                try:
                    from data_fetcher import append_price_sanity_flags, detect_price_sanity_flags

                    append_price_sanity_flags(
                        detect_price_sanity_flags(
                            ticker, df, threshold=PRICE_DISCONTINUITY_THRESHOLD,
                        )
                    )
                except Exception as exc:
                    logger.warning("価格sanityログ記録失敗 %s: %s", ticker, exc)

        report["unavailable"] = unavailable

        # 8) 1行も追加できなければ書き込まない。
        if not added_rows:
            return report

        # 9) プロセス間ファイルロックでマージする。
        #
        #    旧実装 (check-then-replace) は、書く直前にもう一度読み直して
        #    土台が変わっていないか確認していたが、その「確認」と実際の
        #    os.replace の間にも窓が残っていた。厳密には CAS ではなく
        #    ただの check-then-replace で、両方の書き手が同じロックを
        #    取らない限り閉じない (Codex レビュー 2026-08-24。隔離再現:
        #    12:00 の新 state (XLF=999, NEW 追加) を、その窓に割り込んだ
        #    08:30 の古い state で topup が上書きし、それでも status=ok を
        #    返した)。
        #
        #    process_lock は fcntl.flock によるプロセス間の排他ロックで、
        #    別プロセス (cron が起動する get_technical_context) からも
        #    有効。あちら側の書き込みも同じ名前でロックするので、両者は
        #    真に直列化される。ロック保持区間は「読む→合成する→書く」だけに
        #    絞り、ネットワーク取得 (この関数の手順1〜6、最大90秒) は
        #    ロックの外で終えてある — 保持区間はJSONの読み書きだけで
        #    ミリ秒オーダー。
        path = base_dir / "technical_state.json"
        try:
            with process_lock(
                TECHNICAL_STATE_LOCK_NAME, timeout=TECHNICAL_STATE_LOCK_TIMEOUT_SECONDS
            ):
                latest = load_json(path, {})
                if not isinstance(latest, dict) or not isinstance(latest.get("tickers"), dict):
                    return report
                rows = dict(latest["tickers"])
                inserted = []
                for ticker, row in added_rows.items():
                    # 存在しないキーだけ挿入する。既存行を書き換えると、朝の
                    # 再計算が正しく stale と判定した保有銘柄を数銘柄の取得が
                    # 「stale解除」してしまい、fail-closed だったものが ready に
                    # 化ける。
                    if ticker in rows:
                        continue
                    rows[ticker] = row
                    inserted.append(ticker)
                if not inserted:
                    return report

                merged = dict(latest)
                merged["tickers"] = rows
                source_health = merged.get("source_health")
                analyzed = 0
                if isinstance(source_health, dict):
                    try:
                        analyzed = int(source_health.get("analyzed_count") or 0)
                    except (TypeError, ValueError):
                        analyzed = 0
                merged["coverage_topup"] = {
                    "updated_at": now.isoformat(),
                    "base_cached_at": merged.get("cached_at"),
                    "requested": list(targets),
                    "added": sorted(inserted),
                    "unavailable": sorted(unavailable),
                    # 当回の len(added) ではなく導出値。再計算を挟まず2回
                    # topup した時点で突き合わせ式が壊れるため。
                    # len(tickers) == source_health.analyzed_count + rows_outside_rebuild
                    "rows_outside_rebuild": max(0, len(rows) - analyzed),
                }
                atomic_write_json(path, merged)
        except LockBusy:
            # get_technical_context 側の書き込み (JSON の直列化+置換だけ)
            # は通常ミリ秒で終わるので、タイムアウトまで埋まっているのは
            # 異常系。安全側に倒して今回は書かない。
            logger.warning(
                "テクニカル補完: %s秒待っても書込ロックを取得できず、今回は見送り",
                TECHNICAL_STATE_LOCK_TIMEOUT_SECONDS,
            )
            return report

        report["status"] = "ok"
        report["added"] = sorted(inserted)
        logger.info(
            "テクニカル補完: +%d 行 (%s)", len(inserted), ", ".join(sorted(inserted))
        )
        return report
    except Exception as exc:
        logger.warning("テクニカル補完失敗: %s", exc)
        # バッチ単位の失敗 (yf.download の例外など) と個別銘柄の
        # unavailable を報告上区別する。前者では実在銘柄まで巻き込まれる。
        return {**report, "status": "error", "error": f"{type(exc).__name__}: {exc}"}


def _calc_market_breadth(
    tickers_data: dict[str, dict],
    ohlcv: dict[str, pd.DataFrame],
    holdings_tickers: set[str],
) -> dict:
    """
    マーケットブレッドス（市場全体の状況）
      - 保有銘柄のうち MA50 上にいる割合
      - 保有銘柄の平均 RSI
      - ベアリッシュ・ダイバージェンス検出
    """
    above_ma50 = 0
    total_counted = 0
    rsi_values = []
    bearish_divs = []

    for ticker in holdings_tickers:
        if ticker not in tickers_data or ticker not in ohlcv:
            continue
        td = tickers_data[ticker]
        if td.get("data_quality_status") == "blocked":
            continue
        df = ohlcv[ticker]
        close = df["Close"].astype(float)

        # MA50 判定
        if len(close) >= 50:
            ma50 = float(close.rolling(50).mean().iloc[-1])
            total_counted += 1
            if td["price"] > ma50:
                above_ma50 += 1

        rsi_values.append(td["rsi"])

        # ベアリッシュ・ダイバージェンス: 直近20日で価格が高値更新しているが RSI が低下
        if len(close) >= 20:
            rsi_series = calc_rsi(close, 14)
            recent_close = close.tail(20)
            recent_rsi = rsi_series.tail(20).dropna()
            if len(recent_rsi) >= 10:
                mid = len(recent_close) // 2
                # 後半の最高値が前半の最高値を上回る
                price_new_high = float(recent_close.iloc[mid:].max()) > float(recent_close.iloc[:mid].max())
                # 後半の RSI 最高値が前半より低い
                rsi_declining = float(recent_rsi.iloc[mid:].max()) < float(recent_rsi.iloc[:mid].max())
                if price_new_high and rsi_declining:
                    bearish_divs.append(ticker)

    pct_above = round(above_ma50 / total_counted, 2) if total_counted > 0 else 0.0
    avg_rsi = round(float(np.mean(rsi_values)), 1) if rsi_values else 50.0

    return {
        "pct_above_ma50": pct_above,
        "avg_rsi": avg_rsi,
        "bearish_divergences": bearish_divs,
    }


# ====================================================================
# パブリック API
# ====================================================================

def compute_technical_state() -> dict:
    """全銘柄のテクニカル指標を計算し、結果辞書を返す"""
    tickers = _build_ticker_universe()
    logger.info("テクニカル分析対象: %d 銘柄", len(tickers))

    ohlcv = _load_ohlcv(tickers)
    logger.info("OHLCV 取得完了: %d / %d 銘柄", len(ohlcv), len(tickers))

    # 保有銘柄のティッカー一覧（breadth 計算用）
    holdings = load_json(BASE_DIR / "holdings.json", {})
    holdings_tickers = set()
    for key, h in holdings.items():
        t = h.get("ticker", key)
        if key not in SKIP_TICKERS and t not in SKIP_TICKERS:
            holdings_tickers.add(t)

    tickers_result = {}
    for ticker in tickers:
        if ticker not in ohlcv:
            logger.warning("OHLCVデータなし: %s（スキップ）", ticker)
            continue
        analysis = _analyze_ticker(ticker, ohlcv[ticker])
        if analysis:
            tickers_result[ticker] = analysis
            if analysis.get("data_quality_status") == "blocked":
                try:
                    from data_fetcher import append_price_sanity_flags, detect_price_sanity_flags

                    append_price_sanity_flags(
                        detect_price_sanity_flags(
                            ticker,
                            ohlcv[ticker],
                            threshold=PRICE_DISCONTINUITY_THRESHOLD,
                        )
                    )
                except Exception as exc:
                    logger.warning("価格sanityログ記録失敗 %s: %s", ticker, exc)

    # 再計算が行を作れなかった登録銘柄をレジストリから追い出す。
    # 解決不能な銘柄をユニバースに残すと _ensure_technical_state_fresh の
    # universe_is_complete が恒久的に false になり、分析のたびに全銘柄の
    # 強制再計算が無警告で走り続ける。この位置は既に副作用を持つ
    # (append_price_sanity_flags) ので新種の挙動ではない。
    _missing = sorted(set(tickers) - set(tickers_result))
    if _missing:
        try:
            import proposed_ticker_registry

            proposed_ticker_registry.evict_unresolved(
                _missing,
                base_dir=BASE_DIR,
                rebuild_coverage=(len(tickers_result) / len(tickers)) if tickers else 0.0,
            )
        except Exception as exc:
            logger.warning("提案銘柄レジストリの整理に失敗: %s", exc)

    breadth = _calc_market_breadth(tickers_result, ohlcv, holdings_tickers)

    lag_values = [
        int(row["lag_sessions"])
        for row in tickers_result.values()
        if isinstance(row, dict) and row.get("lag_sessions") is not None
    ]
    freshness_counts: dict[str, int] = {}
    quality_counts: dict[str, int] = {}
    for row in tickers_result.values():
        status = str(row.get("freshness_status") or "unknown")
        freshness_counts[status] = freshness_counts.get(status, 0) + 1
        quality = str(row.get("data_quality_status") or "unknown")
        quality_counts[quality] = quality_counts.get(quality, 0) + 1

    return {
        "tickers": tickers_result,
        "market_breadth": breadth,
        "source_health": {
            "max_lag_sessions": max(lag_values) if lag_values else None,
            "freshness_counts": freshness_counts,
            "data_quality_counts": quality_counts,
            "requested_count": len(tickers),
            "analyzed_count": len(tickers_result),
            "missing_count": len(set(tickers) - set(tickers_result)),
            "missing_tickers": sorted(set(tickers) - set(tickers_result))[:30],
        },
        "cached_at": datetime.now(timezone.utc).isoformat(),
    }


def get_technical_context(*, force: bool = False) -> dict:
    """
    テクニカル指標を返す（キャッシュ TTL 30分）。
    キャッシュが有効ならファイルから読み込み、期限切れなら再計算して保存。
    """
    cached = load_json(CACHE_FILE, {})
    if not force and cached.get("cached_at"):
        try:
            cached_dt = datetime.fromisoformat(cached["cached_at"])
            age = (datetime.now(timezone.utc) - cached_dt).total_seconds()
            if age < CACHE_TTL:
                logger.info("キャッシュ有効（残り %d 秒）", int(CACHE_TTL - age))
                return cached
        except Exception:
            pass

    state = compute_technical_state()
    # ensure_technical_coverage (実行内補完) と同じロックを取ってから書く。
    # compute_technical_state 自体はこのファイルへ書き込まない (ネットワーク
    # 取得は utils.process_lock の外) ので、ロック保持区間はこの書き込み
    # だけに絞れる。取れなければ LockBusy を伝播させる —— 全再計算の結果を
    # 書けないのは異常系で、次回のスケジュール実行に委ねるのが妥当。
    with process_lock(TECHNICAL_STATE_LOCK_NAME, timeout=TECHNICAL_STATE_LOCK_TIMEOUT_SECONDS):
        atomic_write_json(CACHE_FILE, state)
    logger.info("technical_state.json 更新完了")
    return state


# ====================================================================
# CLI
# ====================================================================

def _print_summary(state: dict) -> None:
    """ターミナル向けサマリーテーブルを出力"""
    tickers = state.get("tickers", {})
    breadth = state.get("market_breadth", {})

    # ヘッダー
    header = f"{'Ticker':<10} {'Price':>10} {'RSI':>6} {'RSI Sig':>10} {'MACD Hist':>10} {'Cross':>8} {'%B':>6} {'BB Sig':>12} {'Vol Ratio':>10} {'Score':>6} {'Signal':>18}"
    print("=" * len(header))
    print("  ALMANAC テクニカルシグナル サマリー")
    print(f"  計算時刻: {state.get('cached_at', 'N/A')}")
    print("=" * len(header))
    print(header)
    print("-" * len(header))

    # スコア順にソート
    sorted_tickers = sorted(
        tickers.items(),
        key=lambda x: (
            x[1].get("composite_score") is None,
            x[1].get("composite_score") or 0,
        ),
    )
    for ticker, d in sorted_tickers:
        if d.get("data_quality_status") == "blocked":
            print(f"{ticker:<10} {'DATA BLOCKED':>10}  unadjusted split/merge candidate")
            continue
        vol_str = f"{d['volume_ratio']:.2f}" if d.get("volume_ratio") is not None else "N/A"
        print(
            f"{ticker:<10} {d['price']:>10.2f} {d['rsi']:>6.1f} {d['rsi_signal']:>10} "
            f"{d['macd_histogram']:>10.4f} {d['macd_crossover']:>8} {d['bb_pct_b']:>6.2f} "
            f"{d['bb_signal']:>12} {vol_str:>10} {d['composite_score']:>6} {d['composite_signal']:>18}"
        )

    print("-" * len(header))
    print(f"\n  市場ブレッドス:")
    print(f"    MA50 上の銘柄割合: {breadth.get('pct_above_ma50', 0):.0%}")
    print(f"    保有銘柄 平均RSI:  {breadth.get('avg_rsi', 0):.1f}")
    divs = breadth.get("bearish_divergences", [])
    print(f"    弱気ダイバージェンス: {', '.join(divs) if divs else 'なし'}")
    print()


if __name__ == "__main__":
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    )
    t0 = time.time()
    state = get_technical_context()
    elapsed = time.time() - t0
    _print_summary(state)
    print(f"  完了: {elapsed:.1f}秒 / {len(state.get('tickers', {}))} 銘柄分析済み")
