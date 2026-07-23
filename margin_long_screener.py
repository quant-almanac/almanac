"""
margin_long_screener.py — 信用買いスクリーニング

信用買いに特化した条件でスコアリングする。空売りとは逆に、
「押し目反発・モメンタム継続」狙いの銘柄を選別する。

レジーム別ルール:
  A_強気: 積極活用（モメンタム + 押し目 両戦略）
  B_中立: 限定活用（押し目反発 + 強セクターのみ）
  C_弱気: 条件付き許可（押し目反発のみ / スコア≥25 / サイズ半分 / 最大3銘柄 / 損切り-5%）
  VIX > 35: 禁止（高ボラ時の信用買いはリスク過大）

スコアリング軸:
  - RSI（40〜60 が押し目エントリーの理想帯）
  - 50日線からの乖離（-5〜+10% がエントリー圏）
  - 出来高比率（直近 vs 20日平均）
  - 20日ボラティリティ（低い方がスコア高）
  - 強セクター補正

crontab: 55 17 * * 1-5  (平日17:55)
  cd ~/portfolio-bot && venv/bin/python margin_long_screener.py
"""

import json
import os
import sys
import time
from datetime import datetime
from pathlib import Path

import numpy as np
import pandas as pd
import requests
import yfinance as yf
from utils import init_yfinance_timeout

init_yfinance_timeout()

BASE_DIR = Path(__file__).parent

TELEGRAM_TOKEN   = os.environ.get('TELEGRAM_TOKEN', '')
TELEGRAM_CHAT_ID = os.environ.get('TELEGRAM_CHAT_ID', '')

def _get_vix_block_threshold() -> float:
    """tunable_params から最新値を取得（fallback 35.0）"""
    try:
        from tunable_params import get as _tp_get
        v = _tp_get("vix_margin_buy_block", 35.0)
        return float(v) if v is not None else 35.0
    except Exception:
        return 35.0


# 後方互換のため module-level でも保持（再代入で動的更新は不可、初回読み込み時のみ tunable_params 反映）
VIX_BLOCK_THRESHOLD = _get_vix_block_threshold()   # これ以上は信用買い禁止

RESULTS_FILE = BASE_DIR / 'margin_long_candidates.json'
SECTOR_STRENGTH_FILE = BASE_DIR / 'sector_strength.json'

# ---- スキャン対象 ----
# 旧: ハードコード 50 銘柄。新: tickers.json["margin_long_universe"] から動的ロード（53+）
def _default_scan_tickers() -> list[str]:
    try:
        from screening_helpers import load_universe
        ts = load_universe("margin_long_universe")
        return ts or load_universe("all")
    except Exception:
        return []


DEFAULT_SCAN_TICKERS = _default_scan_tickers()


# ============================================================
# ユーティリティ
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


def _calc_rsi(close: pd.Series, period: int = 14) -> float:
    delta = close.diff()
    gain  = delta.clip(lower=0)
    loss  = (-delta).clip(lower=0)
    avg_gain = gain.ewm(com=period - 1, min_periods=period).mean()
    avg_loss = loss.ewm(com=period - 1, min_periods=period).mean()
    rs  = avg_gain / avg_loss.replace(0, np.nan)
    rsi = 100 - (100 / (1 + rs))
    return float(rsi.iloc[-1]) if not rsi.empty else 50.0


def _get_vix() -> float:
    try:
        hist = yf.Ticker('^VIX').history(period='5d')
        return float(hist['Close'].iloc[-1]) if not hist.empty else 20.0
    except Exception:
        return 20.0


def _get_regime() -> str:
    state_file = BASE_DIR / 'regime_state.json'
    if not state_file.exists():
        return 'B_中立'
    try:
        state = json.loads(state_file.read_text())
        spy_above = bool(state.get('spy_above', True))
        nk_above  = bool(state.get('nk_above', True))
        if spy_above and nk_above:
            return 'A_強気'
        if not spy_above and not nk_above:
            return 'C_弱気'
        return 'B_中立'
    except Exception:
        return 'B_中立'


def _get_strong_sectors() -> list[str]:
    if not SECTOR_STRENGTH_FILE.exists():
        return []
    try:
        data = json.loads(SECTOR_STRENGTH_FILE.read_text())
        return [
            sec for sec, info in data.items()
            if (info if isinstance(info, bool) else info.get('strong', False))
        ]
    except Exception:
        return []


def _get_ticker_data(ticker: str) -> dict | None:
    try:
        tk   = yf.Ticker(ticker)
        hist = tk.history(period='120d')
        if hist.empty or len(hist) < 52:
            return None

        close  = hist['Close'].dropna()
        volume = hist['Volume'].dropna()
        price  = float(close.iloc[-1])
        ma50   = float(close.rolling(50).mean().iloc[-1])
        ma20   = float(close.rolling(20).mean().iloc[-1])
        rsi    = _calc_rsi(close)

        pct_from_ma50 = (price - ma50) / ma50 * 100 if ma50 > 0 else 0.0
        pct_from_ma20 = (price - ma20) / ma20 * 100 if ma20 > 0 else 0.0

        # 出来高比率（直近3日 vs 20日平均）
        vol_avg20 = float(volume.tail(20).mean())
        vol_recent = float(volume.tail(3).mean())
        vol_ratio = vol_recent / vol_avg20 if vol_avg20 > 0 else 1.0

        # 20日ボラティリティ（年率%）
        rets  = close.pct_change().dropna()
        vol20 = float(rets.tail(20).std() * np.sqrt(252) * 100) if len(rets) >= 20 else 30.0

        # 直近5日リターン
        ret5d = (price / float(close.iloc[-6]) - 1) * 100 if len(close) >= 6 else 0.0

        # 52週高値比
        high52 = float(close.tail(252).max())
        pct_from_52w_high = (price - high52) / high52 * 100 if high52 > 0 else 0.0

        info   = tk.info
        sector = info.get('sector', 'Unknown')
        name   = info.get('shortName', ticker)

        return {
            'ticker':           ticker,
            'name':             name,
            'price':            round(price, 2),
            'ma50':             round(ma50, 2),
            'ma20':             round(ma20, 2),
            'rsi':              round(rsi, 1),
            'pct_from_ma50':    round(pct_from_ma50, 1),
            'pct_from_ma20':    round(pct_from_ma20, 1),
            'vol_ratio':        round(vol_ratio, 2),
            'vol20':            round(vol20, 1),
            'ret5d':            round(ret5d, 1),
            'pct_from_52w_high': round(pct_from_52w_high, 1),
            'sector':           sector,
            # 流動性フロア用（30日平均出来高）
            'avg_volume_30d':   float(volume.tail(30).mean()) if len(volume) >= 30 else float(vol_avg20),
        }
    except Exception:
        return None


# ============================================================
# ファンダメンタル統合（EDGAR）+ 複合スコア
# ============================================================

def _fundamental_overlay(ticker: str) -> dict:
    """
    EDGAR ファンダから「信用買い」目線の加点/除外シグナルを構築。
    返却:
      {
        'fundamental_score': 0-100（加点 = 健全）
        'skip_reason': str | None  ファンダ悪化により SKIP すべき場合の理由
        'flags': list[str]
      }
    """
    out = {'fundamental_score': 50.0, 'skip_reason': None, 'flags': []}
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
    roe = f.get('roe')
    gm = f.get('gross_margin')

    # 加点（押し目反発はファンダ良好な銘柄に限定したい）
    if rg is not None and rg > 0.15:
        score += 12
        flags.append('rev>15%')
    elif rg is not None and rg > 0.05:
        score += 5
        flags.append('rev>5%')
    if eg is not None and eg > 0.20:
        score += 12
        flags.append('eps>20%')
    elif eg is not None and eg > 0.05:
        score += 5
    if roe is not None and roe > 0.20:
        score += 8
        flags.append('roe>20%')
    if gm is not None and gm > 0.40:
        score += 5

    # 除外: ファンダ悪化銘柄は信用買い対象から外す
    if rg is not None and rg < -0.10:
        out['skip_reason'] = f'revenue_growth={rg:.1%} (<-10%)'
    elif eg is not None and eg < -0.20:
        out['skip_reason'] = f'eps_growth={eg:.1%} (<-20%)'

    out['fundamental_score'] = max(0.0, min(100.0, score))
    out['flags'] = flags
    return out


# ============================================================
# レジーム別判定ロジック
# ============================================================

def _check_momentum(data: dict, strong_sectors: list[str]) -> dict | None:
    """
    モメンタム戦略: 上昇トレンド継続中の押し目エントリー
    - RSI 45〜65（過熱せず、かつ売られすぎでもない）
    - MA50 上（+0〜+10% の範囲が理想）
    - 出来高比率 ≥ 1.0（平均以上）
    """
    rsi = data['rsi']
    pct_ma50 = data['pct_from_ma50']
    vol_ratio = data['vol_ratio']

    if not (45 <= rsi <= 65):
        return None
    if not (-2 <= pct_ma50 <= 12):
        return None
    if vol_ratio < 1.0:
        return None

    score = 0.0
    score += (65 - abs(rsi - 55)) * 1.5   # RSI が 55 に近いほど高スコア
    score += max(0, 10 - pct_ma50) * 2     # MA50 に近いほど高スコア（押し目）
    score += vol_ratio * 5
    score -= data['vol20'] * 0.3           # ボラティリティペナルティ

    if data['sector'] in strong_sectors:
        score *= 1.25

    return {
        'strategy':    'モメンタム押し目',
        'reason':      f"RSI {rsi:.1f} / MA50比{pct_ma50:+.1f}% / 出来高{vol_ratio:.1f}倍",
        'score':       round(score, 1),
        'stop_loss_pct': -7.0,
        'holding_period': '2〜4週間',
    }


def _check_pullback(data: dict, strong_sectors: list[str]) -> dict | None:
    """
    押し目反発戦略: 一時的に売られすぎた強い銘柄
    - RSI 30〜50（売られすぎ回復局面）
    - MA50 近辺〜やや下（-8〜+3%）
    - 52週高値から -20〜-40%（調整圏）
    """
    rsi = data['rsi']
    pct_ma50 = data['pct_from_ma50']
    high52_pct = data['pct_from_52w_high']

    if not (30 <= rsi <= 52):
        return None
    if not (-10 <= pct_ma50 <= 5):
        return None
    if not (-45 <= high52_pct <= -15):
        return None

    score = 0.0
    score += (52 - rsi) * 2              # RSI が低いほど反発余地大
    score += abs(pct_ma50) * 1.5         # MA50 乖離が大きいほど反発余地
    score += abs(high52_pct) * 0.5       # 高値からの下落幅
    score += data['vol_ratio'] * 4
    score -= data['vol20'] * 0.4

    if data['sector'] in strong_sectors:
        score *= 1.2

    return {
        'strategy':    '押し目反発',
        'reason':      f"RSI {rsi:.1f}(反発圏) / MA50比{pct_ma50:+.1f}% / 高値比{high52_pct:.1f}%",
        'score':       round(score, 1),
        'stop_loss_pct': -8.0,
        'holding_period': '1〜3週間',
    }


def _check_breakout(data: dict) -> dict | None:
    """
    ブレイクアウト戦略: 52週高値更新直前 or 直後
    - RSI 55〜75
    - 52週高値比 -5〜+3%（高値圏突破狙い）
    - 出来高比率 ≥ 1.5
    """
    rsi = data['rsi']
    high52_pct = data['pct_from_52w_high']
    vol_ratio = data['vol_ratio']

    if not (55 <= rsi <= 75):
        return None
    if not (-6 <= high52_pct <= 3):
        return None
    if vol_ratio < 1.5:
        return None

    score = 0.0
    score += rsi * 0.8
    score += (6 - abs(high52_pct)) * 3   # 高値に近いほど高スコア
    score += vol_ratio * 6

    return {
        'strategy':    'ブレイクアウト',
        'reason':      f"RSI {rsi:.1f} / 52週高値比{high52_pct:+.1f}% / 出来高{vol_ratio:.1f}倍",
        'score':       round(score, 1),
        'stop_loss_pct': -5.0,
        'holding_period': '1〜2週間',
    }


# ============================================================
# メインスクリーニング
# ============================================================

def run_screening(tickers: list[str] | None = None,
                  regime: str | None = None,
                  send_alert: bool = False,
                  us_only: bool = False,
                  morning: bool = False) -> list[dict]:

    from insider_restrictions import filter_allowed_tickers, filter_signal_records
    tickers = filter_allowed_tickers(tickers or DEFAULT_SCAN_TICKERS)
    if us_only:
        tickers = [t for t in tickers if not t.endswith('.T')]
        print(f"[margin_long] --us-only: {len(tickers)} 銘柄に絞込")
    # 出力先（朝バッチは別ファイル）
    output_file = (BASE_DIR / 'margin_long_candidates_morning.json') if morning else RESULTS_FILE
    vix     = _get_vix()
    regime  = regime or _get_regime()
    strong_sectors = _get_strong_sectors()

    print(f"[margin_long] VIX={vix:.1f} / レジーム={regime} / 強セクター={strong_sectors}")

    # VIX 高すぎる場合は全禁止
    if vix > VIX_BLOCK_THRESHOLD:
        msg = f"⚠️ 信用買い全禁止: VIX={vix:.1f} (閾値{VIX_BLOCK_THRESHOLD}超)"
        print(f"[margin_long] {msg}")
        result = {
            'generated_at': datetime.now().strftime('%Y-%m-%d %H:%M'),
            'regime': regime,
            'vix': round(vix, 1),
            'blocked': True,
            'block_reason': msg,
            'candidates': [],
        }
        output_file.write_text(json.dumps(result, ensure_ascii=False, indent=2))
        return []

    # C_弱気: 条件付き許可（押し目反発のみ / サイズ半分 / 最大3銘柄 / 高スコア閾値）
    bearish_mode = (regime == 'C_弱気')
    BEARISH_MIN_SCORE = 25.0   # C_弱気では高確度の候補のみ通過
    if bearish_mode:
        print(f"[margin_long] ⚠️ C_弱気: 条件付き許可モード（押し目反発のみ・最大3銘柄・サイズ半分・スコア≥{BEARISH_MIN_SCORE}）")

    candidates = []
    for ticker in tickers:
        time.sleep(0.3)
        data = _get_ticker_data(ticker)
        if data is None:
            continue

        # レジーム別に使える戦略を決定
        checks = []
        if regime == 'A_強気':
            checks = [
                _check_momentum(data, strong_sectors),
                _check_pullback(data, strong_sectors),
                _check_breakout(data),
            ]
        elif regime == 'B_中立':
            # 中立相場はモメンタム + 押し目のみ（ブレイクアウトは高リスク）
            checks = [
                _check_momentum(data, strong_sectors),
                _check_pullback(data, strong_sectors),
            ]
        elif regime == 'C_弱気':
            # 弱気相場は押し目反発のみ（モメンタム・ブレイクアウトは禁止）
            checks = [
                _check_pullback(data, strong_sectors),
            ]

        # 最高スコアの戦略を採用
        best = None
        for c in checks:
            if c is None:
                continue
            if best is None or c['score'] > best['score']:
                best = c

        if best is None:
            continue

        # ── 流動性フロア ────────────────────────
        try:
            from screening_helpers import (liquidity_ok, get_historical_win_rate,
                                           days_to_next_earnings, calc_composite_score)
        except Exception:
            liquidity_ok = lambda *a, **k: True  # type: ignore
            get_historical_win_rate = lambda *a, **k: 0.5  # type: ignore
            days_to_next_earnings = lambda *a, **k: None  # type: ignore
            calc_composite_score = lambda technical, **k: technical  # type: ignore
        if not liquidity_ok(ticker, data['price'], data.get('avg_volume_30d', 0)):
            print(f"  ⏭️  {ticker}: 流動性フロア未達 → SKIP")
            continue

        # ── EDGAR ファンダ overlay ─────────────
        fund = _fundamental_overlay(ticker)
        if fund.get('skip_reason'):
            print(f"  ⏭️  {ticker}: ファンダ悪化 ({fund['skip_reason']}) → SKIP")
            continue

        # ── 決算日ガード（2 営業日以内なら警告フラグのみ）─
        days_to_earn = days_to_next_earnings(ticker)
        earnings_imminent = days_to_earn is not None and 0 <= days_to_earn <= 2

        # C_弱気: スコア閾値 + 損切りタイト（-5%）+ サイズ半分
        if bearish_mode:
            if best['score'] < BEARISH_MIN_SCORE:
                continue
            best['stop_loss_pct'] = -5.0
            best['holding_period'] = '1〜2週間'
            best['strategy'] += '（弱気限定）'

        # ── 複合スコア ─────────────────────────
        win_rate = get_historical_win_rate(best['strategy'], ticker)
        # technical は best['score'] を 0-100 に正規化（最大値ターゲット 60 → 100 換算）
        tech_norm = min(100.0, best['score'] * (100.0 / 60.0))
        composite = calc_composite_score(
            technical=tech_norm,
            fundamental=fund['fundamental_score'],
            ai_conviction=50.0,  # margin_long は AI 直接コール無し
            win_rate=win_rate,
            weights=(0.50, 0.30, 0.0, 0.20),  # margin_long は技術と勝率重視
        )
        # 決算 imminent はソフト降格
        if earnings_imminent:
            composite = max(0.0, composite - 10)

        # S4D: ニュース・SNS ブースト（信用買い → side='long'）
        ns = {"news_signal": None, "news_score": None, "news_boost": 0,
              "social_bias": None, "social_buzz": 0}
        try:
            from screening_helpers import get_news_social_boost
            ns = get_news_social_boost(data['ticker'], side='long')
            composite = min(100.0, max(0.0, composite + ns['news_boost'] + ns['social_buzz']))
        except Exception:
            pass

        entry = {
            'ticker':           data['ticker'],
            'name':             data['name'],
            'price':            data['price'],
            'rsi':              data['rsi'],
            'pct_from_ma50':    data['pct_from_ma50'],
            'vol_ratio':        data['vol_ratio'],
            'vol20':            data['vol20'],
            'pct_from_52w_high': data['pct_from_52w_high'],
            'sector':           data['sector'],
            'strategy':         best['strategy'],
            'reason':           best['reason'],
            'score':            best['score'],
            'stop_loss_pct':    best['stop_loss_pct'],
            'holding_period':   best['holding_period'],
            'in_strong_sector': data['sector'] in strong_sectors,
            'half_size':        bearish_mode,
            # 新フィールド（S2/S6 強化）
            'fundamental_score': fund['fundamental_score'],
            'fund_flags':        fund['flags'],
            'win_rate':          round(win_rate, 3),
            'composite_score':   composite,
            'days_to_earnings':  days_to_earn,
            'earnings_imminent': earnings_imminent,
            # S4D
            'news_signal':       ns['news_signal'],
            'news_score':        ns['news_score'],
            'news_boost':        ns['news_boost'],
            'social_bias':       ns['social_bias'],
            'social_buzz':       ns['social_buzz'],
        }
        candidates.append(entry)
        flag_str = ' ⚠️決算間近' if earnings_imminent else ''
        print(f"  ✅ {ticker}: {best['strategy']} tech={best['score']:.1f} comp={composite:.1f}{flag_str}")

    # 複合スコア順にソート（フォールバック: 旧 score）
    candidates.sort(key=lambda x: x.get('composite_score', x.get('score', 0)), reverse=True)
    # C_弱気は最大3銘柄、それ以外は15銘柄
    max_candidates = 3 if bearish_mode else 15

    # S4C: HMM regime confidence で max_candidates をさらに 70% に圧縮
    try:
        from screening_helpers import get_regime_confidence
        conf = get_regime_confidence()
        if conf < 0.6:
            new_max = max(1, int(max_candidates * 0.7))
            print(f"  ⚠️ regime_confidence={conf:.2f} < 0.6 → max_candidates {max_candidates}→{new_max}")
            max_candidates = new_max
    except Exception:
        pass

    candidates = filter_signal_records(candidates[:max_candidates])

    result = {
        'generated_at': datetime.now().strftime('%Y-%m-%d %H:%M'),
        'regime': regime,
        'vix': round(vix, 1),
        'blocked': False,
        'candidates': candidates,
    }
    output_file.write_text(json.dumps(result, ensure_ascii=False, indent=2))
    print(f"[margin_long] {len(candidates)} 件保存 → {output_file}")

    # 信用買い候補の Telegram 通知は廃止。詳細は margin_long_candidates.json / Web UI を参照。
    if send_alert and candidates:
        print(f"[margin_long] {len(candidates)} 件候補生成（通知は UI で確認）")

    return candidates


# ============================================================
# CLI
# ============================================================

if __name__ == '__main__':
    import argparse
    parser = argparse.ArgumentParser(description='信用買いスクリーニング')
    parser.add_argument('--regime', choices=['A_強気', 'B_中立', 'C_弱気'],
                        help='レジームを手動指定')
    parser.add_argument('--alert', action='store_true',
                        help='Telegram アラート送信')
    parser.add_argument('--json', action='store_true',
                        help='JSON 出力')
    parser.add_argument('--us-only', action='store_true',
                        help='米国銘柄のみ対象（朝バッチ用、.T を除外）')
    parser.add_argument('--morning', action='store_true',
                        help='朝バッチモード（出力先を margin_long_candidates_morning.json に分離）')
    args = parser.parse_args()

    print(f"[margin_long] スクリーニング開始 ({datetime.now().strftime('%H:%M:%S')})")
    results = run_screening(regime=args.regime, send_alert=args.alert,
                            us_only=args.us_only, morning=args.morning)

    if args.json:
        print(json.dumps(results, ensure_ascii=False, indent=2))
    else:
        if not results:
            print("候補なし（レジームまたは VIX による禁止の可能性あり）")
        else:
            print(f"\n上位候補 ({len(results)} 件):")
            for c in results[:10]:
                star = '⭐' if c['in_strong_sector'] else '  '
                print(f"  {star} [{c['strategy']}] {c['ticker']} ({c['name'][:20]})")
                print(f"       score={c['score']:.1f} | {c['reason']}")
                print(f"       損切り: {c['stop_loss_pct']}% | 保有期間: {c['holding_period']}")
