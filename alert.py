import html
import os
import yfinance as yf
import requests
import json
import time
from datetime import datetime, timedelta
from pathlib import Path
from utils import atomic_write_json, load_json, init_yfinance_timeout, reset_yfinance_session

init_yfinance_timeout()

TELEGRAM_TOKEN = os.environ.get('TELEGRAM_TOKEN', '')
TELEGRAM_CHAT_ID = os.environ.get('TELEGRAM_CHAT_ID', '')

BASE_DIR = Path(__file__).parent

# 保有銘柄ファイル（売買のたびに更新する）
HOLDINGS_FILE = BASE_DIR / 'holdings.json'

# アラート送信履歴（同一アラートの重複送信を防ぐ）
ALERT_LOG_FILE = BASE_DIR / 'alert_sent_log.json'
ALERT_COOLDOWN_HOURS = 8   # 同じアラートは8時間以内に再送しない

# 投資信託・ETF等の複合キー銘柄（価格アラート対象外）
SKIP_ALERT_KEYS = {
    'SLIM_SP500', 'SLIM_ORCAN', 'MNXACT', 'IFREE_FANGPLUS',
    'NOMURA_SEMI', 'AVGO_toku', 'AVGO_ippan',
}

_ALERT_PRICE_CACHE: dict[str, tuple[float, float]] = {}
ALERT_PRICE_CACHE_TTL_SEC = 900  # 15分。5分ループで同じ yfinance socket を増やさない。


def load_alert_log() -> dict:
    return load_json(ALERT_LOG_FILE, default={})


def save_alert_log(log: dict):
    atomic_write_json(ALERT_LOG_FILE, log)


def already_sent(log: dict, key: str) -> bool:
    """同じアラートキーが ALERT_COOLDOWN_HOURS 以内に送信済みか確認する。"""
    if key not in log:
        return False
    last = datetime.fromisoformat(log[key])
    return datetime.now() - last < timedelta(hours=ALERT_COOLDOWN_HOURS)


def mark_sent(log: dict, key: str):
    log[key] = datetime.now().isoformat()

def send_telegram(message) -> bool:
    """Send one Telegram message and report the actual API result.

    The caller owns HTML escaping because some alert call sites intentionally
    use supported Telegram HTML tags.  Transport failures must not be hidden:
    ``raise_for_status`` includes Bot API 4xx responses such as malformed HTML.
    """
    if not TELEGRAM_TOKEN or not TELEGRAM_CHAT_ID:
        return False
    url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
    response = requests.post(url, data={
        "chat_id": TELEGRAM_CHAT_ID,
        "text": message,
        "parse_mode": "HTML"
    }, timeout=15)
    response.raise_for_status()
    payload = response.json()
    if not isinstance(payload, dict) or payload.get("ok") is not True:
        raise RuntimeError(f"Telegram Bot API rejected message: {payload!r}")
    return True

def load_holdings():
    """保有銘柄を読み込む"""
    return load_json(HOLDINGS_FILE, default={})

def save_holdings(holdings):
    atomic_write_json(HOLDINGS_FILE, holdings)


def is_quiet_hours():
    """通知OK: 平日8:00-8:30 / 12:00-13:00 / 18:00-22:00"""
    now = datetime.now()
    if now.weekday() >= 5:
        return True
    h, m = now.hour, now.minute
    if h < 8 or h >= 22:
        return True
    if (h == 8 and m >= 30) or (9 <= h < 12):
        return True
    if 13 <= h < 18:
        return True
    return False


def _should_skip_alert_price(key: str, info: dict) -> bool:
    """yfinance で価格が取れない/重複する holdings 行を価格アラート対象から外す。"""
    ticker = info.get('ticker', key)
    investment_type = info.get('investment_type', '')
    if key in SKIP_ALERT_KEYS or ticker in SKIP_ALERT_KEYS:
        return True
    if investment_type == 'cash' or str(key).startswith('CASH_') or str(ticker).startswith('CASH_'):
        return True
    if info.get('unit'):
        return True
    return False


def _get_alert_price(ticker: str):
    """価格アラート用の短期 TTL キャッシュ付き yfinance 取得。"""
    now = time.time()
    cached = _ALERT_PRICE_CACHE.get(ticker)
    if cached and now - cached[1] < ALERT_PRICE_CACHE_TTL_SEC:
        return cached[0]
    price = yf.Ticker(ticker).fast_info['lastPrice']
    if price is None:
        raise ValueError(f"{ticker} lastPrice is None")
    price = float(price)
    _ALERT_PRICE_CACHE[ticker] = (price, now)
    return price

def check_regime_flip_notification():
    """
    Bear→Bull レジーム転換を検知して Telegram 通知する。
    regime_state.json の 'regime' フィールドを監視し、
    前回状態を regime_flip_state.json に保存する。
    クールダウン: 24時間（キー: regime_flip_bull）
    """
    FLIP_COOLDOWN_HOURS = 24
    try:
        import json as _json
        regime_file = BASE_DIR / 'regime_state.json'
        flip_state_file = BASE_DIR / 'regime_flip_state.json'

        if not regime_file.exists():
            return

        with open(regime_file) as f:
            regime_data = _json.load(f)

        current_regime = regime_data.get('regime', '')
        updated_time = regime_data.get('updated', '')
        spy_above = regime_data.get('spy_above', False)
        nk_above = regime_data.get('nk_above', False)

        # 前回状態を読み込む
        prev_regime = ''
        if flip_state_file.exists():
            try:
                with open(flip_state_file) as f:
                    flip_data = _json.load(f)
                prev_regime = flip_data.get('regime', '')
            except Exception:
                pass

        # Bear→Bull or Bear→Neutral の転換を検知
        BEAR_LABEL = 'BEAR（弱気）'
        if prev_regime == BEAR_LABEL and current_regime != BEAR_LABEL and current_regime:
            log = load_alert_log()
            cooldown_key = 'regime_flip_bull'
            # 24時間クールダウンチェック（already_sent は ALERT_COOLDOWN_HOURS=8h 固定なので直接チェック）
            already = False
            if cooldown_key in log:
                last = datetime.fromisoformat(log[cooldown_key])
                already = datetime.now() - last < timedelta(hours=FLIP_COOLDOWN_HOURS)

            if not already:
                spy_str = '上' if spy_above else '下'
                nk_str = '上' if nk_above else '下'
                msg = (
                    '🚀 <b>レジーム転換検知: BEAR→BULL</b>\n'
                    '━━━━━━━━━━━━━━\n'
                    '相場が弱気から強気に転換しました。\n\n'
                    '📊 新レジーム: ' + current_regime + '\n'
                    '🛡 SPY 200MA: ' + spy_str + '\n'
                    '🗾 日経 200MA: ' + nk_str + '\n\n'
                    '💡 推奨アクション:\n'
                    '  • NISA積立の継続・追加を検討\n'
                    '  • 長期ポジションの買い増しチャンス\n'
                    '  • ガードレール例外が発動中の場合は積立を優先\n\n'
                    '⚠️ ガードレール状態を必ず確認してください\n'
                    '更新: ' + updated_time
                )
                send_telegram(msg)
                log[cooldown_key] = datetime.now().isoformat()
                save_alert_log(log)
                print(f'レジーム転換通知送信: {prev_regime} → {current_regime}')

        # 現在のレジームを flip_state に保存（通知有無に関わらず）
        with open(flip_state_file, 'w') as f:
            _json.dump({
                'regime': current_regime,
                'updated': datetime.now().isoformat(),
            }, f)

    except Exception as e:
        print(f'レジーム転換通知エラー: {e}')


def check_regime_change():
    def _close_series(symbol: str):
        hist = yf.Ticker(symbol).history(period='3mo')
        if hist is None or getattr(hist, 'empty', True) or 'Close' not in hist:
            raise ValueError(f'{symbol} history is empty')
        close = hist['Close'].dropna()
        if len(close) < 50:
            raise ValueError(f'{symbol} history has only {len(close)} close rows')
        return close

    try:
        import json as _json
        regime_file = str(BASE_DIR / 'regime_state.json')
        spy_close = _close_series('SPY')
        spy_price = float(spy_close.iloc[-1])
        spy_ma50 = float(spy_close.rolling(50).mean().iloc[-1])
        spy_above = spy_price > spy_ma50
        nk_close = _close_series('^N225')
        nk_price = float(nk_close.iloc[-1])
        nk_ma50 = float(nk_close.rolling(50).mean().iloc[-1])
        nk_above = nk_price > nk_ma50
        prev = {'spy_above': True, 'nk_above': True}
        if os.path.exists(regime_file):
            with open(regime_file) as f:
                prev = _json.load(f)
        # S&P500/日経 50日線の割れ・回復は UI の /risk で確認（Telegram 通知なし）
        if prev.get('spy_above', True) and not spy_above:
            print('[alert] SPY が MA50 を割り込み（UI で確認）')
        if prev.get('nk_above', True) and not nk_above:
            print('[alert] 日経 が MA50 を割り込み（UI で確認）')
        if not prev.get('spy_above', True) and spy_above:
            print('[alert] SPY が MA50 を回復（UI で確認）')
        # macro_score を計算して一緒に保存（short_screener.py がレジーム判定に使用）
        try:
            import sys as _sys
            _sys.path.insert(0, str(BASE_DIR))
            from regime_params import get_regime as _get_regime
            from macro_fetcher import get_macro_context as _get_macro
            _macro = _get_macro()
            _vix_score = 10
            try:
                _vix = float(yf.Ticker("^VIX").fast_info['lastPrice'])
                if _vix > 30:   _vix_score = 0
                elif _vix > 25: _vix_score = 6
                elif _vix > 20: _vix_score = 8
            except Exception:
                pass
            _macro_score = max(0, _vix_score + _macro.get("macro_adj", 0))
            _regime_label = _get_regime(_macro_score, spy_above)
            _macro_note = _macro.get("source", "default")
        except Exception:
            _macro_score = 5
            _regime_label = 'B_中立'
            _macro_note = "error"
        atomic_write_json(regime_file, {
            'spy_above':    spy_above,
            'nk_above':     nk_above,
            'macro_score':  _macro_score,
            'regime':       _regime_label,
            'updated':      datetime.now().strftime('%Y-%m-%d %H:%M'),
            'macro_source': _macro_note,
        })
        # Bear→Bull レジーム転換通知
        if not is_quiet_hours():
            check_regime_flip_notification()
    except Exception as e:
        print(f'レジームチェックエラー: {e}')
    finally:
        reset_yfinance_session()

def _alert_short(ticker, key, info, current, change_pct, log, holdings, updated):
    """
    short（短期トレード）: スクリーナー由来の1〜2週ポジション
    - 損切りライン到達 → 即時通知
    - -5%急落 → 警告（8時間に1回）
    - +5% → 部分利確（一度だけ）
    - +12% → 全量利確検討（8時間に1回）
    """
    entry     = info['entry_price']
    strategy  = info.get('strategy', '')
    stop_loss = info.get('stop_loss_atr', 0)
    actual_stop = stop_loss if stop_loss else entry * 0.93
    partial_taken = info.get('partial_taken', False)
    currency = '$' if info.get('currency', 'USD') == 'USD' else '¥'

    # 個別銘柄の価格アラートは UI の /portfolio で確認（Telegram 通知なし）
    if current <= actual_stop:
        print(f'[alert] {ticker} 損切りライン到達 {change_pct:.1f}%（UI で確認）')

    elif change_pct <= -5:
        print(f'[alert] {ticker} 急落 {change_pct:.1f}%（UI で確認）')

    elif change_pct >= 5.0 and not partial_taken:
        holdings[key]['partial_taken'] = True
        return True   # updated（部分利確フラグは保持）

    elif change_pct >= 12.0:
        print(f'[alert] {ticker} +{change_pct:.1f}% 目標到達（UI で確認）')

    return False


def _alert_medium(ticker, key, info, current, change_pct, log):
    """
    medium（中期・成長株）: 1〜5年ホールド予定（NVDA・AVGO・META等）
    - -15%以下 → 大幅下落警告（8時間に1回）
    - 利確通知は出さない（自分で判断）
    """
    entry    = info['entry_price']
    currency = '$' if info.get('currency', 'USD') == 'USD' else '¥'

    # 中期価格アラートは UI の /portfolio で確認（Telegram 通知なし）
    if change_pct <= -15:
        print(f'[alert] {ticker} (medium) -{abs(change_pct):.1f}%（UI で確認）')


def _alert_long(ticker, key, info, current, change_pct, log):
    """
    long（長期・ETF・投信）: 5年以上ホールド予定
    - -25%以下 → 大幅下落の確認を促す（8時間に1回）
    - 利確・部分利確の通知は出さない
    """
    entry    = info['entry_price']
    currency = '$' if info.get('currency', 'USD') == 'USD' else '¥'

    # 長期価格アラートは UI の /portfolio で確認（Telegram 通知なし）
    if change_pct <= -25:
        print(f'[alert] {ticker} (long) -{abs(change_pct):.1f}%（UI で確認）')


def check_alerts():
    # 土日・通知時間帯外はスキップ
    if is_quiet_hours():
        return

    holdings = load_holdings()
    if not holdings:
        return

    log     = load_alert_log()
    updated = False

    try:
        for key, info in holdings.items():
            # 投資信託・複合キー銘柄（yfinanceで価格取得不可）はスキップ
            if _should_skip_alert_price(key, info):
                continue

            ticker          = info.get('ticker', key)
            investment_type = info.get('investment_type', 'swing')   # デフォルトはswing（安全側）

            try:
                current    = _get_alert_price(ticker)
                entry      = info['entry_price']
                change_pct = (current - entry) / entry * 100

                if investment_type == 'swing':
                    if _alert_short(ticker, key, info, current, change_pct, log, holdings, updated):
                        updated = True
                elif investment_type == 'medium':
                    _alert_medium(ticker, key, info, current, change_pct, log)
                elif investment_type == 'long':
                    _alert_long(ticker, key, info, current, change_pct, log)

            except Exception as e:
                print(f'{ticker} チェックエラー: {e}')
    finally:
        reset_yfinance_session()

    save_alert_log(log)
    if updated:
        save_holdings(holdings)


def update_guard_state():
    """
    portfolio_manager からリアルタイム評価額を取得し guard_state.json を更新する。
    前回記録値との差分を日次P&Lとして behavioral_guard に渡す。
    crontab から alert.py が呼ばれるたびに自動実行（1日2回: 8:00 / 17:00）。
    """
    try:
        import sys as _sys
        _sys.path.insert(0, str(BASE_DIR))
        from portfolio_manager import build_portfolio_snapshot
        from behavioral_guard import load_state, update_pnl

        snap = build_portfolio_snapshot(fetch_missing_sectors=False)
        current_value = snap.get('total_jpy', 0)
        if not current_value:
            print('guard_state 更新スキップ: 総資産取得失敗')
            return

        state = load_state()
        prev_value = state.get('portfolio_value', 0)

        if prev_value > 0 and state.get('date') == datetime.now().date().isoformat():
            # 同日内の変動を日次P&Lとして累積（既存の日次P&L + 今回の変動）
            new_pnl_jpy = current_value - prev_value
            update_pnl(new_pnl_jpy, current_value)
            sign = '+' if new_pnl_jpy >= 0 else ''
            print(f'guard_state 更新: 総資産¥{current_value:,.0f} / 日次¥{sign}{new_pnl_jpy:,.0f}')
        else:
            # 日付変わり初回 → 前日末との差は不明なのでP&L=0で総資産のみ更新
            update_pnl(0, current_value)
            print(f'guard_state 更新（日次リセット）: 総資産¥{current_value:,.0f}')
    except Exception as e:
        print(f'guard_state 更新エラー: {e}')
    finally:
        reset_yfinance_session()


if __name__ == "__main__":
    print(f"アラート監視開始 {datetime.now().strftime('%H:%M:%S')}")
    # 起動時にガードレール状態を最新化
    update_guard_state()
    cycle = 0
    while True:
        check_alerts()
        # 30分に1回レジーム変化をチェック
        if cycle % 6 == 0:
            check_regime_change()
        # 60分に1回 guard_state を更新（ポートフォリオ評価額の変動を反映）
        if cycle % 12 == 0 and cycle > 0:
            update_guard_state()
        cycle += 1
        time.sleep(300)  # 5分ごとにチェック
