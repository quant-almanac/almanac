"""
ALMANAC v4.0 - 行動ガードレール
日次/月次P&L監視・ガードレール状態の永続化・crontab連携CLI
"""

import json
import os
import sys
import time
from datetime import datetime, date, timedelta
from pathlib import Path

from utils import atomic_write_json

BASE_DIR = Path(__file__).parent
STATE_FILE = BASE_DIR / 'guard_state.json'
REGIME_FILE = BASE_DIR / 'regime_state.json'
HAIKU_MODEL_ID = "claude-haiku-4-5-20251001"


def _append_llm_call_log(row: dict) -> None:
    try:
        from analyst.llm_client import _append_llm_call_log as _append
        _append(row)
    except Exception:
        pass


def _log_guardrail_suggestion_usage(
    *,
    started: float,
    prompt: str,
    trading_stopped: bool,
    level: str,
    response=None,
    status: str = "ok",
    error: Exception | None = None,
) -> None:
    usage = getattr(response, "usage", None)
    row = {
        "ts": time.strftime("%Y-%m-%dT%H:%M:%S"),
        "role": "guardrail_suggestion",
        "model": HAIKU_MODEL_ID,
        "use_tool": False,
        "max_tokens": 512,
        "elapsed_sec": round(time.monotonic() - started, 2),
        "prompt_chars": len(prompt),
        "status": status,
        "trading_stopped": trading_stopped,
        "level": level,
    }
    if response is not None:
        row.update({
            "stop_reason": getattr(response, "stop_reason", None),
            "content_types": [getattr(block, "type", None) for block in getattr(response, "content", [])],
            "input_tokens": getattr(usage, "input_tokens", None),
            "output_tokens": getattr(usage, "output_tokens", None),
        })
    if error is not None:
        row.update({
            "error_type": type(error).__name__,
            "error": str(error)[:500],
            "cost_usd": 0.0,
        })
    _append_llm_call_log(row)

# ============================================================
# レジーム状態ヘルパー
# ============================================================

def _get_regime_bull() -> bool:
    """regime_state.json を読み込み、spy_above=True なら True を返す。
    48時間以上古いデータは stale 扱いで False を返す（cron 停止時の防御漏れ防止）。"""
    try:
        with open(REGIME_FILE, encoding='utf-8') as f:
            data = json.load(f)
        # staleness check: updated が 48 時間以上古ければ強気判定を取り下げる
        updated = data.get('updated', '')
        if updated:
            try:
                # "2026-05-07 08:26" or ISO 形式に対応
                dt_str = updated.replace(' ', 'T') if 'T' not in updated else updated
                age_h = (datetime.now() - datetime.fromisoformat(dt_str)).total_seconds() / 3600.0
                if age_h > 48:
                    return False  # stale → 安全側 (NISA 例外などを誤発動させない)
            except Exception:
                return False  # parse 失敗時も安全側
        return bool(data.get('spy_above', False))
    except (FileNotFoundError, json.JSONDecodeError):
        return False


# ============================================================
# リバランスクールダウン（Phase 2: 細切れリバランス抑制）
# ============================================================

REBALANCE_COOLDOWN = {
    "min_interval_business_days": 3,   # 直近 N 営業日以内に trim/rebalance 実行があれば抑制
    "vix_emergency_threshold":   25.0, # VIX> threshold ならクールダウンをバイパス
}

_REBAL_DIRECTIONS = {"trim", "sell", "take_profit", "stop_loss", "rebalance"}


def _business_days_since(d: date) -> int:
    """営業日（土日のみ除外、祝日は無視）の経過日数を返す。"""
    if not isinstance(d, date):
        return 999
    today = date.today()
    if d > today:
        return 0
    n = 0
    cur = d
    while cur < today:
        cur = cur + timedelta(days=1)
        if cur.weekday() < 5:  # Mon-Fri
            n += 1
    return n


def _last_rebalance_execution_date():
    """action_executions.json から直近の trim/sell/rebalance 約定日を返す。なければ None。"""
    p = BASE_DIR / "action_executions.json"
    if not p.exists():
        return None
    try:
        data = json.load(open(p, encoding="utf-8"))
    except Exception:
        return None
    items = data.get("executions", []) if isinstance(data, dict) else (data if isinstance(data, list) else [])
    latest = None
    for e in items:
        status = (e.get("status") or "").lower()
        direction = (e.get("direction") or "").lower()
        if status not in {"executed", "filled"} or direction not in _REBAL_DIRECTIONS:
            continue
        ts = e.get("executed_at_time") or e.get("saved_at") or ""
        try:
            dt = datetime.fromisoformat(ts.split("+")[0]).date()
        except Exception:
            continue
        if latest is None or dt > latest:
            latest = dt
    return latest


def is_rebalance_in_cooldown(vix: float | None = None) -> tuple[bool, str]:
    """直近 N 営業日以内に trim/rebalance 実行があれば True と理由を返す。
    VIX > emergency_threshold ならクールダウンをバイパスして False。"""
    last = _last_rebalance_execution_date()
    if last is None:
        return False, "no recent rebalance execution"
    bdays = _business_days_since(last)
    interval    = int(_tp_bg("rebalance_cooldown_days", REBALANCE_COOLDOWN["min_interval_business_days"]))
    vix_thresh  = float(_tp_bg("vix_emergency_threshold", REBALANCE_COOLDOWN["vix_emergency_threshold"]))
    if bdays >= interval:
        return False, f"last rebalance {bdays} business days ago (>= {interval})"
    if vix is not None and vix > vix_thresh:
        return False, f"vix {vix:.1f} > {vix_thresh} → cooldown bypassed"
    return True, (
        f"rebalance cooldown: 直近 {bdays} 営業日以内に trim/sell 実行済み "
        f"(< {interval} 営業日 / VIX {vix if vix is not None else '?'} ≤ {vix_thresh})"
    )


# ============================================================
# ガードレール設定
# ============================================================

GUARDRAILS = {
    'daily_loss_limit':    -0.04,   # 日次-4%: 新規エントリー禁止（fallback default）
    'monthly_loss_limit':  -0.08,   # 月間-8%: 現金積み増し推奨（fallback default）
    'max_short_positions':  3,
    # ポジション数上限は廃止 — 日次/月次損失制限と未発注SLブロックで管理
}


def _get_guardrail(key: str, default_pct: float) -> float:
    """tunable_params から最新値を取得（fallback 必須）。
    値はパーセント（例: -4.0）で保存されているので 100 で割って小数化。
    """
    try:
        from tunable_params import get as _tp_get
        # daily_loss_limit ↔ daily_loss_limit_pct のマッピング
        tp_key = {
            'daily_loss_limit':   'daily_loss_limit_pct',
            'monthly_loss_limit': 'monthly_stage1_pct',
            'max_short_positions': 'max_short_positions',
        }.get(key)
        if tp_key:
            v = _tp_get(tp_key)
            if v is not None:
                # max_short_positions は整数のまま、損失系は % → 小数
                return float(v) if 'positions' in tp_key else float(v) / 100.0
    except Exception:
        pass
    return default_pct

STAGED_GUARDRAILS = {
    # 統計的整合性: 日次VaR 1% × √20 ≈ 4.5%/月 → 正常ノイズで誤発動しない水準に設定
    'stage_1': {'threshold': -0.08, 'risk_factor': 0.50, 'label': 'リスク50%縮小'},
    'stage_2': {'threshold': -0.12, 'risk_factor': 0.25, 'label': 'リスク75%縮小'},
    'stage_3': {'threshold': -0.16, 'risk_factor': 0.00, 'label': '全取引停止'},
}

RECOVERY_MODE = {
    'enabled_after_stage': 1,        # stage_1 or stage_2 からの回復を許可
    'recovery_window_days': 5,       # 回復判定期間（5営業日連続改善で緩和）
    'max_risk_in_recovery': 0.50,    # 回復中の最大リスク係数
}

DRAWDOWN_RULES = {
    'warning':  -0.25,   # -25%: ポジション50%縮小アラート
    'critical': -0.35,   # -35%: 全現金化推奨
}


# ============================================================
# 状態ファイル管理
# ============================================================

def _tp_bg(key: str, fallback):
    """tunable_params から値を取得（fallback 必須）。循環回避のため関数内 import。"""
    try:
        from tunable_params import get as _tp_get
        v = _tp_get(key)
        return v if v is not None else fallback
    except Exception:
        return fallback


# ============================================================
# VIX 連動 portfolio leverage 健全性チェック（Option B-3）
# ============================================================

def _vix_leverage_cap(vix: float | None) -> float:
    """VIX に応じた最大 portfolio leverage を返す。
    tunable_params: vix_leverage_cap_15 / _20 / _25 / _30 を参照。
    """
    if vix is None:
        return float(_tp_bg("vix_leverage_cap_20", 1.1))  # 不明時は中立
    v = float(vix)
    if v < 15:
        return float(_tp_bg("vix_leverage_cap_15", 1.2))
    if v < 20:
        return float(_tp_bg("vix_leverage_cap_20", 1.1))
    if v < 25:
        return float(_tp_bg("vix_leverage_cap_25", 1.0))
    if v < 30:
        return float(_tp_bg("vix_leverage_cap_30", 0.8))
    return 0.6  # VIX>=30: 即時 40% 減ポジ


def evaluate_leverage_health(current_leverage: float | None = None,
                              vix: float | None = None,
                              portfolio_total_jpy: float | None = None) -> dict:
    """
    現在の portfolio leverage と VIX 連動 cap を比較して健全性を判定。

    Returns:
        {
          "current_leverage":   1.05,
          "leverage_cap":       1.1,
          "overshoot_pct":      0,
          "status":             "ok" | "warn" | "deleverage" | "emergency",
          "vix":                17.3,
          "max_leverage_setting": 1.2,
          "action":             "通常運用" | "新規 buy 抑制" | "trim 強制" | "緊急 deleverage",
          "new_buy_allowed":    True/False,
          "margin_buy_allowed": True/False,
        }
    """
    # current_leverage が未指定なら margin_manager から計算
    if current_leverage is None:
        try:
            from margin_manager import get_current_leverage as _gcl
            lev = _gcl(portfolio_total_jpy=portfolio_total_jpy)
            current_leverage = float(lev.get("leverage") or 1.0)
        except Exception:
            current_leverage = 1.0

    # VIX が未指定なら vix_state.json から取得
    if vix is None:
        try:
            v_path = BASE_DIR / "vix_state.json"
            if v_path.exists():
                vd = json.load(open(v_path, encoding="utf-8"))
                vb = vd.get("vix") if isinstance(vd.get("vix"), dict) else None
                if vb:
                    vix = float(vb.get("level") or 0) or None
                else:
                    vix = float(vd.get("vix") or vd.get("level") or 0) or None
        except Exception:
            vix = None

    cap = _vix_leverage_cap(vix)
    max_setting = float(_tp_bg("max_portfolio_leverage", 1.2))
    effective_cap = min(cap, max_setting)
    margin_buy_block_vix = float(_tp_bg("vix_margin_buy_block", 35.0))

    overshoot = current_leverage - effective_cap
    overshoot_pct = round(overshoot * 100, 2)
    vix_allows_margin_buy = vix is not None and float(vix) < margin_buy_block_vix

    # 状態判定
    if overshoot <= 0:
        if current_leverage >= effective_cap * 0.95:
            # cap の 95% 以内なら ok だが新規信用は抑制
            status = "ok"
            action = "通常運用（cap 近傍のため新規信用は控えめ）"
            new_buy_allowed = True
            margin_buy_allowed = vix_allows_margin_buy
        else:
            status = "ok"
            action = "通常運用"
            new_buy_allowed = True
            margin_buy_allowed = vix_allows_margin_buy
    elif overshoot <= 0.05:
        status = "warn"
        action = "cap を僅か超過。新規 buy 抑制 + 信用買い禁止"
        new_buy_allowed = False
        margin_buy_allowed = False
    elif overshoot <= 0.15:
        status = "deleverage"
        action = f"cap +{overshoot_pct}% 超過。20% 程度の trim 推奨"
        new_buy_allowed = False
        margin_buy_allowed = False
    else:
        status = "emergency"
        action = f"⛔ cap +{overshoot_pct}% 大幅超過。即時 deleverage 必須"
        new_buy_allowed = False
        margin_buy_allowed = False

    return {
        "current_leverage":     round(float(current_leverage), 4),
        "leverage_cap":         round(effective_cap, 4),
        "overshoot_pct":        overshoot_pct,
        "status":               status,
        "vix":                  round(float(vix), 2) if vix is not None else None,
        "margin_buy_block_vix": round(float(margin_buy_block_vix), 2),
        "max_leverage_setting": max_setting,
        "action":               action,
        "new_buy_allowed":      new_buy_allowed,
        "margin_buy_allowed":   margin_buy_allowed,
    }


def _default_state() -> dict:
    return {
        'date':              date.today().isoformat(),
        'daily_pnl_jpy':     0.0,   # P0-2: 評価額ベース（snapshot のみが書く）
        'daily_pnl_pct':     0.0,   # P0-2: 評価額ベース（snapshot のみが書く）
        'realized_pnl_jpy_today': 0.0,    # P0-2: 本日の確定損益（update_pnl が累積、informational）
        'last_eod_portfolio_value': 0.0,  # P0-2: 前日EOD評価額（日次P&L計算の基準、日またぎで更新）
        'monthly_pnl_jpy':   0.0,   # 実態は直近30日ローリング（外部互換のためキー名維持）
        'monthly_pnl_pct':   0.0,   # 実態は直近30日ローリング（外部互換のためキー名維持）
        'pnl_history':       [],     # [{'date': 'YYYY-MM-DD', 'pnl_jpy': float}, ...]（日次P&L履歴）
        'portfolio_value':   0.0,
        'active_trades':     0,
        'short_positions':   0,
        'new_entry_allowed':      True,
        'trading_allowed':        True,
        'nisa_exception_allowed': False,
        'alerts':                 [],
        'override_log':      [],
        'last_updated':      datetime.now().isoformat(),
        'guardrail_stage':   0,           # 0=normal, 1/2/3=staged
        'risk_factor':       1.0,         # 現在のリスク係数 (0.0-1.0)
        'recovery_mode':     False,       # 回復モード中かどうか
        'recovery_start_date': None,      # 回復開始日
        'consecutive_positive_days': 0,   # 連続プラス日数
    }


def _update_rolling30(state: dict) -> None:
    """
    pnl_history（日次P&Lの履歴）から直近30日のローリングP&Lを計算して state を更新する。
    今日の daily_pnl_jpy も含めて合計する。
    結果は monthly_pnl_jpy / monthly_pnl_pct に書き込む（外部互換のためキー名を維持）。
    """
    cutoff = (date.today() - timedelta(days=30)).isoformat()
    today_str = date.today().isoformat()

    # 30日超の古い履歴を削除
    history = [e for e in state.get('pnl_history', []) if e['date'] >= cutoff]
    state['pnl_history'] = history

    # 過去日（today以外）の合計 + 今日の日次P&Lを加算
    past_total = sum(e['pnl_jpy'] for e in history if e['date'] != today_str)
    rolling_jpy = past_total + state.get('daily_pnl_jpy', 0.0)

    state['monthly_pnl_jpy'] = rolling_jpy
    pv = state.get('portfolio_value', 0)
    state['monthly_pnl_pct'] = rolling_jpy / pv if pv > 0 else 0.0


def load_state() -> dict:
    if STATE_FILE.exists():
        with open(STATE_FILE, encoding='utf-8') as f:
            state = json.load(f)
        today_str = date.today().isoformat()
        # 日付が変わっていたら前日の日次P&Lを履歴に保存して日次リセット
        if state.get('date') != today_str:
            prev_date = state.get('date', '')
            prev_pnl  = state.get('daily_pnl_jpy', 0.0)
            if prev_date and prev_pnl != 0.0:
                history = state.get('pnl_history', [])
                # 同一日付のエントリーがあれば上書き、なければ追記
                existing = next((e for e in history if e['date'] == prev_date), None)
                if existing:
                    existing['pnl_jpy'] = prev_pnl
                else:
                    history.append({'date': prev_date, 'pnl_jpy': prev_pnl})
                state['pnl_history'] = history
            state['date']          = today_str
            state['daily_pnl_jpy'] = 0.0
            state['daily_pnl_pct'] = 0.0
            # P0-2: 前日 EOD 評価額を今日の基準として確定（昨日最後に記録された portfolio_value）
            state['last_eod_portfolio_value'] = state.get('portfolio_value', 0.0)
            state['realized_pnl_jpy_today']   = 0.0
        # 旧形式（month/monthly_pnl）があれば移行: 旧monthly値をpnl_historyに取り込まない
        # （旧月次P&Lは30日ローリングと互換性がないため破棄）
        state.pop('month', None)
        # 必須フィールドの補完（旧バージョンからの移行）
        state.setdefault('pnl_history', [])
        # P0-2 migration: 新キーが無い旧 guard_state.json への補完
        state.setdefault('realized_pnl_jpy_today', 0.0)
        if 'last_eod_portfolio_value' not in state:
            # 既存データでは現在の portfolio_value を暫定基準（以後の snapshot で校正される）
            state['last_eod_portfolio_value'] = state.get('portfolio_value', 0.0)
        # 直近30日ローリングP&Lを再計算
        _update_rolling30(state)
        return state
    return _default_state()


def save_state(state: dict):
    # 保存前に前回状態と比較してガードレール発動を検知
    prev = {}
    if STATE_FILE.exists():
        try:
            with open(STATE_FILE, encoding='utf-8') as f:
                prev = json.load(f)
        except Exception:
            pass

    state['last_updated'] = datetime.now().isoformat()
    atomic_write_json(STATE_FILE, state)

    # 新たにガードレールが発動した場合のみ自動提案を送信
    newly_entry_blocked   = prev.get('new_entry_allowed', True)  and not state.get('new_entry_allowed', True)
    newly_trading_blocked = prev.get('trading_allowed',   True)  and not state.get('trading_allowed',   True)
    if newly_entry_blocked or newly_trading_blocked:
        _send_guardrail_suggestion(state, newly_trading_blocked)


def _send_guardrail_suggestion(state: dict, trading_stopped: bool):
    """ガードレール発動時に Haiku で対応提案を生成して Telegram 送信"""
    import anthropic

    level = "全取引停止" if trading_stopped else "新規エントリー禁止"
    icon  = "🚨" if trading_stopped else "⛔"

    # Haiku で対応提案生成
    user_prompt = f"""
ガードレール発動: {level}
日次P&L: {state['daily_pnl_pct']*100:+.2f}%  (¥{state['daily_pnl_jpy']:+,.0f})
月次P&L: {state['monthly_pnl_pct']*100:+.2f}%  (¥{state['monthly_pnl_jpy']:+,.0f})
アクティブトレード: {state['active_trades']}件
空売りポジション: {state['short_positions']}件

今すぐやるべきことを3つ、箇条書きで具体的に教えてください。"""
    try:
        client = anthropic.Anthropic(api_key=os.environ.get("ANTHROPIC_API_KEY", ""))
        started = time.monotonic()
        msg = client.messages.create(
            model=HAIKU_MODEL_ID,
            max_tokens=512,
            system="あなたはユーザーの専属ポートフォリオアドバイザーです。ガードレール発動時の緊急対応を日本語で簡潔に提案してください。",
            messages=[{"role": "user", "content": user_prompt}]
        )
        _log_guardrail_suggestion_usage(
            started=started,
            prompt=user_prompt,
            trading_stopped=trading_stopped,
            level=level,
            response=msg,
        )
        suggestion = msg.content[0].text.strip()
    except Exception as e:
        _log_guardrail_suggestion_usage(
            started=started if "started" in locals() else time.monotonic(),
            prompt=user_prompt,
            trading_stopped=trading_stopped,
            level=level,
            status="error",
            error=e,
        )
        suggestion = f"AI提案生成エラー: {e}"

    # Telegram 送信
    token   = os.environ.get("TELEGRAM_TOKEN", "")
    chat_id = os.environ.get("TELEGRAM_CHAT_ID", "")
    if not token or not chat_id:
        return
    try:
        import requests
        text = (
            f"{icon} *ガードレール発動: {level}*\n\n"
            f"日次: {state['daily_pnl_pct']*100:+.2f}% / 月次: {state['monthly_pnl_pct']*100:+.2f}%\n\n"
            f"*今すぐやること:*\n{suggestion}"
        )
        requests.post(
            f"https://api.telegram.org/bot{token}/sendMessage",
            json={"chat_id": chat_id, "text": text, "parse_mode": "Markdown"},
            timeout=10,
        )
    except Exception as e:
        print(f"Telegram送信エラー（ガードレール提案）: {e}")


# ============================================================
# ガードレール評価
# ============================================================

def _evaluate_recovery(state: dict, daily_pct: float, monthly_pct: float,
                       current_stage: int, alerts: list) -> None:
    """リカバリーモードの判定と状態更新"""
    today_str = date.today().isoformat()

    # 日次プラスなら連続カウント
    if daily_pct > 0:
        state['consecutive_positive_days'] = state.get('consecutive_positive_days', 0) + 1
    else:
        state['consecutive_positive_days'] = 0

    # リカバリーモード開始判定: 連続プラス日数が回復期間に達した場合
    _recovery_window = int(_tp_bg("recovery_window_days", RECOVERY_MODE['recovery_window_days']))
    # _evaluate_recovery 内でも stage 閾値を動的上書き
    _stages_r = {k: dict(v) for k, v in STAGED_GUARDRAILS.items()}
    _s1r = _tp_bg("monthly_stage1_pct", None)
    if _s1r is not None:
        _stages_r['stage_1']['threshold'] = float(_s1r) / 100.0
    _s2r = _tp_bg("monthly_stage2_pct", None)
    if _s2r is not None:
        _stages_r['stage_2']['threshold'] = float(_s2r) / 100.0
    _s3r = _tp_bg("monthly_stage3_pct", None)
    if _s3r is not None:
        _stages_r['stage_3']['threshold'] = float(_s3r) / 100.0
    if (not state.get('recovery_mode') and
            state['consecutive_positive_days'] >= _recovery_window):
        state['recovery_mode'] = True
        state['recovery_start_date'] = today_str
        # リスク係数を少し緩和（ただし最大制限あり）
        max_recovery_risk = RECOVERY_MODE['max_risk_in_recovery']
        state['risk_factor'] = min(max_recovery_risk, state.get('risk_factor', 0) + 0.25)
        alerts.append({
            'level':   'info',
            'message': f'リカバリーモード発動: {_recovery_window}営業日連続プラス達成。リスク係数を{state["risk_factor"]:.0%}に緩和',
            'time':    datetime.now().isoformat(),
        })
    elif state.get('recovery_mode'):
        # リカバリー中に日次マイナスが続いたら回復モード解除
        if state['consecutive_positive_days'] == 0 and daily_pct <= _get_guardrail('daily_loss_limit', GUARDRAILS['daily_loss_limit']):
            state['recovery_mode'] = False
            state['recovery_start_date'] = None
            stage_info = _stages_r[f'stage_{current_stage}']
            state['risk_factor'] = stage_info['risk_factor']
            alerts.append({
                'level':   'warning',
                'message': f'リカバリーモード解除: 日次損失-3%到達。リスク係数を{state["risk_factor"]:.0%}に戻す',
                'time':    datetime.now().isoformat(),
            })


def evaluate(state: dict) -> dict:
    """
    現在の状態に基づきガードレールを評価し、state を更新して返す。
    段階的ガードレール（STAGED_GUARDRAILS）とリカバリーモードを適用する。
    """
    alerts            = []
    new_entry_allowed = True
    trading_allowed   = True

    daily_pct   = state['daily_pnl_pct']
    monthly_pct = state['monthly_pnl_pct']
    active      = state['active_trades']
    shorts      = state['short_positions']

    # 日次損失制限
    if daily_pct <= _get_guardrail('daily_loss_limit', GUARDRAILS['daily_loss_limit']):
        new_entry_allowed = False
        alerts.append({
            'level':   'warning',
            'message': f'本日P&L {daily_pct*100:.2f}%（閾値-4%）→ 新規エントリー禁止',
            'time':    datetime.now().isoformat(),
        })

    # 未発注ストップ/売却チェック（3営業日超で新規ブロック）
    try:
        from action_state_tracker import check_new_position_block as _chk_block, send_telegram_alerts as _send_alerts
        _block_info = _chk_block()
        if _block_info.get("blocked"):
            new_entry_allowed = False
            alerts.append({
                'level':   'critical',
                'message': f'⛔ 未発注アクション3日超 → 新規ポジション禁止｜{_block_info["reason"][:80]}',
                'time':    datetime.now().isoformat(),
            })
            _send_alerts()  # Telegram通知（重複防止は送信側で管理）
    except Exception:
        pass

    # 月次-4%: 現金確保リマインダー（緩和: 旧-3%）
    MONTHLY_CASH_WARNING = -0.04
    _monthly_limit = _get_guardrail('monthly_loss_limit', GUARDRAILS['monthly_loss_limit'])
    if _monthly_limit < monthly_pct <= MONTHLY_CASH_WARNING:
        alerts.append({
            'level':   'warning',
            'message': f'月間P&L {monthly_pct*100:.2f}%（-3%ライン）→ 防衛キャッシュ積み増し推奨',
            'time':    datetime.now().isoformat(),
        })

    # === 段階的ガードレール ===
    prev_stage = state.get('guardrail_stage', 0)
    new_stage  = 0
    risk_factor = 1.0

    # tunable_params で stage1/2/3 の閾値を動的上書き
    _stages = {k: dict(v) for k, v in STAGED_GUARDRAILS.items()}
    _s1 = _tp_bg("monthly_stage1_pct", None)
    if _s1 is not None:
        _stages['stage_1']['threshold'] = float(_s1) / 100.0
    _s2 = _tp_bg("monthly_stage2_pct", None)
    if _s2 is not None:
        _stages['stage_2']['threshold'] = float(_s2) / 100.0
    _s3 = _tp_bg("monthly_stage3_pct", None)
    if _s3 is not None:
        _stages['stage_3']['threshold'] = float(_s3) / 100.0

    for stage_num in [3, 2, 1]:  # 最も厳しいステージから判定
        stage = _stages[f'stage_{stage_num}']
        if monthly_pct <= stage['threshold']:
            new_stage   = stage_num
            risk_factor = stage['risk_factor']
            break

    state['guardrail_stage'] = new_stage
    state['risk_factor']     = risk_factor

    if new_stage >= 3:
        trading_allowed   = False
        new_entry_allowed = False
        state['recovery_mode'] = False
        alerts.append({
            'level':   'critical',
            'message': f'月間P&L {monthly_pct*100:.2f}%（閾値-10%）→ 全取引停止（Stage 3）',
            'time':    datetime.now().isoformat(),
        })
    elif new_stage >= 1:
        stage_info = _stages[f'stage_{new_stage}']
        # Stage 1-2: 新規エントリー禁止だが既存ポジション管理は可能
        new_entry_allowed = False
        alerts.append({
            'level':   'critical' if new_stage == 2 else 'warning',
            'message': f'月間P&L {monthly_pct*100:.2f}%（{stage_info["threshold"]*100:.0f}%ライン）→ {stage_info["label"]}（Stage {new_stage}）',
            'time':    datetime.now().isoformat(),
        })
        # ステージ悪化時はリカバリーモードをリセット
        if new_stage > prev_stage:
            state['recovery_mode']              = False
            state['recovery_start_date']        = None
            state['consecutive_positive_days']  = 0

    # === リカバリーモード判定 ===
    if new_stage > 0 and new_stage <= RECOVERY_MODE['enabled_after_stage'] + 1:
        _evaluate_recovery(state, daily_pct, monthly_pct, new_stage, alerts)
    elif new_stage == 0:
        # 正常復帰
        state['recovery_mode']             = False
        state['recovery_start_date']       = None
        state['consecutive_positive_days'] = 0

    # レジームフリップ例外: BULL レジーム時はNISA積立・既存ポジション追加を許可
    if not trading_allowed and _get_regime_bull():
        state['nisa_exception_allowed'] = True
        alerts.append({
            'level':   'info',
            'message': 'レジームフリップ例外: BULL転換中。NISA積立・既存ポジション追加は継続可能',
            'time':    datetime.now().isoformat(),
        })
    else:
        state['nisa_exception_allowed'] = False

    # DCA ラダー限定フラグ: Active tranche がある場合だけ、policy_engine が
    # type="dca" source="dca_ladder" を DD stage 下でも半量通過させる余地を持つ。
    # stage_3 / daily_block、または trading_allowed が True と確認できない場合は
    # policy 側が fail-closed する。
    state['allow_dca_tranche'] = False
    try:
        from pathlib import Path as _P
        _dca_f = _P(__file__).parent / "bottom_fishing_signals.json"
        if _dca_f.exists():
            import json as _json
            _sig = _json.loads(_dca_f.read_text(encoding="utf-8"))
            _active = _sig.get("active_tranche")
            if _active:
                state['allow_dca_tranche'] = True
                state['dca_active_tranche'] = _active
                alerts.append({
                    'level':   'info',
                    'message': f'🩸 DCA ラダー {_active} 観測中: source="dca_ladder" 候補はDD policy限定例外の対象',
                    'time':    datetime.now().isoformat(),
                })
    except Exception:
        pass

    # ポジション数制限は廃止（長期・中期・スウィング混在ポートフォリオには不適切）
    # 実質的なリスク管理は日次/月次損失制限と未発注アクションブロックで行う

    # 空売りポジション上限（tunable_params: max_short_positions）
    _max_short = int(_get_guardrail('max_short_positions', GUARDRAILS['max_short_positions']))
    if shorts >= _max_short:
        alerts.append({
            'level':   'info',
            'message': f'空売りポジション {shorts}/{_max_short} → 上限到達',
            'time':    datetime.now().isoformat(),
        })

    state['new_entry_allowed'] = new_entry_allowed
    state['trading_allowed']   = trading_allowed
    state['alerts']            = alerts

    return state


# ============================================================
# P&L 更新
# ============================================================

def update_pnl(pnl_jpy: float, portfolio_value: float) -> dict:
    """
    トレードのP&Lを記録し、ガードレールを再評価する。

    P0-2: daily_pnl_jpy は snapshot_portfolio_pnl のみが書く（評価額ベース）。
    確定損益は realized_pnl_jpy_today に累積（informational）。
    daily_pnl_jpy は「現在評価額 - 前日EOD評価額」で一貫して算出する。

    Args:
        pnl_jpy: 確定損益（円）。正=利益、負=損失
        portfolio_value: 現在のポートフォリオ総額（円）

    Returns:
        更新後の状態
    """
    state = load_state()

    state['portfolio_value']         = portfolio_value
    state['realized_pnl_jpy_today'] += pnl_jpy

    # P0-2: daily_pnl_jpy = 現在評価額 - 前日EOD基準（評価額ベースで一本化）
    baseline = state.get('last_eod_portfolio_value', 0.0) or portfolio_value
    state['daily_pnl_jpy'] = portfolio_value - baseline
    if baseline > 0:
        state['daily_pnl_pct'] = state['daily_pnl_jpy'] / baseline
    else:
        state['daily_pnl_pct'] = 0.0

    # 直近30日ローリングP&Lを再計算（monthly_pnl_jpy/pct に反映）
    _update_rolling30(state)

    state = evaluate(state)
    save_state(state)
    return state


def update_positions(active_trades: int, short_positions: int = 0) -> dict:
    """ポジション数を更新してガードレールを再評価する。"""
    state = load_state()
    state['active_trades']   = active_trades
    state['short_positions'] = short_positions
    state = evaluate(state)
    save_state(state)
    return state


# ============================================================
# オーバーライド記録
# ============================================================

def log_override(reason: str, action: str) -> dict:
    """
    ガードレールを無視してトレードした場合の記録。
    月次検証用。

    Args:
        reason: ガードレール違反の理由
        action: 実行したアクション
    """
    state = load_state()
    state['override_log'].append({
        'time':   datetime.now().isoformat(),
        'reason': reason,
        'action': action,
        'pnl_at_override': {
            'daily':   state['daily_pnl_pct'],
            'monthly': state['monthly_pnl_pct'],
        },
    })
    save_state(state)

    # 月次レポートのために override_log.json にも追記
    override_path = BASE_DIR / 'override_log.json'
    all_overrides = []
    if override_path.exists():
        with open(override_path, encoding='utf-8') as f:
            all_overrides = json.load(f)
    all_overrides.append(state['override_log'][-1])
    atomic_write_json(override_path, all_overrides)

    return state


# ============================================================
# ドローダウンチェック
# ============================================================

def check_drawdown(current_value: float, peak_value: float) -> dict:
    """
    ドローダウンを計算し、アクション推奨を返す。

    Args:
        current_value: 現在のポートフォリオ価値（円）
        peak_value: 過去最高値（円）

    Returns:
        {'drawdown_pct', 'level', 'action', 'should_alert'}
    """
    if peak_value <= 0:
        return {'drawdown_pct': 0, 'level': 'normal', 'action': '通常運用', 'should_alert': False}

    dd = (current_value - peak_value) / peak_value

    # tunable_params で閾値を動的上書き（dd_full_liquidate / dd_50pct_reduce）
    _dd_critical = DRAWDOWN_RULES['critical']
    _dd_warning  = DRAWDOWN_RULES['warning']
    _v = _tp_bg("dd_full_liquidate", None)
    if _v is not None:
        _dd_critical = float(_v) / 100.0
    _v = _tp_bg("dd_50pct_reduce", None)
    if _v is not None:
        _dd_warning = float(_v) / 100.0

    if dd <= _dd_critical:
        return {
            'drawdown_pct': round(dd, 4),
            'level':        'critical',
            'action':       '全現金化を推奨。直ちにポジションを解消してください。',
            'should_alert': True,
        }
    elif dd <= _dd_warning:
        return {
            'drawdown_pct': round(dd, 4),
            'level':        'warning',
            'action':       '全ポジション50%縮小を推奨。',
            'should_alert': True,
        }
    else:
        return {
            'drawdown_pct': round(dd, 4),
            'level':        'normal',
            'action':       '通常運用継続。',
            'should_alert': False,
        }


# ============================================================
# スイングポジション損切りチェック
# ============================================================

#: swing ポジションに適用するデフォルトのトレーリングストップ率（-20%）
SWING_DEFAULT_STOP_PCT = -0.20


def check_position_stops() -> list[dict]:
    """
    holdings.json のスイングポジションについて現在価格をチェックし、
    損切りラインを下回っているポジションの警告リストを返す。

    各ポジションの損切り価格:
      - holdings.json に stop_loss_atr（価格値）があればそれを使用
      - なければ entry_price × (1 + SWING_DEFAULT_STOP_PCT) を使用

    Returns:
        [{'ticker', 'current_price', 'stop_price', 'entry_price',
          'shares', 'loss_pct', 'message'}, ...]
    """
    holdings_path = BASE_DIR / 'holdings.json'
    if not holdings_path.exists():
        return []

    with open(holdings_path, encoding='utf-8') as f:
        holdings = json.load(f)

    swing_positions = [
        (key, h) for key, h in holdings.items()
        if h.get('investment_type') == 'swing'
    ]
    if not swing_positions:
        return []

    try:
        import yfinance as yf
    except ImportError:
        print('[STOP CHECK] yfinance 未インストール → スキップ')
        return []

    alerts = []
    for key, h in swing_positions:
        ticker = h.get('ticker', key)
        entry_price = h.get('entry_price', 0)
        shares = h.get('shares', 0)
        currency = h.get('currency', 'USD')

        # 損切り価格を決定
        stop_price = h.get('stop_loss_atr')
        if stop_price is None and entry_price:
            stop_price = round(entry_price * (1 + SWING_DEFAULT_STOP_PCT), 2)

        try:
            info = yf.Ticker(ticker).fast_info
            current_price = float(info['lastPrice'])
        except Exception as e:
            print(f'[STOP CHECK] {ticker} 価格取得失敗: {e}')
            continue

        if stop_price and current_price <= stop_price:
            loss_pct = (current_price - entry_price) / entry_price * 100 if entry_price else 0
            msg = (
                f'⚠️ 損切りライン到達: {ticker} '
                f'現在 {currency}{current_price:.2f} ≤ 損切り {currency}{stop_price:.2f} '
                f'（含み損 {loss_pct:+.1f}% / {shares}株）'
            )
            alerts.append({
                'ticker':        ticker,
                'key':           key,
                'current_price': current_price,
                'stop_price':    stop_price,
                'entry_price':   entry_price,
                'shares':        shares,
                'loss_pct':      round(loss_pct, 2),
                'currency':      currency,
                'message':       msg,
                'checked_at':    datetime.now().isoformat(),
            })
            print(msg)

    # 警告があればガードレール state に記録して Telegram 送信
    if alerts:
        state = load_state()
        existing_alerts = state.get('alerts', [])
        for a in alerts:
            existing_alerts.append({
                'level':   'critical',
                'message': a['message'],
                'time':    a['checked_at'],
                'type':    'stop_loss_breach',
                'ticker':  a['ticker'],
            })
        state['alerts'] = existing_alerts
        save_state(state)
        _send_stop_loss_telegram(alerts)

    return alerts


def _send_stop_loss_telegram(alerts: list[dict]) -> None:
    """損切りライン到達アラートを Telegram に送信"""
    token   = os.environ.get('TELEGRAM_TOKEN', '')
    chat_id = os.environ.get('TELEGRAM_CHAT_ID', '')
    if not token or not chat_id:
        return
    try:
        import requests
        lines = ['🚨 *損切りライン到達アラート*\n']
        for a in alerts:
            lines.append(f"• *{a['ticker']}*: 含み損 {a['loss_pct']:+.1f}% — 損切りライン割れ")
        lines.append('\n⚡ 即座に損切り実行を検討してください。')
        text = '\n'.join(lines)
        requests.post(
            f'https://api.telegram.org/bot{token}/sendMessage',
            json={'chat_id': chat_id, 'text': text, 'parse_mode': 'Markdown'},
            timeout=10,
        )
    except Exception as e:
        print(f'Telegram送信エラー（損切りアラート）: {e}')


# ============================================================
# CLI（crontab から呼び出す）
# ============================================================

def _print_status():
    state = load_state()
    state = evaluate(state)
    save_state(state)

    ok  = '✅'
    ng  = '🔴'
    warn = '⚠️'

    print(f'\n=== 行動ガードレール状況 {datetime.now().strftime("%Y-%m-%d %H:%M")} ===')
    print(f'取引ステータス:     {ok if state["trading_allowed"]   else ng} {"正常" if state["trading_allowed"]   else "停止"}')
    print(f'新規エントリー:     {ok if state["new_entry_allowed"] else ng} {"可能" if state["new_entry_allowed"] else "禁止"}')
    print(f'本日P&L:           ¥{state["daily_pnl_jpy"]:+,.0f}  ({state["daily_pnl_pct"]*100:+.2f}%)')
    print(f'直近30日P&L:       ¥{state["monthly_pnl_jpy"]:+,.0f}  ({state["monthly_pnl_pct"]*100:+.2f}%)')
    print(f'アクティブトレード: {state["active_trades"]}件（上限なし）')
    _display_max_short = int(_get_guardrail('max_short_positions', GUARDRAILS['max_short_positions']))
    print(f'空売りポジション:   {state["short_positions"]}/{_display_max_short}')
    stage = state.get('guardrail_stage', 0)
    rf = state.get('risk_factor', 1.0)
    recovery = state.get('recovery_mode', False)
    stage_icon = {0: '🟢', 1: '🟡', 2: '🟠', 3: '🔴'}.get(stage, '❓')
    print(f'ガードレールStage:   {stage_icon} Stage {stage} / リスク係数 {rf:.0%}')
    if recovery:
        cons = state.get('consecutive_positive_days', 0)
        print(f'リカバリーモード:    🔄 ON（連続プラス{cons}日）')

    if state['alerts']:
        print('\n【アラート】')
        for a in state['alerts']:
            icon = {'critical': '🔴', 'warning': '⚠️', 'info': 'ℹ️'}.get(a['level'], '')
            print(f'  {icon} {a["message"]}')
    else:
        print('\n  アラートなし')

    if state['override_log']:
        print(f'\nオーバーライド記録: {len(state["override_log"])}件（今月）')


def snapshot_portfolio_pnl() -> dict:
    """
    ポートフォリオ評価額の前日比を計算し、guard_state に反映する。
    data_fetcher 後（価格更新済み）に毎日実行すること。

    P0-2: daily_pnl_jpy を書く唯一の関数。
    baseline は last_eod_portfolio_value（日またぎで更新）を使用し、
    intraday の連打で基準が上書きされる問題を防ぐ。
    """
    try:
        import portfolio_manager
        snapshot = portfolio_manager.build_portfolio_snapshot()
        current_value = snapshot.get("total_jpy", 0)
    except Exception as e:
        print(f"[SNAPSHOT] ポートフォリオ取得失敗: {e}")
        return load_state()

    state = load_state()
    baseline = state.get('last_eod_portfolio_value', 0.0)

    # 初回 / 移行時: 今日の値をベースラインとして確定（P&L はゼロ）
    if baseline <= 0 and current_value > 0:
        state['last_eod_portfolio_value'] = current_value
        baseline = current_value

    if baseline > 0 and current_value > 0:
        daily_change_jpy = current_value - baseline
        state['daily_pnl_jpy'] = daily_change_jpy
        state['daily_pnl_pct'] = daily_change_jpy / baseline
    else:
        state['daily_pnl_jpy'] = 0.0
        state['daily_pnl_pct'] = 0.0

    state['portfolio_value'] = current_value

    # ポジション数を更新
    positions = snapshot.get("positions", [])
    state['active_trades'] = len(positions)
    # 空売りポジション（信用空売り）のみを集計。swing は通常買いトレードなのでカウントしない。
    true_shorts = [p for p in positions
                   if p.get("side") == "short"
                   or p.get("margin_type") == "short"
                   or p.get("position_side") == "short"]
    # margin_positions.json から信用空売りも追加で集計
    try:
        _mp_path = BASE_DIR / "margin_positions.json"
        if _mp_path.exists():
            _mp = json.loads(_mp_path.read_text(encoding="utf-8"))
            _mp_list = _mp.get("positions", []) if isinstance(_mp, dict) else (_mp if isinstance(_mp, list) else [])
            for p in _mp_list:
                if str(p.get("side", "")).lower() == "short":
                    true_shorts.append(p)
    except Exception:
        pass
    state['short_positions'] = len(true_shorts)

    # 30日ローリングP&L更新
    _update_rolling30(state)

    state = evaluate(state)
    save_state(state)

    realized = state.get('realized_pnl_jpy_today', 0.0)
    print(f"[SNAPSHOT] 評価額 ¥{current_value:,.0f} / 前日比 ¥{state['daily_pnl_jpy']:+,.0f} ({state['daily_pnl_pct']*100:+.2f}%)")
    print(f"[SNAPSHOT] 前日EOD基準 ¥{baseline:,.0f} / 本日確定損益 ¥{realized:+,.0f}")
    print(f"[SNAPSHOT] 直近30日 ¥{state['monthly_pnl_jpy']:+,.0f} ({state['monthly_pnl_pct']*100:+.2f}%)")
    return state


if __name__ == '__main__':
    args = sys.argv[1:]

    if not args or args[0] == 'status':
        _print_status()

    elif args[0] == 'snapshot':
        # 使い方: python behavioral_guard.py snapshot [--eod]
        # data_fetcher 後に毎日実行 → 評価額の前日比を guard_state に反映
        # --eod: 現在評価額を「今日のEOD基準」として明示的に確定（17:00 cron 用）
        # P2-9: ヘルスチェック用ハートビート
        try:
            from utils import heartbeat as _hb
        except Exception:
            _hb = None
        try:
            state = snapshot_portfolio_pnl()
            if '--eod' in args[1:]:
                # P0-2: 明示的なEOD確定。翌日以降の日次P&L計算基準に使用される。
                s = load_state()
                s['last_eod_portfolio_value'] = s.get('portfolio_value', 0.0)
                save_state(s)
                print(f"[SNAPSHOT] EOD基準を確定: ¥{s['last_eod_portfolio_value']:,.0f}")
            _print_status()
            if _hb:
                _hb('behavioral_guard_snapshot', 'ok')
        except Exception as _e:
            if _hb:
                _hb('behavioral_guard_snapshot', 'error', str(_e)[:500])
            raise

    elif args[0] == 'pnl' and len(args) == 3:
        # 使い方: python behavioral_guard.py pnl <pnl_jpy> <portfolio_value>
        state = update_pnl(float(args[1]), float(args[2]))
        _print_status()

    elif args[0] == 'positions' and len(args) >= 2:
        # 使い方: python behavioral_guard.py positions <active> [short]
        short = int(args[2]) if len(args) > 2 else 0
        state = update_positions(int(args[1]), short)
        _print_status()

    elif args[0] == 'override' and len(args) == 3:
        # 使い方: python behavioral_guard.py override "理由" "アクション"
        log_override(args[1], args[2])
        print(f'オーバーライドを記録しました: {args[1]} → {args[2]}')

    elif args[0] == 'stops':
        # 使い方: python behavioral_guard.py stops
        results = check_position_stops()
        if not results:
            print('✅ 損切りライン到達ポジションなし')
        else:
            print(f'\n⚠️  損切りライン到達: {len(results)}件')
            for r in results:
                print(f'  {r["message"]}')

    elif args[0] == 'drawdown' and len(args) == 3:
        # 使い方: python behavioral_guard.py drawdown <current> <peak>
        result = check_drawdown(float(args[1]), float(args[2]))
        icon = {'normal': '🟢', 'warning': '⚠️', 'critical': '🔴'}.get(result['level'])
        print(f'{icon} ドローダウン: {result["drawdown_pct"]*100:.1f}%')
        print(f'   {result["action"]}')

    else:
        print('使い方:')
        print('  python behavioral_guard.py status')
        print('  python behavioral_guard.py pnl <損益円> <総資産円>')
        print('  python behavioral_guard.py positions <アクティブ数> [空売り数]')
        print('  python behavioral_guard.py override "理由" "アクション"')
        print('  python behavioral_guard.py drawdown <現在値> <ピーク値>')
        print('  python behavioral_guard.py stops')
