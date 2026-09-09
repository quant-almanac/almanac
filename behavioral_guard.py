"""
ALMANAC v4.0 - 行動ガードレール
日次/月次P&L監視・ガードレール状態の永続化・crontab連携CLI
"""

import json
import math
import os
import sys
import time
from datetime import datetime, date, timedelta
from pathlib import Path

from utils import atomic_write_json, heartbeat, positive_finite, redact_secret
from risk_policy import POLICY, RISK_POLICY_VERSION, loss_guard_state


class PortfolioValuationUnavailable(RuntimeError):
    """snapshot_portfolio_pnl() が現在評価額を取得できなかった。

    元の実装はこれを内部で握り潰し、直近の (再評価されていない) state を
    そのまま返していた。CLI の ``snapshot --eod`` はこれを検知する手段が無く、
    評価に失敗した run でも古い portfolio_value を無条件に翌日の EOD 基準として
    確定していた（2026-09 レビュー・平日17:35 cron の実際の失敗経路で再現）。
    """

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
    try:
        from execution_reconciliation import load_effective_execution_records
        items = load_effective_execution_records(base_dir=BASE_DIR)
    except Exception:
        return None
    latest = None
    for e in items:
        if e.get("execution_reconciliation_status") == "review":
            continue
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
    # Compatibility display only.  The values are fixed in RiskPolicy, not
    # read from tunable_params or an AI recommendation.
    'max_short_positions': POLICY.max_short_positions,
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
        # _update_rolling30 がこの値を計算した際に使った date.today()。
        # excluded_days<=0（窓内に欠損無し）だけでは「その計算自体がいつ
        # 行われたか」を問わないため、cron 停止中に最後に計算された古い
        # window がそのまま「確認済み」として使われ続け得た
        # （2026-09 レビュー Codex 4ラウンド目 指摘 #2）。
        'monthly_pnl_computed_for_date': None,
        'pnl_history':       [],     # [{'date': 'YYYY-MM-DD', 'pnl_jpy': float}, ...]（日次P&L履歴）
        'portfolio_value':   0.0,
        # 実際に再評価した writer だけが進める（update_pnl / snapshot_portfolio_pnl）。
        # last_updated は save_state が全 save で更新するため「再評価の証拠」に
        # ならない ―― 外部（earnings_proximity_manager）が評価額の鮮度を検証する
        # ためにこのフィールドを読む（2026-09 レビュー S1）。
        'portfolio_value_as_of': None,
        # daily_pnl_jpy を計算した際に基準として使った
        # last_eod_portfolio_value_as_of の値を、その計算時点で凍結した
        # スナップショット。--eod による last_eod_portfolio_value_as_of の
        # 書き換え（翌日向けの先取りステージング）から独立させるためのフィールド
        # （2026-09 レビュー Codex 指摘 #1）。
        'daily_pnl_basis_as_of': None,
        # daily_pnl_basis_as_of と対になる金額版。today の最初の評価で
        # last_eod_portfolio_value を凍結し、同日内の以後の再評価では
        # 再読込みしない ―― さもないと、間に挟まった --eod が翌日向けに
        # ステージング済みの値を「今日の基準」として読み直し、確定済みの
        # 当日損益を壊す（2026-09 レビュー Codex 3ラウンド目 指摘 #1）。
        'daily_pnl_baseline_jpy': 0.0,
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


def _expected_prior_business_day(d: date) -> date:
    """``d`` の直前の営業日（土日のみ除外。祝日は他モジュールと同じ既存の
    weekday-only 規約に合わせて未対応 ―― known limit として明示済み）。"""
    prev = d - timedelta(days=1)
    while prev.weekday() >= 5:  # 5=Sat, 6=Sun
        prev -= timedelta(days=1)
    return prev


def _daily_pnl_basis_is_valid(state: dict, *, as_of_date: str) -> bool:
    """``daily_pnl_jpy`` が ``as_of_date`` の値として信頼できるかを判定する。

    2条件を両方満たす必要がある:
      (a) daily_pnl_basis_as_of ―― 実際に daily_pnl_jpy を計算したときの
          基準日を、計算時点で凍結したスナップショット ―― が
          as_of_date の直前営業日と厳密に一致する。
      (b) portfolio_value_as_of の日付が as_of_date と一致する
          （as_of_date 中に実際に計算が走った）。

    last_eod_portfolio_value_as_of を直接読まない。そのフィールドは
    (1) 当日を通じて daily_pnl_jpy を計算する基準、と
    (2) --eod が「翌日の新基準」を先取りしてステージングする場所、の
    2つの役割を兼ねる。正常な EOD 確定（失敗ではない）が当日中に (2) として
    その日自身の日付を書き込むと、直後の再評価が (1) の意味で
    delta=0 を「無効」と誤判定し、正常な当日損益まで無効化していた
    （2026-09 レビュー Codex 指摘 #1・実際に平日17:35 cron の正常経路で再現）。
    daily_pnl_basis_as_of は update_pnl/snapshot_portfolio_pnl が
    計算のたびに凍結するため、後から --eod が
    last_eod_portfolio_value_as_of を書き換えても影響を受けない。

    単純な暦日差レンジ (旧: 1〜4日) もやめた。火曜基準のまま水曜が欠測した
    状態で木曜に評価すると、旧実装は暦日差2として「有効」を返していた
    （2026-09 レビュー Codex 指摘 #3: 平日の欠測を素通りさせていた）。
    直前営業日との完全一致にすることで、土日はまたぐが平日の欠測は
    許容しない。
    """
    pv_date = str(state.get('portfolio_value_as_of') or '')[:10]
    if pv_date != str(as_of_date):
        return False
    basis_iso = state.get('daily_pnl_basis_as_of')
    if not basis_iso:
        return False
    try:
        basis = date.fromisoformat(str(basis_iso)[:10])
        current = date.fromisoformat(str(as_of_date))
    except (ValueError, TypeError):
        return False
    return basis == _expected_prior_business_day(current)


def _finite_signed(value: object) -> float | None:
    """有限の実数として解釈できる値だけを通す。bool・None・NaN/inf・
    非数値は None（未確認）に倒す。P&L は負数も正当な値のため
    utils.positive_finite は使えない（0以下も拒否してしまう）。

    再現 (2026-09 レビュー Codex 3ラウンド目 指摘 #4): 従来は
    ``float(x or 0.0)`` で None/False/欠損を無条件に「確認済み0%」へ
    丸め、NaN は ``float(nan)`` が例外を投げないためそのまま通過して
    比較が常時 False になる（`daily <= 閾値` が成立しない）ことで
    「安全」に見えていた。実測: None/False/NaN いずれも daily_pnl_pct に
    与えると loss_guard が ok を返していた。
    """
    if value is None or isinstance(value, bool):
        return None
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    if not math.isfinite(number):
        return None
    return number


def resolve_loss_guard_inputs(guard_state: dict | None, *, as_of_date: str | None = None) -> dict:
    """guard_state.json （またはそれと同じ形の dict）から
    ``{"daily": float|None, "rolling": float|None}`` を解決する。

    behavioral_guard.evaluate() 自身に加え、guard_state.json を直接読む
    他の consumer（execution_preflight._guard_metrics,
    analyst/data_gatherer._loss_guard_from_guard_state）もこの関数を
    経由させる。以前はそれぞれが daily_pnl_pct/monthly_pnl_pct を生の値
    として個別に読んでおり、guard 自身が data_confidence_caution と
    判定した同じ state に対して、他の consumer は daily_block などの
    別の結論に達し得た（2026-09 独立レビュー Codex 指摘 #2）。

    rolling は monthly_pnl_basis_excluded_days が 1 以上（30日集計の一部が
    未確認で除外された）なら None にする ―― 除外件数を保存はするが判定に
    反映していなかった（同 Codex 指摘 #4: 除外1日を含む状態でも
    30日損益0・stage=ok・新規リスク許可になっていた）。

    as_of_date 無指定時は実際の今日（date.today()）を使う。以前は
    guard_state 自身の 'date' フィールドへフォールバックしており、cron が
    何日も止まっていても guard_state が内部的に自己整合してさえいれば
    「確認済み」として通ってしまっていた（2026-09 独立レビュー Codex
    3ラウンド目 指摘 #3・実機再現: 1週間 stale な state の daily=-20% が
    execution_preflight/analyst 側で確認済み扱いになった）。
    evaluate() は自身の as_of_date を明示的に渡すため、この既定値変更の
    影響を受けない。
    """
    if not isinstance(guard_state, dict) or not guard_state:
        return {"daily": None, "rolling": None}

    as_of_date = as_of_date or date.today().isoformat()

    daily_basis_valid = _daily_pnl_basis_is_valid(guard_state, as_of_date=as_of_date)
    daily = _finite_signed(guard_state.get("daily_pnl_pct")) if daily_basis_valid else None

    excluded_days_raw = guard_state.get('monthly_pnl_basis_excluded_days', 0)
    try:
        excluded_days = int(excluded_days_raw)
    except (TypeError, ValueError):
        excluded_days = 1  # 形式不正は「除外あり」扱い、安全側
    # excluded_days<=0（窓内に欠損無し）だけでは、その計算自体が「いつ」
    # 行われたかを問わない。cron が丸ごと止まっていても、最後に計算された
    # 時点でたまたま窓に欠損が無ければ、古い window がそのまま
    # 「確認済み」として使われ続け得た。実測: 1ヶ月以上前に計算された
    # excluded_days=0 の state が、実際の今日を as_of_date として渡しても
    # stage_3（全リスク増加凍結）に達した（2026-09 レビュー Codex 4ラウンド目
    # 指摘 #2）。computed_for_date が as_of_date と厳密一致する場合だけ
    # 信頼する。
    computed_for_date = guard_state.get('monthly_pnl_computed_for_date')
    rolling_confirmed = excluded_days <= 0 and str(computed_for_date) == str(as_of_date)
    rolling = _finite_signed(guard_state.get("monthly_pnl_pct")) if rolling_confirmed else None

    return {"daily": daily, "rolling": rolling}


def _update_rolling30(state: dict) -> None:
    """
    pnl_history（日次P&Lの履歴）から直近30日のローリングP&Lを計算して state を更新する。
    今日の daily_pnl_jpy も含めて合計する。
    結果は monthly_pnl_jpy / monthly_pnl_pct に書き込む（外部互換のためキー名を維持）。

    pnl_jpy が None（基準無効で記録された日、_daily_pnl_basis_is_valid 参照）の
    履歴行は合計から除外する。今日分も基準が無効なら除外する ―― 確認済みの
    残り日数分のローリング値を殺さないため、rolling 全体を None にはしない
    （2026-09 レビュー S1b・Codex 指摘1: 判明している制約は維持する）。
    """
    cutoff = (date.today() - timedelta(days=30)).isoformat()
    today_str = date.today().isoformat()

    # 30日超の古い履歴を削除
    history = [e for e in state.get('pnl_history', []) if e.get('date', '') >= cutoff]
    state['pnl_history'] = history

    # 過去日（today以外）の合計。基準無効で pnl_jpy=None の日は除外する
    # （旧実装は無条件 sum() で None 混入時に TypeError していた）。
    past_total = sum(
        e['pnl_jpy'] for e in history
        if e.get('date') != today_str and e.get('pnl_jpy') is not None
    )
    excluded_days = sum(1 for e in history if e.get('pnl_jpy') is None)

    today_valid = _daily_pnl_basis_is_valid(state, as_of_date=state.get('date') or today_str)
    today_component = state.get('daily_pnl_jpy', 0.0) if today_valid else 0.0
    if not today_valid:
        excluded_days += 1

    rolling_jpy = past_total + today_component

    state['monthly_pnl_jpy'] = rolling_jpy
    state['monthly_pnl_basis_excluded_days'] = excluded_days
    state['monthly_pnl_computed_for_date'] = today_str
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
            pv_date = str(state.get('portfolio_value_as_of') or '')[:10]
            # 前日分を「確定値」として記録できるのは、_daily_pnl_basis_is_valid
            # が前日を確認済みと判定する場合だけ。この関数は (a) 前日の計算に
            # 使われた基準（daily_pnl_basis_as_of ―― 計算時点の凍結値。
            # last_eod_portfolio_value_as_of ではない、後述）が前日の直前
            # 営業日と一致し、かつ (b) 前日中に実際に評価が成功した
            # （portfolio_value_as_of が前日を指す）ことを両方要求する。
            # (a) だけでは足りない ―― 基準は新鮮でも、その日一日評価が一度も
            # 成功しなかった（daily_pnl_jpy がロールオーバー既定値 0.0 のまま）
            # ケースを「確認済みゼロ変化」と誤認してしまう
            # （2026-09 レビュー・自己レビューで発見: 全休止日が無記録のまま
            # 30日集計へ黙って0寄与していた）。
            prev_day_confirmed = bool(prev_date) and _daily_pnl_basis_is_valid(
                state, as_of_date=prev_date)
            # 土日は「予定された評価が欠測した日」ではなく、そもそも評価を
            # 予定していない日。読み取り専用のはずの status 表示ですら
            # load_state() を呼んで即 save_state() するため（_print_status）、
            # 金曜EOD確定→土曜status→日曜status、という経路だけで土日2日分が
            # 「基準未確認」として pnl_history に記録され、月曜の正常な snapshot
            # まで monthly_pnl_basis_excluded_days 経由で data_confidence_caution/
            # new_entry_allowed=False に巻き込んでいた（2026-09 レビュー
            # Codex 3ラウンド目 指摘 #2・実機再現）。不正な日付形式は安全側
            # （従来どおり平日として扱い記録する）。
            try:
                prev_is_weekday = date.fromisoformat(prev_date).weekday() < 5 if prev_date else True
            except (ValueError, TypeError):
                prev_is_weekday = True
            if prev_date and prev_is_weekday:
                history = state.get('pnl_history', [])
                # 同一日付のエントリーがあれば上書き、なければ追記
                existing = next((e for e in history if e['date'] == prev_date), None)
                if prev_day_confirmed:
                    if existing:
                        existing['pnl_jpy'] = prev_pnl
                        existing.pop('basis_valid', None)
                    elif prev_pnl != 0.0:
                        history.append({'date': prev_date, 'pnl_jpy': prev_pnl})
                else:
                    # 基準が前日を確認できていない、または前日中に評価が
                    # 一度も成功していない ―― prev_pnl は複数日分の差分か、
                    # 単に「未計測」かもしれないので、それを1日分の確定値
                    # として残さない。「不明」であることを明示的に記録する
                    # （2026-09 レビュー S1b・Codex 指摘1）。
                    if existing:
                        existing['pnl_jpy'] = None
                        existing['basis_valid'] = False
                    else:
                        history.append({'date': prev_date, 'pnl_jpy': None,
                                        'basis_valid': False})
                state['pnl_history'] = history
            state['date']          = today_str
            state['daily_pnl_jpy'] = 0.0
            state['daily_pnl_pct'] = 0.0
            # P0-2: 前日 EOD 評価額を今日の基準として確定（昨日最後に記録された portfolio_value）。
            # ただし portfolio_value 自体が前日を確認済みで評価されていた場合に限る
            # ―― でなければ、何日も前の評価額を「今日確定した」基準として
            # 上書きしてしまう（2026-09 レビュー・平日17:35 cron の実際の失敗
            # 経路で再現。詳細は tests/test_guard_eod_failure_handling.py）。
            # こちらは prev_basis_valid を要求しない ―― 旧基準が古くても、
            # 今まさに得られた新しい評価額を新基準として採用するのが正しい
            # 自己回復動作（基準が古い間ずっと固まったままにしない）。
            # 確認できなければ既存の last_eod_portfolio_value をそのまま保持する
            # ―― _daily_pnl_basis_is_valid() がその古さを検知する。
            if pv_date == prev_date:
                state['last_eod_portfolio_value'] = state.get('portfolio_value', 0.0)
                state['last_eod_portfolio_value_as_of'] = state.get('portfolio_value_as_of')
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

    level = "リスク増加凍結＋人間レビュー" if trading_stopped else "新規エントリー禁止"
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
        from almanac.llm_safety import assert_book_aware_allowed, BookAwareDisabled, log_book_aware_call
        try:
            assert_book_aware_allowed(provider="anthropic")
        except BookAwareDisabled as _e:
            log_book_aware_call(role="guardrail_suggestion", model=HAIKU_MODEL_ID,
                                 fields=["daily_pnl", "monthly_pnl", "active_trades", "short_positions"],
                                 status="blocked")
            raise RuntimeError(str(_e))

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
        # requests/urllib3 は接続エラーの文字列表現に URL (token を含む) を
        # 埋め込む。cron (17:35/18:55) がこの print を guard_log.txt へ
        # リダイレクトするので、伏せずに出すと平文でディスクに残る
        # (2026-08-24 レビューで実際に検出)。
        print(f"Telegram送信エラー（ガードレール提案）: {redact_secret(str(e), token)}")


# ============================================================
# ガードレール評価
# ============================================================

def _legacy_evaluate(state: dict) -> dict:
    """Compatibility alias for callers that still import the old name."""
    return evaluate(state)


def evaluate(state: dict) -> dict:
    """Evaluate the fixed v7 loss guard without regime or DCA exceptions.

    Daily and rolling 30-day P&L are shock controls, not drawdown.  Stage 3
    freezes *risk increases* only; sells, covers, hedges and stops remain
    available.  ``risk_factor`` is retained at 1.0 for compatibility but is
    no longer an automatic sizing instruction.
    """
    # daily_pnl_pct は基準（last_eod_portfolio_value）が対象日の前日を確認済み
    # 評価している場合だけ信頼する。EOD 評価の失敗が続くと基準は古いまま
    # 据え置かれるため（load_state 参照）、その古さを検知せずに daily を渡すと
    # 複数日分の差分を1日分として日次ショック制御に食わせてしまう
    # （2026-09 レビュー S1b・Codex 指摘1）。数値そのものは消さず、判定にだけ
    # None を渡す ―― 履歴/30日集計/表示は元の値を使い続けられる。
    resolved = resolve_loss_guard_inputs(
        state, as_of_date=state.get('date') or date.today().isoformat())
    daily = resolved["daily"]
    rolling = resolved["rolling"]
    decision = loss_guard_state(
        daily_pnl_decimal=daily,
        rolling_30_pnl_decimal=rolling,
    )
    stage_by_name = {
        "ok": 0, "daily_block": 0, "stage_1": 1, "stage_2": 2, "stage_3": 3,
        "data_confidence_caution": 0,
    }
    loss_stage = str(decision["loss_guard_stage"])
    stage = stage_by_name[loss_stage]
    now = datetime.now().isoformat()
    alerts: list[dict] = []
    if loss_stage != "ok":
        # dict リテラルは選ばれなかったキーの値も含めて全て即時評価されるため、
        # rolling=None（除外日数あり、Codex 指摘 #4 対応）のまま stage_1〜3 の
        # f-string を組み立てると daily_block 選択時にも TypeError になる
        # （自己レビューで発見）。daily 側と同じく NaN 表示にフォールバックする。
        daily_pct_display = daily * 100 if daily is not None else float("nan")
        rolling_pct_display = rolling * 100 if rolling is not None else float("nan")
        labels = {
            "daily_block": f"日次P&L {daily_pct_display:.2f}% が -3% 日次ショック制御に到達",
            "stage_1": f"30日P&L {rolling_pct_display:.2f}% が -6% に到達",
            "stage_2": f"30日P&L {rolling_pct_display:.2f}% が -9% に到達",
            "stage_3": f"30日P&L {rolling_pct_display:.2f}% が -12% に到達：リスク増加を凍結し人間レビュー",
            "data_confidence_caution": "日次/30日P&Lの確認可能な基準が不足（EOD評価失敗または移行直後）",
        }
        alerts.append({
            "level": "critical" if stage >= 2 else "warning",
            "message": labels[loss_stage] + "。売却・カバー・ヘッジ・ストップは継続可能",
            "time": now,
        })
    try:
        from action_state_tracker import check_new_position_block as _check_block, send_telegram_alerts as _send_alerts
        pending = _check_block()
        if pending.get("blocked"):
            decision["new_risk_allowed"] = False
            alerts.append({
                "level": "critical",
                "message": f"未発注アクションにより新規リスク停止：{str(pending.get('reason') or '')[:80]}",
                "time": now,
            })
            _send_alerts()
    except Exception:
        pass
    max_short = POLICY.max_short_positions
    if int(state.get("short_positions") or 0) >= max_short:
        alerts.append({"level": "info", "message": f"空売りポジション {state.get('short_positions', 0)}/{max_short} → 上限到達", "time": now})

    state.update({
        "risk_policy_version": RISK_POLICY_VERSION,
        "loss_guard_stage": loss_stage,
        "loss_guard_reason_code": decision["reason_code"],
        # Legacy consumers still use this string.  It is a loss-guard stage,
        # never a drawdown value; data_gatherer publishes the renamed fields.
        "actual_dd_stage": decision["actual_dd_stage"],
        "guardrail_stage": stage,
        "new_entry_allowed": bool(decision["new_risk_allowed"]),
        "trading_allowed": True,
        "risk_factor": 1.0,
        "nisa_exception_allowed": False,
        "allow_dca_tranche": False,
        "dca_active_tranche": None,
        "recovery_mode": False,
        "recovery_start_date": None,
        "consecutive_positive_days": 0,
        "alerts": alerts,
    })
    return state


# ============================================================
# P&L 更新
# ============================================================

def _resolve_daily_pnl_baseline(state: dict, *, today_str: str) -> float:
    """今日の daily_pnl 計算に使う基準額を返す。

    今日まだ一度も凍結していなければ（portfolio_value_as_of が今日を
    指していなければ）、今この瞬間の last_eod_portfolio_value(_as_of) を
    daily_pnl_basis_as_of / daily_pnl_baseline_jpy へ凍結してから返す。
    既に今日凍結済みなら（同日内2回目以降の呼出し）その凍結値をそのまま
    再利用し、last_eod_portfolio_value(_as_of) を再読込みしない。

    再読込みすると、間に挟まった --eod が翌日向けにステージング済みの
    今日自身の値を「今日の基準」として読み直してしまい、確定済みの
    当日 daily_pnl を破壊する ―― 実測: EOD確定→同日再評価で
    daily_pnl_pct が正しい値から 0.00%、stage が ok から
    data_confidence_caution へ変化することを確認（2026-09 レビュー
    Codex 3ラウンド目 指摘 #1）。update_pnl/snapshot_portfolio_pnl の
    どちらか一方だけを直しても、もう一方の writer 経由で再現するため
    共通化した。
    """
    pv_date = str(state.get('portfolio_value_as_of') or '')[:10]
    if pv_date != today_str:
        state['daily_pnl_basis_as_of'] = state.get('last_eod_portfolio_value_as_of')
        state['daily_pnl_baseline_jpy'] = state.get('last_eod_portfolio_value', 0.0)
    return state.get('daily_pnl_baseline_jpy', 0.0)


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
    # snapshot_portfolio_pnl と同じ検証。alert.update_guard_state() から
    # 呼ばれる実経路であり、0・負数・bool がそのまま保存され得た
    # （2026-09 レビュー Codex 3ラウンド目 指摘 #5）。state を一切
    # 変更する前に検証することで、拒否された呼出しが state に触れない
    # ことも保証する。
    validated_value = positive_finite(portfolio_value, label="update_pnl.portfolio_value")

    state = load_state()

    # portfolio_value_as_of を上書きする前に「今日は既に計算済みか」を
    # 判定する。先に上書きしてしまうと _resolve_daily_pnl_baseline が
    # 常に「今日は既に計算済み」と誤判定し、当日1回目の呼出しでも基準を
    # 凍結できなくなる（2026-09 レビュー Codex 3ラウンド目 指摘 #1・
    # 自己レビューで発見: snapshot_portfolio_pnl は元から正しい順序だった
    # が、update_pnl はこの並び順の誤りを最初から持っていた）。
    # P0-2: daily_pnl_jpy = 現在評価額 - 前日EOD基準（評価額ベースで一本化）
    baseline = _resolve_daily_pnl_baseline(state, today_str=date.today().isoformat())

    state['portfolio_value']         = validated_value
    state['portfolio_value_as_of']   = datetime.now().isoformat()
    state['realized_pnl_jpy_today'] += pnl_jpy
    if baseline <= 0:
        baseline = state['portfolio_value']
        state['daily_pnl_baseline_jpy'] = baseline
    state['daily_pnl_jpy'] = state['portfolio_value'] - baseline
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

    # This helper is descriptive only.  The authoritative enforcement path is
    # the flow-adjusted DD state machine; neither a 50% reduction nor a full
    # liquidation is ever automatic.
    if dd <= POLICY.dd_objective_breach_decimal:
        return {
            'drawdown_pct': round(dd, 4),
            'level':        'objective_breach',
            'action':       '12ヶ月DD目標逸脱。緊急の人間レビューが必要です。',
            'should_alert': True,
        }
    if dd <= POLICY.dd_freeze_decimal:
        return {
            'drawdown_pct': round(dd, 4),
            'level':        'freeze',
            'action':       'リスク増加を凍結し、緊急の人間レビューを要請。売却・ヘッジは継続可能。',
            'should_alert': True,
        }
    if dd <= POLICY.dd_derisk_decimal:
        return {
            'drawdown_pct': round(dd, 4), 'level': 'derisk_review',
            'action': '戦術・投機リスク予算50%のデリスク計画を人間レビューへ送る。自動売却はしない。',
            'should_alert': True,
        }
    if dd <= POLICY.dd_block_decimal:
        return {
            'drawdown_pct': round(dd, 4), 'level': 'block',
            'action': '通常の新規リスク経路を停止し、人間承認へ送る。', 'should_alert': True,
        }
    if dd <= POLICY.dd_caution_decimal:
        return {
            'drawdown_pct': round(dd, 4), 'level': 'caution',
            'action': '注意状態。新規リスクは人間確認のうえで判断する。', 'should_alert': True,
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
        print(f'Telegram送信エラー（損切りアラート）: {redact_secret(str(e), token)}')


# ============================================================
# CLI（crontab から呼び出す）
# ============================================================

def _print_status():
    state = load_state()
    state = evaluate(state)
    save_state(state)

    ok  = '✅'
    ng  = '🔴'

    print(f'\n=== 行動ガードレール状況 {datetime.now().strftime("%Y-%m-%d %H:%M")} ===')
    print(
        f'取引ステータス:     {ok if state["trading_allowed"] else ng} '
        f'{"正常" if state["trading_allowed"] else "リスク増加凍結＋人間レビュー"}'
    )
    print(f'新規エントリー:     {ok if state["new_entry_allowed"] else ng} {"可能" if state["new_entry_allowed"] else "禁止"}')
    print(f'本日P&L:           ¥{state["daily_pnl_jpy"]:+,.0f}  ({state["daily_pnl_pct"]*100:+.2f}%)')
    print(f'直近30日P&L:       ¥{state["monthly_pnl_jpy"]:+,.0f}  ({state["monthly_pnl_pct"]*100:+.2f}%)')
    print(f'アクティブトレード: {state["active_trades"]}件（上限なし）')
    _display_max_short = POLICY.max_short_positions
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
        # 読み取り側（earnings_proximity_manager）は NAV/FX を検証するのに
        # 書込み側にはこの検証が無く、0・負数・bool がそのまま
        # portfolio_value として保存され得た（2026-09 レビュー Codex 指摘
        # #5）。ValueError はこの except で PortfolioValuationUnavailable に
        # 揃えて扱う ―― 「評価額を取得できなかった」と「取得はできたが
        # 使い物にならない値だった」を区別する理由が呼出元には無い。
        current_value = positive_finite(
            snapshot.get("total_jpy", 0), label="portfolio_manager.total_jpy")
    except Exception as e:
        print(f"[SNAPSHOT] ポートフォリオ取得失敗: {e}")
        # ここで load_state() を返して「成功」に見せかけない。呼出元
        # （CLI の --eod）が失敗を検知できず、未再評価の state を翌日の
        # EOD 基準として確定してしまっていた（2026-09 レビュー）。
        raise PortfolioValuationUnavailable(str(e)) from e

    state = load_state()
    baseline = _resolve_daily_pnl_baseline(state, today_str=date.today().isoformat())

    # 初回 / 移行時: 今日の値をベースラインとして確定（P&L はゼロ）
    if baseline <= 0 and current_value > 0:
        state['last_eod_portfolio_value'] = current_value
        state['daily_pnl_baseline_jpy'] = current_value
        baseline = current_value

    if baseline > 0 and current_value > 0:
        daily_change_jpy = current_value - baseline
        state['daily_pnl_jpy'] = daily_change_jpy
        state['daily_pnl_pct'] = daily_change_jpy / baseline
    else:
        state['daily_pnl_jpy'] = 0.0
        state['daily_pnl_pct'] = 0.0

    state['portfolio_value'] = current_value
    state['portfolio_value_as_of'] = datetime.now().isoformat()

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


def _run_snapshot_cli(args: list[str]) -> int:
    """``behavioral_guard.py snapshot [--eod]`` を処理する。

    __main__ の平坦な dispatch から分離し、EOD 確定の失敗時ハンドリング
    （2026-09 レビュー S1b）を直接テスト可能にする。

    Args:
        args: ``sys.argv[1:]`` 相当。``args[0]`` は ``'snapshot'``。

    Returns:
        プロセス終了コード。
    """
    # 使い方: python behavioral_guard.py snapshot [--eod]
    # data_fetcher 後に毎日実行 → 評価額の前日比を guard_state に反映
    # --eod: 現在評価額を「今日のEOD基準」として明示的に確定（17:00 cron 用）
    try:
        state = snapshot_portfolio_pnl()
    except PortfolioValuationUnavailable as _e:
        # 評価そのものが失敗した run。EOD 確定も _print_status も実行しない
        # ―― 未再評価の state を「今日確定した」ように見せてはいけない。
        # LockBusy と同種の「予想される外部データ異常」として扱い、
        # traceback は出さず non-zero で終了する。
        print(f"[SNAPSHOT] ⚠️ 評価額取得失敗のため EOD 確定・状態更新をスキップ: {_e}")
        heartbeat('behavioral_guard_snapshot', 'warn', str(_e)[:500])
        return 1
    except Exception as _e:
        # snapshot_portfolio_pnl() 内部の評価額取得より後（例: save_state の
        # atomic_write_json 書き込み失敗）で起きた未知の例外は
        # PortfolioValuationUnavailable を経由しないため上の except では
        # 捕まらない。無音のまま伝播させず、下の --eod ブロックと同じく
        # heartbeat=error を記録してから re-raise する（2026-09 レビュー
        # Codex 2ラウンド目 指摘 #8）。
        heartbeat('behavioral_guard_snapshot', 'error', str(_e)[:500])
        raise

    try:
        if '--eod' in args[1:]:
            # P0-2: 明示的なEOD確定。翌日以降の日次P&L計算基準に使用される。
            # portfolio_value_as_of は上の snapshot_portfolio_pnl() 成功時に
            # 今まさに更新されているので、その値をそのまま基準時刻として使う
            # （datetime.now() を取り直すと実測時刻とズレる）。
            s = load_state()
            s['last_eod_portfolio_value'] = s.get('portfolio_value', 0.0)
            s['last_eod_portfolio_value_as_of'] = s.get('portfolio_value_as_of')
            save_state(s)
            print(f"[SNAPSHOT] EOD基準を確定: ¥{s['last_eod_portfolio_value']:,.0f}")
        _print_status()
        heartbeat('behavioral_guard_snapshot', 'ok')
        return 0
    except Exception as _e:
        heartbeat('behavioral_guard_snapshot', 'error', str(_e)[:500])
        raise


if __name__ == '__main__':
    args = sys.argv[1:]

    if not args or args[0] == 'status':
        _print_status()

    elif args[0] == 'snapshot':
        sys.exit(_run_snapshot_cli(args))

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
