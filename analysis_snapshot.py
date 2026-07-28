"""analysis_snapshot.py — Stage 1B: AnalysisSnapshot の2レーン凍結

背景: 日次分析 (analyst.run_analysis) は holdings/cash/prices/FX/macro/news/
screening を一括取得してから5つの並列ティア LLM を呼び、その後オプション
市場データで拡張して最終 Opus synthesis を呼ぶ。しかしどの入力が「その
分析の判断根拠として確定した瞬間」に存在していたかを示す監査記録が無く、
再現性の検証 (同じコード・同じモデル・同じデータで同じ結論になるか) や
事後のインシデント調査 (何が古いデータだったのか) がしづらい。

本モジュールは以下を提供する:

  - SourceProvenance: 1つのデータソースの鮮度・ハッシュを表す不変レコード
  - BaseSnapshot: holdings/cash/prices/FX/macro/news/screening の7カテゴリ
  - EnrichedSnapshot: BaseSnapshot + 対象銘柄のオプション市場データ
  - DecisionSnapshot: 上記を確定・原子的に永続化した凍結レコード。
    確定後は同じ (decision_snapshot_id, stage) の組では絶対に上書きしない
    (immutability契約) — 2回目以降の freeze 呼び出しは保存済みの内容を返す。

2レーンの区別:
  - decision_snapshot (本モジュールが管理): tier LLM 呼び出し開始と同時に
    確定。投資理由・confidence・expected_return の根拠となった入力の記録。
    確定後は絶対に書き換えない。
  - execution_quote_snapshot (build_execution_quote_snapshot): 注文直前の
    価格・スプレッド・市場状態を自由に再取得してよいレーン。
    decision_snapshot を書き換える目的で使ってはならない — 指値の再計算・
    review/blockへの降格・期限切れの判定材料としてのみ使うこと。

閾値 (max_age_hours) は analyst._compute_data_freshness() が既に使っている
ファイル・タイムスタンプキーの規約にそろえている (新しい数値を発明しない)。
"""
from __future__ import annotations

import hashlib
import json
from dataclasses import asdict, dataclass, replace
from datetime import datetime
from pathlib import Path
from typing import Optional

BASE_DIR = Path(__file__).parent
SNAPSHOT_STATE_PATH = BASE_DIR / "decision_snapshot_state.json"

_MISSING_HASH = "missing"


# ---------------------------------------------------------------------------
# データクラス
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class SourceProvenance:
    source: str
    source_as_of: Optional[str]
    retrieved_at: str
    freshness_status: str  # "fresh" | "degraded" | "stale" | "unknown"
    max_age_policy_hours: float
    artifact_hash: str
    # Hash of the exact in-memory payload handed to the analysis pipeline.
    # artifact_hash alone only proves what was on disk at freeze time.
    payload_hash: Optional[str] = None


@dataclass(frozen=True)
class BaseSnapshot:
    holdings: SourceProvenance
    cash: SourceProvenance
    prices: SourceProvenance
    fx: SourceProvenance
    macro: SourceProvenance
    news: SourceProvenance
    screening: SourceProvenance


@dataclass(frozen=True)
class EnrichedSnapshot:
    base: BaseSnapshot
    options_by_ticker: dict  # ticker -> SourceProvenance


@dataclass(frozen=True)
class DecisionSnapshot:
    decision_snapshot_id: str
    stage: str
    frozen_at: str
    enriched: EnrichedSnapshot
    code_revision: Optional[str]
    model_ids: dict
    prompt_hashes: dict
    policy_version: Optional[str]
    budget_mode: Optional[str]
    tunable_snapshot_hash: Optional[str]
    analysis_clock: str


# ---------------------------------------------------------------------------
# ハッシュ・タイムスタンプ抽出のヘルパー
# ---------------------------------------------------------------------------


def _file_hash(path: Path) -> str:
    if not path.exists():
        return _MISSING_HASH
    try:
        return hashlib.sha256(path.read_bytes()).hexdigest()
    except OSError:
        return _MISSING_HASH


def _payload_hash(payload: object) -> str:
    """Return a stable hash for the exact value consumed by the pipeline."""
    try:
        encoded = json.dumps(
            payload,
            sort_keys=True,
            default=str,
            ensure_ascii=False,
            separators=(",", ":"),
        ).encode("utf-8")
    except Exception:
        return _MISSING_HASH
    return hashlib.sha256(encoded).hexdigest()


def _combined_hash(paths: list[Path]) -> str:
    """複数ファイルにまたがる1カテゴリ (例: screening) を1つのハッシュにまとめる。
    ファイル名込みでハッシュするため、内容が同じでもファイル構成が変われば
    ハッシュも変わる。1件も存在しなければ _MISSING_HASH。"""
    h = hashlib.sha256()
    any_found = False
    for p in sorted(paths, key=lambda x: x.name):
        if p.exists():
            try:
                h.update(p.name.encode("utf-8"))
                h.update(b"\x00")
                h.update(p.read_bytes())
                h.update(b"\x00")
                any_found = True
            except OSError:
                continue
    return h.hexdigest() if any_found else _MISSING_HASH


def _extract_json_timestamp(path: Path, ts_keys: tuple[str, ...]) -> Optional[datetime]:
    """JSON ファイルから ts_keys の最初に見つかったタイムスタンプを抽出する。

    "__mtime__" は特別扱いでファイルの mtime を使う。既存の
    analyst._data_source_age_hours() と同じキー規約 (generated_at /
    cached_at / refreshed_at / as_of / last_updated / __mtime__) に
    そろえている — 鮮度判定の閾値をこのモジュールで独自に発明しない。
    """
    if "__mtime__" in ts_keys and path.exists():
        try:
            return datetime.fromtimestamp(path.stat().st_mtime)
        except OSError:
            return None
    if not path.exists():
        return None
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return None
    if not isinstance(raw, dict):
        return None
    for k in ts_keys:
        if k == "__mtime__":
            continue
        v = raw.get(k)
        if not v:
            continue
        parsed = _parse_timestamp_value(v)
        if parsed is not None:
            return parsed
    return None


def _parse_timestamp_value(value: object) -> Optional[datetime]:
    """Parse one source timestamp into the naive local form used by this module."""
    if value in (None, ""):
        return None
    s = str(value).replace("Z", "+00:00")
    for parser in (
        lambda x: datetime.fromisoformat(x),
        lambda x: datetime.strptime(x[:19], "%Y-%m-%d %H:%M:%S"),
        lambda x: datetime.strptime(x[:16], "%Y-%m-%d %H:%M"),
    ):
        try:
            dt = parser(s)
            return dt.astimezone().replace(tzinfo=None) if dt.tzinfo is not None else dt
        except (ValueError, TypeError):
            continue
    return None


def _freshness_status(as_of: Optional[datetime], *, now: datetime, max_age_hours: float) -> str:
    if as_of is None:
        return "unknown"
    age_hours = max(0.0, (now - as_of).total_seconds() / 3600)
    if age_hours > max_age_hours:
        return "stale"
    if age_hours > max_age_hours / 2:
        return "degraded"
    return "fresh"


def _provenance_for_file(
    path: Path,
    *,
    ts_keys: tuple[str, ...],
    max_age_hours: float,
    now: datetime,
    source_label: str,
) -> SourceProvenance:
    as_of = _extract_json_timestamp(path, ts_keys)
    return SourceProvenance(
        source=source_label,
        source_as_of=as_of.isoformat() if as_of else None,
        retrieved_at=now.isoformat(),
        freshness_status=_freshness_status(as_of, now=now, max_age_hours=max_age_hours),
        max_age_policy_hours=max_age_hours,
        artifact_hash=_file_hash(path),
    )


# ---------------------------------------------------------------------------
# base_snapshot / enriched_snapshot 構築
# ---------------------------------------------------------------------------


def build_base_snapshot(*, base_dir: Path = BASE_DIR, now: Optional[datetime] = None) -> BaseSnapshot:
    """base_snapshot: holdings/cash/prices/FX/macro/news/screening の7カテゴリ。

    データの取得方法自体は変えない (data_gatherer.gather_data() が既に読んで
    いるのと同じファイルを、確定時点のスナップショットとして改めて読み取り、
    ハッシュ化するだけ)。
    """
    now = now or datetime.now()

    holdings = _provenance_for_file(
        base_dir / "holdings.json", ts_keys=("__mtime__",), max_age_hours=96.0,
        now=now, source_label="holdings.json",
    )
    cash = _provenance_for_file(
        base_dir / "account.json", ts_keys=("last_updated",), max_age_hours=96.0,
        now=now, source_label="account.json",
    )
    prices = _provenance_for_file(
        base_dir / "technical_state.json", ts_keys=("cached_at",), max_age_hours=8.0,
        now=now, source_label="technical_state.json",
    )
    # FX レートは account.json 経由 (utils.get_fx_rate_cached) で解決されるため
    # account.json の鮮度をそのまま流用する。専用の FX レートファイルは無い。
    fx = _provenance_for_file(
        base_dir / "account.json", ts_keys=("last_updated",), max_age_hours=24.0,
        now=now, source_label="account.json(fx_rate_usdjpy)",
    )
    macro = _provenance_for_file(
        base_dir / "macro_event_state.json", ts_keys=("refreshed_at",), max_age_hours=24.0,
        now=now, source_label="macro_event_state.json",
    )
    news = _provenance_for_file(
        base_dir / "news_signal_candidates.json", ts_keys=("generated_at", "scan_time"), max_age_hours=12.0,
        now=now, source_label="news_signal_candidates.json",
    )

    screening_files = [
        (base_dir / "short_candidates.json", ("generated_at", "scanned")),
        (base_dir / "margin_long_candidates.json", ("generated_at",)),
        (base_dir / "long_term_screen_results.json", ("as_of",)),
    ]
    screening_as_of: Optional[datetime] = None
    for path, keys in screening_files:
        ts = _extract_json_timestamp(path, keys)
        if ts is not None and (screening_as_of is None or ts < screening_as_of):
            screening_as_of = ts  # 複数ファイルの中で最も古いものを採用 (保守的)
    screening = SourceProvenance(
        source="+".join(p.name for p, _ in screening_files),
        source_as_of=screening_as_of.isoformat() if screening_as_of else None,
        retrieved_at=now.isoformat(),
        freshness_status=_freshness_status(screening_as_of, now=now, max_age_hours=72.0),
        max_age_policy_hours=72.0,
        artifact_hash=_combined_hash([p for p, _ in screening_files]),
    )

    return BaseSnapshot(
        holdings=holdings, cash=cash, prices=prices, fx=fx,
        macro=macro, news=news, screening=screening,
    )


def build_base_snapshot_from_data(
    data: dict,
    *,
    base_dir: Path = BASE_DIR,
    now: Optional[datetime] = None,
) -> BaseSnapshot:
    """Freeze provenance for the exact in-memory payload used by the LLMs.

    ``build_base_snapshot`` records the authoritative source artifact and its
    freshness.  This wrapper additionally records hashes of the already loaded
    values that are actually passed downstream.  Callers must build this once
    after ``gather_data()`` and reuse the returned object for every later stage;
    rebuilding it from disk would create a second, non-authoritative view.
    """
    base = build_base_snapshot(base_dir=base_dir, now=now)
    payloads = {
        "holdings": data.get("positions"),
        "cash": data.get("cash_info"),
        "prices": data.get("technical_state"),
        "fx": (data.get("cash_info") or {}).get("fx_rate_usdjpy"),
        "macro": {
            "market_meta": data.get("market_meta"),
            "regime": data.get("regime"),
            "scenario": data.get("scenario"),
        },
        "news": {
            "feed": data.get("news"),
            "web_search": data.get("web_search_news"),
        },
        "screening": {
            "screening": data.get("screening"),
            "screen_candidates": data.get("screen_candidates"),
            "beliefs_context": data.get("beliefs_context"),
            "catalyst_context": data.get("catalyst_context"),
        },
    }
    return BaseSnapshot(**{
        key: replace(getattr(base, key), payload_hash=_payload_hash(payload))
        for key, payload in payloads.items()
    })


def decision_freshness_issues(enriched: EnrichedSnapshot) -> list[dict]:
    """Return non-fresh inputs that must be visible to readiness consumers."""
    issues: list[dict] = []
    for category in ("holdings", "cash", "prices", "fx", "macro", "news", "screening"):
        provenance = getattr(enriched.base, category)
        if provenance.freshness_status in {"stale", "unknown"}:
            issues.append({
                "category": category,
                "status": provenance.freshness_status,
                "source": provenance.source,
                "source_as_of": provenance.source_as_of,
                "max_age_policy_hours": provenance.max_age_policy_hours,
            })
    for ticker, provenance in enriched.options_by_ticker.items():
        if provenance.freshness_status in {"stale", "unknown"}:
            issues.append({
                "category": "options",
                "ticker": ticker,
                "status": provenance.freshness_status,
                "source": provenance.source,
                "source_as_of": provenance.source_as_of,
                "max_age_policy_hours": provenance.max_age_policy_hours,
            })
    return issues


def decision_snapshot_content_hash(enriched: EnrichedSnapshot) -> str:
    """Hash the complete frozen decision payload for deterministic keys."""
    return _payload_hash(asdict(enriched))


def build_enriched_snapshot(
    base: BaseSnapshot,
    *,
    options_by_ticker_raw: Optional[dict] = None,
    now: Optional[datetime] = None,
) -> EnrichedSnapshot:
    """enriched_snapshot: base + 保有/候補/open order 銘柄のオプション市場データ。

    options_fetcher は専用のキャッシュファイルを公開していないため、実際に
    tier 分析へ渡された in-memory payload (options_fetcher.get_option_signals()
    の戻り値) をそのまま受け取ってハッシュ化する — 「本当に使われたデータ」を
    確定させる。payload が無い呼び出し (options_by_ticker_raw=None) は
    options_by_ticker={} の enriched_snapshot を返す (base のみの拡張)。
    """
    now = now or datetime.now()
    options_by_ticker: dict[str, SourceProvenance] = {}
    for ticker, payload in (options_by_ticker_raw or {}).items():
        if not isinstance(payload, dict):
            continue
        serialized = json.dumps(payload, sort_keys=True, default=str, ensure_ascii=False).encode("utf-8")
        as_of_raw = payload.get("as_of") or payload.get("fetched_at") or payload.get("timestamp")
        as_of = _parse_timestamp_value(as_of_raw)
        options_by_ticker[ticker] = SourceProvenance(
            source=f"options_fetcher:{ticker}",
            source_as_of=as_of.isoformat() if as_of else None,
            retrieved_at=now.isoformat(),
            freshness_status=_freshness_status(
                as_of, now=now, max_age_hours=24.0,
            ),
            max_age_policy_hours=24.0,
            artifact_hash=hashlib.sha256(serialized).hexdigest(),
        )
    return EnrichedSnapshot(base=base, options_by_ticker=options_by_ticker)


# ---------------------------------------------------------------------------
# DecisionSnapshot の確定・永続化・復元
# ---------------------------------------------------------------------------


def _source_provenance_from_dict(d: dict) -> SourceProvenance:
    return SourceProvenance(
        source=d["source"], source_as_of=d.get("source_as_of"),
        retrieved_at=d["retrieved_at"], freshness_status=d["freshness_status"],
        max_age_policy_hours=d["max_age_policy_hours"], artifact_hash=d["artifact_hash"],
        payload_hash=d.get("payload_hash"),
    )


def _base_snapshot_from_dict(d: dict) -> BaseSnapshot:
    return BaseSnapshot(**{k: _source_provenance_from_dict(v) for k, v in d.items()})


def _enriched_snapshot_from_dict(d: dict) -> EnrichedSnapshot:
    return EnrichedSnapshot(
        base=_base_snapshot_from_dict(d["base"]),
        options_by_ticker={
            k: _source_provenance_from_dict(v) for k, v in (d.get("options_by_ticker") or {}).items()
        },
    )


def _decision_snapshot_from_dict(d: dict) -> DecisionSnapshot:
    return DecisionSnapshot(
        decision_snapshot_id=d["decision_snapshot_id"],
        stage=d["stage"],
        frozen_at=d["frozen_at"],
        enriched=_enriched_snapshot_from_dict(d["enriched"]),
        code_revision=d.get("code_revision"),
        model_ids=dict(d.get("model_ids") or {}),
        prompt_hashes=dict(d.get("prompt_hashes") or {}),
        policy_version=d.get("policy_version"),
        budget_mode=d.get("budget_mode"),
        tunable_snapshot_hash=d.get("tunable_snapshot_hash"),
        analysis_clock=d.get("analysis_clock", d["frozen_at"]),
    )


def freeze_decision_snapshot(
    enriched: EnrichedSnapshot,
    *,
    decision_snapshot_id: str,
    stage: str,
    code_revision: Optional[str] = None,
    model_ids: Optional[dict] = None,
    prompt_hashes: Optional[dict] = None,
    policy_version: Optional[str] = None,
    budget_mode: Optional[str] = None,
    tunable_snapshot_hash: Optional[str] = None,
    now: Optional[datetime] = None,
    base_dir: Path = BASE_DIR,
) -> DecisionSnapshot:
    """enriched_snapshot を DecisionSnapshot として確定・原子的に永続化する。

    Immutability契約: 同じ (decision_snapshot_id, stage) の組で2回目以降
    呼ばれても、保存済みの内容を上書きしない — 呼び出し側に渡されたのが
    違う enriched_snapshot でも、最初に確定した記録を権威として返す。
    """
    now = now or datetime.now()
    path = base_dir / "decision_snapshot_state.json"

    try:
        existing = json.loads(path.read_text(encoding="utf-8")) if path.exists() else {}
    except Exception:
        existing = {}
    if not isinstance(existing, dict):
        existing = {}

    record = existing.setdefault(decision_snapshot_id, {})
    if stage in record:
        return _decision_snapshot_from_dict(record[stage])

    snapshot = DecisionSnapshot(
        decision_snapshot_id=decision_snapshot_id,
        stage=stage,
        frozen_at=now.isoformat(),
        enriched=enriched,
        code_revision=code_revision,
        model_ids=dict(model_ids or {}),
        prompt_hashes=dict(prompt_hashes or {}),
        policy_version=policy_version,
        budget_mode=budget_mode,
        tunable_snapshot_hash=tunable_snapshot_hash,
        analysis_clock=now.isoformat(),
    )
    record[stage] = asdict(snapshot)

    tmp_path = path.with_suffix(".tmp")
    tmp_path.write_text(json.dumps(existing, ensure_ascii=False, indent=2), encoding="utf-8")
    tmp_path.replace(path)
    return snapshot


def resolve_decision_snapshot(
    decision_snapshot_id: str,
    *,
    stage: Optional[str] = None,
    base_dir: Path = BASE_DIR,
) -> Optional[dict]:
    """監査・調査用の読み取り専用ルックアップ。stage を省略すると全stage分の
    dict ({"tier": {...}, "synthesis": {...}}) を返す。"""
    path = base_dir / "decision_snapshot_state.json"
    if not path.exists():
        return None
    try:
        existing = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return None
    if not isinstance(existing, dict):
        return None
    record = existing.get(decision_snapshot_id)
    if record is None:
        return None
    return record.get(stage) if stage is not None else record


# ---------------------------------------------------------------------------
# execution_quote_snapshot — 決定レーンとは別の、注文直前の再取得レーン
# ---------------------------------------------------------------------------


def build_execution_quote_snapshot(
    ticker: str,
    *,
    price: Optional[float] = None,
    spread: Optional[float] = None,
    market_status: Optional[str] = None,
    source_as_of: Optional[str] = None,
    decision_snapshot_id: Optional[str] = None,
    decision_snapshot_hash: Optional[str] = None,
    now: Optional[datetime] = None,
) -> dict:
    """execution_quote_snapshot: 注文直前に自由に再取得してよいレーン。

    契約: 指値の再計算・review/blockへの降格・期限切れの判定材料としてのみ
    使うこと。decision_snapshot (投資理由・confidence・expected_return) を
    書き換える目的でこの戻り値を使ってはならない。呼び出し側は
    snapshot_kind="execution_quote" のタグで decision_snapshot と明確に
    区別すること。

    実際の価格・スプレッド再取得ロジックは呼び出し側が持ち込む
    (price/spread/market_status を引数で渡す) — 本関数はそれを
    decision_snapshot と混同されない形に整えるだけで、新しい市場データ
    取得経路は増やさない。
    """
    now = now or datetime.now()
    payload = {
        "snapshot_kind": "execution_quote",
        "ticker": ticker,
        "price": price,
        "spread": spread,
        "market_status": market_status,
        "source_as_of": source_as_of,
        "retrieved_at": now.isoformat(),
        "decision_snapshot_id": decision_snapshot_id,
        "decision_snapshot_hash": decision_snapshot_hash,
    }
    return {**payload, "quote_hash": _payload_hash(payload)}
