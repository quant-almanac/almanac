"""US 売建可否を「基準ベース近似」で自動生成する(楽天証券 米株信用)。

楽天証券の米株信用 売建対象は「S&P500/NASDAQ100/NYダウ構成 OR 時価総額≥$5B
(かつ月間平均売買代金≥$50M)」の中から当社選定。機械可読な公式リスト/APIは
無いため、index 構成(repo の tickers.json)∪ 時価総額≥$5B(yfinance)で近似し、
data/broker_short_us.json を生成する。

これは近似であり、最終的な売建可否は発注画面が権威(human_execution_only)。
基準外/時価総額不明は fail-closed で除外(出力に載せない=builder で shortable=false)。
HTB(借りにくい)銘柄は実コストが既定 2.0% を超える場合がある。

使い方:
  ./venv/bin/python sync_broker_short_us_index.py            # live(yfinanceで時価総額取得)
  ./venv/bin/python sync_broker_short_us_index.py --no-mktcap  # index 構成のみ(無通信・最速)
"""

from __future__ import annotations

import argparse
import json
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

BASE_DIR = Path(__file__).parent
DEFAULT_BORROW_COST = 0.02       # 楽天 米株信用 貸株料 基本 年率2.0%
MIN_MARKET_CAP_USD = 5e9         # 時価総額 ≥ $5B


def _load_index_members(base_dir: Path) -> set[str]:
    """tickers.json の sp500_major ∪ nasdaq100_extra を index 構成の近似とする。"""
    try:
        d = json.loads((Path(base_dir) / "tickers.json").read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return set()
    members = set(d.get("sp500_major") or []) | set(d.get("nasdaq100_extra") or [])
    return {t for t in members if not str(t).endswith(".T")}


def _scan_tickers(base_dir: Path) -> list[str]:
    try:
        d = json.loads((Path(base_dir) / "tickers.json").read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return []
    return [t for t in (d.get("short_scan_tickers") or []) if not str(t).endswith(".T")]


def classify_us_eligibility(ticker: str, *, market_cap: Optional[float],
                            index_members: set[str]) -> Optional[dict]:
    """1 銘柄の売建可否を基準ベースで判定。基準外は None(fail-closed)。"""
    in_index = ticker in index_members
    big = market_cap is not None and market_cap >= MIN_MARKET_CAP_USD
    if not (in_index or big):
        return None
    return {
        "rakuten": True,
        "sbi": None,
        "borrow_cost_annual_pct": DEFAULT_BORROW_COST,
        "eligibility": "rule_based_index_or_mktcap",
        "basis": "index" if in_index else "market_cap>=5e9",
        "confirm_at_order": True,
    }


def build_broker_us(tickers, *, index_members: set[str],
                    market_caps: Optional[dict] = None) -> dict[str, dict]:
    market_caps = market_caps or {}
    out: dict[str, dict] = {}
    for t in tickers:
        entry = classify_us_eligibility(t, market_cap=market_caps.get(t),
                                        index_members=index_members)
        if entry is not None:
            out[t] = entry
    return out


def _fetch_market_caps(tickers: list[str]) -> dict[str, Optional[float]]:
    """yfinance で時価総額を並列取得(best-effort)。取得不能は None=fail-closed。"""
    import yfinance as yf

    def _one(t: str):
        try:
            return t, yf.Ticker(t).info.get("marketCap")
        except Exception:
            return t, None

    caps: dict[str, Optional[float]] = {}
    with ThreadPoolExecutor(max_workers=10) as ex:
        for fut in as_completed([ex.submit(_one, t) for t in tickers]):
            t, cap = fut.result()
            caps[t] = float(cap) if cap else None
    return caps


def sync(*, base_dir: Path = BASE_DIR, tickers: Optional[list[str]] = None,
         index_members: Optional[set[str]] = None,
         market_caps: Optional[dict] = None, use_mktcap: bool = True,
         now: Optional[datetime] = None) -> dict:
    """売建可否近似を生成し data/broker_short_us.json に書く。"""
    now = now or datetime.now(timezone.utc)
    base_dir = Path(base_dir)
    tickers = tickers if tickers is not None else _scan_tickers(base_dir)
    index_members = index_members if index_members is not None else _load_index_members(base_dir)
    if market_caps is None and use_mktcap:
        # index 外の銘柄だけ時価総額を確認(index 内は無条件で eligible)
        need = [t for t in tickers if t not in index_members]
        market_caps = _fetch_market_caps(need) if need else {}

    entries = build_broker_us(tickers, index_members=index_members, market_caps=market_caps)
    payload = {
        "generated_at": now.isoformat(),
        "source": "rule_based_index_or_mktcap (楽天 米株信用 売建基準の近似)",
        "note": "近似。最終売建可否は発注画面が権威(human_execution_only)。HTBは実コストが2.0%超の場合あり。",
        "tickers": entries,
    }
    data_dir = base_dir / "data"
    data_dir.mkdir(parents=True, exist_ok=True)
    path = data_dir / "broker_short_us.json"
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    tmp.replace(path)
    return {"eligible_count": len(entries), "scanned": len(tickers),
            "index_members": len(index_members)}


def main() -> int:
    parser = argparse.ArgumentParser(description="US 売建可否を基準ベースで近似生成")
    parser.add_argument("--no-mktcap", action="store_true",
                        help="時価総額確認をスキップし index 構成のみで判定(無通信)")
    args = parser.parse_args()
    summary = sync(use_mktcap=not args.no_mktcap)
    print(json.dumps(summary, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
