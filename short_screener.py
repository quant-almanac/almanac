"""
short_screener.py — レジーム別空売りスクリーニング（v2.0）

変更点 (v2.0):
  - 対象銘柄: tickers.json の short_scan_tickers から自動ロード（~150銘柄）
  - バッチダウンロード: yf.download(threads=True) で一括取得（逐次→高速化）
  - セクターキャッシュ: data/sector_cache.json（TTL 7日）+ 並列取得
  - 投機ティア: HIGH_RISK（C_弱気のみ）/ MED_RISK（B/C）/ STD（既存ルール）
  - スクイーズリスク警告: shortRatio > 10 → ⚠️MED, > 20 → 🚨HIGH

レジームルール（変更なし）:
  A_強気: 原則禁止（例外: RSI ≥ 80 かつ MA50 +20% 以上）
  B_中立: 弱セクター + (RSI ≥ 62 OR MA50 +8% 以上)
  C_弱気: メイン戦略（RSI ≥ 65 OR MA50 +10% 以上）
  VIX ≥ 50: 全禁止
"""

import json
import os
import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime
from pathlib import Path

import numpy as np
import pandas as pd
import requests
import yfinance as yf
from utils import init_yfinance_timeout

init_yfinance_timeout()

BASE_DIR = Path(__file__).parent

# ---- Telegram ----
TELEGRAM_TOKEN   = os.environ.get('TELEGRAM_TOKEN', '')
TELEGRAM_CHAT_ID = os.environ.get('TELEGRAM_CHAT_ID', '')

# ---- VIX 禁止閾値 ----
def _get_vix_block_threshold() -> float:
    """tunable_params から最新値を取得（fallback 50.0）"""
    try:
        from tunable_params import get as _tp_get
        v = _tp_get("vix_short_ban_threshold", 50.0)
        return float(v) if v is not None else 50.0
    except Exception:
        return 50.0


# 後方互換のため module-level でも保持
VIX_BLOCK_THRESHOLD = _get_vix_block_threshold()

# ---- ファイルパス ----
SHORT_CANDIDATES_FILE = BASE_DIR / 'short_candidates.json'
SECTOR_STRENGTH_FILE  = BASE_DIR / 'sector_strength.json'
SECTOR_CACHE_FILE     = BASE_DIR / 'data' / 'sector_cache.json'

# yfinance.info が 404 を返す ETF（財務諸表を持たない）
# 呼び出し前にスキップしてログノイズを防ぐ
ETF_NO_FUNDAMENTALS = {
    # レバレッジ系
    'SOXL', 'SOXS', 'TQQQ', 'SQQQ', 'SPXL', 'SPXS', 'UPRO', 'SDOW',
    'TMF', 'TMV', 'FAS', 'FAZ', 'TNA', 'TZA', 'FNGU', 'FNGD',
    # SPDR セクター
    'XLK', 'XLF', 'XLE', 'XLV', 'XLY', 'XLI', 'XLP', 'XLU', 'XLB', 'XLRE', 'XLC',
    # その他主要 ETF
    'SOXX', 'SMH', 'XBI', 'IBB', 'ARKK', 'ARKG', 'ARKQ', 'ARKW',
    'IWM', 'QQQ', 'SPY', 'VTI', 'GLD', 'SLV', 'TLT',
}

# ============================================================
# 投機ティア定義
# ============================================================

# C_弱気レジームのみ空売り許可（スクイーズリスク最大）
HIGH_RISK_TICKERS: frozenset[str] = frozenset({
    'MSTR', 'COIN', 'SOXL', 'TQQQ', 'SPXL', 'FNGU',
    'IONQ', 'QUBT', 'RGTI',
    'ARKK', 'ARKG', 'ARKQ', 'LABU',
    'RIOT', 'MARA', 'HUT', 'CLSK', 'BTBT',
    'HOOD', 'RIVN', 'LCID', 'CHPT', 'BLNK',
})

# B_中立 / C_弱気で空売り許可
MED_RISK_TICKERS: frozenset[str] = frozenset({
    'NVDA', 'AMD', 'META', 'TSLA', 'ARM', 'SMCI', 'PLTR', 'CRWV',
    'SOFI', 'AFRM', 'UPST', 'NIO', 'XPEV', 'BABA', 'BIDU',
    'SNOW', 'CRWD', 'DDOG', 'ZS', 'NET', 'MDB', 'TWLO',
})

# それ以外は STD（既存レジームルール通り）


# ============================================================
# ティッカーロード
# ============================================================

def _load_scan_tickers() -> list[str]:
    """tickers.json の short_scan_tickers を返す。なければ all を使用。"""
    tickers_file = BASE_DIR / 'tickers.json'
    if not tickers_file.exists():
        return []
    try:
        with open(tickers_file, encoding='utf-8') as f:
            t = json.load(f)
        return list(t.get('short_scan_tickers', t.get('all', [])))
    except Exception:
        return []


# ============================================================
# ティア管理
# ============================================================

def _get_tier(ticker: str) -> str:
    if ticker in HIGH_RISK_TICKERS:
        return 'HIGH_RISK'
    if ticker in MED_RISK_TICKERS:
        return 'MED_RISK'
    return 'STD'


def _tier_allows_regime(ticker: str, regime: str) -> bool:
    """HIGH_RISK ティア (MSTR/COIN/SOXL/TQQQ/SPXL/FNGU/IONQ等) のレジーム制約。

    既定 (high_risk_override_enabled=false): C_弱気 レジームのみ空売り許可
    オーバーライド (high_risk_override_enabled=true): A_強気/B_中立でも許可
        → ただし後段の _check_regime_* で「RSI≥75 + MA50+25%以上」など厳しい条件で
          実質的にフィルタされるため、ユニバース拡張のみで暴走しない設計。
    """
    if ticker in HIGH_RISK_TICKERS and regime != 'C_弱気':
        try:
            from tunable_params import get as _tp_get
            if bool(_tp_get("high_risk_override_enabled", False)):
                return True  # 全レジームで許可（後段の厳格条件で実質フィルタ）
        except Exception:
            pass
        return False
    return True


# ============================================================
# セクターキャッシュ（TTL: sector/name 7日, shortRatio 1日）
# ============================================================

_SECTOR_CACHE: dict = {}
_SECTOR_CACHE_LOADED = False


def _load_sector_cache() -> dict:
    global _SECTOR_CACHE, _SECTOR_CACHE_LOADED
    if _SECTOR_CACHE_LOADED:
        return _SECTOR_CACHE
    SECTOR_CACHE_FILE.parent.mkdir(exist_ok=True)
    if SECTOR_CACHE_FILE.exists():
        try:
            with open(SECTOR_CACHE_FILE, encoding='utf-8') as f:
                _SECTOR_CACHE = json.load(f)
        except Exception:
            _SECTOR_CACHE = {}
    _SECTOR_CACHE_LOADED = True
    return _SECTOR_CACHE


def _save_sector_cache() -> None:
    try:
        import tempfile
        SECTOR_CACHE_FILE.parent.mkdir(exist_ok=True)
        fd, tmp = tempfile.mkstemp(dir=SECTOR_CACHE_FILE.parent, suffix='.tmp')
        with os.fdopen(fd, 'w', encoding='utf-8') as f:
            json.dump(_SECTOR_CACHE, f, ensure_ascii=False, indent=2)
        os.replace(tmp, SECTOR_CACHE_FILE)
    except Exception:
        pass


def _fetch_ticker_info(ticker: str, now: datetime) -> tuple[str, dict]:
    """1銘柄の yfinance info を取得してキャッシュエントリを返す"""
    # ETF はファンダ取得不可（404）。基本情報のみで返してログノイズを抑える。
    if ticker in ETF_NO_FUNDAMENTALS:
        return ticker, {
            'sector':                'ETF',
            'name':                  ticker,
            'short_ratio':           0.0,
            'cached_at':             now.isoformat(),
            'short_ratio_cached_at': now.isoformat(),
        }
    try:
        info = yf.Ticker(ticker).info
        return ticker, {
            'sector':                info.get('sector', 'Unknown'),
            'name':                  info.get('shortName', ticker),
            'short_ratio':           float(info.get('shortRatio') or 0.0),
            'cached_at':             now.isoformat(),
            'short_ratio_cached_at': now.isoformat(),
        }
    except Exception:
        return ticker, {
            'sector':                'Unknown',
            'name':                  ticker,
            'short_ratio':           0.0,
            'cached_at':             now.isoformat(),
            'short_ratio_cached_at': now.isoformat(),
        }


def _prefetch_sector_cache(tickers: list[str]) -> None:
    """キャッシュ切れ銘柄のセクター情報を並列取得（最大10並列）"""
    cache = _load_sector_cache()
    now   = datetime.now()

    sector_missing: list[str] = []
    short_ratio_missing: list[str] = []

    for t in tickers:
        entry = cache.get(t, {})
        if not entry:
            sector_missing.append(t)
            continue
        try:
            cached_at = datetime.fromisoformat(entry.get('cached_at', '2000-01-01'))
            if (now - cached_at).days >= 7:
                sector_missing.append(t)
                continue
        except Exception:
            sector_missing.append(t)
            continue
        try:
            sr_cached = datetime.fromisoformat(entry.get('short_ratio_cached_at', '2000-01-01'))
            if (now - sr_cached).days >= 1:
                short_ratio_missing.append(t)
        except Exception:
            short_ratio_missing.append(t)

    # セクター情報が古い → 全フィールドを一括更新
    if sector_missing:
        print(f'  セクターキャッシュ更新: {len(sector_missing)}銘柄 (並列取得中)...')
        with ThreadPoolExecutor(max_workers=10) as ex:
            futures = {ex.submit(_fetch_ticker_info, t, now): t for t in sector_missing}
            for future in as_completed(futures):
                ticker, entry = future.result()
                _SECTOR_CACHE[ticker] = entry
        _save_sector_cache()

    # shortRatio のみ古い → shortRatio だけ更新
    remaining = [t for t in short_ratio_missing if t not in sector_missing]
    if remaining:
        print(f'  shortRatio 更新: {len(remaining)}銘柄...')
        def _fetch_short_ratio(ticker: str) -> tuple[str, float]:
            # ETF はファンダ取得不可。short_ratio = 0.0 で返す
            if ticker in ETF_NO_FUNDAMENTALS:
                return ticker, 0.0
            try:
                info = yf.Ticker(ticker).info
                return ticker, float(info.get('shortRatio') or 0.0)
            except Exception:
                return ticker, 0.0

        with ThreadPoolExecutor(max_workers=10) as ex:
            futures = {ex.submit(_fetch_short_ratio, t): t for t in remaining}
            for future in as_completed(futures):
                ticker, ratio = future.result()
                if ticker in _SECTOR_CACHE:
                    _SECTOR_CACHE[ticker]['short_ratio'] = ratio
                    _SECTOR_CACHE[ticker]['short_ratio_cached_at'] = now.isoformat()
        _save_sector_cache()


def _get_sector_cached(ticker: str) -> dict:
    cache = _load_sector_cache()
    return cache.get(ticker, {'sector': 'Unknown', 'name': ticker, 'short_ratio': 0.0})


def _assign_short_lane(candidate: dict, regime: str) -> str:
    """technical 候補を 3レーンに分類(overheat / event / bear)。

    単一の short_universe.classify_short_lane を経由する(分類の単一情報源)。
    BULL過熱逆張り戦略 → overheat、弱気レジーム regime_short → bear。
    """
    try:
        from short_universe import classify_short_lane
    except Exception:
        return 'bear'
    sig = {
        'rsi': candidate.get('rsi'),
        'ma50_diff_pct': candidate.get('pct_from_ma50'),
        'dilution_flag': candidate.get('dilution_flag', False),
        # regime_short(B_中立/C_弱気)は弱気レーン。classify は overheat を優先するので
        # 過熱(rsi≥80)銘柄は弱気局面でも overheat に倒れる(より具体的なテーゼ)。
        'regime': 'BEAR' if regime in ('C_弱気', 'B_中立') else regime,
        'trend': 'down' if regime == 'C_弱気' else '',
    }
    return classify_short_lane(sig) or 'bear'


def _squeeze_risk_label(short_ratio: float) -> str:
    if short_ratio >= 20:
        return '🚨HIGH'
    if short_ratio >= 10:
        return '⚠️MED'
    return ''


def _short_execution_metadata(ticker: str, *, horizon_days: int = 10) -> tuple[dict, dict]:
    """Attach fail-safe short cost and borrow/squeeze metadata to screener rows."""
    market = 'JP' if str(ticker).endswith('.T') else 'US'
    cost_model = {
        'model': 'disclosure_shadow_book',
        'market': market,
        'direction': 'short',
        'horizon_days': horizon_days,
        'available': False,
    }
    tradeability = {
        'ticker': ticker,
        'market': market,
        'untradeable': True,
        'reasons': ['short_tradeability_not_checked'],
        'excluded_from_certify': True,
    }
    try:
        from disclosure_shadow_book import estimate_round_trip_cost_pct, load_config
        cfg = load_config()
        notional = float(cfg.get('notional_jpy') or 100_000)
        cost_model.update({
            'available': True,
            'notional_jpy': round(notional),
        })
        if market == 'JP':
            std = estimate_round_trip_cost_pct(
                market='JP', notional_jpy=notional, config=cfg,
                direction=-1, horizon_days=horizon_days, short_credit_type='standard',
            )
            gen = estimate_round_trip_cost_pct(
                market='JP', notional_jpy=notional, config=cfg,
                direction=-1, horizon_days=horizon_days, short_credit_type='general',
            )
            cost_model['standard_credit_round_trip_cost_pct'] = round(float(std), 6)
            cost_model['general_credit_round_trip_cost_pct'] = round(float(gen), 6)
        else:
            us = estimate_round_trip_cost_pct(
                market='US', notional_jpy=notional, config=cfg,
                direction=-1, horizon_days=horizon_days,
            )
            cost_model['round_trip_cost_pct'] = round(float(us), 6)
    except Exception as exc:
        cost_model['error'] = str(exc)[:160]

    try:
        if market == 'JP':
            from jp_loanability import evaluate_short_tradeability
            tradeability.update(evaluate_short_tradeability(ticker))
            tradeability['market'] = market
        else:
            tradeability.update({
                'untradeable': True,
                'reasons': ['us_short_not_enabled'],
            })
    except Exception as exc:
        tradeability.update({
            'untradeable': True,
            'reasons': ['short_tradeability_check_failed'],
            'error': str(exc)[:160],
        })
    tradeability['excluded_from_certify'] = bool(tradeability.get('untradeable'))
    return cost_model, tradeability


# ============================================================
# バッチ価格ダウンロード
# ============================================================

def _bulk_download(tickers: list[str]) -> dict[str, dict]:
    """
    yf.download で一括 OHLCV 取得（逐次処理を廃止し大幅に高速化）。
    戻り値: { ticker: {'close': pd.Series, 'volume': pd.Series} }
    """
    if not tickers:
        return {}
    try:
        raw = yf.download(
            tickers,
            period='4mo',
            auto_adjust=True,
            threads=True,
            progress=False,
        )
    except Exception:
        return {}

    if raw.empty:
        return {}

    result: dict[str, dict] = {}

    if isinstance(raw.columns, pd.MultiIndex):
        # 複数銘柄: columns = (field, ticker)
        close_df  = raw['Close']
        volume_df = raw['Volume']
        for t in tickers:
            if t not in close_df.columns:
                continue
            close  = close_df[t].dropna()
            volume = volume_df[t].dropna()
            if len(close) >= 52:
                result[t] = {'close': close, 'volume': volume}
    else:
        # 単一銘柄
        t      = tickers[0]
        close  = raw['Close'].dropna()
        volume = raw['Volume'].dropna()
        if len(close) >= 52:
            result[t] = {'close': close, 'volume': volume}

    return result


# ============================================================
# 指標計算
# ============================================================

def _calc_rsi(close: pd.Series, period: int = 14) -> float:
    """RSI を計算して最新値を返す"""
    delta    = close.diff()
    gain     = delta.clip(lower=0)
    loss     = (-delta).clip(lower=0)
    avg_gain = gain.ewm(com=period - 1, min_periods=period).mean()
    avg_loss = loss.ewm(com=period - 1, min_periods=period).mean()
    rs  = avg_gain / avg_loss.replace(0, np.nan)
    rsi = 100 - (100 / (1 + rs))
    return float(rsi.iloc[-1]) if not rsi.empty else 50.0


def _calc_indicators(close: pd.Series, volume: pd.Series) -> dict:
    """Close/Volume 系列から各指標を計算"""
    price         = float(close.iloc[-1])
    ma50          = float(close.rolling(50).mean().iloc[-1])
    rsi           = _calc_rsi(close)
    pct_from_ma50 = (price - ma50) / ma50 * 100 if ma50 > 0 else 0.0
    rets          = close.pct_change().dropna()
    vol20         = float(rets.tail(20).std() * np.sqrt(252) * 100) if len(rets) >= 20 else 0.0
    avg_vol30     = float(volume.tail(30).mean()) if len(volume) >= 30 else float(volume.mean() if len(volume) else 0.0)
    return {
        'price':          price,
        'ma50':           ma50,
        'rsi':            rsi,
        'pct_from_ma50':  pct_from_ma50,
        'vol20':          vol20,
        'avg_volume_30d': avg_vol30,
    }


# ============================================================
# EDGAR ファンダ overlay（空売り視点：悪化 = 加点）
# ============================================================

def _short_fundamental_overlay(ticker: str) -> dict:
    """
    空売り視点では「ファンダ悪化＝加点」（追加減点要因）。
    返却:
      fundamental_score 0-100（悪化度合いを 50→100 にスケール）
      flags: list[str]
    """
    out = {'fundamental_score': 50.0, 'flags': []}
    try:
        from screening_helpers import get_edgar_summary
        f = get_edgar_summary(ticker)
    except Exception:
        return out
    if f.get('source') == 'unavailable':
        return out
    score = 50.0
    flags: list[str] = []
    rg = f.get('revenue_growth')
    eg = f.get('eps_growth')
    gm = f.get('gross_margin')

    if rg is not None and rg < 0:
        score += 12; flags.append('rev_decline')
    elif rg is not None and rg < 0.03:
        score += 5
    if eg is not None and eg < 0:
        score += 12; flags.append('eps_decline')
    elif eg is not None and eg < 0.05:
        score += 5
    if gm is not None and gm < 0.20:
        score += 8; flags.append('gm<20%')
    out['fundamental_score'] = max(0.0, min(100.0, score))
    out['flags'] = flags
    return out


# ============================================================
# 内部ユーティリティ（既存コードを維持）
# ============================================================

def _send_telegram(msg: str) -> None:
    if not TELEGRAM_TOKEN or not TELEGRAM_CHAT_ID:
        return
    try:
        url = f'https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage'
        requests.post(url, data={'chat_id': TELEGRAM_CHAT_ID, 'text': msg,
                                 'parse_mode': 'HTML'}, timeout=10)
    except Exception:
        pass


def _get_vix() -> float:
    """現在の VIX を取得"""
    try:
        vix  = yf.Ticker('^VIX')
        hist = vix.history(period='5d')
        if hist.empty:
            return 20.0
        return float(hist['Close'].iloc[-1])
    except Exception:
        return 20.0


def _get_current_regime() -> str:
    """regime_state.json からレジームを取得する"""
    state_file = BASE_DIR / 'regime_state.json'
    if not state_file.exists():
        return 'B_中立'
    try:
        with open(state_file, encoding='utf-8') as f:
            state = json.load(f)
        if 'regime' in state:
            return state['regime']
        from regime_params import get_regime
        macro_score = state.get('macro_score', 5)
        spy_above   = bool(state.get('spy_above', True))
        return get_regime(macro_score, spy_above)
    except Exception:
        return 'B_中立'


def _get_weak_sectors() -> list[str]:
    """sector_strength.json から弱いセクターリストを返す（日英両方）"""
    _JP_TO_EN: dict[str, list[str]] = {
        'テクノロジー':  ['Technology'],
        '通信':          ['Communication Services', 'Communication'],
        '金融':          ['Financial Services', 'Financial', 'Finance'],
        'ヘルスケア':    ['Healthcare', 'Health Care'],
        '消費財':        ['Consumer Cyclical', 'Consumer Defensive', 'Consumer'],
        'エネルギー':    ['Energy'],
        '公益':          ['Utilities'],
        '素材':          ['Basic Materials', 'Materials'],
        '生活必需品':    ['Consumer Defensive', 'Consumer Staples'],
        '不動産':        ['Real Estate'],
        '資本財':        ['Industrials', 'Industrial'],
    }
    if not SECTOR_STRENGTH_FILE.exists():
        return []
    try:
        with open(SECTOR_STRENGTH_FILE, encoding='utf-8') as f:
            data = json.load(f)
        weak: list[str] = []
        for sector, info in data.items():
            strong = info if isinstance(info, bool) else info.get('strong', True)
            if not strong:
                weak.append(sector)
                for en in _JP_TO_EN.get(sector, []):
                    if en not in weak:
                        weak.append(en)
        return weak
    except Exception:
        return []


def _get_holdings_tickers() -> list[str]:
    """holdings.json からティッカーリストを取得"""
    holdings_file = BASE_DIR / 'holdings.json'
    if not holdings_file.exists():
        return []
    try:
        with open(holdings_file, encoding='utf-8') as f:
            holdings = json.load(f)
        tickers = []
        skip = {'SLIM_SP500', 'SLIM_ORCAN', 'MNXACT', 'IFREE_FANGPLUS',
                'NOMURA_SEMI', 'AVGO_特定', 'AVGO_一般'}
        for key, val in holdings.items():
            if key in skip:
                continue
            ticker = val.get('ticker', key)
            if ticker not in skip:
                tickers.append(ticker)
        return tickers
    except Exception:
        return []


# ============================================================
# レジーム別判定ロジック（既存コードを維持）
# ============================================================

def _check_regime_a(data: dict) -> dict | None:
    """A_強気: 原則禁止。例外: RSI ≥ 80 かつ MA50 +20% 以上"""
    rsi = data['rsi']
    pct = data['pct_from_ma50']
    if rsi >= 80 and pct >= 20.0:
        return {
            'reason':        f'A_強気例外: RSI={rsi:.1f}(≥80) / MA50比+{pct:.1f}%(≥20%)',
            'strength':      'weak',
            'rsi':           rsi,
            'pct_from_ma50': pct,
        }
    return None


def _check_regime_b(data: dict, weak_sectors: list[str]) -> dict | None:
    """B_中立: 弱セクター + (RSI ≥ 62 OR MA50 +8% 以上)"""
    rsi    = data['rsi']
    pct    = data['pct_from_ma50']
    sector = data['sector']

    sector_weak = any(ws.lower() in sector.lower() for ws in weak_sectors) if weak_sectors else False

    if sector_weak and (rsi >= 62 or pct >= 8.0):
        triggers = []
        if rsi >= 62:
            triggers.append(f'RSI={rsi:.1f}(≥62)')
        if pct >= 8.0:
            triggers.append(f'MA50比+{pct:.1f}%(≥8%)')
        return {
            'reason':        f'B_中立: 弱セクター({sector}) / {" / ".join(triggers)}',
            'strength':      'medium',
            'rsi':           rsi,
            'pct_from_ma50': pct,
        }
    if not weak_sectors and (rsi >= 70 or pct >= 12.0):
        triggers = []
        if rsi >= 70:
            triggers.append(f'RSI={rsi:.1f}(≥70)')
        if pct >= 12.0:
            triggers.append(f'MA50比+{pct:.1f}%(≥12%)')
        return {
            'reason':        f'B_中立: {" / ".join(triggers)} ※セクター情報なし',
            'strength':      'medium',
            'rsi':           rsi,
            'pct_from_ma50': pct,
        }
    return None


def _check_regime_c(data: dict) -> dict | None:
    """C_弱気: メイン戦略。RSI ≥ 65 OR MA50 +10% 以上"""
    rsi = data['rsi']
    pct = data['pct_from_ma50']
    reasons = []
    if rsi >= 65:
        reasons.append(f'RSI={rsi:.1f}(≥65)')
    if pct >= 10.0:
        reasons.append(f'MA50比+{pct:.1f}%(≥10%)')
    if reasons:
        return {
            'reason':        'C_弱気: ' + ' / '.join(reasons),
            'strength':      'strong',
            'rsi':           rsi,
            'pct_from_ma50': pct,
        }
    return None


# ============================================================
# メインスクリーニング
# ============================================================

def screen_candidates(
    tickers:          list[str] | None = None,
    regime:           str | None       = None,
    include_holdings: bool             = True,
    us_only:          bool             = False,
    morning:          bool             = False,
) -> dict:
    """
    空売り候補をスクリーニングして返す。

    Returns
    -------
    dict:
        vix         : float  — 現在の VIX
        regime      : str    — 適用レジーム
        vix_blocked : bool   — VIX ≥ 50 で全禁止
        candidates  : list   — 空売り候補リスト（tier / squeeze_risk / short_ratio 追加）
        as_of       : str    — 実行時刻
        scanned     : int    — スキャン銘柄数
    """
    as_of = datetime.now().strftime('%Y-%m-%d %H:%M')
    output_path = (BASE_DIR / 'short_candidates_morning.json') if morning else SHORT_CANDIDATES_FILE

    # VIX チェック
    vix = _get_vix()
    if vix >= VIX_BLOCK_THRESHOLD:
        result = {
            'vix':         vix,
            'regime':      regime or _get_current_regime(),
            'vix_blocked': True,
            'candidates':  [],
            'as_of':       as_of,
            'scanned':     0,
            'message':     f'VIX={vix:.1f} ≥ {VIX_BLOCK_THRESHOLD} のため空売り全禁止',
        }
        _save_candidates(result, output_path)
        return result

    # レジーム取得
    if regime is None:
        regime = _get_current_regime()

    # スキャン対象ティッカー
    scan_tickers = list(tickers) if tickers else _load_scan_tickers()
    if us_only:
        scan_tickers = [t for t in scan_tickers if not t.endswith('.T')]
        print(f'[short] --us-only: {len(scan_tickers)} 銘柄に絞込')
    if include_holdings:
        for t in _get_holdings_tickers():
            if t not in scan_tickers:
                # holdings は us_only 時も例外的に許可（既存ポジション空売りリスク管理のため）
                scan_tickers.append(t)
    from insider_restrictions import filter_allowed_tickers
    scan_tickers = filter_allowed_tickers(scan_tickers)

    # 弱セクター取得
    weak_sectors = _get_weak_sectors()

    # セクターキャッシュ事前取得（初回は並列フェッチ）
    print(f'[{as_of}] セクターキャッシュ確認...')
    _prefetch_sector_cache(scan_tickers)

    # バッチ価格ダウンロード
    print(f'  バッチDL開始: {len(scan_tickers)}銘柄...')
    price_data = _bulk_download(scan_tickers)
    print(f'  取得完了: {len(price_data)}銘柄')

    candidates = []
    for ticker in scan_tickers:
        if ticker not in price_data:
            continue

        # 投機ティアゲート（HIGH_RISK は C_弱気のみ）
        if not _tier_allows_regime(ticker, regime):
            continue

        # 指標計算
        pd_data    = price_data[ticker]
        indicators = _calc_indicators(pd_data['close'], pd_data['volume'])

        # セクター情報（キャッシュから）
        sector_info = _get_sector_cached(ticker)

        data = {
            'ticker': ticker,
            'name':   sector_info['name'],
            'sector': sector_info['sector'],
            **indicators,
        }

        # レジーム別シグナル判定
        signal = None
        if regime == 'A_強気':
            signal = _check_regime_a(data)
        elif regime == 'B_中立':
            signal = _check_regime_b(data, weak_sectors)
        elif regime == 'C_弱気':
            signal = _check_regime_c(data)

        if signal:
            short_ratio = sector_info.get('short_ratio', 0.0)
            if regime == 'A_強気' and short_ratio >= 20:
                # BULL 逆張り short は踏み上げが最大リスク。高 short-ratio は
                # squeeze guard で候補化しない（測定対象にも入れない）ことで fail-closed。
                continue
            # ── 流動性フロア・EDGAR・決算ガード（screening_helpers）──
            try:
                from screening_helpers import (liquidity_ok, get_historical_win_rate,
                                               days_to_next_earnings, calc_composite_score)
            except Exception:
                liquidity_ok = lambda *a, **k: True  # type: ignore
                get_historical_win_rate = lambda *a, **k: 0.5  # type: ignore
                days_to_next_earnings = lambda *a, **k: None  # type: ignore
                calc_composite_score = lambda technical, **k: technical  # type: ignore

            if not liquidity_ok(ticker, data['price'], data.get('avg_volume_30d', 0)):
                continue

            fund = _short_fundamental_overlay(ticker)
            days_to_earn = days_to_next_earnings(ticker)
            earnings_imminent = days_to_earn is not None and 0 <= days_to_earn <= 2
            win_rate = get_historical_win_rate(f"short_{regime}", ticker)
            # 空売り signal['strength'] は文字列（強/中/弱）、簡易マップ
            strength_map = {'強': 80, '中': 60, '弱': 40}
            tech_norm = strength_map.get(str(signal.get('strength', '')), 60)
            composite = calc_composite_score(
                technical=tech_norm,
                fundamental=fund['fundamental_score'],
                ai_conviction=50.0,
                win_rate=win_rate,
                weights=(0.40, 0.40, 0.0, 0.20),
            )
            if earnings_imminent:
                composite = max(0.0, composite - 10)

            # S4D: ニュース・SNS ブースト（空売りは bearish ニュースで +5）
            ns = {"news_signal": None, "news_score": None, "news_boost": 0,
                  "social_bias": None, "social_buzz": 0}
            try:
                from screening_helpers import get_news_social_boost
                ns = get_news_social_boost(ticker, side='short')
                composite = min(100.0, max(0.0, composite + ns['news_boost'] + ns['social_buzz']))
            except Exception:
                pass

            risk_controls = {
                'observe_only_first': True,
                'human_execution_only': True,
                'requires_borrow_cost_check': True,
                'requires_squeeze_guard': True,
                'size_cap_pct_nav': 0.005 if regime == 'A_強気' else 0.01,
                'size_cap_note': (
                    'BULL過熱逆張りは最大NAV 0.5%目安・手動承認必須'
                    if regime == 'A_強気'
                    else '空売り候補は最大NAV 1%目安・手動承認必須'
                ),
                'stop_loss': '直近高値+3% または entry+5% の厳しい方',
                'squeeze_block_short_ratio_gte': 20,
            }
            constraints = [
                'observe_only_first',
                'human_execution_only',
                'borrow_cost_check_required',
                'squeeze_guard_required',
                'position_size_cap_required',
                'hard_stop_required',
            ]
            execution_cost_model, tradeability = _short_execution_metadata(ticker)

            candidates.append({
                'ticker':            ticker,
                'name':              data['name'],
                'price':             data['price'],
                'rsi':               data['rsi'],
                'pct_from_ma50':     data['pct_from_ma50'],
                'vol20':             data['vol20'],
                'sector':            data['sector'],
                'reason':            signal['reason'],
                'strength':          signal['strength'],
                'tier':              _get_tier(ticker),
                'squeeze_risk':      _squeeze_risk_label(short_ratio),
                'short_ratio':       short_ratio,
                # 新フィールド
                'fundamental_score': fund['fundamental_score'],
                'fund_flags':        fund['flags'],
                'win_rate':          round(win_rate, 3),
                'composite_score':   composite,
                'days_to_earnings':  days_to_earn,
                'earnings_imminent': earnings_imminent,
                'news_signal':       ns['news_signal'],
                'news_score':        ns['news_score'],
                'news_boost':        ns['news_boost'],
                'social_bias':       ns['social_bias'],
                'social_buzz':       ns['social_buzz'],
                'strategy':          'bull_overheat_contrarian_short' if regime == 'A_強気' else 'regime_short',
                'observe_only':      True,
                'human_execution_only': True,
                'risk_controls':     risk_controls,
                'constraints':       constraints,
                'risk_flags':        [f'squeeze_risk:{_squeeze_risk_label(short_ratio) or "LOW"}'],
                'execution_cost_model': execution_cost_model,
                'tradeability':      tradeability,
            })

    # ── short_universe tradeability gate（Step C）──
    # 「借りて売れるか」の権威判定を short_universe に一本化。technical 候補に
    # verdict を載せ、shortable=false は observe_only のまま executable に昇格させない。
    # 候補は黙って消さず shortable フラグ + reason で可視化（fail-closed）。
    # 3レーン分離(Step D): 各候補に lane(overheat/event/bear)を付与
    for c in candidates:
        c['lane'] = _assign_short_lane(c, regime)
    try:
        from short_universe import build_short_universe, apply_shortability_gate
        cand_tickers = [c['ticker'] for c in candidates]
        universe = build_short_universe(cand_tickers, write=True)
        candidates = [apply_shortability_gate(c, universe) for c in candidates]
    except Exception as exc:
        # gate 失敗時は fail-closed: 全候補 shortable=false に倒す
        for c in candidates:
            c['shortable'] = False
            c.setdefault('shortability', {})['reasons'] = [f'gate_error: {str(exc)[:120]}']

    # 複合スコア降順ソート（フォールバック: RSI）
    candidates.sort(key=lambda x: x.get('composite_score', x.get('rsi', 0)), reverse=True)

    # S4C: HMM regime confidence で候補数を制限
    try:
        from screening_helpers import get_regime_confidence
        conf = get_regime_confidence()
        if conf < 0.6 and candidates:
            trim = max(1, int(len(candidates) * 0.7))
            print(f"  ⚠️ regime_confidence={conf:.2f} < 0.6 → 候補 {len(candidates)}→{trim} に縮小")
            candidates = candidates[:trim]
    except Exception:
        pass

    shortable_count = sum(1 for c in candidates if c.get('shortable'))
    result = {
        'vix':             vix,
        'regime':          regime,
        'vix_blocked':     False,
        'candidates':      candidates,
        'shortable_count': shortable_count,
        'as_of':           as_of,
        'scanned':         len(price_data),
        'message':         (
            f'VIX={vix:.1f} / レジーム={regime} / '
            f'{len(candidates)}件検出 (うち借株可 {shortable_count}件 / '
            f'スキャン{len(price_data)}銘柄)'
        ),
    }
    _save_candidates(result, output_path)
    return result


def _save_candidates(result: dict, output_path: Path | None = None) -> None:
    from insider_restrictions import filter_signal_records
    result = dict(result)
    result['candidates'] = filter_signal_records(result.get('candidates', []))
    target = output_path or SHORT_CANDIDATES_FILE
    try:
        import tempfile
        fd, tmp = tempfile.mkstemp(dir=target.parent, suffix='.tmp')
        with os.fdopen(fd, 'w', encoding='utf-8') as f:
            json.dump(result, f, ensure_ascii=False, indent=2)
        os.replace(tmp, target)
    except Exception:
        pass


def load_last_candidates() -> dict:
    """最後のスクリーニング結果をロード"""
    if not SHORT_CANDIDATES_FILE.exists():
        return {}
    try:
        with open(SHORT_CANDIDATES_FILE, encoding='utf-8') as f:
            result = json.load(f)
            from insider_restrictions import filter_signal_records
            result['candidates'] = filter_signal_records(result.get('candidates', []))
            return result
    except Exception:
        return {}


def send_short_alert(result: dict) -> None:
    """空売り候補を Telegram に通知"""
    if not result:
        return

    vix     = result.get('vix', 0)
    regime  = result.get('regime', '?')
    blocked = result.get('vix_blocked', False)
    cands   = result.get('candidates', [])

    if blocked:
        print(f'[short_screener] VIX={vix:.1f} ≥ {VIX_BLOCK_THRESHOLD} — 全空売り禁止（Telegram 通知なし）')
        return

    if not cands:
        return

    # 空売り候補の Telegram 通知は廃止。詳細は short_candidates.json / Web UI を参照。
    print(f'[short_screener] {len(cands)} 件候補生成（通知は UI で確認）')


# ============================================================
# CLI
# ============================================================

def _print_result(result: dict) -> None:
    print(f"\n=== 空売りスクリーニング結果 ===")
    print(f"実行時刻 : {result.get('as_of')}")
    print(f"VIX      : {result.get('vix', 0):.2f}")
    print(f"レジーム : {result.get('regime')}")
    print(f"スキャン : {result.get('scanned', '-')}銘柄")

    if result.get('vix_blocked'):
        print(f"\n⚠️  {result.get('message')}")
        return

    cands = result.get('candidates', [])
    if not cands:
        print(f"\n候補なし（{result.get('message')}）")
        return

    print(f"\n候補 {len(cands)} 件:")
    for i, c in enumerate(cands, 1):
        tier_label    = c.get('tier', 'STD')
        squeeze_label = f' {c["squeeze_risk"]}' if c.get('squeeze_risk') else ''
        sr            = c.get('short_ratio', 0.0)
        print(f"\n  [{i}] {c['ticker']} — {c['name']} [{tier_label}]{squeeze_label}")
        if sr:
            print(f"       shortRatio={sr:.1f}日")
        print(f"       価格: ${c['price']:,.2f} / RSI: {c['rsi']:.1f} / MA50比: {c['pct_from_ma50']:+.1f}%")
        print(f"       ボラ(年率): {c['vol20']:.1f}% / セクター: {c['sector']}")
        print(f"       理由: {c['reason']}")


if __name__ == '__main__':
    import argparse

    parser = argparse.ArgumentParser(description='空売りスクリーニング（v2.0: バッチDL対応）')
    parser.add_argument('--regime', choices=['A_強気', 'B_中立', 'C_弱気'],
                        help='レジームを強制指定（省略時は regime_state.json から自動判定）')
    parser.add_argument('--tickers', nargs='+', metavar='TICKER',
                        help='スキャン対象ティッカー（省略時は tickers.json の short_scan_tickers）')
    parser.add_argument('--alert', action='store_true',
                        help='deprecated: JSON/UI only; no Telegram is sent')
    parser.add_argument('--last', action='store_true',
                        help='最後のスクリーニング結果を表示')
    parser.add_argument('--us-only', action='store_true',
                        help='米国銘柄のみ対象（朝バッチ用、.T を除外、holdings は例外）')
    parser.add_argument('--morning', action='store_true',
                        help='朝バッチモード（出力先を short_candidates_morning.json に分離）')
    args = parser.parse_args()

    if args.last:
        result = load_last_candidates()
        if result:
            _print_result(result)
        else:
            print('スクリーニング結果がありません。先に実行してください。')
        sys.exit(0)

    print(f'スクリーニング開始... (レジーム指定: {args.regime or "自動"})')
    result = screen_candidates(
        tickers=args.tickers,
        regime=args.regime,
        us_only=args.us_only,
        morning=args.morning,
    )
    _print_result(result)

    if args.alert:
        send_short_alert(result)
        print('\n--alert is deprecated; Telegram notification was not sent')
