"""
ALMANAC — AI 動的外貨比率方針の検証・保存・解決。

AI (Opus synthesis) が出力する currency_target_recommendation を:
  1. 検証する (basis / 合計100% / confidence / valid_until / 急変・horizon クランプ)
  2. valid なら currency_policy_state.json + currency_policy_log.jsonl に保存する
  3. rebalance_engine 用に「有効な AI 方針 or static fallback」を解決する

設計原則 (objective.md §7, Codex/Claude レビュー 2026-07):
  - AI は候補生成器。外貨比率は AI が判断するが **自動発注はしない**。Policy/人間実行は不変。
  - rebalance に適用するのは basis="long_tier" の方針のみ
    (AI が見る whole_portfolio 比率を long tier 母数へ誤適用しないため)。
  - 壊れ/古い/自信不足/合計不一致/basis不一致 → static CURRENCY_TARGETS に fail-closed。
  - 全 ingest は log.jsonl に append (採否問わず監査可能)。state は採用時のみ atomic write。
  - 目標変化は1回 ±MAX_DELTA_PCT pt、horizon は ±MAX_HORIZON_DAYS 日までクランプ。
    AI 申告の valid_until/horizon を無条件採用しない。
"""

from __future__ import annotations

import json
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Optional

from utils import atomic_write_json, process_lock

BASE_DIR = Path(__file__).parent

# ============================================================
# 定数 (objective.md と整合。後付け閾値はここに追記する)
# ============================================================

SCHEMA_VERSION = 1

#: rebalance に適用してよい唯一の basis。AI が whole_portfolio 基準で出した値を
#: long tier 母数に当てる母数ズレを防ぐためのガード。
APPLICABLE_BASIS = "long_tier"

#: 採用に必要な最低 confidence。これ未満は static fallback (fail-closed)。
MIN_CONFIDENCE_PCT = 60

#: 1回の改訂で許す USD 目標の最大変化 (pt)。超過分はクランプ (reject ではない)。
MAX_DELTA_PCT = 10

#: AI 方針の最大有効期間 (日)。AI 申告 valid_until/horizon はこれを超えてクランプ。
MAX_HORIZON_DAYS = 30

#: usd+jpy 合計の 100% 許容誤差 (pt)。
SUM_TOLERANCE_PCT = 0.5

#: ideal から min/max を導く帯幅 (fraction)。static CURRENCY_TARGETS と同じ ±0.05。
TARGET_BAND = 0.05

DEFAULT_STATE_PATH = BASE_DIR / "currency_policy_state.json"
DEFAULT_LOG_PATH = BASE_DIR / "currency_policy_log.jsonl"

#: 並行書き込み (uvicorn reload / cron 同時実行) 耐性のためのプロセス間ロック名。
_LOCK_NAME = "currency_policy"

try:
    from zoneinfo import ZoneInfo
    _JST = ZoneInfo("Asia/Tokyo")
except Exception:  # pragma: no cover - zoneinfo は標準だが防御的に
    _JST = None


# ============================================================
# 小道具
# ============================================================

def _today(now=None) -> date:
    """now (date|datetime|None) を JST の date に正規化する。"""
    if now is None:
        if _JST is not None:
            return datetime.now(_JST).date()
        return date.today()
    if isinstance(now, datetime):
        return now.date()
    if isinstance(now, date):
        return now
    # 文字列等
    return date.fromisoformat(str(now)[:10])


def _num(x) -> Optional[float]:
    """数値化できれば float、できなければ None。bool は除外。"""
    if isinstance(x, bool) or x is None:
        return None
    try:
        return float(x)
    except (TypeError, ValueError):
        return None


def _parse_date(x) -> Optional[date]:
    if not x:
        return None
    try:
        return date.fromisoformat(str(x)[:10])
    except (TypeError, ValueError):
        return None


def _targets_from_pct(usd_pct: float, jpy_pct: float, band: float = TARGET_BAND) -> dict:
    """
    AI の整数 pct 目標を rebalance_engine.CURRENCY_TARGETS と同じ形
    ({'USD': {min,max,ideal}, 'JPY': {...}}, fraction) に変換する。
    """
    u = usd_pct / 100.0
    j = jpy_pct / 100.0

    def _row(mid: float) -> dict:
        return {
            "min": round(max(0.0, mid - band), 4),
            "max": round(min(1.0, mid + band), 4),
            "ideal": round(mid, 4),
        }

    return {"USD": _row(u), "JPY": _row(j)}


# ============================================================
# 検証 (pure)
# ============================================================

def validate_recommendation(rec, *, current_targets: dict, now=None) -> dict:
    """
    AI の currency_target_recommendation を検証・正規化する (ファイル I/O なし)。

    Returns:
        {
          "ok":         bool,   # rebalance に適用可能か
          "verdict":    "accepted" | "clamped" | "rejected",
          "reason":     str,
          "normalized": dict | None,  # 採用時の state 候補
          "clamped":    bool,
        }
    """
    def _reject(reason: str) -> dict:
        return {"ok": False, "verdict": "rejected", "reason": reason,
                "normalized": None, "clamped": False}

    if not isinstance(rec, dict):
        return _reject("recommendation が dict ではない")

    basis = rec.get("basis")
    if basis != APPLICABLE_BASIS:
        return _reject(f"basis={basis!r} は適用対象外 (要 {APPLICABLE_BASIS!r})")

    usd = _num(rec.get("usd_target_pct"))
    jpy = _num(rec.get("jpy_target_pct"))
    if usd is None or jpy is None:
        return _reject("usd_target_pct / jpy_target_pct が数値でない")
    if not (0.0 <= usd <= 100.0) or not (0.0 <= jpy <= 100.0):
        return _reject(f"目標 pct が範囲外 (usd={usd}, jpy={jpy})")
    if abs((usd + jpy) - 100.0) > SUM_TOLERANCE_PCT:
        return _reject(f"usd+jpy 合計が100%でない (={usd + jpy})")

    conf = _num(rec.get("confidence_pct"))
    if conf is None:
        return _reject("confidence_pct 欠落 (fail-closed)")
    if conf < MIN_CONFIDENCE_PCT:
        return _reject(f"confidence {conf}% < {MIN_CONFIDENCE_PCT}% (fail-closed)")

    horizon = _num(rec.get("horizon_days"))
    if horizon is None or horizon <= 0:
        return _reject("horizon_days 欠落/非正 (fail-closed)")

    today = _today(now)
    vu = _parse_date(rec.get("valid_until"))
    if vu is None:
        return _reject("valid_until 欠落/不正 (fail-closed)")
    if vu < today:
        return _reject(f"valid_until {vu} は既に期限切れ")

    clamped = False
    clamp_notes: list[str] = []
    source_usd = usd

    # 急変クランプ: current ideal からの USD 変化を ±MAX_DELTA_PCT に制限。
    try:
        cur_ideal_usd = float(current_targets["USD"]["ideal"]) * 100.0
    except (KeyError, TypeError, ValueError):
        cur_ideal_usd = usd  # current 不明なら据え置き比較 (= クランプなし)
    delta = usd - cur_ideal_usd
    if abs(delta) > MAX_DELTA_PCT:
        sign = 1.0 if delta > 0 else -1.0
        usd = cur_ideal_usd + sign * MAX_DELTA_PCT
        jpy = 100.0 - usd
        clamped = True
        clamp_notes.append(
            f"USD 変化 {delta:+.0f}pt を ±{MAX_DELTA_PCT}pt にクランプ "
            f"({source_usd:.0f}%→{usd:.0f}%)"
        )

    # horizon クランプ + valid_until 上限。
    horizon_eff = min(int(round(horizon)), MAX_HORIZON_DAYS)
    if horizon_eff != int(round(horizon)):
        clamped = True
        clamp_notes.append(f"horizon {int(round(horizon))}d を {MAX_HORIZON_DAYS}d にクランプ")
    vu_cap = today + timedelta(days=MAX_HORIZON_DAYS)
    vu_eff = min(vu, vu_cap)
    if vu_eff != vu:
        clamped = True
        clamp_notes.append(f"valid_until {vu} を {vu_eff} にクランプ")

    usd_i = int(round(usd))
    jpy_i = int(round(jpy))
    # クランプ後の整数化で合計がずれた場合は JPY 側で吸収。
    if usd_i + jpy_i != 100:
        jpy_i = 100 - usd_i

    normalized = {
        "schema_version": SCHEMA_VERSION,
        "as_of": datetime.now().isoformat(timespec="seconds"),
        "basis": APPLICABLE_BASIS,
        "usd_target_pct": usd_i,
        "jpy_target_pct": jpy_i,
        "confidence_pct": int(round(conf)),
        "horizon_days": horizon_eff,
        "valid_until": vu_eff.isoformat(),
        "reason": str(rec.get("reason", "")),
        "review_triggers": list(rec.get("review_triggers") or []),
        "risk_notes": str(rec.get("risk_notes", "")),
        "clamped": clamped,
        "clamp_note": "; ".join(clamp_notes) if clamp_notes else None,
        "source_usd_target_pct": int(round(source_usd)),
    }
    return {
        "ok": True,
        "verdict": "clamped" if clamped else "accepted",
        "reason": "; ".join(clamp_notes) if clamped else "valid",
        "normalized": normalized,
        "clamped": clamped,
    }


# ============================================================
# ingest (検証 → log append → state atomic write)
# ============================================================

def _append_log(log_path: Path, entry: dict) -> None:
    log_path.parent.mkdir(parents=True, exist_ok=True)
    with log_path.open("a", encoding="utf-8") as f:
        f.write(json.dumps(entry, ensure_ascii=False) + "\n")


def ingest(rec, *, current_targets: dict, now=None,
           state_path=DEFAULT_STATE_PATH, log_path=DEFAULT_LOG_PATH) -> dict:
    """
    AI 方針を検証し、log に必ず追記、valid なら state を atomic write する。

    保存順は「log 追記 → state atomic write」。途中失敗でも log が監査の真実源。

    Returns:
        {"actionable": bool, "verdict": str, "reason": str,
         "clamped": bool, "normalized": dict | None}
    """
    state_path = Path(state_path)
    log_path = Path(log_path)

    result = validate_recommendation(rec, current_targets=current_targets, now=now)

    raw = rec if isinstance(rec, dict) else {}
    log_entry = {
        "ts": datetime.now().isoformat(timespec="seconds"),
        "actionable": result["ok"],
        "verdict": result["verdict"],
        "reason": result["reason"],
        "clamped": result["clamped"],
        "basis": raw.get("basis"),
        "usd_target_pct": raw.get("usd_target_pct"),
        "jpy_target_pct": raw.get("jpy_target_pct"),
        "confidence_pct": raw.get("confidence_pct"),
        "valid_until": raw.get("valid_until"),
    }

    state_saved = False
    write_error: Optional[Exception] = None

    try:
        with process_lock(_LOCK_NAME, timeout=10):
            _append_log(log_path, log_entry)
            if result["ok"]:
                # fail-closed: state を実際に保存できて初めて actionable。
                try:
                    atomic_write_json(state_path, result["normalized"])
                    state_saved = True
                except Exception as _we:
                    write_error = _we
    except Exception as _le:
        # ロック取得失敗等。ベストエフォートで log だけは残す。
        write_error = write_error or _le
        try:
            _append_log(log_path, {**log_entry, "lock_degraded": True})
        except Exception:
            pass

    # valid でも state を保存できなければ採用しない (fail-closed)。
    actionable = bool(result["ok"] and state_saved)
    verdict = result["verdict"]
    reason = result["reason"]
    if result["ok"] and not state_saved:
        verdict = "rejected"
        reason = f"state_write_failed: {write_error}"
        # 監査: 検証は通ったが永続化に失敗したことを log に明示する。
        try:
            _append_log(log_path, {
                **log_entry,
                "actionable": False,
                "verdict": "rejected",
                "reason": reason,
                "state_write_failed": True,
            })
        except Exception:
            pass

    return {
        "actionable": actionable,
        "verdict": verdict,
        "reason": reason,
        "clamped": result["clamped"],
        "normalized": result["normalized"] if actionable else None,
    }


# ============================================================
# 解決 (rebalance / UI 向け)
# ============================================================

def load_state(state_path=DEFAULT_STATE_PATH) -> Optional[dict]:
    """state を読む。欠落/破損/型不正は None (fail-closed の起点)。"""
    try:
        p = Path(state_path)
        if not p.exists():
            return None
        data = json.loads(p.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else None
    except Exception:
        return None


def resolve_effective_targets(*, static: dict, now=None,
                              state_path=DEFAULT_STATE_PATH) -> tuple[dict, dict]:
    """
    rebalance に渡す実効通貨ターゲットを解決する。

    有効な AI 方針 (basis=long_tier, 未期限切れ, 健全) があればそれを
    CURRENCY_TARGETS 形に変換して返す。無ければ static にフォールバック。

    Returns:
        (targets, meta)
        targets: {'USD': {min,max,ideal}, 'JPY': {...}} (= static と同じ形)
        meta:    {"source": "ai_policy"|"static_fallback", "reason": str, ...}
    """
    def _static(reason: str) -> tuple[dict, dict]:
        return static, {"source": "static_fallback", "reason": reason}

    state = load_state(state_path)
    if state is None:
        return _static("AI方針 state が無い/読めない")

    try:
        if state.get("basis") != APPLICABLE_BASIS:
            return _static(f"basis={state.get('basis')!r} は適用対象外")

        usd = _num(state.get("usd_target_pct"))
        jpy = _num(state.get("jpy_target_pct"))
        if usd is None or jpy is None or abs((usd + jpy) - 100.0) > SUM_TOLERANCE_PCT:
            return _static("state の usd/jpy が不正")

        vu = _parse_date(state.get("valid_until"))
        if vu is None:
            return _static("state の valid_until が不正")
        today = _today(now)
        if today > vu:
            return _static(f"AI方針 expired (valid_until={vu}, today={today})")

        targets = _targets_from_pct(usd, jpy)
        meta = {
            "source": "ai_policy",
            "reason": "valid AI currency policy",
            "as_of": state.get("as_of"),
            "valid_until": state.get("valid_until"),
            "confidence_pct": state.get("confidence_pct"),
            "usd_target_pct": int(round(usd)),
            "jpy_target_pct": int(round(jpy)),
            "clamped": bool(state.get("clamped")),
        }
        return targets, meta
    except Exception as e:  # pragma: no cover - 想定外は必ず static へ
        return _static(f"resolve 例外: {e}")
