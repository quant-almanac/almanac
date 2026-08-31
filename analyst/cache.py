"""
キャッシュ管理モジュール
- キャッシュ有効性チェック（VIX/ガードレール/ドローダウン考慮）
- 分析結果の保存・履歴管理
- 進捗ファイル書き込み
"""
import json
import re
import sys
from datetime import datetime, timedelta
from pathlib import Path

BASE_DIR      = Path(__file__).parent.parent
CACHE_PATH    = BASE_DIR / "ai_portfolio_analysis.json"
HISTORY_PATH  = BASE_DIR / "ai_analysis_history.json"
PROGRESS_PATH = BASE_DIR / "analysis_progress.json"
CACHE_HOURS   = 6
HISTORY_MAX   = 5

_HISTORY_STANCES = {
    "defensive",
    "neutral",
    "moderately_aggressive",
    "aggressive",
}
_HISTORY_ACTION_TYPES = {
    "buy", "add", "margin_buy", "sell", "rebalance", "short", "cover",
    "dca", "trim", "reduce", "stop_loss", "take_profit",
}
_HISTORY_TICKER_RE = re.compile(r"^[A-Z0-9][A-Z0-9._-]{0,19}$")

# utils はルートに存在
sys.path.insert(0, str(BASE_DIR))
from utils import atomic_write_json, process_lock


def write_progress(step: int, total: int, label: str, detail: str = "") -> None:
    """各ステップの進捗を analysis_progress.json にアトミックに書き込む"""
    try:
        data = {
            "step": step, "total": total,
            "label": label, "detail": detail,
            "pct": round(step / total * 100),
            "updated_at": datetime.now().isoformat(),
        }
        atomic_write_json(PROGRESS_PATH, data)
    except Exception:
        pass


def load_json(path: Path, default=None):
    try:
        if path.exists():
            with open(path, encoding="utf-8") as f:
                return json.load(f)
    except Exception:
        pass
    return default if default is not None else {}


def save_cache(data: dict) -> None:
    """分析結果をキャッシュに保存し、履歴サマリーを追記する"""
    with process_lock("ai_analysis_cache", timeout=30.0):
        hist = load_json(HISTORY_PATH, {"history": []})
        # 後方互換: 旧形式 (list) も dict も両方受け入れる。
        if isinstance(hist, list):
            records = hist
            hist = {"history": records}
        elif isinstance(hist, dict):
            records = hist.get("history", [])
        else:
            records = []
            hist = {"history": records}
        s = data.get("synthesis", {})
        records.append({
            "as_of": data.get("as_of", ""),
            "overall_stance": s.get("overall_stance", ""),
            "stance_reason": s.get("stance_reason", ""),
            "weekly_theme": s.get("weekly_theme", ""),
            "priority_actions": [
                {"ticker": a.get("ticker"), "type": a.get("type"), "action": a.get("action", "")[:80]}
                for a in s.get("priority_actions", [])[:5]
            ],
            "risk_warnings": s.get("risk_warnings", [])[:3],
            "geopolitical_note": s.get("geopolitical_note", ""),
        })
        if len(records) > HISTORY_MAX:
            records = records[-HISTORY_MAX:]
        hist["history"] = records
        # The formal cache is the investment authority.  Commit it before the
        # derived history so a history write can never advertise a generation
        # that the authoritative cache failed to publish.  If history fails it
        # may lag conservatively; the reverse ordering exposed a new analysis
        # through history while execution still used the old cache.
        atomic_write_json(CACHE_PATH, data)
        atomic_write_json(HISTORY_PATH, hist)


def load_history_context() -> str:
    """過去の分析履歴を構造化された方向情報だけにして返す。

    ``stance_reason``、``risk_warnings``、action自由文は過去のLLM出力で、
    現在の構造化データから再検証できない。これらを次回プロンプトへ戻すと、
    誤った金融数値やmetric意味が自己強化されるため、監査artifactには保持しても
    分析入力には使わない。
    """
    hist = load_json(HISTORY_PATH, {"history": []})
    # 後方互換: list/dict 両対応
    if isinstance(hist, list):
        records = hist
    elif isinstance(hist, dict):
        records = hist.get("history", [])
    else:
        records = []
    if not records:
        return "（過去の分析履歴なし — 初回分析）"
    lines = [
        "※ 過去履歴は構造化された方向情報のみ。過去の自由文・金融数値は再利用せず、"
        "本日の構造化入力から再計算すること。"
    ]
    for r in records:
        if not isinstance(r, dict):
            continue
        stance = str(r.get("overall_stance") or "").strip().lower()
        if stance not in _HISTORY_STANCES:
            stance = "unknown"
        actions = []
        for action in (r.get("priority_actions") or [])[:3]:
            if not isinstance(action, dict):
                continue
            ticker = str(action.get("ticker") or "").strip().upper()
            action_type = str(action.get("type") or "").strip().lower()
            if (
                _HISTORY_TICKER_RE.fullmatch(ticker)
                and action_type in _HISTORY_ACTION_TYPES
            ):
                actions.append(f"{ticker}:{action_type}")
        as_of = str(r.get("as_of") or "unknown")[:32]
        lines.append(
            f"[{as_of}] stance={stance} / actions={','.join(actions) or 'none'}"
        )
    return "\n---\n".join(lines)


def is_cache_valid(hours: int = CACHE_HOURS) -> bool:
    """
    キャッシュ有効性チェック。以下の条件で強制無効化:
      - VIX >= 30（高恐怖）→ キャッシュ有効期間を 2h に短縮
      - ガードレール発動中（新規禁止 or 取引停止）→ 即無効
    """
    data = load_json(CACHE_PATH)
    as_of = data.get("as_of", "")
    if not as_of:
        return False
    try:
        ts = datetime.strptime(as_of, "%Y-%m-%d %H:%M")
        age = datetime.now() - ts

        guard = load_json(BASE_DIR / "guard_state.json")
        if not guard.get("new_entry_allowed", True) or not guard.get("trading_allowed", True):
            print("⚠️  ガードレール発動中 → キャッシュ無効")
            return False

        try:
            import yfinance as _yf
            vix_raw = float(_yf.Ticker("^VIX").fast_info['lastPrice'])
        except Exception:
            vix_raw = None
        if vix_raw and vix_raw >= 30:
            effective_hours = 2
            if age >= timedelta(hours=effective_hours):
                print(f"⚠️  VIX={vix_raw:.1f}(高恐怖) → キャッシュ有効期間 2h に短縮")
                return False

        return age < timedelta(hours=hours)
    except Exception:
        return False


def get_cached() -> dict:
    """キャッシュされた分析を返す（なければ空dict）"""
    return load_json(CACHE_PATH, {})
