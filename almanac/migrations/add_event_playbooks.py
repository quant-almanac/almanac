"""Add ``ipo_proxy_event`` and ``earnings_revision_drift`` to ``event_playbook.json``.

Plan §5 step 5 (Round 3 of the dialectic) separates ticker/event-level
playbooks from macro/regime-level scenarios. ``scenario_playbook.json``
holds macro scenarios (e.g. ``bull_pullback``); ``event_playbook.json``
holds ticker- and event-driven playbooks.

This migration creates ``event_playbook.json`` if absent and appends the
two Round-3 playbooks:

``ipo_proxy_event``
    A non-listed entity announces IPO / funding event → proxy listed
    tickers can rally on derived exposure. Uses ``proxy_seed_map.json``
    for 4-layer validation (seed + LLM proposer + skeptic +
    self-consistency, R11 #C-3 hallucination guard).

``earnings_revision_drift``
    Upward earnings revision → ride the post-announcement drift
    (Bernard & Thomas 1989). Fires when ``revision_tracker`` detects
    ``direction='up'``, ``surprise_score >= 0.5``, and
    ``priced_in_penalty <= 0.3``.

Why a migration rather than a hand-edit?
-----------------------------------------
Same reasons as :mod:`almanac.migrations.agent_beliefs_v1_to_v2`:

1. **Idempotent.** Re-runs are no-ops, so the cron / CI can call it
   safely.
2. **Backup.** ``.bak.<UTC stamp>`` written before any change.
3. **Atomic.** ``.tmp`` + ``os.replace`` so a crash mid-write cannot
   corrupt the live file.
4. **Creates file if absent.** Unlike ``add_bull_pullback_playbook``
   which edits an existing file, this migration initialises
   ``event_playbook.json`` (``version=1.0``, ``playbooks=[]``) when
   the file does not yet exist.

Usage::

    from almanac.migrations.add_event_playbooks import migrate
    result = migrate("event_playbook.json")

CLI::

    python -m almanac.migrations.add_event_playbooks event_playbook.json
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

__all__ = [
    "EARNINGS_REVISION_DRIFT_ID",
    "EARNINGS_REVISION_DRIFT_PLAYBOOK",
    "IPO_PROXY_EVENT_ID",
    "IPO_PROXY_EVENT_PLAYBOOK",
    "MigrationResult",
    "migrate",
]

IPO_PROXY_EVENT_ID = "ipo_proxy_event"
EARNINGS_REVISION_DRIFT_ID = "earnings_revision_drift"

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Playbook payloads
# ---------------------------------------------------------------------------


#: Full payload for the IPO-proxy event-driven playbook.
#: Kept as a top-level constant so it can be imported by tests and by the
#: event-scenario engine.
IPO_PROXY_EVENT_PLAYBOOK: dict[str, Any] = {
    "id": IPO_PROXY_EVENT_ID,
    "name": "非上場プロキシ・イベントドリブン",
    "icon": "🪂",
    "color": "#7C4DFF",
    "priority": "high",
    "description": (
        "非上場企業 (OpenAI / SpaceX / Stripe 等) の IPO 申請 / 大型ファンディング / "
        "メディア露出時に、proxy_seed_map.json で関連付けられた上場プロキシ "
        "(9984.T, ARM, MSFT, NVDA 等) を段階的に買い増す playbook。proxy_mapper の "
        "4 層検証 (seed + LLM proposer + skeptic + self-consistency) を通過した候補のみ採用。"
    ),
    # Round 11 #C feature-flag axes.
    "enabled_for_decision": True,
    "observe_only": False,
    "detect": {
        "news_keywords": [
            "ipo filing",
            "ipo申請",
            "s-1 filed",
            "funding round",
            "valuation",
            "資金調達",
            "上場準備",
            "公開買付",
            "tender offer",
        ],
        "indicators": {
            "vix": {
                "condition": "below",
                "threshold": 30,
                "key": "vix_current",
            },
            "proxy_self_consistency": {
                "condition": "above",
                "threshold": 0.5,
                "description": "proxy_mapper L4 Jaccard >= 0.5 必須 (R11 #C-3 ハルシネーション防止)",
            },
        },
        "technical": {
            "primary_proxy_above_MA20": {
                "condition": "true",
                "description": "最有力 proxy が MA20 より上",
            },
        },
        "min_signals": 2,
    },
    "actions": {
        # Codex Round 12 P1 #2: every buy entry carries an explicit
        # ``currency`` so a future executor cannot interpret a JPY
        # ``allocation_amount`` as USD.
        "phase_1_seed_only": {
            "label": "シード proxy のみ即時購入 (0-2 日)",
            "buy": [
                {
                    "ticker": "<determined by proxy_mapper>",
                    "allocation_amount": 1000,
                    "currency": "USD",
                    "reason": "proxy_seed_map L1 deterministic hit — 最高信頼層",
                },
            ],
            "notes": "L1 seed のみ。LLM レイヤー (L2-L4) の確認後に phase_2 へ進む。",
        },
        "phase_2_llm_confirmed": {
            "label": "LLM 4 層通過後の段階買い増し (2-5 日)",
            "buy": [
                {
                    "ticker": "<L4 self-consistency 通過 proxy>",
                    "allocation_amount": 1500,
                    "currency": "USD",
                    "reason": "L2 proposer + L3 skeptic + L4 (Jaccard >= 0.5) 全通過",
                },
            ],
            "confirmation_required": [
                "proxy_audit_log.jsonl の最新エントリで jaccard >= 0.5",
                "primary proxy が phase_1 投入後 +1% 以上",
                "VIX が 30 未満を維持",
            ],
            "notes": "全 4 層通過した proxy のみ追加投入。",
        },
    },
}


#: Full payload for the earnings-revision-drift playbook.
EARNINGS_REVISION_DRIFT_PLAYBOOK: dict[str, Any] = {
    "id": EARNINGS_REVISION_DRIFT_ID,
    "name": "業績上方修正ドリフト",
    "icon": "📈",
    "color": "#00C853",
    "priority": "medium",
    "description": (
        "revision_tracker が direction='up' を検出した銘柄を、surprise_score "
        ">= 0.5 かつ priced_in_penalty <= 0.3 のとき段階的に買い増す playbook。"
        "5-20 営業日の post-announcement drift を狙う (Bernard & Thomas 1989)。"
    ),
    # Round 11 #C feature-flag axes.
    "enabled_for_decision": True,
    "observe_only": False,
    "detect": {
        "news_keywords": [
            "上方修正",
            "増額修正",
            "guidance raise",
            "raised guidance",
            "upward revision",
            "raises full-year",
        ],
        "indicators": {
            "vix": {
                "condition": "below",
                "threshold": 30,
                "key": "vix_current",
            },
            "revision_surprise_score": {
                "condition": "above",
                "threshold": 0.5,
                "description": "revision_tracker.surprise_score >= 0.5 (R9 #7)",
            },
            "revision_priced_in_penalty": {
                "condition": "below",
                "threshold": 0.3,
                "description": "priced_in_penalty <= 0.3 — まだ織り込まれていない",
            },
        },
        "technical": {
            "above_MA50": {
                "condition": "true",
                "description": "銘柄が MA50 より上 (上昇トレンド継続)",
            },
        },
        "min_signals": 3,
    },
    "actions": {
        # Codex Round 12 P1 #2: every buy entry carries an explicit
        # ``currency`` so a future executor cannot interpret ¥150,000 as USD.
        "phase_1_initial": {
            "label": "初動買い (発表後 0-3 営業日)",
            "buy": [
                {
                    "ticker": "<determined by revision_state.json>",
                    "allocation_amount": 1500,
                    "currency": "USD",
                    "reason": "上方修正発表 — 後続ドリフトを狙う初動",
                },
            ],
            "notes": "USD 銘柄は 1500 USD、JP 銘柄は 150000 JPY を目安。",
        },
        "phase_2_drift_continuation": {
            "label": "ドリフト継続確認後の追加 (5-15 営業日)",
            "buy": [
                {
                    "ticker": "<同銘柄>",
                    "allocation_amount": 1000,
                    "currency": "USD",
                    "reason": "post-announcement drift 継続確認 — sell-side 上方改定確認後",
                },
            ],
            "confirmation_required": [
                "phase_1 投入から +2% 以上",
                "アナリスト上方改定が 2 社以上",
                "VIX が 30 未満を維持",
            ],
            "notes": "20 営業日経過後は exit。drift 効果は減衰する。",
        },
    },
}

_ALL_PLAYBOOKS = [IPO_PROXY_EVENT_PLAYBOOK, EARNINGS_REVISION_DRIFT_PLAYBOOK]


@dataclass(frozen=True)
class MigrationResult:
    """Summary returned by :func:`migrate`."""

    path: Path
    migrated: bool
    backup_path: Path | None
    playbooks_after: int


def _utc_stamp() -> str:
    return datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")


def _backup_path_for(src: Path) -> Path:
    return src.with_suffix(src.suffix + f".bak.{_utc_stamp()}")


def _empty_event_playbook() -> dict[str, Any]:
    return {
        "version": "1.0",
        "description": (
            "ticker/event-level playbooks (separate from scenario_playbook.json "
            "which holds macro scenarios)"
        ),
        "updated_at": datetime.now(timezone.utc).isoformat(),
        "playbooks": [],
    }


def migrate(path: Path | str) -> MigrationResult:
    """Append ``ipo_proxy_event`` and ``earnings_revision_drift`` to ``event_playbook.json``.

    Creates the file with an empty ``playbooks`` list when it does not yet
    exist.  Re-runs are no-ops when both playbooks are already present.

    Raises
    ------
    ValueError
        If the file exists but is not the expected
        ``{version, description, updated_at, playbooks: [...]}`` shape.
    """
    path = Path(path)

    # ------------------------------------------------------------------ load
    if not path.exists():
        data: dict[str, Any] = _empty_event_playbook()
    else:
        with path.open("r", encoding="utf-8") as fh:
            data = json.load(fh)

        if not isinstance(data, dict) or "playbooks" not in data:
            raise ValueError(
                f"{path}: expected top-level dict with 'playbooks' field"
            )
        playbooks = data["playbooks"]
        if not isinstance(playbooks, list):
            raise ValueError(
                f"{path}: 'playbooks' must be a list, got {type(playbooks).__name__}"
            )

    playbooks = data["playbooks"]

    # ------------------------------------------------------------------ idempotency
    existing_ids = {p.get("id") for p in playbooks if isinstance(p, dict)}
    missing = [pb for pb in _ALL_PLAYBOOKS if pb["id"] not in existing_ids]

    if not missing:
        logger.info("all playbooks already present; nothing to do")
        return MigrationResult(
            path=path,
            migrated=False,
            backup_path=None,
            playbooks_after=len(playbooks),
        )

    # ------------------------------------------------------------------ backup (only if file pre-existed)
    backup: Path | None = None
    if path.exists():
        backup = _backup_path_for(path)
        shutil.copy2(path, backup)
        logger.info("backup written: %s", backup)

    # ------------------------------------------------------------------ append
    for pb in missing:
        playbooks.append(json.loads(json.dumps(pb)))
        logger.info("appending playbook: %s", pb["id"])

    data["playbooks"] = playbooks
    data["updated_at"] = datetime.now(timezone.utc).isoformat()

    tmp = path.with_suffix(path.suffix + ".tmp")
    with tmp.open("w", encoding="utf-8") as fh:
        json.dump(data, fh, ensure_ascii=False, indent=2)
        fh.flush()
        os.fsync(fh.fileno())
    os.replace(tmp, path)

    logger.info(
        "migration complete; playbooks_after=%d", len(playbooks)
    )
    return MigrationResult(
        path=path,
        migrated=True,
        backup_path=backup,
        playbooks_after=len(playbooks),
    )


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def _main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description=(
            "Append ipo_proxy_event and earnings_revision_drift "
            "to event_playbook.json (creates file if absent)"
        )
    )
    parser.add_argument("path", type=Path, help="path to event_playbook.json")
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
            f"appended playbooks; backup={result.backup_path}; "
            f"playbooks_after={result.playbooks_after}"
        )
    else:
        print(f"no-op (already present); playbooks_after={result.playbooks_after}")
    return 0


if __name__ == "__main__":  # pragma: no cover
    sys.exit(_main())
