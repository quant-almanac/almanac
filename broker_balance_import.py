"""
broker_balance_import.py — ブローカー残高スナップショットの取り込み (4 モード明示)

楽天証券の assetbalance(all)_*.csv と、手入力/スクショ由来の SBI 円残高を使って、
account.json / holdings.json の現金ミラーを同期する。

差分の意味付けは --mode で明示する (Codex 2026-05-17 P0):

  --mode reset (default)
    既存挙動。ブローカー残高に内部台帳を合わせるだけの補正。
    event_ledger には event_type='reconcile' を audit として 1 件記録 (TWR 中立)。

  --mode external_deposit
    銀行など外部からの入金。差分は外部から入った資金として扱う。
    event_ledger に event_type='cash_flow' direction='in' を通貨別に記録 (TWR で controlled out)。

  --mode external_withdraw
    外部への引き出し。差分は外部へ出した資金として扱う。
    event_ledger に event_type='cash_flow' direction='out' を通貨別に記録。

  --mode internal_transfer
    SBI→楽天 など、管理対象口座内の移動。net delta が 0 に近いこと (許容 ¥1,000) を check し、
    event_ledger に event_type='internal_transfer' を audit として 1 件記録 (TWR 中立)。
    NAV 合算は変化しないことを assert する。

使い方:
  python broker_balance_import.py --rakuten-csv ~/Downloads/assetbalance.csv --sbi-jpy 195151
  python broker_balance_import.py --rakuten-csv ~/Downloads/assetbalance.csv --sbi-jpy 195151 --apply
  python broker_balance_import.py --rakuten-csv ~/Downloads/assetbalance.csv --sbi-jpy 0 --mode internal_transfer --apply
"""
from __future__ import annotations

import argparse
import csv
import hashlib
import json
import os
import re
import time
import uuid
from copy import deepcopy
from datetime import datetime
from pathlib import Path
from typing import Optional

from utils import atomic_write_json, load_json_strict, process_lock

BASE_DIR = Path(__file__).parent
ACCOUNT_FILE = BASE_DIR / "account.json"
HOLDINGS_FILE = BASE_DIR / "holdings.json"
RECONCILE_LOG = BASE_DIR / "broker_balance_reconcile_log.jsonl"
# Codex P1 #9: prepare/commit journal。account→holdings→ledger→log の逐次書込みが
# 途中で落ちると JSON と台帳が乖離するため、apply を prepared→committed で挟む。
JOURNAL_FILE = BASE_DIR / "broker_balance_journal.jsonl"


def _num(value, *, default: Optional[float] = None) -> Optional[float]:
    if value is None:
        return default
    s = str(value).replace(",", "").replace("+", "").strip()
    s = s.replace(" USD", "").replace("円", "").replace("円/USD", "").strip()
    if not s or s == "-":
        return default
    try:
        return float(s)
    except ValueError:
        return default


def _read_csv_rows(path: Path) -> list[list[str]]:
    text = None
    for enc in ("utf-8-sig", "utf-8", "cp932", "shift_jis"):
        try:
            text = path.read_text(encoding=enc)
            break
        except UnicodeDecodeError:
            continue
    if text is None:
        raise RuntimeError(f"CSV のエンコーディングを判定できません: {path}")
    return list(csv.reader(text.splitlines()))


def _date_from_filename(path: Path) -> Optional[str]:
    m = re.search(r"_(\d{8})_\d{6}\.csv$", path.name)
    if not m:
        return None
    raw = m.group(1)
    return f"{raw[:4]}-{raw[4:6]}-{raw[6:8]}"


def _fx_as_of_from_rows(rows: list[list[str]], fallback_date: Optional[str]) -> Optional[float]:
    year = int((fallback_date or datetime.now().date().isoformat())[:4])
    for row in rows:
        if len(row) >= 4 and row[0] == "米ドル":
            m = re.search(r"\((\d{2})/(\d{2})\s+(\d{2}):(\d{2})\)", row[3])
            if not m:
                return None
            month, day, hour, minute = map(int, m.groups())
            return datetime(year, month, day, hour, minute).timestamp()
    return None


def _usd_cash_jpy_from_usd_fx(usd_cash: float, fx_rate: float) -> int:
    return int(round(float(usd_cash) * float(fx_rate)))


def _cash_total_jpy(jpy_cash: float, usd_cash_jpy: float) -> int:
    return int(round(float(jpy_cash) + float(usd_cash_jpy)))


def parse_rakuten_asset_balance(path: Path) -> dict:
    """楽天 assetbalance(all)_*.csv から現金・外貨MMFの残高を抽出する。"""
    rows = _read_csv_rows(path)
    as_of = _date_from_filename(path)
    parsed: dict = {
        "source": str(path),
        "as_of": as_of,
        "rakuten_jpy_cash": None,
        "rakuten_usd_cash": None,
        "rakuten_usd_cash_jpy": None,
        "rakuten_cash_total_jpy": None,
        "fx_rate_usdjpy": None,
        "fx_rate_usdjpy_as_of": _fx_as_of_from_rows(rows, as_of),
        "gs_mmf_usd_value": None,
        "gs_mmf_jpy_value": None,
    }

    for row in rows:
        if not row:
            continue
        label = row[0]
        if label == "預り金" and len(row) > 1:
            parsed["rakuten_jpy_cash"] = _num(row[1])
        elif label == "外貨預り金" and len(row) > 14:
            # summary row と detail row がある。detail row は row[2] == 米ドル。
            if len(row) > 4 and row[2] == "米ドル":
                parsed["rakuten_usd_cash"] = _num(row[4])
                parsed["fx_rate_usdjpy"] = _num(row[8])
                parsed["rakuten_usd_cash_jpy"] = _num(row[14])
            else:
                parsed["rakuten_usd_cash_jpy"] = parsed["rakuten_usd_cash_jpy"] or _num(row[1])
        elif label == "預り金合計" and len(row) > 1:
            parsed["rakuten_cash_total_jpy"] = _num(row[1])
        elif label == "外貨建MMF" and len(row) > 15:
            parsed["gs_mmf_jpy_value"] = _num(row[14])
            parsed["gs_mmf_usd_value"] = _num(row[15])
        elif label == "米ドル" and len(row) > 1:
            parsed["fx_rate_usdjpy"] = parsed["fx_rate_usdjpy"] or _num(row[1])

    required = ["rakuten_jpy_cash", "rakuten_usd_cash", "fx_rate_usdjpy"]
    missing = [k for k in required if parsed.get(k) is None]
    if missing:
        raise ValueError(f"楽天CSVから必須項目を抽出できません: {missing}")

    parsed["rakuten_jpy_cash"] = int(parsed["rakuten_jpy_cash"])
    parsed["rakuten_usd_cash"] = round(float(parsed["rakuten_usd_cash"]), 2)
    parsed["fx_rate_usdjpy"] = float(parsed["fx_rate_usdjpy"])

    computed_usd_jpy = _usd_cash_jpy_from_usd_fx(parsed["rakuten_usd_cash"], parsed["fx_rate_usdjpy"])
    raw_usd_jpy = parsed.get("rakuten_usd_cash_jpy")
    if raw_usd_jpy is None or abs(float(raw_usd_jpy) - computed_usd_jpy) > max(1_000, computed_usd_jpy * 0.005):
        parsed["rakuten_usd_cash_jpy"] = computed_usd_jpy
    else:
        parsed["rakuten_usd_cash_jpy"] = int(raw_usd_jpy)

    computed_total = _cash_total_jpy(parsed["rakuten_jpy_cash"], parsed["rakuten_usd_cash_jpy"])
    raw_total = parsed.get("rakuten_cash_total_jpy")
    if raw_total is None or abs(float(raw_total) - computed_total) > max(1_000, computed_total * 0.005):
        parsed["rakuten_cash_total_jpy"] = computed_total
    else:
        parsed["rakuten_cash_total_jpy"] = int(raw_total)
    return parsed


def _cash_subset(account: dict, holdings: dict) -> dict:
    return {
        "account": {
            "balance": account.get("balance"),
            "usd_balance": account.get("usd_balance"),
            "fx_rate_usdjpy": account.get("fx_rate_usdjpy"),
            "jpy_equivalent_usd": account.get("jpy_equivalent_usd"),
            "total_cash": account.get("total_cash"),
            "last_updated": account.get("last_updated"),
        },
        "holdings": {
            key: (holdings.get(key) or {}).get("shares")
            for key in ("CASH_JPY", "CASH_USD", "CASH_JPY_SBI", "GS_MMF_USD")
        },
    }


def build_reconciled_state(
    *,
    rakuten: dict,
    sbi_jpy: Optional[float] = None,
    sbi_note: Optional[str] = None,
    update_mmf: bool = True,
    account_path: Optional[Path] = None,
    holdings_path: Optional[Path] = None,
) -> tuple[dict, dict, dict]:
    # Codex P2 #9: パスは呼出時にモジュール globals から解決する。デフォルト引数で
    # import 時の Path を束縛すると monkeypatch / 設定差し替えが効かない (テスト不達 + 誤読込)。
    account_path = account_path or ACCOUNT_FILE
    holdings_path = holdings_path or HOLDINGS_FILE
    account = load_json_strict(account_path)
    holdings = load_json_strict(holdings_path)
    if not isinstance(account, dict) or not isinstance(holdings, dict):
        raise ValueError("account.json / holdings.json の形式が不正です")

    next_account = deepcopy(account)
    next_holdings = deepcopy(holdings)

    next_account["balance"] = int(rakuten["rakuten_jpy_cash"])
    next_account["usd_balance"] = round(float(rakuten["rakuten_usd_cash"]), 2)
    next_account["fx_rate_usdjpy"] = float(rakuten["fx_rate_usdjpy"])
    next_account["jpy_equivalent_usd"] = _usd_cash_jpy_from_usd_fx(
        next_account["usd_balance"], next_account["fx_rate_usdjpy"]
    )
    next_account["total_cash"] = _cash_total_jpy(next_account["balance"], next_account["jpy_equivalent_usd"])
    next_account["last_updated"] = rakuten.get("as_of") or datetime.now().date().isoformat()
    if rakuten.get("fx_rate_usdjpy_as_of"):
        next_account["fx_rate_usdjpy_as_of"] = rakuten["fx_rate_usdjpy_as_of"]

    for key in ("CASH_JPY", "CASH_USD"):
        if key not in next_holdings or not isinstance(next_holdings[key], dict):
            raise ValueError(f"holdings.json に {key} がありません")
    next_holdings["CASH_JPY"]["shares"] = int(rakuten["rakuten_jpy_cash"])
    next_holdings["CASH_JPY"]["note"] = f"楽天CSV同期 {rakuten.get('as_of') or ''}".strip()
    next_holdings["CASH_USD"]["shares"] = round(float(rakuten["rakuten_usd_cash"]), 2)
    next_holdings["CASH_USD"]["note"] = f"楽天CSV同期 FX {rakuten['fx_rate_usdjpy']} {rakuten.get('as_of') or ''}".strip()

    if sbi_jpy is not None:
        if "CASH_JPY_SBI" not in next_holdings or not isinstance(next_holdings["CASH_JPY_SBI"], dict):
            raise ValueError("holdings.json に CASH_JPY_SBI がありません")
        next_holdings["CASH_JPY_SBI"]["shares"] = int(sbi_jpy)
        next_holdings["CASH_JPY_SBI"]["note"] = sbi_note or f"SBIスクリーンショット同期 {rakuten.get('as_of') or ''}".strip()

    if update_mmf and rakuten.get("gs_mmf_usd_value") is not None and "GS_MMF_USD" in next_holdings:
        next_holdings["GS_MMF_USD"]["shares"] = round(float(rakuten["gs_mmf_usd_value"]), 2)
        next_holdings["GS_MMF_USD"]["current_nav"] = 1.0
        next_holdings["GS_MMF_USD"]["note"] = f"楽天CSV同期 外貨建MMF {rakuten.get('as_of') or ''}".strip()

    diff = {
        "before": _cash_subset(account, holdings),
        "after": _cash_subset(next_account, next_holdings),
        "rakuten": rakuten,
        "sbi_jpy": sbi_jpy,
    }
    return next_account, next_holdings, diff


VALID_MODES = {"reset", "external_deposit", "external_withdraw", "internal_transfer"}
# internal_transfer の net delta 許容誤差 (端株丸め / 数銭の差を吸収)
INTERNAL_TRANSFER_TOLERANCE_JPY = 1000.0


def _compute_cash_deltas(before: dict, after: dict) -> dict:
    """
    before / after の `_cash_subset` から、各通貨・口座の delta を計算する。
    Returns:
        {
            "delta_jpy_rakuten": float,        # account.balance の差
            "delta_usd_rakuten": float,        # account.usd_balance の差
            "delta_jpy_sbi":     float,        # holdings.CASH_JPY_SBI.shares の差
            "delta_jpy_total":   float,        # 楽天 JPY + SBI JPY の合計差
            "delta_usd_total":   float,
            "fx_rate":           float,        # before の FX (USD→JPY 概算用)
            "net_delta_jpy_equivalent": float, # JPY 合算 + USD×FX で総資産差を見る
        }
    """
    acc_before = before.get("account") or {}
    acc_after  = after.get("account") or {}
    h_before   = before.get("holdings") or {}
    h_after    = after.get("holdings") or {}

    def _f(v):
        try:
            return float(v) if v is not None else 0.0
        except (TypeError, ValueError):
            return 0.0

    fx_rate = _f(acc_after.get("fx_rate_usdjpy") or acc_before.get("fx_rate_usdjpy") or 150.0)
    delta_jpy_rakuten = _f(acc_after.get("balance")) - _f(acc_before.get("balance"))
    delta_usd_rakuten = _f(acc_after.get("usd_balance")) - _f(acc_before.get("usd_balance"))
    delta_jpy_sbi = _f(h_after.get("CASH_JPY_SBI")) - _f(h_before.get("CASH_JPY_SBI"))
    delta_jpy_total = delta_jpy_rakuten + delta_jpy_sbi
    delta_usd_total = delta_usd_rakuten  # SBI USD は管理対象外
    return {
        "delta_jpy_rakuten": round(delta_jpy_rakuten, 2),
        "delta_usd_rakuten": round(delta_usd_rakuten, 2),
        "delta_jpy_sbi":     round(delta_jpy_sbi, 2),
        "delta_jpy_total":   round(delta_jpy_total, 2),
        "delta_usd_total":   round(delta_usd_total, 2),
        "fx_rate":           fx_rate,
        "net_delta_jpy_equivalent": round(delta_jpy_total + delta_usd_total * fx_rate, 2),
    }


def _build_ledger_events_for_mode(*, mode: str, diff: dict, deltas: dict,
                                  occurred_at: str) -> list[dict]:
    """
    mode に応じて event_ledger に append する event の引数リストを返す。
    呼出側は append_event(**kwargs) でそのまま記録できる。
    raw_payload に diff/deltas/mode を audit として残す。
    """
    base_payload = {
        "broker_balance_import": True,
        "mode": mode,
        "deltas": deltas,
        "rakuten_as_of": diff.get("rakuten", {}).get("as_of"),
        "sbi_jpy_after": diff.get("sbi_jpy"),
    }
    base_event_id_prefix = f"bbi_{mode}_{occurred_at.replace(':', '').replace('-', '').replace('T', '_')}"

    events: list[dict] = []

    if mode == "reset":
        # audit のみ (TWR 中立)
        events.append({
            "event_type":  "reconcile",
            "occurred_at": occurred_at,
            "source":      "broker_import",
            "note":        f"broker_balance reset reconcile (rakuten {diff.get('rakuten', {}).get('as_of', '')})",
            "raw_payload": dict(base_payload),
            "event_id":    f"{base_event_id_prefix}_reconcile",
        })
        return events

    if mode == "internal_transfer":
        # net delta ≈ 0 を assert (audit のみ TWR 中立)
        net = abs(float(deltas.get("net_delta_jpy_equivalent") or 0.0))
        if net > INTERNAL_TRANSFER_TOLERANCE_JPY:
            raise ValueError(
                f"internal_transfer mode で net delta が大きすぎます (¥{net:,.0f} > tolerance ¥{INTERNAL_TRANSFER_TOLERANCE_JPY:,.0f})。"
                " 本当に内部移動なら --mode external_deposit/external_withdraw / reset を検討してください。"
            )
        payload = dict(base_payload)
        payload["net_delta_jpy_equivalent"] = deltas.get("net_delta_jpy_equivalent")
        events.append({
            "event_type":  "internal_transfer",
            "occurred_at": occurred_at,
            "source":      "broker_import",
            "note":        f"internal transfer (SBI⇄楽天 等), net ≈ ¥{deltas.get('net_delta_jpy_equivalent', 0):+,.0f}",
            "raw_payload": payload,
            "event_id":    f"{base_event_id_prefix}_internal",
        })
        return events

    if mode in ("external_deposit", "external_withdraw"):
        # 通貨ごとに cash_flow event を作る (TWR で controlled out)
        expected_sign = 1.0 if mode == "external_deposit" else -1.0
        direction     = "in"   if mode == "external_deposit" else "out"

        for currency, key in (("JPY", "delta_jpy_total"), ("USD", "delta_usd_total")):
            d = float(deltas.get(key) or 0.0)
            if abs(d) < 1e-6:
                continue
            # 符号矛盾チェック (deposit で d<0 / withdraw で d>0 → error)
            if d * expected_sign < 0:
                raise ValueError(
                    f"{mode} mode で {currency} 残高の動きが逆方向です ({key}={d:+.2f})。"
                    " 反対方向の mode、または reset の使用を検討してください。"
                )
            abs_amount = abs(d)
            # cash_flow event は currency 単位の残高動きを quantity=金額, price=1.0 で表現
            payload = dict(base_payload)
            payload["currency"] = currency
            payload["delta"] = d
            events.append({
                "event_type":     "cash_flow",
                "occurred_at":    occurred_at,
                "direction":      direction,
                "quantity":       abs_amount,
                "price":          1.0,
                "currency":       currency,
                "fx_rate_usdjpy": deltas.get("fx_rate") if currency == "USD" else None,
                "source":         "broker_import",
                "note":           f"{mode} (broker_balance) {currency} {direction} ¥/$ {abs_amount:,.2f}",
                "raw_payload":    payload,
                "event_id":       f"{base_event_id_prefix}_{currency}",
            })

        if not events:
            # 差分ゼロでも明示的に何も記録しないのは正しい (apply の中で no-op)
            pass

        return events

    raise ValueError(f"unknown mode: {mode}. allowed: {sorted(VALID_MODES)}")


def _append_ledger_events(events: list[dict]) -> list[dict]:
    """append_event をまとめて呼ぶ。fail-loud。"""
    if not events:
        return []
    from event_ledger import append_event
    results = []
    for ev in events:
        results.append(append_event(**ev))
    return results


def _read_last_journal_record() -> Optional[dict]:
    """JOURNAL_FILE 上、最後に出現した operation の最新レコードを返す。"""
    if not JOURNAL_FILE.exists():
        return None
    last_by_op: dict[str, dict] = {}
    order: list[str] = []
    try:
        for line in JOURNAL_FILE.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                rec = json.loads(line)
            except Exception:
                continue
            op = rec.get("operation_id")
            if not op:
                continue
            if op not in last_by_op:
                order.append(op)
            last_by_op[op] = rec
    except Exception:
        return None
    return last_by_op[order[-1]] if order else None


def _assert_no_incomplete_journal() -> None:
    """前回 apply が prepared のまま (=途中失敗) なら fail-closed で停止する。"""
    last = _read_last_journal_record()
    if last is not None and last.get("status") == "prepared":
        raise RuntimeError(
            "前回の broker_balance_import apply が完了していません "
            f"(operation_id={last.get('operation_id')}, prepared @ {last.get('timestamp')})。"
            " account.json / holdings.json / event_ledger が部分反映の可能性があります。"
            " 内容を検証し、解消後に JOURNAL の該当 operation を committed 化 (または該当行削除)"
            f" してから再実行してください。JOURNAL: {JOURNAL_FILE}"
        )


def _append_journal(record: dict) -> None:
    rec = {**record,
           "timestamp": record.get("timestamp") or datetime.now().isoformat(timespec="seconds")}
    with JOURNAL_FILE.open("a", encoding="utf-8") as f:
        f.write(json.dumps(rec, ensure_ascii=False) + "\n")
        f.flush()
        os.fsync(f.fileno())


def _operation_id(*, mode: str, rakuten: dict, sbi_jpy: Optional[float]) -> str:
    """入力スナップショット (mode + 楽天CSV正規化 + SBI現金) から決定論 operation_id を作る。

    Codex P2 #9: UUID だと再実行が別 op 扱いになり、event_id も変わってリプレイで二重計上する。
    同一入力 → 同一 op_id → 同一 event_id にして idempotent replay を成立させる。
    """
    key = json.dumps(
        {"mode": mode, "rakuten": rakuten, "sbi_jpy": sbi_jpy},
        sort_keys=True, ensure_ascii=False, default=str,
    )
    return hashlib.sha256(key.encode("utf-8")).hexdigest()[:16]


def _apply_plan(plan: dict, *, diff: Optional[dict] = None) -> list:
    """prepared(全書込み内容を記録) → account/holdings/ledger/log → committed を順に実行。

    各書込みは idempotent (JSON は full overwrite、ledger は決定論 event_id で dedup) なので、
    どのステップで落ちても同じ plan の再適用で完了状態に収束できる。
    """
    op = plan["operation_id"]
    _append_journal({
        "operation_id": op,
        "status": "prepared",
        "mode": plan.get("mode"),
        "plan": plan,  # next_account / next_holdings / ledger_events を含む (resume 用)
    })
    atomic_write_json(ACCOUNT_FILE, plan["next_account"])
    atomic_write_json(HOLDINGS_FILE, plan["next_holdings"])
    ledger_results = _append_ledger_events(plan["ledger_events"])
    log_entry = {
        "timestamp": datetime.now().isoformat(timespec="seconds"),
        "operation_id": op,
        "kind": f"broker_balance_{plan.get('mode')}",
        "diff": diff if diff is not None else plan.get("diff"),
        "ledger_events": ledger_results,
    }
    with RECONCILE_LOG.open("a", encoding="utf-8") as f:
        f.write(json.dumps(log_entry, ensure_ascii=False) + "\n")
    _append_journal({"operation_id": op, "status": "committed", "mode": plan.get("mode")})
    return ledger_results


def _resume_incomplete_journal() -> None:
    """前回 apply が prepared のまま (=途中失敗) なら、記録済み plan を idempotent に再適用して完了させる。

    Codex P2 #9: 旧実装は検知して停止するだけで、JSON 更新後・ledger 書込み前に落ちると
    再実行時 diff=0 となり cash_flow が永久に欠落し得た。記録済み plan を再適用すれば、
    意図した最終状態 (account/holdings/ledger) に確実に収束する。
    """
    last = _read_last_journal_record()
    if last is None or last.get("status") != "prepared":
        return
    plan = last.get("plan")
    if not isinstance(plan, dict) or "next_account" not in plan:
        # plan を持たない旧 journal は自動 resume 不能 → 手動対応を促す。
        raise RuntimeError(
            "未完了 journal に plan が無く自動 resume できません "
            f"(operation_id={last.get('operation_id')})。手動で台帳を確認し、"
            f" JOURNAL の該当行を解消してください: {JOURNAL_FILE}"
        )
    _apply_plan(plan)


def apply_reconcile(
    *,
    rakuten_csv: Path,
    sbi_jpy: Optional[float] = None,
    sbi_note: Optional[str] = None,
    update_mmf: bool = True,
    apply: bool = False,
    mode: str = "reset",
) -> dict:
    if mode not in VALID_MODES:
        raise ValueError(f"unknown mode: {mode}. allowed: {sorted(VALID_MODES)}")

    rakuten = parse_rakuten_asset_balance(rakuten_csv)
    occurred_at = datetime.now().isoformat(timespec="seconds")

    with process_lock("portfolio_ledger"):
        # Codex P2 #9: load → 判定 → 書込み → journal を同一 lock 内で行う。
        # まず前回未完了 op があれば記録済み plan で resume してから新規 op を構築する。
        if apply:
            _resume_incomplete_journal()

        next_account, next_holdings, diff = build_reconciled_state(
            rakuten=rakuten,
            sbi_jpy=sbi_jpy,
            sbi_note=sbi_note,
            update_mmf=update_mmf,
        )
        deltas = _compute_cash_deltas(diff["before"], diff["after"])
        diff["deltas"] = deltas
        diff["mode"]   = mode

        # mode に応じた event_ledger event を先に生成して validation (apply 前に raise する)
        ledger_event_kwargs = _build_ledger_events_for_mode(
            mode=mode, diff=diff, deltas=deltas, occurred_at=occurred_at
        )

        if apply:
            operation_id = _operation_id(mode=mode, rakuten=rakuten, sbi_jpy=sbi_jpy)
            # ledger event_id を決定論 id で上書き (同一入力の再実行は同一 id → idempotent)。
            for i, ev in enumerate(ledger_event_kwargs):
                ev["event_id"] = f"{operation_id}:{i}"
            plan = {
                "operation_id": operation_id,
                "mode": mode,
                "next_account": next_account,
                "next_holdings": next_holdings,
                "ledger_events": ledger_event_kwargs,
            }
            _apply_plan(plan, diff=diff)

    return {
        "dry_run": not apply,
        "mode":    mode,
        "planned_ledger_events": [
            {k: v for k, v in ev.items() if k != "raw_payload"} for ev in ledger_event_kwargs
        ],
        **diff,
    }


def _main() -> None:
    parser = argparse.ArgumentParser(description="Import broker cash balance snapshot")
    parser.add_argument("--rakuten-csv", required=True)
    parser.add_argument("--sbi-jpy", type=float, default=None)
    parser.add_argument("--sbi-note", default=None)
    parser.add_argument("--no-mmf", action="store_true", help="GS_MMF_USD を同期しない")
    parser.add_argument("--apply", action="store_true")
    parser.add_argument(
        "--mode",
        choices=sorted(VALID_MODES),
        default="reset",
        help=(
            "差分の意味付け。reset=実残高に合わせるだけ(TWR中立) / "
            "external_deposit=外部入金(TWR cash_flow) / "
            "external_withdraw=外部出金(TWR cash_flow) / "
            "internal_transfer=SBI⇄楽天など内部移動(net≈0 を assert、TWR中立)"
        ),
    )
    args = parser.parse_args()

    result = apply_reconcile(
        rakuten_csv=Path(args.rakuten_csv),
        sbi_jpy=args.sbi_jpy,
        sbi_note=args.sbi_note,
        update_mmf=not args.no_mmf,
        apply=args.apply,
        mode=args.mode,
    )
    print(json.dumps(result, ensure_ascii=False, indent=2, default=str))
    if not args.apply:
        print("\n[dry-run] 反映するには --apply を付けて再実行してください")


if __name__ == "__main__":
    _main()
