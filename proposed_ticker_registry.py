"""proposed_ticker_registry.py — AI が名指しした新規銘柄をテクニカル取得対象に残す。

モデルは保有・セクターETF・主要指数・シナリオ playbook・直近スクリーナー候補の
どこにも属さない銘柄を提案できる。朝の再計算はそれを値付けしていないので
execution_readiness は technical_data_missing で必ず止める (それ自体は正しい)。

technical_signals.ensure_technical_coverage が実行内でその行を補完するが、
crontab は平日 08:30 / 12:00 / 17:05 に get_technical_context() を叩き、
compute_technical_state() は technical_state.json を**丸ごと置換**する。
ユニバースに居ない銘柄の行は毎回消えるので、補完だけでは
「朝 ready → 正午 technical_data_missing」という間欠障害になる。

このレジストリは補完に成功した銘柄を
technical_signals.CANDIDATE_UNIVERSE_FILES 経由でユニバースへ戻し、
再計算がネイティブに行を作れるようにする。

設計上の制約が2つある。

1. **ポジティブキャッシュにすること。**
   analyst._ensure_technical_state_fresh は
   `source_health.missing_count == 0` と
   `_build_ticker_universe() ⊆ cached_tickers` の両方を要求する。解決できない
   銘柄をユニバースへ登録すると両条件が恒久的に false になり、分析のたびに
   全銘柄の強制再計算がエラーも警告も無く走り続ける。よって
   「実際にテクニカル行を生成できた銘柄」だけを登録し (record の resolved)、
   再計算で落ちた銘柄は evict_unresolved で追い出す。

2. **last_seen の新しい順に並べること。**
   _build_ticker_universe は rows[:CANDIDATE_TICKERS_PER_FILE] で切る。
   古い順のままだと最新の提案が黙って落ちる — screen_results_us.json 事件
   (508e948) と同じ「静かな取りこぼし」になる。MAX_ENTRIES を消費側の
   スライスと一致させ、切り捨てが不可視にならないようにする。
"""

from __future__ import annotations

from datetime import date, datetime, timedelta
from pathlib import Path

from utils import atomic_write_json, load_json

# _build_ticker_universe が既に解釈できる形 (candidates キー・ticker フィールド)
# で書く。ユニバース解析コードを新規に書かないため。
REGISTRY_FILENAME = "proposed_ticker_candidates.json"

# technical_signals.CANDIDATE_TICKERS_PER_FILE 以下に保つこと。消費側の
# スライスより大きいと、超過分が読まれないまま黙って捨てられる。
MAX_ENTRIES = 30
TTL_DAYS = 21

# yfinance の全面障害で「今日は誰も解決できなかった」だけの状態と、
# 個別銘柄が本当に上場廃止された状態を区別できない。再計算のカバレッジが
# この閾値を割った日は追い出しごとスキップし、レジストリを守る。
MIN_REBUILD_COVERAGE_FOR_EVICTION = 0.5

SOURCE_LABEL = "ai_proposal"


def _today(now: datetime | None) -> date:
    return (now or datetime.now()).date()


def _as_date(value: object) -> date | None:
    try:
        return date.fromisoformat(str(value)[:10])
    except Exception:
        return None


def _read_rows(base_dir: Path) -> list[dict]:
    payload = load_json(Path(base_dir) / REGISTRY_FILENAME, {})
    rows = payload.get("candidates") if isinstance(payload, dict) else payload
    if not isinstance(rows, list):
        return []
    out: list[dict] = []
    for row in rows:
        if not isinstance(row, dict):
            continue
        ticker = str(row.get("ticker") or "").strip().upper()
        if not ticker:
            continue
        out.append({
            "ticker": ticker,
            "first_seen": str(row.get("first_seen") or ""),
            "last_seen": str(row.get("last_seen") or ""),
            "seen_count": int(row.get("seen_count") or 0),
            "source": str(row.get("source") or SOURCE_LABEL),
        })
    return out


def _prune_and_order(rows: list[dict], *, today: date) -> list[dict]:
    """TTL を切ってから新しい順に並べ、消費側スライスと同じ幅に収める。"""
    cutoff = today - timedelta(days=TTL_DAYS)
    alive = []
    for row in rows:
        seen = _as_date(row.get("last_seen"))
        # last_seen を読めない行は破棄する。並び順を決められない行を残すと
        # 消費側スライスの切り捨てが不可視になる。
        if seen is None or seen < cutoff:
            continue
        alive.append(row)
    alive.sort(
        key=lambda r: (str(r.get("last_seen") or ""), int(r.get("seen_count") or 0), r["ticker"]),
        reverse=True,
    )
    return alive[:MAX_ENTRIES]


def _write(base_dir: Path, rows: list[dict], *, now: datetime | None) -> None:
    atomic_write_json(Path(base_dir) / REGISTRY_FILENAME, {
        "version": 1,
        "generated_at": (now or datetime.now()).isoformat(),
        "candidates": rows,
    })


def load_registered(base_dir: Path) -> dict[str, dict]:
    """ticker -> エントリ。順序は last_seen の新しい順。"""
    return {row["ticker"]: row for row in _read_rows(base_dir)}


def record(
    proposed,
    *,
    resolved: set[str],
    base_dir: Path,
    now: datetime | None = None,
) -> dict:
    """補完に成功した銘柄だけを登録する。例外は投げない。

    proposed は監査のための全要求。実際に登録されるのは resolved との積集合
    だけで、これはポジティブキャッシュ制約 (モジュール docstring 1.) の
    実装そのもの。解決できなかった銘柄を入れると強制再計算が恒久化する。
    """
    try:
        today = _today(now)
        stamp = today.isoformat()
        resolved_upper = {str(t or "").strip().upper() for t in (resolved or set())}
        resolved_upper.discard("")

        requested = []
        for value in (proposed or []):
            ticker = str(value or "").strip().upper()
            if ticker and ticker not in requested:
                requested.append(ticker)

        additions = [t for t in requested if t in resolved_upper]
        rows = {row["ticker"]: row for row in _read_rows(base_dir)}
        for ticker in additions:
            row = rows.get(ticker)
            if row is None:
                rows[ticker] = {
                    "ticker": ticker,
                    "first_seen": stamp,
                    "last_seen": stamp,
                    "seen_count": 1,
                    "source": SOURCE_LABEL,
                }
            else:
                row["last_seen"] = stamp
                row["seen_count"] = int(row.get("seen_count") or 0) + 1
                if not row.get("first_seen"):
                    row["first_seen"] = stamp

        ordered = _prune_and_order(list(rows.values()), today=today)
        if not additions and len(ordered) == len(rows):
            # 変化なし。mtime を動かさない。
            return {"status": "noop", "requested": requested, "registered": []}
        # 追加が無くても TTL 切れは書き戻して落とす。_build_ticker_universe は
        # このファイルを直接読むので、追加のあった日にしか prune しないと、
        # 二度と提案されない銘柄が期限を過ぎてもユニバースに残り続け、
        # 毎回の再計算で無駄にダウンロードされる。
        _write(base_dir, ordered, now=now)
        kept = {row["ticker"] for row in ordered}
        return {
            "status": "ok",
            "requested": requested,
            "registered": [t for t in additions if t in kept],
            "dropped": [t for t in additions if t not in kept],
            "entries": len(ordered),
        }
    except Exception as exc:  # レジストリは補助。失敗しても分析を止めない。
        return {"status": "error", "error": f"{type(exc).__name__}: {exc}"}


def evict_unresolved(
    missing,
    *,
    base_dir: Path,
    rebuild_coverage: float,
    now: datetime | None = None,
) -> dict:
    """再計算が行を作れなかった登録銘柄を追い出す。例外は投げない。

    上場廃止・改称された銘柄をユニバースに残し続けると
    _ensure_technical_state_fresh の universe_is_complete が恒久的に false に
    なり、毎回の強制再計算が無警告で走る。最悪ケースは「強制再計算が2回
    余分に走ってから自己修復」で、それ以上悪化しない。
    """
    try:
        coverage = float(rebuild_coverage)
    except (TypeError, ValueError):
        coverage = 0.0
    if coverage < MIN_REBUILD_COVERAGE_FOR_EVICTION:
        # 全面障害の日に追い出すと、翌日には復活する銘柄まで消える。
        return {"status": "skipped_low_coverage", "rebuild_coverage": coverage, "evicted": []}

    try:
        today = _today(now)
        drop = {str(t or "").strip().upper() for t in (missing or [])}
        drop.discard("")
        rows = _read_rows(base_dir)
        if not rows:
            return {"status": "noop", "evicted": []}

        survivors = [row for row in rows if row["ticker"] not in drop]
        evicted = sorted({row["ticker"] for row in rows} & drop)
        ordered = _prune_and_order(survivors, today=today)
        if len(ordered) == len(rows) and not evicted:
            # TTL でも落ちていないなら書かない (mtime を動かさない)。
            return {"status": "noop", "evicted": []}
        _write(base_dir, ordered, now=now)
        return {"status": "ok", "evicted": evicted, "entries": len(ordered)}
    except Exception as exc:
        return {"status": "error", "error": f"{type(exc).__name__}: {exc}"}
