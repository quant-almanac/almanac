"""short_universe — Short tradeability evaluator(fail-closed)+ lane classifier.

Step B: tests/test_short_universe_e2e.py の契約を満たす最小実装。
「借りて売れるか(loanable / broker / JSF / squeeze / liquidity / 規制 / insider)」を
fail-closed で判定し、shortable verdict を返す。自動発注は一切行わない
(human_execution_only=True, executable=False を verdict に明示。auto_execute キーは持たない)。

screener / catalyst への本格配線は Step C 以降。ここは builder/evaluator の契約達成に集中。
"""

from __future__ import annotations

import json
import os
import tempfile
from datetime import datetime
from pathlib import Path
from typing import Any, Optional

BASE_DIR = Path(__file__).parent

# squeeze 閾値(short_screener と整合: shortRatio>10 warn / >20 high)
_SQUEEZE_WARN = 10.0
_SQUEEZE_BLOCK = 20.0
_DAYS_TO_COVER_BLOCK = 10.0
# 規制で short を完全除外するもの
_BLOCKING_REGULATION = {"short_ban"}


def _source_status(row: dict, key: str) -> Optional[str]:
    src = (row.get("sources") or {}).get(key) or {}
    return src.get("status")


def _source_stale(row: dict, key: str, now: datetime, stale_days: int) -> bool:
    """source が missing / stale(as_of が古い)なら True(=fail-closed 対象)。"""
    src = (row.get("sources") or {}).get(key)
    if not isinstance(src, dict):
        return True
    status = str(src.get("status") or "")
    if status in ("", "missing", "stale", "error"):
        return True
    as_of = src.get("as_of")
    if not as_of:
        return True
    try:
        dt = datetime.fromisoformat(str(as_of))
    except (TypeError, ValueError):
        return True
    if dt.tzinfo is not None:
        dt = dt.replace(tzinfo=None)
    return (now - dt).days > stale_days


def _squeeze_guard_status(row: dict, *, market: str, now: datetime,
                          jsf_stale_days: int) -> tuple[str, list[str]]:
    """squeeze ガード状態を返す。unknown/stale は fail-closed で blocked。"""
    reasons: list[str] = []
    # JP は JSF(日証金 貸借残/逆日歩)で squeeze を見る。stale/missing は判定不能→blocked。
    if market == "JP" and _source_stale(row, "jsf", now, jsf_stale_days):
        reasons.append("jsf source stale/missing → squeeze 判定不能で blocked(fail-closed)")
        return "blocked", reasons

    short_ratio = _to_float(row.get("short_ratio"))
    days_to_cover = _to_float(row.get("days_to_cover"))
    if (short_ratio is not None and short_ratio >= _SQUEEZE_BLOCK) or (
        days_to_cover is not None and days_to_cover >= _DAYS_TO_COVER_BLOCK
    ):
        reasons.append(f"squeeze high: short_ratio={short_ratio} days_to_cover={days_to_cover} → blocked")
        return "blocked", reasons

    if row.get("reverse_daily_fee") is True:
        reasons.append("reverse_daily_fee(逆日歩)発生中 → squeeze warn")
        return "warn", reasons
    if short_ratio is not None and short_ratio >= _SQUEEZE_WARN:
        reasons.append(f"short_ratio={short_ratio} ≥ {_SQUEEZE_WARN} → squeeze warn")
        return "warn", reasons
    return "ok", reasons


def _to_float(v) -> Optional[float]:
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def evaluate_shortability(row: dict, *, now: Optional[datetime] = None,
                          jsf_stale_days: int = 2) -> dict[str, Any]:
    """1 銘柄が「安全に空売れるか」を fail-closed で判定する。

    どれか一つでも unknown/stale/NG なら shortable=false。自動発注はしない
    (human_execution_only=True / executable=False、auto_execute キーは付けない)。
    """
    now = now or datetime.now()
    market = str(row.get("market") or "").upper()
    reasons: list[str] = []

    # squeeze guard(JP は JSF 鮮度込み)
    squeeze_status, squeeze_reasons = _squeeze_guard_status(
        row, market=market, now=now, jsf_stale_days=jsf_stale_days
    )
    reasons.extend(squeeze_reasons)

    # ── 除外ゲート(どれか該当で shortable=false)──
    if row.get("insider_restricted") is True:
        reasons.append("insider_restricted → short でも除外(コンプライアンス)")

    regulation = str(row.get("regulation") or "none")
    if regulation in _BLOCKING_REGULATION:
        reasons.append(f"regulation={regulation} → 売禁/規制(short_ban)で除外")

    if market == "JP":
        loanable_ok = (row.get("jpx_loanable") is True
                       and _source_status(row, "jpx_loanable") == "ok")
        if not loanable_ok:
            reasons.append("jpx_loanable が unknown/false/stale → fail-closed(借株不可)")
    elif market == "US":
        # US は broker が明示的に売建可と確認した場合のみ。borrow unknown は fail-closed。
        broker_ok = (row.get("rakuten_general_short") is True
                     or row.get("sbi_general_short") is True)
        if not broker_ok:
            reasons.append("US broker short availability unknown/missing → fail-closed "
                           "(明示的な broker borrow 無し)")
    else:
        reasons.append(f"unknown market={market!r} → fail-closed")

    if row.get("liquidity_ok") is not True:
        reasons.append("liquidity_ok でない(流動性不足)→ 除外")

    if row.get("reverse_daily_fee") is True:
        reasons.append("reverse_daily_fee(逆日歩)発生中 → 借株コスト過大で除外")

    if squeeze_status == "blocked":
        reasons.append("squeeze_guard=blocked → 踏み上げリスクで除外")

    # 除外理由が一つも無ければ shortable
    # (squeeze 'warn' は cost 注記のみで、reverse_daily_fee 等の hard 理由が別途無ければ可)
    _hard_reasons = [r for r in reasons if "warn" not in r]
    shortable = len(_hard_reasons) == 0

    borrow_cost = _to_float(row.get("borrow_cost_annual_pct"))
    cost_model = {
        "borrow_cost_annual_pct": borrow_cost,
        "reverse_daily_fee": bool(row.get("reverse_daily_fee") or False),
        "reverse_daily_fee_bps": row.get("reverse_daily_fee_bps"),
    }

    return {
        "ticker": row.get("ticker"),
        "market": market,
        "shortable": bool(shortable),
        "squeeze_guard_status": squeeze_status,
        "reasons": reasons,
        "borrow_cost_annual_pct": borrow_cost,
        "cost_model": cost_model,
        "lane_eligibility": list(row.get("lane_eligibility") or []),
        # ── 安全不変: 自動発注しない ──
        "human_execution_only": True,
        "executable": False,   # observe_only-first。priority 昇格は Step C 以降の別ゲート。
    }


def classify_short_lane(signal: dict) -> Optional[str]:
    """short シグナルを 3 レーンに分類。event(触媒)> overheat(過熱)> bear(弱気)の優先。"""
    if not isinstance(signal, dict):
        return None
    if signal.get("dilution_flag") is True or signal.get("going_concern_flag") is True:
        return "event"
    rsi = _to_float(signal.get("rsi"))
    ma50_diff = _to_float(signal.get("ma50_diff_pct"))
    if rsi is not None and rsi >= 80 and (ma50_diff is None or ma50_diff >= 20):
        return "overheat"
    regime = str(signal.get("regime") or "").upper()
    trend = str(signal.get("trend") or "").lower()
    if "BEAR" in regime or trend == "down":
        return "bear"
    return None


# ============================================================
# Step C: builder(data source → short_universe.json ledger)
# ============================================================

def _is_restricted(ticker: str) -> bool:
    """insider 制限(勤務先株等)。short でも必ず除外。import 失敗時は fail-closed。"""
    try:
        from insider_restrictions import is_restricted_ticker
        return bool(is_restricted_ticker(ticker))
    except Exception:
        return True


def _jp_borrow_cost(jentry: dict | None) -> Optional[float]:
    """JP 借株コスト(年率)。逆日歩発生なら buffer を上乗せ。config 不在なら None。"""
    try:
        with open(BASE_DIR / "disclosure_shadow_config.json", encoding="utf-8") as f:
            jp = (json.load(f).get("cost_model") or {}).get("jp_short") or {}
    except (OSError, json.JSONDecodeError):
        return None
    base = _to_float(jp.get("standard_borrow_rate_annual"))
    if base is None:
        return None
    if jentry and jentry.get("reverse_daily_fee"):
        base += _to_float(jp.get("reverse_daily_fee_buffer_annual")) or 0.0
    return round(base, 6)


def build_universe_row(ticker: str, *, market: Optional[str] = None,
                       loanable_map: Optional[dict] = None,
                       jsf_state: Optional[dict] = None,
                       broker_us_map: Optional[dict] = None,
                       squeeze_map: Optional[dict] = None,
                       regulation_map: Optional[dict] = None,
                       liquidity_ok: bool = True,
                       pinned_at: Optional[str] = None,
                       now: Optional[datetime] = None) -> dict[str, Any]:
    """data source から 1 銘柄の short_universe row(schema 行)を組み立てる。

    欠落値は None のまま残し、shortable 判定は evaluate_shortability に委ねる
    (= ここでは fail-closed の根拠だけ揃える)。
    """
    market = (market or ("JP" if str(ticker).endswith(".T") else "US")).upper()
    loanable_map = loanable_map or {}
    jsf_state = jsf_state or {}
    broker_us_map = broker_us_map or {}
    squeeze_map = squeeze_map or {}
    regulation_map = regulation_map or {}

    sq = squeeze_map.get(ticker) or {}
    row: dict[str, Any] = {
        "ticker": ticker,
        "market": market,
        "name": ticker,
        "short_ratio": _to_float(sq.get("short_ratio")),
        "days_to_cover": _to_float(sq.get("days_to_cover")),
        "regulation": str(regulation_map.get(ticker) or "none"),
        "adv_jpy": _to_float(sq.get("adv_jpy")),
        "liquidity_ok": bool(liquidity_ok),
        "insider_restricted": _is_restricted(ticker),
        "earnings_blackout": False,
        "human_execution_only": True,
        "sources": {},
    }

    if market == "JP":
        loanable = loanable_map.get(ticker, None)
        row["jpx_loanable"] = loanable
        row["sources"]["jpx_loanable"] = {
            "as_of": pinned_at,
            "status": "ok" if loanable is not None else "missing",
        }
        # JSF は「データセットの鮮度」で判定する。日証金は逆日歩/品貸のある銘柄だけを
        # 載せるので、データセットが新しければ未収載=squeeze 無し(ok)。データセット
        # 自体が無い(generated_at 不在)場合のみ判定不能→missing→blocked(fail-closed)。
        jsf_as_of = jsf_state.get("generated_at")
        jentry = (jsf_state.get("tickers") or {}).get(ticker)
        row["sources"]["jsf"] = {
            "as_of": jsf_as_of,
            "status": "ok" if jsf_as_of else "missing",
        }
        if jentry:
            row["reverse_daily_fee"] = bool(jentry.get("reverse_daily_fee"))
            row["jsf_loan_balance_ratio"] = _to_float(jentry.get("loan_ratio"))
        else:
            # データセットは新しいが当該銘柄は逆日歩/品貸リスト外 = squeeze 無し
            row["reverse_daily_fee"] = False if jsf_as_of else None
            row["jsf_loan_balance_ratio"] = None
        row["borrow_cost_annual_pct"] = _jp_borrow_cost(jentry)
    else:  # US
        bentry = broker_us_map.get(ticker) or {}
        row["rakuten_general_short"] = bentry.get("rakuten")
        row["sbi_general_short"] = bentry.get("sbi")
        row["broker"] = [k for k in ("rakuten", "sbi") if bentry.get(k) is True]
        row["borrow_cost_annual_pct"] = _to_float(bentry.get("borrow_cost_annual_pct"))
        row["reverse_daily_fee"] = False
        row["sources"]["broker"] = {
            "as_of": bentry.get("as_of") or broker_us_map.get("_as_of"),
            "status": "manual" if bentry else "missing",
        }

    return row


def _load_json(path: Path) -> dict:
    if not path.exists():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except (OSError, json.JSONDecodeError):
        return {}


def _load_sources(base_dir: Path) -> dict:
    """既定の data source を読む。欠落ファイルは空 → fail-closed。"""
    duj = _load_json(base_dir / "disclosure_universe_jp.json")
    # 日次 sync(sync_jp_short_taisyaku.py)が書く fresh な loanable を優先。
    # 無ければ pinned な disclosure_universe_jp.json に fallback。
    loanable_file = _load_json(base_dir / "data" / "jp_loanable_state.json")
    regulation_file = _load_json(base_dir / "data" / "jp_regulation_state.json")
    jsf = _load_json(base_dir / "data" / "jsf_lending_state.json")
    broker_file = _load_json(base_dir / "data" / "broker_short_us.json")
    # sync_broker_short_us は {generated_at, tickers:{...}} で書く。
    # builder は flat な ticker→entry map を期待するので展開する。
    broker = dict(broker_file.get("tickers") or {})
    if broker:
        broker["_as_of"] = broker_file.get("generated_at")
    sector = _load_json(base_dir / "data" / "sector_cache.json")
    # yfinance shortRatio ≈ days-to-cover を squeeze proxy に流用
    squeeze_map = {
        t: {"days_to_cover": v.get("short_ratio")}
        for t, v in sector.items() if isinstance(v, dict)
    }
    loanable_map = loanable_file.get("loanable_by_ticker") or duj.get("loanable_by_ticker") or {}
    pinned_at = loanable_file.get("generated_at") or duj.get("_pinned_at")
    return {
        "loanable_map": loanable_map,
        "pinned_at": pinned_at,
        "jsf_state": jsf,
        "broker_us_map": broker,
        "squeeze_map": squeeze_map,
        "regulation_map": regulation_file.get("tickers") or {},
    }


def _merge_existing_ledger(ledger: dict, output_path: Path) -> dict:
    existing = _load_json(output_path)
    current = dict(ledger.get("tickers") or {})
    previous = existing.get("tickers") if isinstance(existing, dict) else None
    if isinstance(previous, dict):
        merged = dict(previous)
        merged.update(current)
    else:
        merged = current
    out = dict(ledger)
    out["tickers"] = merged
    out["shortable_count"] = sum(1 for r in merged.values() if isinstance(r, dict) and r.get("shortable"))
    return out


def _write_ledger(ledger: dict, output_path: Path) -> dict:
    ledger = _merge_existing_ledger(ledger, output_path)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=output_path.parent, suffix=".tmp")
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        json.dump(ledger, f, ensure_ascii=False, indent=2)
    os.replace(tmp, output_path)
    return ledger


def build_short_universe(tickers, *, now: Optional[datetime] = None,
                         base_dir: Path = BASE_DIR,
                         sources: Optional[dict] = None,
                         liquidity_ok: bool = True,
                         write: bool = False,
                         output_path: Optional[Path] = None) -> dict[str, Any]:
    """tickers 全件の tradeability を data source から評価し ledger を返す/書く。

    候補は黙って消さず、shortable=false でも reason 付きで全件 ledger に残す。
    """
    now = now or datetime.now()
    src = _load_sources(base_dir) if sources is None else dict(sources)
    rows: dict[str, Any] = {}
    for t in tickers:
        row = build_universe_row(
            t, now=now, liquidity_ok=liquidity_ok,
            loanable_map=src.get("loanable_map"),
            jsf_state=src.get("jsf_state"),
            broker_us_map=src.get("broker_us_map"),
            squeeze_map=src.get("squeeze_map"),
            regulation_map=src.get("regulation_map"),
            pinned_at=src.get("pinned_at"),
        )
        verdict = evaluate_shortability(row, now=now)
        rows[t] = {**row, **verdict, "last_evaluated_at": now.isoformat()}

    ledger = {
        "as_of": now.isoformat(),
        "shortable_count": sum(1 for r in rows.values() if r.get("shortable")),
        "tickers": rows,
    }
    if write:
        ledger = _write_ledger(ledger, output_path or (base_dir / "data" / "short_universe.json"))
    return ledger


def apply_shortability_gate(candidate: dict, universe: Optional[dict] = None, *,
                            now: Optional[datetime] = None) -> dict:
    """technical 候補に short_universe の verdict を載せる(screener gate)。

    shortable=false / 未収載 は fail-closed。shortable=true でも observe_only /
    human_execution_only を固定し、自動発注フラグ(auto_execute / executable=True)は付けない。
    """
    ticker = candidate.get("ticker")
    entry = ((universe or {}).get("tickers") or {}).get(ticker)
    if entry is None:
        entry = {
            "shortable": False,
            "squeeze_guard_status": "blocked",
            "reasons": [f"{ticker} は short_universe 未収載 → fail-closed"],
            "lane_eligibility": [],
            "borrow_cost_annual_pct": None,
            "cost_model": None,
        }

    out = dict(candidate)
    out["shortable"] = bool(entry.get("shortable"))
    out["shortability"] = {
        "shortable": bool(entry.get("shortable")),
        "squeeze_guard_status": entry.get("squeeze_guard_status"),
        "reasons": list(entry.get("reasons") or []),
        "borrow_cost_annual_pct": entry.get("borrow_cost_annual_pct"),
        "cost_model": entry.get("cost_model"),
        "lane_eligibility": list(entry.get("lane_eligibility") or []),
    }
    # observe_only-first 不変: 自動発注はしない
    out["observe_only"] = True
    out["human_execution_only"] = True
    out.pop("auto_execute", None)
    out["executable"] = False
    return out
