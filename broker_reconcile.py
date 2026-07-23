"""
broker_reconcile.py — P1-19: ブローカー照合フレームワーク（雛型）

Codex 3 ラウンド目 (T4: ブローカー照合がない、推測ロジックのみ) への応答。
ブローカー (楽天 / SBI) の取引履歴 CSV を取り込み、内部 event_ledger と日次差分を取る。

設計:
  1. parse_csv(path, broker) → 正規化された list[BrokerTrade] (in-memory)
  2. compare_to_ledger(broker_trades, date_from, date_to) → ReconcileReport
  3. CLI: python broker_reconcile.py --csv rakuten_202605.csv --broker rakuten

正規化スキーマ (BrokerTrade):
  - trade_date    : YYYY-MM-DD
  - ticker        : 楽天/SBI の銘柄コードを内部表記に変換 (例: '7203' → '7203.T')
  - direction     : 'buy' | 'sell'
  - quantity      : 株数
  - price         : 単価
  - currency      : 'JPY' | 'USD'
  - account       : '特定' | 'NISA成長投資枠' | etc.
  - broker        : 'rakuten' | 'sbi'
  - external_id   : ブローカー側の約定 id (idempotency 用)

注意:
  rakuten / sbi の実 CSV フォーマットはユーザー実機からエクスポートしないと確定しない。
  本ファイルは parser interface + 正規化済みデータの突合ロジックを実装し、
  具体的な CSV → 正規化変換は `_parse_rakuten` / `_parse_sbi` に stub として置く。
  実 CSV を見たら stub を置き換えれば動く設計。

ReconcileReport:
  - only_in_broker  : ブローカーにあるが ledger に無い trade (誤入力 / ledger 漏れ)
  - only_in_ledger  : ledger にあるが broker に無い trade (架空入力 / 未約定が executed 化)
  - mismatched      : ticker/direction 一致だが quantity/price/date がズレる trade
  - matched_count   : 完全一致した件数
"""
from __future__ import annotations

import argparse
import csv
import json
import sys
from dataclasses import asdict, dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Iterable, List, Optional

BASE_DIR = Path(__file__).parent


# ============================================================
# Data classes
# ============================================================

@dataclass
class BrokerTrade:
    trade_date:  str            # ISO YYYY-MM-DD
    ticker:      str
    direction:   str            # 'buy' | 'sell'
    quantity:    float
    price:       float
    currency:    str            # 'JPY' | 'USD'
    account:     Optional[str] = None
    broker:      Optional[str] = None
    external_id: Optional[str] = None


@dataclass
class TradeMismatch:
    broker_trade: dict
    ledger_trade: dict
    differences:  List[str]


@dataclass
class ParseReport:
    """Codex P2 #12: パース件数と skip 理由を可視化 (不正行の沈黙 skip を廃止)。"""
    broker:       str
    rows_total:   int = 0
    parsed:       int = 0
    skipped:      int = 0
    skip_reasons: List[dict] = field(default_factory=list)  # {"row": int, "reason": str}

    def as_dict(self) -> dict:
        return {
            "broker":       self.broker,
            "rows_total":   self.rows_total,
            "parsed":       self.parsed,
            "skipped":      self.skipped,
            "skip_reasons": self.skip_reasons[:50],
        }


@dataclass
class ReconcileReport:
    matched_count:  int = 0
    only_in_broker: List[dict] = field(default_factory=list)
    only_in_ledger: List[dict] = field(default_factory=list)
    mismatched:     List[TradeMismatch] = field(default_factory=list)
    scope:          dict = field(default_factory=dict)  # broker scope メタ (Codex P2 #12)

    def as_dict(self) -> dict:
        return {
            "matched_count":  self.matched_count,
            "scope":          self.scope,
            "only_in_broker": self.only_in_broker,
            "only_in_ledger": self.only_in_ledger,
            "mismatched":     [
                {
                    "broker_trade": m.broker_trade,
                    "ledger_trade": m.ledger_trade,
                    "differences":  m.differences,
                }
                for m in self.mismatched
            ],
        }

    @property
    def has_discrepancy(self) -> bool:
        return bool(self.only_in_broker or self.only_in_ledger or self.mismatched)


def compare_tax_cost_basis(
    broker_positions: Iterable,
    *,
    price_tolerance_pct: float = 0.005,
    quantity_tolerance: float = 1e-6,
    db_path: Optional[Path] = None,
) -> dict:
    """Compare Rakuten taxable-account average acquisition prices with internal open lots."""
    from tax_lot import portfolio_lot_snapshot

    positions = [
        pos for pos in broker_positions
        if str(getattr(pos, "broker", "")) == "楽天証券"
        and str(getattr(pos, "account", "")) == "特定"
    ]
    tickers = sorted({str(pos.ticker) for pos in positions})
    snapshot = portfolio_lot_snapshot(tickers=tickers, db_path=db_path).get("lots", {})
    matched = 0
    discrepancies = []
    missing_internal = []
    for pos in positions:
        lots = [
            lot for lot in snapshot.get(pos.ticker, [])
            if lot.get("account") == pos.account and float(lot.get("remaining_qty") or 0) > 0
        ]
        if not lots:
            missing_internal.append({
                "ticker": pos.ticker,
                "account": pos.account,
                "broker_quantity": pos.quantity,
                "broker_entry_price": pos.entry_price,
            })
            continue
        internal_qty = sum(float(lot["remaining_qty"]) for lot in lots)
        internal_cost = sum(
            float(lot["remaining_qty"]) * float(lot["cost_per_share"])
            for lot in lots
        )
        internal_avg = internal_cost / internal_qty
        price_diff_pct = (
            abs(float(pos.entry_price) - internal_avg) / internal_avg
            if internal_avg else float("inf")
        )
        quantity_diff = abs(float(pos.quantity) - internal_qty)
        row = {
            "ticker": pos.ticker,
            "account": pos.account,
            "currency": pos.currency,
            "broker_quantity": float(pos.quantity),
            "internal_quantity": internal_qty,
            "broker_entry_price": float(pos.entry_price),
            "internal_weighted_average_price": internal_avg,
            "price_diff_pct": price_diff_pct * 100.0,
            "quantity_diff": quantity_diff,
            "method_note": "internal open lots weighted average vs Rakuten displayed acquisition price",
        }
        if price_diff_pct > price_tolerance_pct or quantity_diff > quantity_tolerance:
            discrepancies.append(row)
        else:
            matched += 1
    return {
        "scope": "rakuten_taxable_cost_basis",
        "matched_count": matched,
        "discrepancies": discrepancies,
        "missing_internal": missing_internal,
        "has_discrepancy": bool(discrepancies or missing_internal),
    }


# ============================================================
# Parser registry
# ============================================================

def parse_csv(path: Path, broker: str) -> List[BrokerTrade]:
    """ブローカー名で適切な parser に振り分け、trade のみ返す (後方互換)。"""
    trades, _ = parse_csv_with_report(path, broker)
    return trades


def parse_csv_with_report(path: Path, broker: str) -> "tuple[List[BrokerTrade], ParseReport]":
    """parse_csv に加え、パース件数 / skip 理由 (ParseReport) も返す (Codex P2 #12)。"""
    broker = broker.lower()
    if broker == "rakuten":
        return _parse_rakuten(path)
    if broker == "sbi":
        return _parse_sbi(path)
    raise ValueError(f"unknown broker: {broker} (rakuten | sbi のみ対応)")


def _parse_rakuten(path: Path) -> "tuple[List[BrokerTrade], ParseReport]":
    """
    楽天証券 国内株式・米国株式 約定履歴 CSV のパース。

    仕様 (推定):
      Shift_JIS、ヘッダ行あり、カラム: 約定日, 銘柄コード, 銘柄名, 売買, 数量, 単価, 通貨, 口座区分, ...
      実機 CSV を見て確定したら本関数のヘッダ/カラム名を実値に合わせる。

    現状: stub として汎用 UTF-8 CSV を受け、ヘッダのキーワード一致で列を抜き出す。
    """
    return _parse_generic(path, broker="rakuten")


def _parse_sbi(path: Path) -> "tuple[List[BrokerTrade], ParseReport]":
    """SBI 証券 約定履歴 CSV のパース (stub — 楽天と同じ汎用 parser に委譲)。"""
    return _parse_generic(path, broker="sbi")


# ── 汎用 parser ─────────────────────────────────────────
# 列名のキーワード一致でフィールドを抜く。実 CSV のヘッダが分かったら mapping を確定させる。

_HEADER_HINTS = {
    "trade_date":  ["約定日", "trade_date", "date"],
    "ticker":      ["銘柄コード", "ticker", "code", "symbol"],
    "direction":   ["売買", "buy_sell", "side", "type"],
    "quantity":    ["数量", "quantity", "shares", "qty"],
    "price":       ["単価", "price", "avg_price"],
    "currency":    ["通貨", "currency", "ccy"],
    "account":     ["口座区分", "account", "account_type"],
    "external_id": ["約定番号", "external_id", "id", "trade_id"],
}


def _match_header(headers: List[str], field_name: str) -> Optional[str]:
    hints = _HEADER_HINTS.get(field_name, [])
    for h in headers:
        for hint in hints:
            if hint in h or hint.lower() in h.lower():
                return h
    return None


def _normalize_direction(s: str) -> Optional[str]:
    s = (s or "").strip().lower()
    if s in ("買", "買付", "buy", "b"):
        return "buy"
    if s in ("売", "売却", "sell", "s"):
        return "sell"
    return None


def _normalize_ticker(raw: str, currency: Optional[str]) -> str:
    raw = (raw or "").strip().upper()
    if not raw:
        return raw
    # 4 桁数字 + JPY → .T を付ける
    if raw.isdigit() and len(raw) == 4 and (currency or "").upper() == "JPY":
        return f"{raw}.T"
    return raw


def _parse_generic(path: Path, *, broker: str) -> "tuple[List[BrokerTrade], ParseReport]":
    """汎用 CSV parser (UTF-8 / Shift_JIS 両対応)。実 CSV 確定後に専用 parser へ置換可能。

    Codex P2 #12: 不正行を黙って捨てず ParseReport に件数 + 理由を記録する。
    """
    text = None
    for enc in ("utf-8-sig", "utf-8", "cp932", "shift_jis"):
        try:
            text = path.read_text(encoding=enc)
            break
        except UnicodeDecodeError:
            continue
    if text is None:
        raise RuntimeError(f"CSV のエンコーディングが判定不能: {path}")

    reader = csv.DictReader(text.splitlines())
    headers = reader.fieldnames or []
    col = {f: _match_header(headers, f) for f in _HEADER_HINTS}

    if not all([col["trade_date"], col["ticker"], col["direction"], col["quantity"], col["price"]]):
        raise RuntimeError(
            f"必須カラム不足。検出: {col}。実 CSV のヘッダ: {headers}"
        )

    trades: List[BrokerTrade] = []
    rep = ParseReport(broker=broker)
    for i, row in enumerate(reader):
        rep.rows_total += 1
        line_no = i + 2  # header が 1 行目、最初のデータ行 = 2
        try:
            dir_norm = _normalize_direction(row[col["direction"]])
            if dir_norm is None:
                rep.skipped += 1
                rep.skip_reasons.append({
                    "row": line_no,
                    "reason": f"売買区分を解釈不能: {row.get(col['direction'])!r}",
                })
                continue
            currency = (row[col["currency"]] if col["currency"] else "JPY").upper() or "JPY"
            ticker = _normalize_ticker(row[col["ticker"]], currency)
            trades.append(BrokerTrade(
                trade_date  = row[col["trade_date"]][:10],
                ticker      = ticker,
                direction   = dir_norm,
                quantity    = float(str(row[col["quantity"]]).replace(",", "")),
                price       = float(str(row[col["price"]]).replace(",", "")),
                currency    = currency,
                account     = row[col["account"]] if col["account"] else None,
                broker      = broker,
                external_id = row[col["external_id"]] if col["external_id"] else None,
            ))
            rep.parsed += 1
        except (KeyError, ValueError, TypeError) as e:
            rep.skipped += 1
            rep.skip_reasons.append({"row": line_no, "reason": f"{type(e).__name__}: {e}"})
            continue
    return trades, rep


# ============================================================
# Reconciliation
# ============================================================

# 突合のためのキー: (date, ticker, direction)
# 同一キーで quantity/price が一致すれば matched、ズレれば mismatched。

def _key(trade: dict) -> tuple:
    return (
        (trade.get("trade_date") or trade.get("occurred_at") or "")[:10],
        (trade.get("ticker") or "").upper(),
        (trade.get("direction") or "").lower(),
    )


def _ledger_to_dict(row: dict) -> dict:
    """event_ledger row → 比較用 dict に正規化。external_id は raw_payload から拾う。"""
    payload = {}
    rp = row.get("raw_payload")
    if isinstance(rp, str) and rp:
        try:
            payload = json.loads(rp)
        except Exception:
            payload = {}
    elif isinstance(rp, dict):
        payload = rp
    return {
        "trade_date": (row.get("occurred_at") or "")[:10],
        "ticker":     (row.get("ticker") or "").upper(),
        "direction":  (row.get("direction") or "").lower(),
        "quantity":   row.get("quantity"),
        "price":      row.get("price"),
        "currency":   row.get("currency"),
        "account":    row.get("account"),
        "event_id":   row.get("event_id"),
        "external_id": payload.get("external_id") if isinstance(payload, dict) else None,
    }


def _infer_broker_from_account(account: Optional[str]) -> str:
    """口座名から broker を推定する (ledger event に broker 列が無いため)。
    'SBI' / '妻' を含めば 'sbi'、それ以外は 'rakuten'。"""
    a = account or ""
    if "SBI" in a.upper() or "妻" in a:
        return "sbi"
    return "rakuten"


def compare_to_ledger(
    broker_trades: Iterable[BrokerTrade],
    *,
    date_from: str,
    date_to: str,
    broker: Optional[str] = None,
    qty_tolerance: float = 1e-6,
    price_tolerance_pct: float = 0.005,  # 0.5% (約定時 mid と CSV avg price の小ズレ許容)
    db_path: Optional[Path] = None,
) -> ReconcileReport:
    """
    指定期間の event_ledger (trade event) と broker CSV を突合する。

    Args:
        broker_trades: parse_csv の出力
        date_from / date_to: 比較対象期間 (ISO YYYY-MM-DD)
        broker: 'rakuten' | 'sbi'。指定すると ledger 側を当該 broker に限定する
                (Codex P2 #12: 全 ledger と突合して他ブローカーを誤検出するのを防ぐ)。
        qty_tolerance: 数量一致の絶対許容差
        price_tolerance_pct: 単価一致の相対許容差 (default 0.5%)

    Returns:
        ReconcileReport (report.scope に broker scope メタを格納)
    """
    from event_ledger import query_events

    ledger_rows = query_events(
        date_from=date_from,
        date_to=date_to + "T23:59:59",
        types=["trade"],
        db_path=db_path,
    )
    ledger = [_ledger_to_dict(r) for r in ledger_rows]

    # Codex P2 #12: broker scope。ledger に broker 列が無いため account から推定して限定。
    scope = {"broker": broker, "ledger_total": len(ledger)}
    if broker:
        bl = broker.lower()
        ledger = [r for r in ledger if _infer_broker_from_account(r.get("account")) == bl]
        scope["ledger_in_scope"] = len(ledger)
    else:
        scope["warning"] = "broker 未指定: 全 ledger trade と突合 (他ブローカーが誤検出される可能性)。"

    b_trades = [asdict(t) for t in broker_trades]

    # index by key / external_id
    ledger_by_key: dict = {}
    ledger_by_extid: dict = {}
    for r in ledger:
        ledger_by_key.setdefault(_key(r), []).append(r)
        eid = r.get("external_id")
        if eid:
            ledger_by_extid.setdefault(str(eid), []).append(r)

    report = ReconcileReport(scope=scope)
    matched_indices: set = set()

    for b in b_trades:
        # Codex P2 #12: external_id があれば完全一致を最優先 (date/qty/price の許容差に依存しない)。
        b_eid = b.get("external_id")
        if b_eid and str(b_eid) in ledger_by_extid:
            exact = next(
                (lr for lr in ledger_by_extid[str(b_eid)] if id(lr) not in matched_indices),
                None,
            )
            if exact is not None:
                matched_indices.add(id(exact))
                diff = _diff_trade(b, exact, qty_tolerance, price_tolerance_pct)
                if diff:
                    report.mismatched.append(
                        TradeMismatch(broker_trade=b, ledger_trade=exact, differences=diff)
                    )
                else:
                    report.matched_count += 1
                continue

        k = _key(b)
        candidates = ledger_by_key.get(k, [])
        if not candidates:
            report.only_in_broker.append(b)
            continue

        # Codex re-review #12: 未使用 (未マッチ) の candidate のみ対象にする。
        # 旧実装は broker 側が重複すると candidates[0] (既に別 broker trade に使用済み) を
        # 再割当てし、空 differences の偽 mismatch を生んでいた。
        unused = [lr for lr in candidates if id(lr) not in matched_indices]
        if not unused:
            # key 一致だが利用可能な ledger 行が無い (broker 側の重複) → only_in_broker
            report.only_in_broker.append(b)
            continue

        matched = None
        for lr in unused:
            if not _diff_trade(b, lr, qty_tolerance, price_tolerance_pct):
                matched = lr
                matched_indices.add(id(lr))
                break
        if matched:
            report.matched_count += 1
        else:
            # 完全一致は無いが未使用候補がある → 最初の未使用候補との実 diff を mismatch 記録
            partner = unused[0]
            report.mismatched.append(TradeMismatch(
                broker_trade=b,
                ledger_trade=partner,
                differences=_diff_trade(b, partner, qty_tolerance, price_tolerance_pct),
            ))
            matched_indices.add(id(partner))

    # ledger にあるが broker に出てこなかったものを only_in_ledger に
    for r in ledger:
        if id(r) not in matched_indices:
            report.only_in_ledger.append(r)

    return report


def _diff_trade(b: dict, l: dict, qty_tol: float, price_tol_pct: float) -> List[str]:
    diffs: List[str] = []
    bc, lc = (b.get("currency") or "").upper(), (l.get("currency") or "").upper()
    if bc and lc and bc != lc:
        diffs.append(f"currency {bc} vs {lc}")
    ba, la = b.get("account"), l.get("account")
    if ba and la and str(ba).strip() != str(la).strip():
        diffs.append(f"account {ba} vs {la}")
    bq, lq = b.get("quantity"), l.get("quantity")
    if bq is None or lq is None or abs(float(bq) - float(lq)) > qty_tol:
        diffs.append(f"quantity {bq} vs {lq}")
    bp, lp = b.get("price"), l.get("price")
    if bp is None or lp is None:
        diffs.append(f"price {bp} vs {lp}")
    elif lp == 0:
        diffs.append(f"ledger price = 0 (invalid)")
    elif abs((float(bp) - float(lp)) / float(lp)) > price_tol_pct:
        diffs.append(f"price {bp} vs {lp} (>{price_tol_pct * 100:.1f}%)")
    return diffs


# ============================================================
# CLI
# ============================================================

def _main() -> None:
    parser = argparse.ArgumentParser(description="ALMANAC broker reconcile")
    parser.add_argument("--csv",    required=True, help="ブローカー CSV ファイルパス")
    parser.add_argument("--broker", required=True, choices=["rakuten", "sbi"])
    parser.add_argument("--from", dest="date_from", required=True, help="YYYY-MM-DD")
    parser.add_argument("--to",   dest="date_to",   required=True, help="YYYY-MM-DD")
    args = parser.parse_args()

    csv_path = Path(args.csv)
    if not csv_path.exists():
        print(f"CSV が見つかりません: {csv_path}", file=sys.stderr)
        sys.exit(1)

    trades, parse_report = parse_csv_with_report(csv_path, args.broker)
    print(f"[broker] parsed {parse_report.parsed}/{parse_report.rows_total} rows "
          f"(skipped {parse_report.skipped}) from {csv_path}", file=sys.stderr)
    for sr in parse_report.skip_reasons[:20]:
        print(f"  [skip] row {sr['row']}: {sr['reason']}", file=sys.stderr)

    report = compare_to_ledger(
        trades,
        date_from=args.date_from,
        date_to=args.date_to,
        broker=args.broker,
    )
    out = {"parse": parse_report.as_dict(), **report.as_dict()}
    print(json.dumps(out, ensure_ascii=False, indent=2))
    # Codex P2 #12: パース skip / 差分があれば本番反映前に loud に失敗 (exit 2)。
    if parse_report.skipped > 0 or report.has_discrepancy:
        sys.exit(2)


if __name__ == "__main__":
    _main()
