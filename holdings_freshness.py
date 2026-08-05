"""holdings_freshness.py — CSV に依存しない holdings/cash 鮮度アンカーの前進。

背景 (2026-08-05):
``holdings.json`` / ``account.json`` を書くのは楽天CSV取込だけ
(``broker_position_import`` / ``broker_balance_import``) なのに、
``freshness_policy`` は 96h で失効させる。約定を記録しても holdings は
更新されないため、次の自己ロックが起きる:

    4日経過 → snapshot stale → 全候補 review → 発注が出ない
       ↑                                          ↓
       └────── holdings 更新されない ← 約定も記録されない

実際に6営業日連続で ``ready_count == 0`` になった。CSV を毎日出すのは
運用上非現実的なので、アンカーを前進させる経路を2つ用意する。

1. **attestation** — 「証券会社の保有と holdings.json は一致している」と
   利用者が明示的に表明する。CSV不要。表明はその時点の holdings 内容の
   ハッシュに紐づき、内容が変わればその表明はもう効かない (別の内容を
   保証したことにしない)。
2. **roll-forward** — broker確認済みで完全な約定を holdings の数量に適用し、
   約定レコードに ``portfolio_applied`` を立てる。これにより
   ``position_identity.is_complete_broker_confirmed_fill`` が True になり、
   Stage 0B の権威が約定時刻まで前進する (= invalidated の解消)。

どちらも「経過時間だけでは失効させない、既知の後続約定があれば再照合を
求める」という Stage 0B の契約に合わせてあり、安全側の性質は落とさない。
"""
from __future__ import annotations

import argparse
import hashlib
import json
from datetime import datetime
from pathlib import Path
from typing import Optional

from utils import atomic_write_json, load_json, load_json_strict

BASE_DIR = Path(__file__).parent

ATTESTATION_FILE_NAME = "holdings_attestation.json"
HOLDINGS_FILE_NAME = "holdings.json"
ACCOUNT_FILE_NAME = "account.json"
EXECUTION_FILE_NAME = "action_executions.json"

# 表明できる対象。snapshot 側の holdings / cash カテゴリに 1:1 で対応する。
ATTESTABLE_SCOPES = ("holdings", "cash")


def _attestation_path(base_dir: Path) -> Path:
    return Path(base_dir) / ATTESTATION_FILE_NAME


def _scope_source_path(scope: str, base_dir: Path) -> Path:
    if scope == "holdings":
        return Path(base_dir) / HOLDINGS_FILE_NAME
    if scope == "cash":
        return Path(base_dir) / ACCOUNT_FILE_NAME
    raise ValueError(f"unknown attestation scope: {scope}")


def content_hash(path: Path) -> str:
    """analysis_snapshot._file_hash と同じ規約 (sha256 hexdigest)。"""
    try:
        return hashlib.sha256(Path(path).read_bytes()).hexdigest()
    except OSError:
        return ""


def _parse_iso(value: object) -> Optional[datetime]:
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(str(value))
    except (TypeError, ValueError):
        return None
    # snapshot 側は naive local time で比較するため、tz 付きは naive に落とす。
    return parsed.replace(tzinfo=None) if parsed.tzinfo is not None else parsed


def load_attestations(*, base_dir: Path = BASE_DIR) -> list[dict]:
    payload = load_json(_attestation_path(base_dir), default={})
    rows = payload.get("attestations") if isinstance(payload, dict) else None
    return [r for r in rows if isinstance(r, dict)] if isinstance(rows, list) else []


def record_attestation(
    *,
    scope: str,
    note: str = "",
    actor: str = "user",
    base_dir: Path = BASE_DIR,
    now: Optional[datetime] = None,
) -> dict:
    """「この内容で証券会社と一致している」を追記専用で記録する。

    表明時点の内容ハッシュを一緒に残すので、あとで holdings が別経路
    (CSV取込 / roll-forward) で書き換わった場合、その表明は自動的に
    効力を失う。監査上「何を保証したか」が一意に定まる。
    """
    if scope not in ATTESTABLE_SCOPES:
        raise ValueError(f"unknown attestation scope: {scope} (expected one of {ATTESTABLE_SCOPES})")
    source_path = _scope_source_path(scope, base_dir)
    if not source_path.exists():
        raise FileNotFoundError(f"cannot attest a missing source file: {source_path}")

    now = now or datetime.now()
    record = {
        "scope": scope,
        "attested_at": now.isoformat(),
        "actor": actor,
        "note": note or "",
        "source": source_path.name,
        "source_hash": content_hash(source_path),
    }
    rows = load_attestations(base_dir=base_dir)
    rows.append(record)
    atomic_write_json(_attestation_path(base_dir), {"attestations": rows})
    return record


def latest_valid_attestation(
    *, scope: str, base_dir: Path = BASE_DIR
) -> Optional[dict]:
    """現在の内容に対して有効な最新の表明を返す。

    内容ハッシュが一致しない表明は、別の内容に対する保証なので無視する。
    """
    source_path = _scope_source_path(scope, base_dir)
    current = content_hash(source_path)
    if not current:
        return None
    best: Optional[dict] = None
    best_at: Optional[datetime] = None
    for row in load_attestations(base_dir=base_dir):
        if row.get("scope") != scope or row.get("source_hash") != current:
            continue
        at = _parse_iso(row.get("attested_at"))
        if at is None:
            continue
        if best_at is None or at > best_at:
            best, best_at = row, at
    return best


def effective_source_as_of(
    *,
    scope: str,
    file_as_of: Optional[datetime],
    base_dir: Path = BASE_DIR,
) -> tuple[Optional[datetime], str]:
    """ファイル由来の as_of と有効な表明のうち、新しい方を返す。

    Returns: (as_of, source_label)  source_label は "file" か "attestation"。
    """
    attestation = latest_valid_attestation(scope=scope, base_dir=base_dir)
    attested_at = _parse_iso(attestation.get("attested_at")) if attestation else None
    if attested_at is None:
        return file_as_of, "file"
    if file_as_of is None or attested_at > file_as_of:
        return attested_at, "attestation"
    return file_as_of, "file"


# ---------------------------------------------------------------------------
# roll-forward: broker確認済み約定を holdings 数量へ適用する
# ---------------------------------------------------------------------------


def _holding_key_for(row: dict, holdings: dict) -> Optional[str]:
    """約定レコードに対応する holdings のキーを PositionIdentity で解決する。

    holdings のキーは ``AVGO_toku`` のような口座別サフィックス付きなので、
    ticker 直引きでは特定できない。identity 一致で厳密に決める。
    """
    from position_identity import position_identity_for_action

    target = position_identity_for_action(row)
    if target is None:
        return None
    matches = [
        key for key, entry in holdings.items()
        if isinstance(entry, dict) and position_identity_for_action(entry) == target
    ]
    # 1件に定まらない場合は適用しない (曖昧な口座への自動適用は禁止)。
    return matches[0] if len(matches) == 1 else None


def plan_rollforward(*, base_dir: Path = BASE_DIR) -> dict:
    """未適用の broker確認済み約定を holdings 数量差分に変換する (副作用なし)。

    ``require_applied=False`` で「まだ適用されていないが broker 確認は
    完全」な約定を拾う。適用済み (``portfolio_applied``) はスキップする
    ので、繰り返し実行しても二重計上しない。
    """
    from execution_reconciliation import load_effective_execution_records
    from position_identity import is_complete_broker_confirmed_fill

    base_dir = Path(base_dir)
    holdings = load_json(base_dir / HOLDINGS_FILE_NAME, default={})
    rows = load_effective_execution_records(base_dir=base_dir)

    planned: list[dict] = []
    skipped: list[dict] = []
    for row in rows:
        if not isinstance(row, dict):
            continue
        execution_id = row.get("execution_id") or row.get("id")
        if bool(row.get("portfolio_applied") or row.get("portfolio_updated")):
            continue  # 既に holdings へ反映済み
        if not is_complete_broker_confirmed_fill(row, require_applied=False):
            continue  # broker 確認が不完全なものは自動適用しない
        key = _holding_key_for(row, holdings)
        if key is None:
            skipped.append({"execution_id": execution_id, "reason": "holding_key_unresolved"})
            continue
        try:
            quantity = float(row.get("filled_quantity"))
        except (TypeError, ValueError):
            skipped.append({"execution_id": execution_id, "reason": "filled_quantity_invalid"})
            continue
        side = str(row.get("side") or row.get("type") or "").lower()
        if side in {"sell", "trim", "exit"}:
            delta = -quantity
        elif side in {"buy", "add", "dca"}:
            delta = quantity
        else:
            skipped.append({"execution_id": execution_id, "reason": f"unmapped_side:{side}"})
            continue
        before = float(holdings.get(key, {}).get("shares") or 0.0)
        after = before + delta
        if after < 0:
            # 台帳と holdings が食い違っている。自動では触らず人間の再照合へ回す。
            skipped.append({
                "execution_id": execution_id,
                "reason": "would_go_negative",
                "key": key, "before": before, "delta": delta,
            })
            continue
        planned.append({
            "execution_id": execution_id,
            "key": key,
            "ticker": row.get("ticker"),
            "side": side,
            "delta": delta,
            "before": before,
            "after": after,
            "authority_at": row.get("reconciled_at") or row.get("broker_reported_at"),
        })

    return {"planned": planned, "skipped": skipped, "planned_count": len(planned)}


def apply_rollforward(*, base_dir: Path = BASE_DIR, now: Optional[datetime] = None) -> dict:
    """plan_rollforward の差分を holdings.json と約定台帳へ適用する。

    holdings 側は数量とロールフォワード由来の provenance を更新し、約定側は
    ``portfolio_applied`` を立てる。後者により
    ``is_complete_broker_confirmed_fill`` が True となり、Stage 0B の
    ポジション権威が約定時刻まで前進する。
    """
    base_dir = Path(base_dir)
    now = now or datetime.now()
    plan = plan_rollforward(base_dir=base_dir)
    if not plan["planned"]:
        return {**plan, "applied": 0}

    holdings_path = base_dir / HOLDINGS_FILE_NAME
    # holdings は台帳系なので strict 読み: 壊れたファイルを default {} で
    # 握りつぶすと、空の holdings を書き戻して保有を消しかねない。
    holdings = load_json_strict(holdings_path)
    applied_ids: set[str] = set()
    for item in plan["planned"]:
        entry = holdings.get(item["key"])
        if not isinstance(entry, dict):
            continue
        entry["shares"] = item["after"]
        entry["source_as_of"] = item["authority_at"] or now.isoformat()
        entry["rollforward_applied_at"] = now.isoformat()
        entry["rollforward_execution_id"] = item["execution_id"]
        entry["note"] = (
            f"約定ロールフォワード {item['side']} {abs(item['delta'])} "
            f"({item['before']}→{item['after']})"
        )
        applied_ids.add(str(item["execution_id"]))
    atomic_write_json(holdings_path, holdings)

    # 約定側に適用済みフラグを立てる (二重適用防止 + Stage 0B の権威前進)。
    # load_effective_execution_records は route 補正を重ねた *読み取り用* の
    # コピーを返すので、書き戻しは生の台帳ファイルに対して行う。
    exec_path = base_dir / EXECUTION_FILE_NAME
    executions = load_json_strict(exec_path)
    if isinstance(executions, list):
        for row in executions:
            if not isinstance(row, dict):
                continue
            if str(row.get("execution_id") or row.get("id")) in applied_ids:
                row["portfolio_applied"] = True
                row["portfolio_applied_at"] = now.isoformat()
        atomic_write_json(exec_path, executions)

    return {**plan, "applied": len(applied_ids)}


def main() -> int:
    parser = argparse.ArgumentParser(
        description="holdings/cash の鮮度アンカーを CSV 無しで前進させる",
    )
    sub = parser.add_subparsers(dest="command", required=True)

    p_attest = sub.add_parser("attest", help="証券会社の保有と一致していることを表明する")
    p_attest.add_argument(
        "--scope", choices=(*ATTESTABLE_SCOPES, "all"), default="all",
        help="表明する対象 (既定: holdings と cash の両方)",
    )
    p_attest.add_argument("--note", default="", help="監査用のメモ")
    p_attest.add_argument("--actor", default="user")

    p_roll = sub.add_parser("rollforward", help="broker確認済み約定を holdings に適用する")
    p_roll.add_argument("--apply", action="store_true", help="実際に書き込む (既定は dry-run)")

    sub.add_parser("status", help="現在の有効な表明を表示する")

    args = parser.parse_args()

    if args.command == "attest":
        scopes = ATTESTABLE_SCOPES if args.scope == "all" else (args.scope,)
        out = [
            record_attestation(scope=s, note=args.note, actor=args.actor)
            for s in scopes
        ]
        print(json.dumps(out, ensure_ascii=False, indent=2))
        return 0

    if args.command == "rollforward":
        result = apply_rollforward() if args.apply else plan_rollforward()
        print(json.dumps(result, ensure_ascii=False, indent=2, default=str))
        return 0

    status = {
        s: latest_valid_attestation(scope=s) for s in ATTESTABLE_SCOPES
    }
    print(json.dumps(status, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
