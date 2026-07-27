"""
tax_lot.py — P2: Tax Lot tracker & solver

event_ledger の trade event 系列から、銘柄ごとの lot (取得単位) timeline を再構築する。
既存 tax_optimizer.py は holdings.json の加重平均単価しか見ていないため、
特定の lot を狙って売って実損確定 / NISA 枠を踏まないという制御ができなかった。

本モジュールはそのギャップを埋める:
  - build_lots(ticker)              : event_ledger から FIFO で lot を組み立てる
  - recommend_sell_lots(...)        : 売却量に対し、戦略別 (FIFO/LIFO/loss_harvest/gain_minimize) で lot を選ぶ
  - realized_pnl_in_year(year)      : 確定申告用、年内の実現損益サマリ
  - portfolio_lot_snapshot()        : 保有銘柄全件の現存 lot リスト (account 別に集計可能)

設計:
  - source of truth は event_ledger.ledger_events (event_type='trade' のみ)
  - SELL は FIFO で消費 (account/currency が一致する lot から)
  - cost basis は event 時点の price × quantity × (USD なら fx_rate_usdjpy)、JPY 換算で保存
  - "specific identification" は recommend_sell_lots で sell lot を chosen list として返し、
    呼出側が UI で確認した後に SELL event として post する想定

注意:
  - 配当・分割・併合の event_type も将来は cost basis 調整に組み込むが、
    現状は trade event のみ。split は parquet 側で adjusted close が更新されるため、
    realized 計算では「lot の cost_jpy が時系列で歪まないか」を別途確認する必要がある。
  - 全 lot を毎回 event_ledger から再構築する rebuild モデル (キャッシュなし)。
    1000 trade 程度なら数 ms。large scale ではキャッシュテーブルを追加する想定。
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Dict, List, Optional


# ============================================================
# Data classes
# ============================================================

@dataclass
class Lot:
    """単一の取得 lot (BUY event 1 件に対応)。"""
    lot_id:          str            # event_id of BUY
    ticker:          str
    purchase_date:   str            # YYYY-MM-DD
    initial_qty:     float          # 取得時点の数量
    remaining_qty:   float          # 残り数量 (SELL で減る)
    cost_per_share:  float          # 取得単価 (current 通貨)
    currency:        str            # JPY / USD
    cost_per_share_jpy: float       # 取得時 fx で JPY 換算した単価
    account:         Optional[str]  # 特定 / NISA成長投資枠 / 持株会 / etc.

    @property
    def remaining_cost_jpy(self) -> float:
        return self.remaining_qty * self.cost_per_share_jpy

    @property
    def is_open(self) -> bool:
        return self.remaining_qty > 1e-9


@dataclass
class RealizedTrade:
    """SELL で確定した損益 1 件 (1 つの lot を部分/全消費)。"""
    sell_date:       str
    ticker:          str
    lot_id:          str
    quantity:        float
    sell_price:      float
    sell_fx:         Optional[float]
    cost_per_share_jpy: float
    proceeds_jpy:    float
    cost_basis_jpy:  float
    realized_jpy:    float
    account:         Optional[str]


@dataclass
class TaxLotState:
    open_lots:        List[Lot] = field(default_factory=list)
    realized_trades:  List[RealizedTrade] = field(default_factory=list)


# ============================================================
# Build lots from event_ledger
# ============================================================

def _proceeds_jpy(*, price: float, qty: float, currency: str, fx: Optional[float]) -> float:
    if currency.upper() == "USD":
        if fx is None or fx <= 0:
            raise ValueError("USD SELL event に fx_rate_usdjpy が無いため tax lot の損益を計算できません")
        return price * qty * fx
    return price * qty


def _split_ratio(ev: dict) -> Optional[float]:
    """split / merge event の比率 (旧1株あたり新株数 = new/old) を返す。

    規約 (canonical): raw_payload["ratio"] または ["split_ratio"] に float new/old を入れる。
      例: 2:1 forward split → 2.0 / 3:2 → 1.5 / 1:10 reverse split・併合 → 0.1。
    取得総額を不変に保つため、各 open lot は qty×ratio / price÷ratio で調整する。

    Codex P1: quantity は比率に流用しない。quantity=100 の比率未指定イベントを 100:1 と
    誤解釈すると 10株@100円→1000株@1円 のように lot が壊れる。ratio/split_ratio が無ければ
    None を返し、build_lots 側で fail-loud (ValueError) にする。
    """
    payload = ev.get("raw_payload")
    if isinstance(payload, str) and payload:
        try:
            payload = json.loads(payload)
        except Exception:
            payload = {}
    if not isinstance(payload, dict):
        payload = {}
    r = payload.get("ratio")
    if r is None:
        r = payload.get("split_ratio")
    if r is None:
        return None
    try:
        r = float(r)
    except (TypeError, ValueError):
        return None
    return r if r > 0 else None


def build_lots(
    ticker: str,
    *,
    db_path: Optional[Path] = None,
    until: Optional[str] = None,
) -> TaxLotState:
    """
    特定 ticker の lot timeline を event_ledger から再構築する。

    Args:
        ticker:   銘柄
        until:    ISO datetime / date (inclusive). この時点までの events で組み立てる。
                  None なら全期間。
    """
    from event_ledger import query_events

    events = query_events(
        ticker=ticker,
        types=["trade", "split", "merge"],
        date_to=(until + "T23:59:59" if until and "T" not in until else until),
        db_path=db_path,
    )

    state = TaxLotState()

    for ev in events:
        etype = (ev.get("event_type") or "").lower()

        # 株式分割 / 併合 (Codex P1 #1 follow-up): その時点で開いている lot の数量と単価を
        # 比率で調整する。取得総額 (remaining_qty × cost_per_share) は不変。
        # split_ratio = 旧1株あたり新株数 (2:1 forward → 2.0 / 1:10 reverse・併合 → 0.1)。
        if etype in ("split", "merge"):
            ratio = _split_ratio(ev)
            if ratio is None or ratio <= 0:
                raise ValueError(
                    f"{ticker} の {etype} event に有効な split_ratio がありません "
                    f"(event_id={ev.get('event_id')})。raw_payload.split_ratio (旧1株あたり新株数) を指定してください。"
                )
            for lot in state.open_lots:
                if lot.remaining_qty <= 1e-12:
                    continue  # 消費済み lot は調整不要 (realized は分割前に確定済み)
                lot.initial_qty        *= ratio
                lot.remaining_qty      *= ratio
                lot.cost_per_share     /= ratio
                lot.cost_per_share_jpy /= ratio
            continue

        direction = (ev.get("direction") or "").lower()
        qty       = float(ev.get("quantity") or 0)
        price     = float(ev.get("price") or 0)
        currency  = (ev.get("currency") or "JPY").upper()
        fx        = ev.get("fx_rate_usdjpy")
        if qty <= 0 or price <= 0:
            continue

        purchase_date = (ev.get("occurred_at") or "")[:10]

        if direction == "buy":
            if currency == "USD" and (fx is None or float(fx) <= 0):
                raise ValueError(f"{ticker} の USD BUY event に fx_rate_usdjpy がありません")
            cps_jpy = price * float(fx) if currency == "USD" and fx else price
            lot = Lot(
                lot_id            = ev.get("event_id") or f"lot_{len(state.open_lots)}",
                ticker            = ticker,
                purchase_date     = purchase_date,
                initial_qty       = qty,
                remaining_qty     = qty,
                cost_per_share    = price,
                currency          = currency,
                cost_per_share_jpy = cps_jpy,
                account           = ev.get("account"),
            )
            state.open_lots.append(lot)

        elif direction == "sell":
            # FIFO で消費 (open_lots は購入日昇順想定。event_ledger は occurred_at ASC で取得済み)
            remaining_to_sell = qty
            # Codex P1 #1: 口座をまたいだ lot 消費は禁止 (特定口座の SELL が NISA lot を
            # 食う等の誤りを防ぐ)。同一 account の lot のみを FIFO (購入日昇順) で消費する。
            sell_account = ev.get("account")
            same_account = [l for l in state.open_lots if l.is_open and l.account == sell_account]

            for lot in same_account:
                if remaining_to_sell <= 1e-9:
                    break
                consume = min(lot.remaining_qty, remaining_to_sell)
                if consume <= 1e-9:
                    continue
                cost_jpy = consume * lot.cost_per_share_jpy
                proc_jpy = _proceeds_jpy(price=price, qty=consume, currency=currency, fx=fx)
                state.realized_trades.append(RealizedTrade(
                    sell_date          = purchase_date,
                    ticker             = ticker,
                    lot_id             = lot.lot_id,
                    quantity           = consume,
                    sell_price         = price,
                    sell_fx            = float(fx) if fx else None,
                    cost_per_share_jpy = lot.cost_per_share_jpy,
                    proceeds_jpy       = round(proc_jpy, 2),
                    cost_basis_jpy     = round(cost_jpy, 2),
                    realized_jpy       = round(proc_jpy - cost_jpy, 2),
                    account            = lot.account,
                ))
                lot.remaining_qty -= consume
                remaining_to_sell -= consume

            # Codex P1 #1: lot 不足を黙って捨てない。同一口座で売却数量を賄えない =
            # event_ledger の整合性問題として fail-loud。
            # (口座違いの売却 / BUY の取りこぼし / split・merge event の欠落・比率誤りの可能性。
            #  split/merge は build_lots 内で cost-basis 調整済みなので、ここに来るのは
            #  「分割 event が ledger に記録されていない」等の真の不整合。)
            if remaining_to_sell > 1e-9:
                raise ValueError(
                    f"{ticker} の SELL ({qty}株 @ {purchase_date}, account={sell_account!r}) を "
                    f"同一口座の lot で賄えません (不足 {remaining_to_sell:.6g}株)。"
                    " event_ledger の取引整合性を確認してください。"
                )

    # 完全消費された lot は open_lots から落とさず remaining_qty=0 のまま残す (audit のため)
    return state


# ============================================================
# Solver: sell lot recommendation
# ============================================================

SELL_MODES = {"fifo", "lifo", "loss_harvest", "gain_minimize"}


def recommend_sell_lots(
    ticker: str,
    quantity: float,
    *,
    current_price: float,
    current_fx: Optional[float] = None,
    currency: str = "JPY",
    mode: str = "fifo",
    account_filter: Optional[str] = None,
    db_path: Optional[Path] = None,
    until: Optional[str] = None,
) -> dict:
    """
    指定 ticker から quantity 株を売却する場合の、どの lot をどの順で潰すかの推奨。

    Args:
        quantity:        売却したい数量
        current_price:   現在価格 (proceeds 推定に使う)
        current_fx:      USD なら USDJPY 必要
        mode:            'fifo' (古い順) | 'lifo' (新しい順) |
                         'loss_harvest' (含み損大きい順) | 'gain_minimize' (含み益小さい順)
        account_filter:  '特定' のみ / 'NISA成長投資枠' を除外 など

    Returns:
        {
          'requested_qty': float,
          'plan': [{lot_id, quantity, cost_per_share_jpy, est_realized_jpy, account, ...}, ...],
          'total_realized_jpy': float,
          'unfulfilled_qty':  float,  # lot 不足分
          'mode':             str,
        }
    """
    if mode not in SELL_MODES:
        raise ValueError(f"unknown mode: {mode}. allowed: {sorted(SELL_MODES)}")
    if quantity <= 0:
        raise ValueError("quantity must be positive")

    state = build_lots(ticker, db_path=db_path, until=until)
    candidates = [l for l in state.open_lots if l.is_open]
    if account_filter is not None:
        candidates = [l for l in candidates if l.account == account_filter]

    # current_price を JPY 換算した「lot 1 株あたりの含み損益 (JPY)」で並べ替え
    cur_price_jpy = current_price * (current_fx or 1.0) if currency.upper() == "USD" else current_price

    def unrealized_per_share_jpy(lot: Lot) -> float:
        return cur_price_jpy - lot.cost_per_share_jpy

    if mode == "fifo":
        ordered = sorted(candidates, key=lambda l: l.purchase_date)
    elif mode == "lifo":
        ordered = sorted(candidates, key=lambda l: l.purchase_date, reverse=True)
    elif mode == "loss_harvest":
        # 含み損 (per share) が小さい = より損 → 先に売る
        ordered = sorted(candidates, key=unrealized_per_share_jpy)
    elif mode == "gain_minimize":
        # 含み益 (per share) を最小化したい → 含み損 or 益が小さい順
        # = 単純に unrealized 昇順 (loss_harvest と本質同じだが、利益発生時は最小利益を選ぶ)
        ordered = sorted(candidates, key=unrealized_per_share_jpy)

    plan = []
    remaining = quantity
    total_realized = 0.0

    for lot in ordered:
        if remaining <= 1e-9:
            break
        consume = min(lot.remaining_qty, remaining)
        if consume <= 1e-9:
            continue
        est_realized = consume * (cur_price_jpy - lot.cost_per_share_jpy)
        total_realized += est_realized
        plan.append({
            "lot_id":             lot.lot_id,
            "ticker":             lot.ticker,
            "purchase_date":      lot.purchase_date,
            "account":            lot.account,
            "quantity":           round(consume, 6),
            "cost_per_share_jpy": round(lot.cost_per_share_jpy, 4),
            "est_realized_jpy":   round(est_realized, 2),
        })
        remaining -= consume

    return {
        "requested_qty":      quantity,
        "plan":               plan,
        "total_realized_jpy": round(total_realized, 2),
        "unfulfilled_qty":    round(max(remaining, 0.0), 6),
        "mode":               mode,
    }


# ============================================================
# Year-level realized P&L aggregation
# ============================================================

def realized_pnl_in_year(
    year: int,
    *,
    tickers: Optional[List[str]] = None,
    db_path: Optional[Path] = None,
) -> dict:
    """
    指定年内に確定した実現損益サマリ (確定申告用)。

    Args:
        tickers: 指定なら対象銘柄のみ、None なら event_ledger に出てくる全銘柄

    Returns:
        {
          'year':          int,
          'realized_jpy':  float (年内合計),
          'by_account':    {account: jpy, ...},
          'by_ticker':     {ticker: jpy, ...},
          'trade_count':   int,
        }
    """
    from event_ledger import query_events

    # 対象銘柄を確定
    if tickers is None:
        all_events = query_events(types=["trade"], db_path=db_path)
        tickers = sorted({(ev.get("ticker") or "") for ev in all_events if ev.get("ticker")})

    total = 0.0
    by_account: Dict[str, float] = {}
    by_ticker: Dict[str, float] = {}
    trade_count = 0

    year_from = f"{year}-01-01"
    year_to   = f"{year}-12-31"

    for ticker in tickers:
        state = build_lots(ticker, db_path=db_path, until=year_to)
        for rt in state.realized_trades:
            if rt.sell_date < year_from or rt.sell_date > year_to:
                continue
            total += rt.realized_jpy
            by_ticker[ticker] = by_ticker.get(ticker, 0.0) + rt.realized_jpy
            acc = rt.account or "(unknown)"
            by_account[acc] = by_account.get(acc, 0.0) + rt.realized_jpy
            trade_count += 1

    return {
        "year":         year,
        "realized_jpy": round(total, 2),
        "by_account":   {k: round(v, 2) for k, v in by_account.items()},
        "by_ticker":    {k: round(v, 2) for k, v in by_ticker.items()},
        "trade_count":  trade_count,
    }


# ============================================================
# Snapshot of all open lots (for UI / audit)
# ============================================================

def portfolio_lot_snapshot(
    *,
    tickers: Optional[List[str]] = None,
    db_path: Optional[Path] = None,
) -> dict:
    """全銘柄の open lots を一覧化する (audit / UI 用)。"""
    from event_ledger import query_events

    if tickers is None:
        all_events = query_events(types=["trade"], db_path=db_path)
        tickers = sorted({(ev.get("ticker") or "") for ev in all_events if ev.get("ticker")})

    lots_by_ticker: Dict[str, List[dict]] = {}
    for t in tickers:
        state = build_lots(t, db_path=db_path)
        opens = [l for l in state.open_lots if l.is_open]
        lots_by_ticker[t] = [
            {
                "lot_id":             l.lot_id,
                "purchase_date":      l.purchase_date,
                "remaining_qty":      round(l.remaining_qty, 6),
                "cost_per_share":     l.cost_per_share,
                "cost_per_share_jpy": round(l.cost_per_share_jpy, 4),
                "currency":           l.currency,
                "account":            l.account,
                "remaining_cost_jpy": round(l.remaining_cost_jpy, 2),
            }
            for l in opens
        ]
    return {"lots": lots_by_ticker}


# ============================================================
# Stage 5A: CostBasisEstimate — 総平均法に準ずる方法との dual-run 比較
# ============================================================
#
# 単一の計算結果を強制しない: 消費先ごとに入力の権威が違う。証券会社の
# 値が使えるならそれが税務表示の権威、台帳計算 (FIFO / 総平均法いずれも)
# は照合値。本節は FIFO (build_lots が既に実装) と「総平均法に準ずる方法」
# の見積り (method="total_average_like") を同じ凍結済み event_ledger
# スナップショットから並行計算し、差分を比較できるようにする。
#
# 副作用は一切無い (action_state.json 書き込み・通知・永続化のいずれも
# 行わない) — Stage 5A の合格条件そのもの。7つの下流消費先
# (tax_harvest_scanner.py / tax_optimizer.py / analyst/__init__.py /
# performance.py / nisa_migration_planner.py / espp_plan_manager.py /
# rebalance_engine.py) への配線は Stage 5B 以降で行う。
#
# 確認のお願い: 実装後の数値は証券会社の年間取引報告書と突き合わせて
# ください。総平均法に準ずる方法には証券会社ごとの計算期間の取り扱い等
# 細目があり、本実装はそれを完全再現するものではありません。

@dataclass(frozen=True)
class CostBasisEstimate:
    """取得原価の1つの見積り。source/method の組で権威度が変わる —
    呼び出し側が単一の「正しい値」として扱わず、reconciled/
    data_quality_issues を必ず確認すること。"""
    amount_jpy: float
    source: str    # "broker_report" | "event_ledger" | "manual_opening"
    method: str    # "broker_reported" | "total_average_like"
    as_of: str
    reconciled: bool = False
    data_quality_issues: tuple[str, ...] = field(default_factory=tuple)


def compute_total_average_cost_basis(
    ticker: str,
    *,
    db_path: Optional[Path] = None,
    until: Optional[str] = None,
) -> Dict[str, CostBasisEstimate]:
    """総平均法に準ずる方法での取得原価を account 別に見積る。

    build_lots() と同じ event_ledger ソース (trade/split/merge, occurred_at
    昇順) を使うが、消費ルールが異なる: FIFO は lot ごとに個別追跡するが、
    総平均法は BUY のたびに
        新平均 = (残数量×旧平均 + 買付数量×買付単価) / (残数量+買付数量)
    で平均を更新し、SELL では平均単価を変えずに残数量だけ減らす
    (総平均法の定義そのもの — 売却は平均を動かさない)。

    データ不整合 (fx 欠落・残高不足の SELL 等) は例外を投げず
    data_quality_issues に記録し、その口座を fail-closed (結果から除外)
    にする — 1銘柄の1口座のデータ不備で他の集計まで壊さない。
    """
    from event_ledger import query_events

    events = query_events(
        ticker=ticker,
        types=["trade", "split", "merge"],
        date_to=(until + "T23:59:59" if until and "T" not in until else until),
        db_path=db_path,
    )

    state: Dict[str, dict] = {}
    excluded_accounts: set = set()
    shared_issues: List[str] = []

    for ev in events:
        etype = (ev.get("event_type") or "").lower()

        if etype in ("split", "merge"):
            ratio = _split_ratio(ev)
            if ratio is None or ratio <= 0:
                shared_issues.append(
                    f"split/merge event {ev.get('event_id')} に有効な比率が無く全account調整をスキップ"
                )
                continue
            for acct_state in state.values():
                if acct_state["qty"] > 1e-9:
                    acct_state["qty"] *= ratio
                    acct_state["avg_cost_jpy"] /= ratio
            continue

        direction = (ev.get("direction") or "").lower()
        qty       = float(ev.get("quantity") or 0)
        price     = float(ev.get("price") or 0)
        currency  = (ev.get("currency") or "JPY").upper()
        fx        = ev.get("fx_rate_usdjpy")
        if qty <= 0 or price <= 0:
            continue
        account = ev.get("account")
        occurred_at = ev.get("occurred_at") or ""

        if direction == "buy":
            if currency == "USD" and (fx is None or float(fx) <= 0):
                excluded_accounts.add(account)
                shared_issues.append(
                    f"account={account!r}: USD BUY event {ev.get('event_id')} に "
                    "fx_rate_usdjpy が無いため除外"
                )
                continue
            cps_jpy = price * float(fx) if currency == "USD" and fx else price
            acct_state = state.setdefault(
                account, {"avg_cost_jpy": 0.0, "qty": 0.0, "last_event_at": occurred_at},
            )
            old_qty, old_avg = acct_state["qty"], acct_state["avg_cost_jpy"]
            new_qty = old_qty + qty
            acct_state["avg_cost_jpy"] = (
                (old_qty * old_avg + qty * cps_jpy) / new_qty if new_qty > 1e-12 else cps_jpy
            )
            acct_state["qty"] = new_qty
            acct_state["last_event_at"] = occurred_at

        elif direction == "sell":
            acct_state = state.get(account)
            if acct_state is None or acct_state["qty"] < qty - 1e-9:
                excluded_accounts.add(account)
                shared_issues.append(
                    f"account={account!r}: SELL event {ev.get('event_id')} ({qty}株) を賄う"
                    "残高が総平均法計算に見つからないため除外"
                )
                continue
            acct_state["qty"] -= qty  # 総平均法の定義: 売却は平均単価を変えない
            acct_state["last_event_at"] = occurred_at

    result: Dict[str, CostBasisEstimate] = {}
    for account, acct_state in state.items():
        if account in excluded_accounts or acct_state["qty"] <= 1e-9:
            continue
        result[account] = CostBasisEstimate(
            amount_jpy=round(acct_state["qty"] * acct_state["avg_cost_jpy"], 2),
            source="event_ledger",
            method="total_average_like",
            as_of=acct_state["last_event_at"] or datetime.now().isoformat(),
            reconciled=False,
            data_quality_issues=tuple(shared_issues),
        )
    return result


def compare_old_new_cost_basis(
    ticker: str,
    *,
    db_path: Optional[Path] = None,
    until: Optional[str] = None,
) -> dict:
    """FIFO (既存 build_lots) と総平均法に準ずる方法 (新) の取得原価を、
    同じ凍結済み event_ledger スナップショットから account 別に比較する。

    副作用なし・永続化なし — 純粋関数。呼び出し側 (compute_tax_harvest 等)
    がこの結果をどう使うか (レポート出力 / 何もしない) を別途決める。
    """
    fifo_state = build_lots(ticker, db_path=db_path, until=until)
    fifo_by_account: Dict[str, float] = {}
    for lot in fifo_state.open_lots:
        if lot.remaining_qty > 1e-9:
            fifo_by_account[lot.account] = (
                fifo_by_account.get(lot.account, 0.0) + lot.remaining_cost_jpy
            )

    total_avg_by_account = compute_total_average_cost_basis(ticker, db_path=db_path, until=until)

    accounts = sorted({str(a) for a in fifo_by_account} | {str(a) for a in total_avg_by_account}, key=str)
    rows = []
    for account in accounts:
        fifo_amount = fifo_by_account.get(account)
        estimate = total_avg_by_account.get(account)
        total_avg_amount = estimate.amount_jpy if estimate else None
        rows.append({
            "ticker": ticker,
            "account": account,
            "fifo_cost_basis_jpy": round(fifo_amount, 2) if fifo_amount is not None else None,
            "total_average_cost_basis_jpy": total_avg_amount,
            "diff_jpy": (
                round(total_avg_amount - fifo_amount, 2)
                if (total_avg_amount is not None and fifo_amount is not None) else None
            ),
            "total_average_data_quality_issues": list(estimate.data_quality_issues) if estimate else [],
        })

    return {
        "ticker": ticker,
        "rows": rows,
        "compared_at": datetime.now().isoformat(),
        "note": (
            "total_average_cost_basis_jpy は method='total_average_like' の見積りです。"
            "証券会社の年間取引報告書と必ず突き合わせてください。"
        ),
    }
