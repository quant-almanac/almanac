"""Add the ``bull_pullback`` scenario to ``scenario_playbook.json``.

Plan §5 step 5 introduces ``bull_pullback`` as the first ticker-/regime-
level pullback playbook. The trigger is the textbook setup:

- VIX below 25 (no panic).
- Bull regime confirmed.
- SPY trading above its MA50 (uptrend intact).
- Index sitting 3-8 % below the prior swing high (a meaningful pullback,
  not a 1 % wiggle).

Per Round 7 C7-3 / Round 11 #C the scenario carries two new feature-flag
fields:

- ``enabled_for_decision`` (default ``true``) — whether the playbook may
  drive any priority action.
- ``observe_only`` (default ``false``) — when ``true``, the catalyst layer
  still logs candidates the playbook fires, but they cannot become
  ``adopted``. Used in Week 3-6 shadow validation before we let it drive
  real money.

Why a migration rather than a hand-edit?
----------------------------------------

Same reasons as :mod:`almanac.migrations.agent_beliefs_v1_to_v2`:

1. **Idempotent.** Re-runs are no-ops, so the cron / CI can call it
   safely.
2. **Backup.** ``.bak.<UTC stamp>`` written before any change.
3. **Atomic.** ``.tmp`` + ``os.replace`` so a crash mid-write cannot
   corrupt the live file.
4. **Reusable pattern.** Phase 2 will add ``ipo_proxy_event`` and
   ``earnings_revision_drift``; each gets its own tiny migration with
   the same shape.

Usage::

    from almanac.migrations.add_bull_pullback_playbook import migrate
    result = migrate("scenario_playbook.json")

CLI::

    python -m almanac.migrations.add_bull_pullback_playbook scenario_playbook.json
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import shutil
import sys
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

__all__ = ["BULL_PULLBACK_ID", "BULL_PULLBACK_PLAYBOOK", "MigrationResult", "migrate"]

BULL_PULLBACK_ID = "bull_pullback"

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Scenario payload
# ---------------------------------------------------------------------------


#: The full payload appended to ``scenarios[]`` when the migration runs.
#: Kept as a top-level constant so it can be imported by tests and by the
#: scenario engine (once it gains awareness of ``enabled_for_decision``).
BULL_PULLBACK_PLAYBOOK: dict[str, Any] = {
    "id": BULL_PULLBACK_ID,
    "name": "強気相場の押し目買い",
    "icon": "📈",
    "color": "#1976D2",
    "priority": "medium",
    "description": (
        "強気レジーム継続中に指数が 3-8% 押した場面で段階的に買い増す playbook。"
        " VIX 25 未満 / SPY が MA50 より上 / 押し幅 3-8% / Fear&Greed が極端な Greed"
        " ではない、を全て満たすときに ACTIVE。Conservative (大型ETF) /"
        " Aggressive (個別グロース) / Tactical (レバETF) の 3 階層で実行。"
    ),
    # Round 7 C7-3 / Round 11 #C feature-flag axes.
    "enabled_for_decision": True,
    "observe_only": False,
    "detect": {
        "news_keywords": [],
        "indicators": {
            "vix": {
                "condition": "below",
                "threshold": 25,
                "key": "vix_current",
                "description": "VIX 25 未満で panic でないことを確認",
            },
            "fear_greed_index": {
                "condition": "below",
                "threshold": 80,
                "description": "Fear&Greed 80 未満 (極端な Greed を排除)",
            },
            "spy_dist_from_ma50_pct": {
                "condition": "between",
                "lower": -0.08,
                "upper": -0.03,
                "description": "SPY が MA50 から 3-8% 下に押した",
            },
        },
        "technical": {
            "SPY_above_MA50": {
                "condition": "true",
                "description": "SPY 終値が MA50 より上 (うわっち相場の押し)",
            },
            "regime_bull_confirmed": {
                "condition": "true",
                "description": "regime detector が bull_confirmed を返している",
            },
        },
        "min_signals": 3,
    },
    "actions": {
        # Codex Round 12 P1 #2: every buy entry carries an explicit
        # ``currency`` so a future executor cannot interpret a JPY
        # ``allocation_amount`` of 200000 as USD.
        "phase_1_conservative": {
            "label": "守りの押し目買い (0-2 日)",
            "buy": [
                {"ticker": "SPY", "allocation_amount": 2000, "currency": "USD", "reason": "大型ETF — リスク最小"},
                {"ticker": "QQQ", "allocation_amount": 2000, "currency": "USD", "reason": "NASDAQ — 押し目"},
                {"ticker": "SMH", "allocation_amount": 1500, "currency": "USD", "reason": "半導体ETF — リーダーセクター"},
                {"ticker": "EWJ", "allocation_amount": 1500, "currency": "USD", "reason": "日本株 — 通貨ヘッジ込み"},
                {"ticker": "1489.T", "allocation_amount": 200000, "currency": "JPY", "reason": "JP 高配当 — 円建て保守的"},
            ],
            "notes": "USD $7K + JPY ¥200K を初動投入。Conservative 階層は失敗時の損失上限が小さい。",
        },
        "phase_2_aggressive": {
            "label": "確認後の個別株積み増し (3-5 日)",
            "buy": [
                {"ticker": "NVDA", "allocation_amount": 2000, "currency": "USD", "reason": "AI リーダー — 押し目で買い増し"},
                {"ticker": "AVGO", "allocation_amount": 1500, "currency": "USD", "reason": "AI ネットワーク — 押し目"},
                {"ticker": "9984.T", "allocation_amount": 100000, "currency": "JPY", "reason": "JP テック — proxy 銘柄"},
                {"ticker": "ARM", "allocation_amount": 1000, "currency": "USD", "reason": "半導体 IP — 押し目"},
            ],
            "confirmation_required": [
                "VIX が 25 未満を維持していること",
                "SPY が MA50 を割っていないこと",
                "Conservative 階層の含み損が -3% を超えていないこと",
            ],
            "notes": "Conservative が機能していれば Aggressive を追加。逆張り単独では入らない。",
        },
        "phase_3_tactical": {
            "label": "勢いの加速確認後 (5-10 日)",
            "buy": [
                {"ticker": "SOXL", "allocation_amount": 1000, "currency": "USD", "reason": "半導体 3 倍レバ — 確認後の加速時のみ"},
                {"ticker": "TQQQ", "allocation_amount": 1000, "currency": "USD", "reason": "QQQ 3 倍レバ — 加速時のみ"},
                {"ticker": "1321.T", "allocation_amount": 100000, "currency": "JPY", "reason": "日経225 ETF — 加速時のみ。1570.T はレバETFのため generic universe へ混入させない"},
            ],
            "confirmation_required": [
                "Aggressive 階層が +2% 以上含み益",
                "SPY が MA20 を奪回",
                "出来高が 20 日平均を上回っている",
            ],
            "notes": "Tactical 階層は失敗時の損失が大きい。Aggressive が機能していなければスキップ。",
        },
    },
}


@dataclass(frozen=True)
class MigrationResult:
    """Summary returned by :func:`migrate`."""

    path: Path
    migrated: bool
    backup_path: Path | None
    scenarios_after: int


def _utc_stamp() -> str:
    return datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")


def _backup_path_for(src: Path) -> Path:
    return src.with_suffix(src.suffix + f".bak.{_utc_stamp()}")


def migrate(path: Path | str) -> MigrationResult:
    """Append ``bull_pullback`` to ``scenarios[]`` if not already present.

    Raises
    ------
    FileNotFoundError
        If ``path`` does not exist.
    ValueError
        If the file is not the expected
        ``{version, description, updated_at, scenarios: [...], global_rules}``
        shape.
    """
    path = Path(path)
    if not path.exists():
        raise FileNotFoundError(path)

    with path.open("r", encoding="utf-8") as fh:
        data = json.load(fh)

    if not isinstance(data, dict) or "scenarios" not in data:
        raise ValueError(
            f"{path}: expected top-level dict with 'scenarios' field"
        )
    scenarios = data["scenarios"]
    if not isinstance(scenarios, list):
        raise ValueError(
            f"{path}: 'scenarios' must be a list, got {type(scenarios).__name__}"
        )

    if any(s.get("id") == BULL_PULLBACK_ID for s in scenarios if isinstance(s, dict)):
        logger.info("%s already present; nothing to do", BULL_PULLBACK_ID)
        return MigrationResult(
            path=path,
            migrated=False,
            backup_path=None,
            scenarios_after=len(scenarios),
        )

    backup = _backup_path_for(path)
    shutil.copy2(path, backup)
    logger.info("backup written: %s", backup)

    # Use a *copy* of the payload constant so a future caller cannot
    # accidentally mutate the canonical reference via the file's data
    # structure.
    scenarios.append(json.loads(json.dumps(BULL_PULLBACK_PLAYBOOK)))
    data["scenarios"] = scenarios
    data["updated_at"] = datetime.now(timezone.utc).isoformat()

    tmp = path.with_suffix(path.suffix + ".tmp")
    with tmp.open("w", encoding="utf-8") as fh:
        json.dump(data, fh, ensure_ascii=False, indent=2)
        fh.flush()
        os.fsync(fh.fileno())
    os.replace(tmp, path)

    logger.info(
        "appended %s; scenarios_after=%d", BULL_PULLBACK_ID, len(scenarios)
    )
    return MigrationResult(
        path=path,
        migrated=True,
        backup_path=backup,
        scenarios_after=len(scenarios),
    )


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def _main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Append the bull_pullback scenario to scenario_playbook.json"
    )
    parser.add_argument("path", type=Path, help="path to scenario_playbook.json")
    parser.add_argument("--verbose", "-v", action="store_true")
    args = parser.parse_args(argv)
    logging.basicConfig(
        level=logging.INFO if args.verbose else logging.WARNING,
        format="%(levelname)s %(name)s: %(message)s",
    )
    try:
        result = migrate(args.path)
    except (FileNotFoundError, ValueError) as exc:
        print(f"migration failed: {exc}", file=sys.stderr)
        return 2
    if result.migrated:
        print(
            f"appended {BULL_PULLBACK_ID}; backup={result.backup_path}; "
            f"scenarios_after={result.scenarios_after}"
        )
    else:
        print(f"no-op (already present); scenarios_after={result.scenarios_after}")
    return 0


if __name__ == "__main__":  # pragma: no cover
    sys.exit(_main())
