"""
ALMANAC Phase 0.5 — TDnet 適時開示フェッチャー（disclosure feature pipeline 用）

TDnet には公式の公開 JSON API が無いため、無料の **yanoshin TDnet WebAPI**(JSON)を
経由して適時開示メタデータを取得し、正規化アイテムに変換する（best-effort）。

- ``normalize_tdnet_items()`` は pure（fixture でテスト可能・ネットワーク非依存）。
- ``fetch_tdnet_items()`` はネットワークを **gate** する（live=True のときのみ）。
- list API は title のみ（本文なし）。本文エンリッチは後続課題。
"""

from datetime import datetime
from typing import Optional

__all__ = ["normalize_tdnet_items", "fetch_tdnet_items"]

_YANOSHIN_URL = "https://webapi.yanoshin.jp/webapi/tdnet/list/{q}.json"

# TDnet タイトルキーワード → disclosure_type（順に優先）
_TITLE_TO_TYPE = (
    ("業績予想", "guidance"), ("予想の修正", "guidance"), ("配当予想", "guidance"),
    ("月次", "monthly_sales"),
    ("決算短信", "earnings"), ("四半期", "earnings"),
)


def _classify(title: str) -> str:
    if ("自己株式" in title or "自社株" in title) and "処分" not in title:
        # 自己株式の「処分」(処分=放出=希薄化方向) は取得(買い戻し)と逆方向のため
        # buyback には分類しない。以降の他キーワードにもマッチしなければ other に落ちる。
        return "buyback"
    for kw, t in _TITLE_TO_TYPE:
        if kw in title:
            return t
    return "other"


def _ticker_from_code(code: str) -> Optional[str]:
    """TDnet company_code（5桁: 4桁証券コード+0）→ ``"7203.T"``。"""
    digits = "".join(ch for ch in str(code or "") if ch.isdigit())
    if len(digits) < 4:
        return None
    return f"{digits[:4]}.T"


def _to_iso_jst(pubdate: str) -> Optional[str]:
    """``"2026-06-04 15:00:00"`` → ``"2026-06-04T15:00:00+09:00"`` (TDnet は JST)。"""
    if not pubdate:
        return None
    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M", "%Y/%m/%d %H:%M:%S"):
        try:
            return datetime.strptime(pubdate.strip(), fmt).strftime("%Y-%m-%dT%H:%M:%S+09:00")
        except ValueError:
            continue
    try:
        return datetime.strptime(pubdate.strip()[:10], "%Y-%m-%d").strftime("%Y-%m-%dT00:00:00+09:00")
    except ValueError:
        return None


def normalize_tdnet_items(payload: dict, *, limit: int = 300) -> list[dict]:
    """Convert a yanoshin TDnet ``list`` payload to disclosure items (pure).

    The payload shape is ``{"items": [{"Tdnet": {...}}, ...]}``. Only entries with
    a 4-digit ``company_code`` (listed names) and a stable ``id`` + ``pubdate`` are
    emitted; funds / malformed rows are skipped.
    """
    items_raw = (payload or {}).get("items") or []
    out: list[dict] = []
    for wrap in items_raw:
        t = wrap.get("Tdnet") if isinstance(wrap, dict) else None
        if not isinstance(t, dict):
            continue
        ticker = _ticker_from_code(t.get("company_code"))
        if not ticker:
            continue
        doc_id = t.get("id")
        if not doc_id:
            continue
        publish_time = _to_iso_jst(t.get("pubdate") or "")
        if not publish_time:
            continue
        title = (t.get("title") or "").strip()
        out.append({
            "source": "tdnet",
            "ticker": ticker,
            "native_doc_id": str(doc_id),
            "source_url": t.get("document_url") or f"https://www.release.tdnet.info/{doc_id}",
            "publish_time": publish_time,
            "market": "JP",
            "language": "ja",
            "disclosure_type": _classify(title),
            "title": title or str(doc_id),
            "body": title or str(doc_id),
            "ticker_resolution_method": "sec_code",
            "ticker_resolution_confidence": 1.0,
        })
        if len(out) >= limit:
            break
    return out


def fetch_tdnet_items(query: str = "today", *, live: bool = False,
                      limit: int = 300) -> list[dict]:
    """Fetch a day's TDnet disclosures as items. Network is **gated**.

    Returns ``[]`` unless ``live=True``. ``query`` is a yanoshin path segment
    (``"today"``, ``"yesterday"``, or ``"YYYYMMDD"``). Best-effort third-party
    proxy — failures degrade to an empty list.
    """
    if not live:
        return []
    import requests
    import time as _time
    last_err: Exception | None = None
    for attempt, timeout in enumerate((30, 45), start=1):
        try:
            r = requests.get(_YANOSHIN_URL.format(q=query), timeout=timeout)
            r.raise_for_status()
            return normalize_tdnet_items(r.json(), limit=limit)
        except Exception as e:  # noqa: BLE001
            last_err = e
            print(f"[tdnet] {query} 取得失敗 (attempt {attempt}): {type(e).__name__}: {e}")
            if attempt == 1:
                _time.sleep(3)
    print(f"[tdnet] {query} 取得失敗（リトライ後も失敗）: {type(last_err).__name__}: {last_err}")
    return []
