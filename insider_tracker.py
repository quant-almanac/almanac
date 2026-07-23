"""
insider_tracker.py (Part E-1)
==============================

OpenInsider (openinsider.com) の Latest Cluster Buys ページを HTML スクレイピングし、
「14 日以内に 3 人以上の Directors/Officers が $100K+ を買った cluster buy」と
「Latest Insider Purchases $100K+ ページに載った CEO 単独 $500K+ 買い」を抽出する。

- 出力: insider_cluster_signals.json  (TTL 24h, 後段は format_for_prompt)
- Opus 合成 (analyst/__init__.py) に insider_context として注入される

openinsider は 2025 年以降 .rss エンドポイントを廃止し同 URL で HTML を返す仕様に
なっているため、本実装は `<table class="tinytable">` の <tbody> 行を正規表現で抽出する。
"""
from __future__ import annotations

import json
import re
import sys
import time
import urllib.request
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

BASE_DIR = Path(__file__).parent
OUTPUT   = BASE_DIR / "insider_cluster_signals.json"

CLUSTER_URL   = "http://openinsider.com/latest-cluster-buys"
BIG_BUY_URL   = "http://openinsider.com/latest-insider-purchases-25k"  # $25K+ purchases

WINDOW_DAYS           = 14
CLUSTER_MIN_INSIDERS  = 3
CLUSTER_MIN_VALUE_USD = 100_000
CEO_SOLO_MIN_USD      = 500_000
HTTP_TIMEOUT          = 15
UA                    = {"User-Agent": "Mozilla/5.0 (ALMANAC insider tracker)"}


def _fetch_html(url: str) -> str:
    req = urllib.request.Request(url, headers=UA)
    with urllib.request.urlopen(req, timeout=HTTP_TIMEOUT) as r:
        return r.read().decode("utf-8", errors="ignore")


# ---------------------------------------------------------------------------
# Cluster Buys: 1 行 = 1 銘柄集計（columns: filing_dt, trade_dt, ticker,
#   company, industry, #insiders, trade_type, price, qty, owned, d%, value)
# ---------------------------------------------------------------------------
_ROW_RE = re.compile(r"<tr[^>]*>(.*?)</tr>", re.DOTALL)
_TD_RE  = re.compile(r"<td[^>]*>(.*?)</td>", re.DOTALL)
_TAG_RE = re.compile(r"<[^>]+>")
_TK_RE  = re.compile(r'href="/([A-Z][A-Z0-9.\-]{0,5})"')


def _strip_tags(s: str) -> str:
    return _TAG_RE.sub("", s).strip()


def _extract_ticker(cell: str) -> str:
    """<td> 内の最後の <a href="/TICKER"> からティッカーを拾う。"""
    m = _TK_RE.search(cell or "")
    return m.group(1).upper() if m else ""


def _parse_table(html: str) -> list[list[str]]:
    """各セルの raw HTML を返す（後段で _strip_tags / _extract_ticker を選択）。"""
    idx = html.find('class="tinytable"')
    if idx < 0:
        return []
    tbody_start = html.find("<tbody>", idx)
    tbody_end   = html.find("</tbody>", tbody_start)
    if tbody_start < 0 or tbody_end < 0:
        return []
    body = html[tbody_start:tbody_end]
    rows: list[list[str]] = []
    for m in _ROW_RE.finditer(body):
        tds = _TD_RE.findall(m.group(1))
        if tds:
            rows.append(tds)
    return rows


def _parse_value(s: str) -> int | None:
    m = re.search(r"\$([\d,]+)", s or "")
    if not m:
        return None
    try:
        return int(m.group(1).replace(",", ""))
    except Exception:
        return None


def _parse_date(s: str) -> datetime | None:
    # "2026-04-17 16:03:18" or "2026-04-15"
    s = (s or "").strip()
    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d"):
        try:
            return datetime.strptime(s, fmt).replace(tzinfo=timezone.utc)
        except Exception:
            continue
    return None


def _fetch_clusters() -> list[dict]:
    try:
        html = _fetch_html(CLUSTER_URL)
    except Exception as e:
        print(f"[insider] cluster fetch failed: {e}", file=sys.stderr)
        return []

    rows = _parse_table(html)
    cutoff = datetime.now(timezone.utc) - timedelta(days=WINDOW_DAYS)
    out: list[dict] = []
    for r in rows:
        if len(r) < 13:
            continue
        filing_dt = _parse_date(_strip_tags(r[1]))
        trade_dt  = _parse_date(_strip_tags(r[2]))
        ticker    = _extract_ticker(r[3])
        company   = _strip_tags(r[4])
        industry  = _strip_tags(r[5])
        try:
            insiders = int(re.sub(r"\D", "", _strip_tags(r[6]) or "0") or 0)
        except Exception:
            insiders = 0
        value     = _parse_value(_strip_tags(r[12])) or 0
        dt_ref    = filing_dt or trade_dt
        if not ticker or not dt_ref:
            continue
        if dt_ref < cutoff:
            continue
        if insiders < CLUSTER_MIN_INSIDERS or value < CLUSTER_MIN_VALUE_USD:
            continue
        out.append({
            "ticker":        ticker,
            "company":       company,
            "industry":      industry,
            "insider_count": insiders,
            "total_usd":     value,
            "filing_date":   dt_ref.strftime("%Y-%m-%d"),
            "signal_type":   "cluster_buy",
            "reason": (
                f"{insiders} insiders bought $100K+ (total ${value:,}) "
                f"in past {WINDOW_DAYS}d — {industry}"
            ),
        })
    return out


# ---------------------------------------------------------------------------
# CEO solo: Latest Insider Purchases $25K+ ページから role CEO かつ
# value >= $500K の行を拾う。このページの role 列は index 7 付近。
# ---------------------------------------------------------------------------
def _fetch_ceo_solos() -> list[dict]:
    try:
        html = _fetch_html(BIG_BUY_URL)
    except Exception as e:
        print(f"[insider] big-buy fetch failed: {e}", file=sys.stderr)
        return []

    rows = _parse_table(html)
    cutoff = datetime.now(timezone.utc) - timedelta(days=WINDOW_DAYS)
    out: list[dict] = []
    # Latest Insider Purchases schema:
    # 0 X, 1 FilingDate, 2 TradeDate, 3 Ticker, 4 Company, 5 Industry,
    # 6 InsiderName, 7 Title, 8 TradeType, 9 Price, 10 Qty, 11 Owned,
    # 12 dOwn, 13 Value
    for r in rows:
        if len(r) < 14:
            continue
        filing_dt = _parse_date(_strip_tags(r[1])) or _parse_date(_strip_tags(r[2]))
        if not filing_dt or filing_dt < cutoff:
            continue
        ticker   = _extract_ticker(r[3])
        company  = _strip_tags(r[4])
        industry = _strip_tags(r[5])
        insider  = _strip_tags(r[6])
        title    = _strip_tags(r[7]).upper()
        value    = _parse_value(_strip_tags(r[13])) or 0
        is_ceo   = any(k in title for k in ("CEO", "CHIEF EXECUTIVE", "PRES"))
        if not (ticker and is_ceo and value >= CEO_SOLO_MIN_USD):
            continue
        out.append({
            "ticker":        ticker,
            "company":       company,
            "industry":      industry,
            "insider_count": 1,
            "total_usd":     value,
            "filing_date":   filing_dt.strftime("%Y-%m-%d"),
            "signal_type":   "ceo_solo",
            "reason":        f"CEO {insider} solo buy ${value:,} (Title: {title})",
        })
    return out


def scan(dry_run: bool = False) -> dict:
    clusters = _fetch_clusters()
    ceo_solos = _fetch_ceo_solos()

    # 重複排除: cluster で既出 ticker の CEO solo は捨てる
    cluster_tks = {c["ticker"] for c in clusters}
    ceo_solos = [c for c in ceo_solos if c["ticker"] not in cluster_tks]

    all_signals = clusters + ceo_solos
    all_signals.sort(key=lambda x: x["total_usd"], reverse=True)

    out = {
        "generated_at":   time.strftime("%Y-%m-%d %H:%M:%S"),
        "window_days":    WINDOW_DAYS,
        "cluster_count":  len(clusters),
        "ceo_solo_count": len(ceo_solos),
        "clusters":       all_signals,
    }
    if not dry_run:
        OUTPUT.write_text(json.dumps(out, indent=2, ensure_ascii=False), encoding="utf-8")
        print(f"[insider] wrote {OUTPUT.name}: {len(clusters)} clusters + {len(ceo_solos)} CEO solos")
    return out


def format_for_prompt(max_entries: int = 10) -> str:
    if not OUTPUT.exists():
        return ""
    try:
        if time.time() - OUTPUT.stat().st_mtime > 24 * 3600:
            return ""
        data = json.loads(OUTPUT.read_text(encoding="utf-8"))
    except Exception:
        return ""
    signals = data.get("clusters", [])[:max_entries]
    if not signals:
        return ""
    lines = ["## 🕵️ Insider Buying Clusters (openinsider, past 14d)", ""]
    for c in signals:
        lines.append(
            f"- **{c.get('ticker')}** [{c.get('signal_type')}] "
            f"insiders={c.get('insider_count')} total=${c.get('total_usd'):,} — "
            f"{c.get('reason')}"
        )
    lines.append("")
    lines.append(
        "→ 機関投資家が見落としがちな内部者買い集中。Long/Medium ティア保有銘柄と合致すれば"
        "追加検討、未保有ならば新規 Long 候補として screening_context と同格に扱うこと。"
    )
    return "\n".join(lines)


if __name__ == "__main__":
    dry = "--dry-run" in sys.argv
    out = scan(dry_run=dry)
    if dry:
        print(json.dumps(out, indent=2, ensure_ascii=False))
