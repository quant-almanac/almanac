"""
ALMANAC Phase 0 — EDINET 開示イベントフェッチャー（disclosure feature pipeline 用）

金融庁 EDINET の公開 API（v2）から、上場企業の開示書類メタデータをイベントとして取得し、
``disclosure_feature_extractor.extract_features`` が消費する正規化アイテムに変換する。

エンドポイント:
  https://api.edinet-fsa.go.jp/api/v2/documents.json?date=YYYY-MM-DD&type=2
  （v2 は Subscription-Key 必須。EDINET_API_KEY 環境変数から読む）

設計:
  - ``normalize_edinet_documents()`` は pure（fixture でテスト可能・ネットワーク非依存）。
  - ``fetch_edinet_documents()`` はネットワークを **gate** する（live=True のときだけ実通信）。
  - 本文は取得しない（書類取得 API は別呼び出し・重い）。Phase 0 は docDescription を
    body に使い、本文エンリッチは後続課題とする。
"""

import os
import csv
import io
import json
import re
from datetime import datetime
from pathlib import Path
from typing import Callable, Optional

import requests

__all__ = [
    "normalize_edinet_documents",
    "fetch_edinet_documents",
    "load_edinet_code_map",
]

# EDINET docTypeCode → disclosure_type（almanac.observability.disclosure_features の語彙）
_DOCTYPE_TO_TYPE = {
    "120": "earnings", "130": "earnings",   # 有価証券報告書 / 訂正
    "140": "earnings", "150": "earnings",   # 四半期報告書 / 訂正
    "160": "earnings", "170": "earnings",   # 半期報告書 / 訂正
    "180": "other", "190": "other",          # 臨時報告書 / 訂正
    "350": "stake", "360": "stake",          # 大量保有報告書 / 変更
}
_EDINET_URL = "https://api.edinet-fsa.go.jp/api/v2/documents.json"
_ACTIVIST_PATH = Path(__file__).with_name("activist_filers_jp.json")
_STAKE_DOCTYPES = {"350", "360"}

# 大量保有報告書の「対象企業」は提出者(ファンド)の secCode では引けない。EDINET の公式
# コードリスト(Edinetcode.zip)で EDINETコード→証券コードを引いて target を解決する。
_EDINET_CODELIST_URL = (
    "https://disclosure2dl.edinet-fsa.go.jp/searchdocument/codelist/Edinetcode.zip"
)
_CODE_MAP_CACHE = Path(__file__).parent / "data" / "edinet_code_map.json"
_CODE_MAP_TTL_DAYS = 30


def _to_iso_jst(submit: str) -> Optional[str]:
    """``"2026-06-01 09:00"`` → ``"2026-06-01T09:00:00+09:00"`` (EDINET は JST)。"""
    if not submit:
        return None
    for fmt in ("%Y-%m-%d %H:%M", "%Y-%m-%d %H:%M:%S"):
        try:
            dt = datetime.strptime(submit.strip(), fmt)
            return dt.strftime("%Y-%m-%dT%H:%M:%S+09:00")
        except ValueError:
            continue
    # 日付だけでも拾う
    try:
        dt = datetime.strptime(submit.strip()[:10], "%Y-%m-%d")
        return dt.strftime("%Y-%m-%dT00:00:00+09:00")
    except ValueError:
        return None


def _ticker_from_sec_code(sec_code: str) -> Optional[str]:
    """EDINET secCode（5桁: 4桁証券コード+チェック）→ ``"7203.T"``。"""
    if not sec_code:
        return None
    digits = "".join(ch for ch in str(sec_code) if ch.isdigit())
    if len(digits) < 4:
        return None
    return f"{digits[:4]}.T"


def _load_activist_names(path: Path = _ACTIVIST_PATH) -> list[str]:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return []
    names = data.get("names", []) if isinstance(data, dict) else data
    return [str(name).strip().lower() for name in names if str(name).strip()]


def _target_ticker(
    r: dict, *, target_map: Optional[dict[str, str]] = None
) -> tuple[Optional[str], float]:
    """Resolve the issuer targeted by a large-shareholding report (350/360).

    The filer of a 大量保有報告書 is usually a fund/individual with no ``secCode``,
    so the *target* must be resolved separately. EDINET v2 exposes the target's
    EDINET code in ``issuerEdinetCode`` (``subjectEdinetCode`` for TOBs); we map
    it to a securities code via the official EDINET code list. A securities code
    printed in the description is a softer fallback. Returns ``(None, 0.0)`` when
    the target can't be resolved — never a guess.
    """
    tmap = target_map or {}
    for key in ("issuerEdinetCode", "subjectEdinetCode"):
        code = str(r.get(key) or "").strip()
        if code and code in tmap:
            return tmap[code], 1.0

    desc = " ".join(
        str(r.get(key) or "")
        for key in ("docDescription", "currentReportReason")
    )
    # Require an *explicit* securities-code label: a bare "コード" also matches
    # "EDINETコード E12345" and would mis-resolve those 5 digits as a ticker.
    match = re.search(r"(?:証券コード|銘柄コード)\D{0,8}(\d{4})(?:\D|$)", desc)
    if match:
        return f"{match.group(1)}.T", 0.9
    return None, 0.0


def _default_codelist_fetch(url: str) -> bytes:
    r = requests.get(
        url, headers={"User-Agent": "ALMANAC research@almanac.local"}, timeout=60
    )
    r.raise_for_status()
    return r.content


def _build_code_map(zip_bytes: bytes) -> dict[str, str]:
    """Parse EDINET ``Edinetcode.zip`` → ``{edinet_code: "NNNN.T"}`` (pure)."""
    import zipfile

    try:
        with zipfile.ZipFile(io.BytesIO(zip_bytes)) as zf:
            name = next((n for n in zf.namelist() if n.lower().endswith(".csv")), None)
            if not name:
                return {}
            raw = zf.read(name)
    except (zipfile.BadZipFile, OSError, ValueError):
        return {}
    text = raw.decode("cp932", errors="replace")
    rows = [row for row in csv.reader(io.StringIO(text)) if row]

    def _has(row, *needles):
        return any(any(n in cell for n in needles) for cell in row)

    header_idx = next(
        (
            i for i, row in enumerate(rows[:5])
            if _has(row, "証券コード") and _has(row, "ＥＤＩＮＥＴコード", "EDINETコード")
        ),
        None,
    )
    if header_idx is None:
        return {}
    header = rows[header_idx]
    e_col = next(
        (i for i, c in enumerate(header) if "ＥＤＩＮＥＴコード" in c or "EDINETコード" in c),
        None,
    )
    s_col = next((i for i, c in enumerate(header) if "証券コード" in c), None)
    if e_col is None or s_col is None:
        return {}
    out: dict[str, str] = {}
    for row in rows[header_idx + 1:]:
        if len(row) <= max(e_col, s_col):
            continue
        ecode = row[e_col].strip()
        ticker = _ticker_from_sec_code(row[s_col])
        if ecode and ticker:
            out[ecode] = ticker
    return out


def load_edinet_code_map(
    *,
    live: bool = False,
    fetch: Optional[Callable[[str], bytes]] = None,
    cache_path: Optional[Path] = None,
    ttl_days: int = _CODE_MAP_TTL_DAYS,
) -> dict[str, str]:
    """Return ``{edinet_code: "NNNN.T"}`` for large-shareholding target resolution.

    Cached to ``data/edinet_code_map.json`` (TTL ``ttl_days``). The network fetch
    is **gated** (``live`` or an injected ``fetch``) so imports/tests never touch
    EDINET. Any failure degrades to ``{}`` — stake resolution then falls back to
    the description regex or drops the row, never crashes.
    """
    import time

    cache = Path(cache_path) if cache_path is not None else _CODE_MAP_CACHE
    if cache.exists():
        try:
            age_days = (time.time() - cache.stat().st_mtime) / 86_400.0
            cached = json.loads(cache.read_text(encoding="utf-8"))
            if isinstance(cached, dict) and cached:
                if age_days < ttl_days or (not live and fetch is None):
                    return {str(k): str(v) for k, v in cached.items()}
        except (OSError, ValueError):
            pass
    if fetch is None and not live:
        return {}
    fetcher = fetch or _default_codelist_fetch
    try:
        code_map = _build_code_map(fetcher(_EDINET_CODELIST_URL))
    except Exception as e:  # noqa: BLE001 — best-effort, never crash ingestion
        print(f"[edinet] code list 取得失敗: {type(e).__name__}: {e}")
        return {}
    if code_map:
        try:
            cache.parent.mkdir(parents=True, exist_ok=True)
            tmp = cache.with_suffix(cache.suffix + ".tmp")
            tmp.write_text(json.dumps(code_map, ensure_ascii=False), encoding="utf-8")
            tmp.replace(cache)
        except OSError:
            pass
    return code_map


def normalize_edinet_documents(
    payload: dict,
    *,
    limit: int = 200,
    target_map: Optional[dict[str, str]] = None,
    activist_names: Optional[list[str]] = None,
) -> list[dict]:
    """Convert an EDINET ``documents.json`` payload to disclosure items (pure).

    Ordinary reports use the filer's ``secCode``. Large-shareholding reports
    (350/360) instead resolve the *target issuer*, because the filer is commonly
    a fund or individual without a tradable security code.
    """
    results = (payload or {}).get("results") or []
    activist_list = activist_names if activist_names is not None else _load_activist_names()
    items: list[dict] = []
    for r in results:
        if not isinstance(r, dict):
            continue
        doctype = str(r.get("docTypeCode") or "")
        resolution_method = "sec_code"
        resolution_confidence = 1.0
        if doctype in _STAKE_DOCTYPES:
            ticker, resolution_confidence = _target_ticker(r, target_map=target_map)
            resolution_method = "target_resolution"
        else:
            ticker = _ticker_from_sec_code(r.get("secCode"))
        if not ticker:
            continue
        doc_id = r.get("docID")
        if not doc_id:
            continue
        publish_time = _to_iso_jst(r.get("submitDateTime") or "")
        if not publish_time:
            continue
        desc = (r.get("docDescription") or "").strip()
        filer = (r.get("filerName") or "").strip()
        filer_lower = filer.lower()
        activist_flag = bool(
            doctype in _STAKE_DOCTYPES
            and any(name in filer_lower for name in activist_list)
        )
        items.append({
            "source": "edinet",
            "ticker": ticker,
            "native_doc_id": doc_id,
            "source_url": f"https://disclosure2.edinet-fsa.go.jp/WEEK0010.aspx?docid={doc_id}",
            "publish_time": publish_time,
            "market": "JP",
            "language": "ja",
            "disclosure_type": _DOCTYPE_TO_TYPE.get(doctype, "other"),
            "title": desc or filer or doc_id,
            "body": " / ".join(p for p in (filer, desc) if p) or desc,
            "ticker_resolution_method": resolution_method,
            "ticker_resolution_confidence": resolution_confidence,
            "activist_flag": activist_flag if doctype in _STAKE_DOCTYPES else None,
        })
        if len(items) >= limit:
            break
    return items


def fetch_edinet_documents(
    date: str,
    *,
    live: bool = False,
    api_key: Optional[str] = None,
    limit: int = 200,
    target_map: Optional[dict[str, str]] = None,
) -> list[dict]:
    """Fetch one day's EDINET disclosure events as items. Network is **gated**.

    Returns ``[]`` unless ``live=True`` so importing / dry-running never hits
    EDINET. Live mode needs a Subscription-Key (arg or ``EDINET_API_KEY`` env).
    ``date`` is ``YYYY-MM-DD`` (JST business day).
    """
    if not live:
        return []
    key = api_key or os.environ.get("EDINET_API_KEY", "")
    if not key:
        print("[edinet] EDINET_API_KEY 未設定 → スキップ")
        return []
    try:
        r = requests.get(
            _EDINET_URL,
            params={"date": date, "type": 2, "Subscription-Key": key},
            timeout=30,
        )
        r.raise_for_status()
        payload = r.json()
        # EDINET v2 returns HTTP 200 even for auth/quota errors, wrapping the real
        # status in the body ({"StatusCode": 401, "message": ...}) — there is no
        # "results" key, so normalize() would silently yield 0 items and the whole
        # JP large-holding lane would masquerade as "no disclosures" forever (e.g.
        # on an invalid or expired Subscription-Key). Surface it loudly instead.
        envelope_status = payload.get("StatusCode")
        meta_status = (payload.get("metadata") or {}).get("status")
        if envelope_status not in (None, 200, "200") or meta_status not in (None, 200, "200"):
            msg = (payload.get("message")
                   or (payload.get("metadata") or {}).get("message") or "")
            raise RuntimeError(
                f"EDINET API status {envelope_status or meta_status}: {msg}".strip()
            )
        tmap = target_map if target_map is not None else load_edinet_code_map(live=True)
        return normalize_edinet_documents(payload, limit=limit, target_map=tmap)
    except Exception as e:
        print(f"[edinet] {date} 取得失敗: {e}")
        return []
