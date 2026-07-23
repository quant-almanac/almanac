"""config_clean_baseline.py — 信頼できる NAV 履歴の起点 (CLEAN_NAV_SINCE)

バグ修正前 (〜2026-04-17 の P0/P1 audit, 〜2026-05-25 の stabilization) の汚染 NAV を
測定・意思決定から除外するための単一基準。全測定レイヤが本モジュールを参照する:

  - nav_recorder.modified_dietz_twr        : TWR / excess α の window クランプ
  - benchmark_tracker.get_benchmark_twr     : benchmark 側の整合
  - analyst.data_gatherer (VaR/CVaR/DD)     : fallback 経路の clean-gate
  - analyst.__init__ (stance / α 注入)       : 汚染メトリクスを stance 根拠から除外

背景: daily_performance テーブルの 2026-02〜04 の NAV は cost_jpy の /10000 誤適用・
FX 150 固定・通貨 USD 固定などのバグ修正前の値で、CVaR のテールも TWR の起点も汚染
していた。クリーン履歴が十分貯まるまでは「データ不足」として正直に縮退する。
"""
from __future__ import annotations

from datetime import date

from almanac.runtime_config import get_env

# バグ修正が出揃った HEAD commit 日。これ以前の NAV は信頼しない。
DEFAULT_CLEAN_NAV_SINCE = "2026-05-25"

# クリーン履歴がこの営業日数に満たない間は TWR/excess α/CVaR を「未確定」として縮退する。
DEFAULT_MIN_CLEAN_DAYS = 20


def clean_nav_since() -> date:
    """信頼できる NAV の起点日 (env ALMANAC_CLEAN_NAV_SINCE で上書き可)。"""
    raw = (get_env("ALMANAC_CLEAN_NAV_SINCE", DEFAULT_CLEAN_NAV_SINCE) or DEFAULT_CLEAN_NAV_SINCE).strip()
    try:
        return date.fromisoformat(raw)
    except ValueError:
        return date.fromisoformat(DEFAULT_CLEAN_NAV_SINCE)


def clean_nav_since_iso() -> str:
    """clean_nav_since() を ISO 文字列で返す (SQL バインド等に便利)。"""
    return clean_nav_since().isoformat()


def min_clean_days() -> int:
    """TWR/CVaR を確定値として扱うのに必要な最小クリーン営業日数 (env で上書き可)。"""
    raw = get_env("ALMANAC_MIN_CLEAN_DAYS")
    if raw is None or not str(raw).strip():
        return DEFAULT_MIN_CLEAN_DAYS
    try:
        return int(str(raw).strip())
    except ValueError:
        return DEFAULT_MIN_CLEAN_DAYS


def clamp_date_from(date_from_iso: str) -> str:
    """date_from を clean_nav_since 以降にクランプして返す (ISO 文字列)。"""
    floor = clean_nav_since()
    try:
        d = date.fromisoformat((date_from_iso or "")[:10])
    except ValueError:
        return floor.isoformat()
    return max(d, floor).isoformat()


if __name__ == "__main__":
    import json
    print(json.dumps({
        "clean_nav_since": clean_nav_since_iso(),
        "min_clean_days": min_clean_days(),
        "env_override_active": bool(get_env("ALMANAC_CLEAN_NAV_SINCE")),
    }, ensure_ascii=False, indent=2))
