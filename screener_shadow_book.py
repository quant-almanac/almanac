"""screener_shadow_book.py — screener候補のobserve-onlyフォワードリターン計測。

Swingレーン発見の計測（docs/design_screener_shadow_2026_07.md）。
screener.py（モメンタム/逆張り/ギャップダウン/決算前モメンタム/ボリュームサージ/
イベントドリブンの7戦略）が毎日出す swing 候補を、手動タグ付けに依存せず
observe_only でフォワードリターン計測し、戦略別に効き目を判定する。

設計判断:
  - screen_results*.json は上書きスナップショットで履歴が残らないため、
    毎日 capture して append-only ログ (screener_candidate_log.jsonl) に点in時記録する。
  - 同一 (ticker, strategy) の連日出現は EPISODE_COOLDOWN_DAYS 以内をスキップして
    サンプル独立性を担保（持続モメンタム銘柄の窓重複による n 水増しを防ぐ）。
  - エントリは as_of日翌営業日の始値（look-ahead無し）、5/20営業日で決済。
  - 全 screener 戦略は buy 方向のため long のみ。楽天実コストを差し引く。
  - event_ledger には一切書かない（財務台帳非接触）。自動発注もしない。
  - 価格ロードとコストモデルは disclosure_shadow_book から再利用（重複実装しない）。
"""
from __future__ import annotations

import argparse
import hashlib
import json
from collections import defaultdict
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any, Callable, Optional

import pandas as pd

from disclosure_shadow_book import (
    _load_prices,
    _prepare_prices,
    estimate_round_trip_cost_pct,
    load_config,
)

BASE_DIR = Path(__file__).parent
CANDIDATE_LOG_PATH = BASE_DIR / "data" / "screener_candidate_log.jsonl"
SHADOW_BOOK_PATH = BASE_DIR / "data" / "screener_shadow_book.json"
SCREEN_RESULT_FILES = (
    "screen_results.json",
    "screen_results_morning.json",
    "screen_results_jp.json",
)

HORIZONS = (5, 20)
EPISODE_COOLDOWN_DAYS = 28  # 20営業日 ≈ 28暦日。エピソード窓の重複を避ける。
NOTIONAL_JPY_DEFAULT = 100_000
FX_RATE_DEFAULT = 150.0


# ============================================================
# Capture: screen_results*.json → append-only 点in時ログ
# ============================================================

def _episode_id(ticker: str, strategy: str, as_of_date: str) -> str:
    raw = f"{ticker}|{strategy}|{as_of_date}"
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:16]


def _as_of_date_from_result(data: dict, fallback_path: Optional[Path]) -> str:
    ts = data.get("timestamp") or data.get("generated_at") or data.get("as_of")
    if isinstance(ts, str) and len(ts) >= 10:
        return ts[:10]
    if fallback_path is not None and fallback_path.exists():
        return datetime.fromtimestamp(fallback_path.stat().st_mtime).strftime("%Y-%m-%d")
    return datetime.now().strftime("%Y-%m-%d")


def _read_log(path: Path) -> list[dict]:
    if not path.exists():
        return []
    rows: list[dict] = []
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            rows.append(json.loads(line))
        except json.JSONDecodeError:
            continue  # 1行の破損で全読み取りを壊さない
    return rows


def _append_log(path: Path, rows: list[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as f:
        for row in rows:
            f.write(json.dumps(row, ensure_ascii=False) + "\n")


def _within_cooldown(existing_dates: list[str], as_of_date: str, cooldown_days: int) -> bool:
    """same (ticker, strategy) の既存 as_of が cooldown 以内にあれば True（=新規登録しない）。"""
    try:
        target = date.fromisoformat(as_of_date)
    except ValueError:
        return False
    for d in existing_dates:
        try:
            prior = date.fromisoformat(d)
        except ValueError:
            continue
        if abs((target - prior).days) < cooldown_days:
            return True
    return False


def capture_candidates(
    *,
    as_of: Optional[str] = None,
    result_files: tuple[str, ...] = SCREEN_RESULT_FILES,
    log_path: Path = CANDIDATE_LOG_PATH,
    base_dir: Path = BASE_DIR,
    cooldown_days: int = EPISODE_COOLDOWN_DAYS,
) -> dict:
    """現在の screen_results*.json の候補を点in時ログに追記する。

    idempotent: 同一 episode_id は再登録しない。同一 (ticker, strategy) が
    cooldown_days 以内に既登録なら新規エピソードとして扱わない。
    """
    existing = _read_log(log_path)
    known_ids = {r.get("episode_id") for r in existing}
    dates_by_key: dict[tuple[str, str], list[str]] = defaultdict(list)
    for r in existing:
        dates_by_key[(r.get("ticker"), r.get("strategy"))].append(r.get("as_of_date") or "")

    new_rows: list[dict] = []
    seen_this_run: set[str] = set()
    captured_at = datetime.now(timezone.utc).isoformat()

    for fname in result_files:
        fpath = base_dir / fname
        if not fpath.exists():
            continue
        try:
            data = json.loads(fpath.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            continue
        as_of_date = as_of or _as_of_date_from_result(data, fpath)
        for cand in (data.get("candidates") or []):
            if not isinstance(cand, dict):
                continue
            ticker = str(cand.get("ticker") or "").strip()
            strategy = str(cand.get("strategy") or "").strip()
            if not ticker or not strategy:
                continue
            eid = _episode_id(ticker, strategy, as_of_date)
            if eid in known_ids or eid in seen_this_run:
                continue
            key = (ticker, strategy)
            if _within_cooldown(dates_by_key[key], as_of_date, cooldown_days):
                continue
            market = "JP" if (cand.get("is_japan") or ticker.endswith(".T")) else "US"
            new_rows.append({
                "episode_id": eid,
                "as_of_date": as_of_date,
                "captured_at": captured_at,
                "ticker": ticker,
                "strategy": strategy,
                "market": market,
                "candidate_price": cand.get("price"),
                "composite_score": cand.get("composite_score"),
                "source_file": fname,
            })
            seen_this_run.add(eid)
            dates_by_key[key].append(as_of_date)

    if new_rows:
        _append_log(log_path, new_rows)
    return {"captured": len(new_rows), "log_total": len(existing) + len(new_rows)}


# ============================================================
# Measure: 点in時ログ → フォワードリターン
# ============================================================

def _measure_episode(
    episode: dict,
    prices_df: Any,
    *,
    config: dict,
    fx_rate: float,
) -> Optional[dict]:
    """1エピソードの各 horizon のフォワードリターンを返す。満期未達なら pending 情報付き。"""
    prices = _prepare_prices(prices_df)
    if prices.empty:
        return None
    event_date = pd.Timestamp(str(episode["as_of_date"])[:10])
    eligible = prices[prices.index.normalize() > event_date.normalize()]
    if eligible.empty:
        return {"pending": True, "reason": "no_price_after_as_of"}
    entry_at = eligible.index[0]
    entry_position = prices.index.get_loc(entry_at)
    entry_price = float(prices.loc[entry_at, "Open"])
    if entry_price <= 0:
        return None
    market = episode["market"]
    notional = float(config.get("notional_jpy", NOTIONAL_JPY_DEFAULT))
    results: list[dict] = []
    pending_horizons: list[int] = []
    for horizon in HORIZONS:
        exit_position = entry_position + int(horizon)
        if exit_position >= len(prices):
            pending_horizons.append(horizon)
            continue
        exit_price = float(prices.iloc[exit_position]["Close"])
        gross = (exit_price - entry_price) / entry_price  # long のみ
        cost = estimate_round_trip_cost_pct(
            market=market, notional_jpy=notional, fx_rate=fx_rate,
            config=config, direction=1, horizon_days=int(horizon),
        )
        results.append({
            "horizon_days": int(horizon),
            "entry_at": entry_at.isoformat(),
            "entry_price": entry_price,
            "exit_price": exit_price,
            "gross_return": round(gross, 8),
            "cost_return": round(cost, 8),
            "net_return": round(gross - cost, 8),
        })
    if not results:
        return {"pending": True, "reason": "not_matured", "pending_horizons": pending_horizons}
    return {"pending": False, "returns": results, "pending_horizons": pending_horizons}


def measure(
    *,
    log_path: Path = CANDIDATE_LOG_PATH,
    output_path: Optional[Path] = SHADOW_BOOK_PATH,
    price_loader: Optional[Callable[[set], dict]] = None,
    config: Optional[dict] = None,
    fx_rate: float = FX_RATE_DEFAULT,
) -> dict:
    """点in時ログの全エピソードを計測し、shadow book を生成（observe_only）。"""
    cfg = config or load_config()
    episodes = _read_log(log_path)
    tickers = {e.get("ticker") for e in episodes if e.get("ticker")}
    loader = price_loader or (lambda ts: _load_prices(ts))
    price_data = loader(tickers) if tickers else {}

    measured: list[dict] = []
    pending_count = 0
    missing_price: set[str] = set()

    for ep in episodes:
        ticker = ep.get("ticker")
        if ticker not in price_data:
            missing_price.add(ticker)
            continue
        result = _measure_episode(ep, price_data[ticker], config=cfg, fx_rate=fx_rate)
        if result is None:
            missing_price.add(ticker)
            continue
        if result.get("pending"):
            pending_count += 1
            continue
        for r in result["returns"]:
            measured.append({
                "episode_id": ep.get("episode_id"),
                "ticker": ticker,
                "strategy": ep.get("strategy"),
                "market": ep.get("market"),
                "as_of_date": ep.get("as_of_date"),
                **r,
                "observe_only": True,
            })

    book = {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "status": "observe_only_screener_shadow",
        "episode_count": len(episodes),
        "measured_return_count": len(measured),
        "pending_episode_count": pending_count,
        "missing_price_tickers": sorted(t for t in missing_price if t),
        "returns": measured,
    }
    if output_path is not None:
        output_path.parent.mkdir(parents=True, exist_ok=True)
        tmp = output_path.with_suffix(output_path.suffix + ".tmp")
        tmp.write_text(json.dumps(book, ensure_ascii=False, indent=2), encoding="utf-8")
        tmp.replace(output_path)
    return book


# ============================================================
# Aggregate by strategy
# ============================================================

def aggregate_by_strategy(book: dict) -> dict:
    """戦略×horizon 別に n / hit率 / after-cost平均リターンを集計する。"""
    grouped: dict[tuple[str, int], list[float]] = defaultdict(list)
    for row in book.get("returns", []):
        strategy = row.get("strategy")
        horizon = row.get("horizon_days")
        net = row.get("net_return")
        if strategy is None or horizon is None or net is None:
            continue
        grouped[(strategy, int(horizon))].append(float(net))

    agg: dict[str, dict] = {}
    for (strategy, horizon), nets in grouped.items():
        n = len(nets)
        agg.setdefault(strategy, {})[str(horizon)] = {
            "n": n,
            "hit_rate": round(sum(1 for x in nets if x > 0) / n, 4) if n else None,
            "mean_net_return": round(sum(nets) / n, 6) if n else None,
        }
    return agg


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="screener候補のobserve-onlyフォワードリターン計測")
    sub = parser.add_subparsers(dest="cmd")
    sub.add_parser("capture", help="現在の screen_results*.json を点in時ログに追記")
    sub.add_parser("measure", help="点in時ログを計測して shadow book を生成")
    sub.add_parser("daily", help="capture → measure（cron用）")
    sub.add_parser("report", help="戦略別集計を表示")
    args = parser.parse_args()

    if args.cmd == "capture":
        print(json.dumps(capture_candidates(), ensure_ascii=False, indent=2))
    elif args.cmd == "measure":
        book = measure()
        print(json.dumps({k: book[k] for k in (
            "episode_count", "measured_return_count", "pending_episode_count",
        )}, ensure_ascii=False))
    elif args.cmd == "daily":
        cap = capture_candidates()
        book = measure()
        print(json.dumps({"capture": cap, "measured_return_count": book["measured_return_count"]},
                         ensure_ascii=False))
    elif args.cmd == "report":
        book = json.loads(SHADOW_BOOK_PATH.read_text(encoding="utf-8")) if SHADOW_BOOK_PATH.exists() else {}
        print(json.dumps(aggregate_by_strategy(book), ensure_ascii=False, indent=2))
    else:
        parser.print_help()
