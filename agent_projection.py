"""agent_projection.py — Agent SDK へ渡す sanitized payload の単一の作り手。

## なぜこれが要るか

CLI (portfolio_agent.py) と API (api/routes/agent.py) は、どちらも Agent へ
「作業ディレクトリ <絶対パス>」と「technical_state.json を読め」を渡していた。
つまり:

  - 内部の絶対パスがプロンプトへ漏れる
  - Agent が raw の technical_state.json を読むので、他の全 consumer が
    通っている品質契約 (technical_quality) を丸ごと迂回できる
  - holdings.json の note / owner / broker / account / 各種 hash まで
    Agent の目に入る
  - CLI は Write/Bash まで許可しており、出力の検証も保存もホスト側に無い

プロンプトのファイル名を変えるだけでは足りない —— Read/Bash が残っていれば
Agent は raw ファイルへ戻れる。**入力を projection そのものにし、ツールを
外し、出力はホストが検証してから保存する** のが唯一の構造的な解。

## 契約 (Codex レビュー round 11 で合意)

    build_agent_projection()
      → validate_projection()
      → canonical_json(sort_keys=True)
      → projection_sha256
      → build_agent_prompt()

- 自由形式の整形済みテキストを正規形にしない。正規形は**構造化 dict**で、
  レンダリングは共通 renderer が行う。
- unusable な行は数値を入れず、usable / reason / data_as_of だけ。
- mode ごとに不要なデータを相互に載せない。
- 絶対パス・account note・内部 key・秘密情報を含めない。
- CLI/API は同じ builder と renderer を使う。
- request_id など実行固有の情報は payload hash の**外**。
- evaluation_as_of は freshness の意味を変えるので hash の**中**。

Agent の出力は ticker ではなく candidate_id を参照する。ticker はホストが
projection から復元する —— これで「projection に無い銘柄」を構造的に
提案できなくなる。
"""

from __future__ import annotations

import hashlib
import json
import re
from datetime import datetime, timezone
from pathlib import Path

from technical_quality import DEGRADED, USABLE, classify_technical_row
from utils import load_json

SCHEMA_VERSION = "agent_projection_v1"
MODES = ("default", "risk", "nisa")

# Agent 出力の上限。長大な文字列でプロンプト/保存を膨らませない。
MAX_STRING_CHARS = 2000
MAX_ACTIONS = 20
MAX_WARNINGS = 20

# 銘柄あたりの候補上限 (プロンプト長の制御)。
MAX_CANDIDATES = 40

# actionability の順序。Agent はこれを超えて昇格できない。
ACTIONABILITY_ORDER = {"blocked": 0, "watch_only": 1, "review": 2}

# projection へ載せてよい holdings のフィールド。
# ⚠️ 明示的な allow-list にすること。deny-list だと holdings.json に
# フィールドが増えたときに黙って漏れる (note / owner / broker / account /
# reconciliation_snapshot_hash などは決して載せない)。
_HOLDING_PUBLIC_FIELDS = ("ticker", "shares", "currency", "asset_type", "investment_type")

# projection 内に現れてはいけないパターン。テストと validate の両方で使う。
_FORBIDDEN_PATTERNS = (
    re.compile(r"/Users/"),
    re.compile(r"/home/"),
    re.compile(r"[A-Za-z]:\\\\"),
    re.compile(r"\.json\b"),          # 内部ファイル名も渡さない
    re.compile(r"sk-[A-Za-z0-9]"),    # API キー様の文字列
)


class ProjectionError(ValueError):
    """projection の生成・検証に失敗した。"""


class AgentOutputError(ValueError):
    """Agent 出力がホスト側検証を通らなかった。"""


# ──────────────────────────────────────────────────────────────
#  ハッシュ / 正規化
# ──────────────────────────────────────────────────────────────

def canonical_json(payload: dict) -> str:
    """hash と比較のための正規形。キー順を固定し、空白も固定する。"""
    return json.dumps(payload, sort_keys=True, ensure_ascii=False, separators=(",", ":"))


def projection_sha256(payload: dict) -> str:
    return hashlib.sha256(canonical_json(payload).encode("utf-8")).hexdigest()


def _file_hash(path: Path) -> str | None:
    """入力ファイルの内容ハッシュ。projection 生成後に元データが変わって
    いないかをホストが確認するために使う。"""
    try:
        return hashlib.sha256(path.read_bytes()).hexdigest()
    except OSError:
        return None


# ──────────────────────────────────────────────────────────────
#  projection の構築
# ──────────────────────────────────────────────────────────────

def _technical_projection(row: object) -> dict:
    """テクニカル行を projection 形へ。使えない行には数値を入れない。"""
    verdict, reason = classify_technical_row(row)
    if verdict not in (USABLE, DEGRADED):
        return {
            "usable": False,
            "reason": reason or "unknown",
            "data_as_of": (row.get("data_as_of") if isinstance(row, dict) else None),
        }
    assert isinstance(row, dict)
    out = {
        "usable": True,
        "data_as_of": row.get("data_as_of"),
        "price": row.get("price"),
        "rsi": row.get("rsi"),
        "change_5d_pct": row.get("change_5d_pct"),
        "change_20d_pct": row.get("change_20d_pct"),
        "composite_signal": row.get("composite_signal"),
    }
    if verdict == DEGRADED:
        # 数値は出すが、1セッション遅延であることを必ず添える。
        out["reason"] = "technical_data_degraded"
    return out


def _holding_projection(key: str, row: object) -> dict | None:
    if not isinstance(row, dict):
        return None
    ticker = str(row.get("ticker") or key or "").strip()
    if not ticker:
        return None
    out = {"canonical_instrument_id": ticker}
    for field in _HOLDING_PUBLIC_FIELDS:
        if field == "ticker":
            continue
        if field in row:
            out[field] = row[field]
    return out


def _actionability_for(tech: dict) -> str:
    """この銘柄について Agent が提案してよい上限。

    テクニカルが使えない銘柄は「見るだけ」。実際の発注可否は
    execution_readiness が別途決めるので、ここは天井であって許可ではない。
    """
    return "review" if tech.get("usable") else "watch_only"


def build_agent_projection(
    mode: str,
    *,
    base_dir: Path,
    now: datetime | None = None,
    analysis_id: str | None = None,
) -> dict:
    """mode ごとの sanitized projection を作る。CLI/API 共通の唯一の入口。"""
    if mode not in MODES:
        raise ProjectionError(f"unknown mode: {mode!r}")
    base_dir = Path(base_dir)
    now = now or datetime.now(timezone.utc)

    tech_state = load_json(base_dir / "technical_state.json", {})
    tech_rows = tech_state.get("tickers", {}) if isinstance(tech_state, dict) else {}
    holdings = load_json(base_dir / "holdings.json", {})

    source_hashes = {
        "technical_state": _file_hash(base_dir / "technical_state.json"),
        "holdings": _file_hash(base_dir / "holdings.json"),
    }

    portfolio_context: dict = {}
    market_context: dict = {}
    candidates: list[dict] = []
    action_scope: list[dict] = []

    held_tickers: list[str] = []
    if isinstance(holdings, dict):
        for key, row in holdings.items():
            projected = _holding_projection(str(key), row)
            if projected:
                held_tickers.append(projected["canonical_instrument_id"])
                portfolio_context.setdefault("holdings", []).append(projected)

    if mode == "default":
        # 保有 + 直近の正式分析が挙げた銘柄。
        analysis = load_json(base_dir / "ai_portfolio_analysis.json", {})
        source_hashes["ai_portfolio_analysis"] = _file_hash(
            base_dir / "ai_portfolio_analysis.json")
        proposed: list[str] = []
        synthesis = analysis.get("synthesis", {}) if isinstance(analysis, dict) else {}
        for act in (synthesis.get("priority_actions") or []):
            if isinstance(act, dict) and act.get("ticker"):
                proposed.append(str(act["ticker"]))
        portfolio_context["overall_stance"] = synthesis.get("overall_stance")
        universe = list(dict.fromkeys(held_tickers + proposed))[:MAX_CANDIDATES]
        for ticker in universe:
            tech = _technical_projection(tech_rows.get(ticker))
            candidates.append({
                "candidate_id": f"candidate:{ticker}",
                "canonical_instrument_id": ticker,
                "held": ticker in held_tickers,
                "technical": tech,
            })
            action_scope.append({
                "candidate_id": f"candidate:{ticker}",
                "canonical_instrument_id": ticker,
                "allowed_actions": (
                    ["trim", "add", "hold", "watch"] if ticker in held_tickers
                    else ["buy", "watch"]
                ),
                "max_actionability": _actionability_for(tech),
            })

    elif mode == "risk":
        # リスク集中の分析。テクニカルは載せない (この判断に使わない)。
        for name in ("guard_state", "macro_state"):
            source_hashes[name] = _file_hash(base_dir / f"{name}.json")
        guard = load_json(base_dir / "guard_state.json", {})
        macro = load_json(base_dir / "macro_state.json", {})
        if isinstance(guard, dict):
            market_context["guardrails"] = {
                k: guard.get(k) for k in
                ("daily_pnl_pct", "monthly_pnl_pct", "portfolio_value")
                if k in guard
            }
        if isinstance(macro, dict):
            market_context["macro"] = {
                k: macro.get(k) for k in ("fed_rate", "yield_10y", "unemp_rate")
                if k in macro
            }
        # ⚠️ 同じ ticker が複数の holdings 行を持つことがある
        # (特定口座と一般口座で別行になる)。candidate_id は銘柄単位なので
        # 必ず重複排除する。
        for ticker in list(dict.fromkeys(held_tickers))[:MAX_CANDIDATES]:
            candidates.append({
                "candidate_id": f"candidate:{ticker}",
                "canonical_instrument_id": ticker,
                "held": True,
            })
            action_scope.append({
                "candidate_id": f"candidate:{ticker}",
                "canonical_instrument_id": ticker,
                "allowed_actions": ["trim", "hedge", "hold", "watch"],
                "max_actionability": "review",
            })

    else:  # nisa
        source_hashes["nisa_portfolio"] = _file_hash(base_dir / "nisa_portfolio.json")
        nisa = load_json(base_dir / "nisa_portfolio.json", {})
        if isinstance(nisa, dict):
            # owner ごとの内訳も **名前も** 出さない。Agent が枠の消化を
            # 論じるのに要るのは「何人分の枠があるか」だけで、誰かは要らない。
            portfolio_context["nisa"] = {
                "last_updated": nisa.get("last_updated"),
                "owner_count": len([k for k in nisa.keys() if k != "last_updated"]),
            }
        screen = load_json(base_dir / "long_term_screen_results.json", {})
        source_hashes["long_term_screen_results"] = _file_hash(
            base_dir / "long_term_screen_results.json")
        # long_term_screener の合格リストは "passed"。"candidates" では無い
        # —— 名前を間違えても load_json は既定値を返すだけで、候補が丸ごと
        # 空になっても誰も気づかない (508e948 / screen_results_us.json と
        # 同じ静かな取りこぼし)。実データで件数を確認して配線すること。
        rows = screen.get("passed") if isinstance(screen, dict) else None
        for row in (rows or [])[:MAX_CANDIDATES]:
            if not isinstance(row, dict):
                continue
            ticker = str(row.get("ticker") or "").strip()
            if not ticker:
                continue
            tech = _technical_projection(tech_rows.get(ticker))
            candidates.append({
                "candidate_id": f"candidate:{ticker}",
                "canonical_instrument_id": ticker,
                "held": ticker in held_tickers,
                "technical": tech,
            })
            action_scope.append({
                "candidate_id": f"candidate:{ticker}",
                "canonical_instrument_id": ticker,
                "allowed_actions": ["buy", "watch"],
                "max_actionability": _actionability_for(tech),
            })

    projection = {
        "schema_version": SCHEMA_VERSION,
        "mode": mode,
        "analysis_id": analysis_id or "",
        # ⚠️ hash の中に入れる。freshness の解釈を変えるため、これが違えば
        # 別の projection として扱われねばならない。
        "evaluation_as_of": now.isoformat(),
        "source_hashes": {k: v for k, v in source_hashes.items() if v},
        "portfolio_context": portfolio_context,
        "market_context": market_context,
        "candidates": candidates,
        "action_scope": action_scope,
    }
    validate_projection(projection)
    return projection


# ──────────────────────────────────────────────────────────────
#  projection の検証
# ──────────────────────────────────────────────────────────────

_PROJECTION_KEYS = {
    "schema_version", "mode", "analysis_id", "evaluation_as_of",
    "source_hashes", "portfolio_context", "market_context",
    "candidates", "action_scope",
}


def validate_projection(projection: object) -> None:
    """envelope の形と、漏れてはいけない内容が無いことを検査する。"""
    if not isinstance(projection, dict):
        raise ProjectionError("projection must be a dict")
    extra = set(projection) - _PROJECTION_KEYS
    if extra:
        raise ProjectionError(f"unexpected projection keys: {sorted(extra)}")
    missing = _PROJECTION_KEYS - set(projection)
    if missing:
        raise ProjectionError(f"missing projection keys: {sorted(missing)}")
    if projection["schema_version"] != SCHEMA_VERSION:
        raise ProjectionError(f"bad schema_version: {projection['schema_version']!r}")
    if projection["mode"] not in MODES:
        raise ProjectionError(f"bad mode: {projection['mode']!r}")

    scope_ids = set()
    for entry in projection["action_scope"]:
        if not isinstance(entry, dict):
            raise ProjectionError("action_scope entries must be dicts")
        cid = entry.get("candidate_id")
        if not cid or cid in scope_ids:
            raise ProjectionError(f"bad or duplicate candidate_id: {cid!r}")
        scope_ids.add(cid)
        if entry.get("max_actionability") not in ACTIONABILITY_ORDER:
            raise ProjectionError(
                f"bad max_actionability: {entry.get('max_actionability')!r}")
        if not isinstance(entry.get("allowed_actions"), list) or not entry["allowed_actions"]:
            raise ProjectionError(f"empty allowed_actions for {cid!r}")

    for candidate in projection["candidates"]:
        if not isinstance(candidate, dict):
            raise ProjectionError("candidates entries must be dicts")
        if candidate.get("candidate_id") not in scope_ids:
            raise ProjectionError(
                f"candidate without action_scope: {candidate.get('candidate_id')!r}")
        tech = candidate.get("technical")
        if isinstance(tech, dict) and not tech.get("usable"):
            leaked = [k for k in ("price", "rsi", "change_5d_pct", "change_20d_pct",
                                  "composite_signal") if k in tech]
            if leaked:
                raise ProjectionError(
                    f"unusable technical row leaked values {leaked} "
                    f"for {candidate.get('candidate_id')!r}")

    # 内容全体を1本の文字列にして禁止パターンを当てる。個別フィールドの
    # allow-list だけだと、将来ネストが増えたときに見落とす。
    blob = canonical_json(projection)
    for pattern in _FORBIDDEN_PATTERNS:
        if pattern.search(blob):
            raise ProjectionError(f"projection contains forbidden pattern: {pattern.pattern}")


# ──────────────────────────────────────────────────────────────
#  プロンプトの生成 (共通 renderer)
# ──────────────────────────────────────────────────────────────

_MODE_BRIEF = {
    "default": (
        "あなたは ALMANAC ポートフォリオ AI です。"
        "与えられた projection だけを根拠に、統合的な当日の方針を出してください。"
    ),
    "risk": (
        "あなたは ALMANAC リスク管理 AI です。"
        "与えられた projection だけを根拠に、集中リスクとガードレール接近を評価してください。"
    ),
    "nisa": (
        "あなたは ALMANAC NISA 戦略 AI です。"
        "与えられた projection だけを根拠に、NISA 枠の消化戦略を立ててください。"
    ),
}

OUTPUT_SCHEMA = {
    "type": "object",
    "additionalProperties": False,
    "required": ["headline", "overall_stance", "actions", "risk_warnings"],
    "properties": {
        "headline": {"type": "string", "maxLength": MAX_STRING_CHARS},
        "overall_stance": {
            "type": "string",
            "enum": ["defensive", "neutral", "moderately_aggressive", "aggressive"],
        },
        "actions": {
            "type": "array",
            "maxItems": MAX_ACTIONS,
            "items": {
                "type": "object",
                "additionalProperties": False,
                "required": ["rank", "candidate_id", "action_type", "actionability", "reason"],
                "properties": {
                    "rank": {"type": "integer", "minimum": 1},
                    "candidate_id": {"type": "string"},
                    "action_type": {"type": "string"},
                    "actionability": {
                        "type": "string",
                        "enum": ["blocked", "watch_only", "review"],
                    },
                    "reason": {"type": "string", "maxLength": MAX_STRING_CHARS},
                },
            },
        },
        "risk_warnings": {
            "type": "array",
            "maxItems": MAX_WARNINGS,
            "items": {"type": "string", "maxLength": MAX_STRING_CHARS},
        },
    },
}


#: Agent へ見せない envelope フィールド。
#: source_hashes はホスト側の「実行中に元データが変わっていないか」検査に
#: 必要だが、キー名が内部ファイル名そのもの (technical_state / holdings …) で、
#: Agent へ渡すと内部構造を教えることになる。hash 対象には残し、
#: プロンプトからは落とす。
_AGENT_HIDDEN_FIELDS = ("source_hashes",)


def agent_visible_projection(projection: dict) -> dict:
    """projection のうち Agent に見せてよい部分だけを返す。"""
    return {k: v for k, v in projection.items() if k not in _AGENT_HIDDEN_FIELDS}


def build_agent_prompt(projection: dict) -> str:
    """projection を Agent への入力テキストへ。ファイルパスも内部ファイル名も
    一切渡さない。"""
    validate_projection(projection)
    projection = agent_visible_projection(projection)
    return "\n".join([
        _MODE_BRIEF[projection["mode"]],
        "",
        "## 厳守事項",
        "- ここに書かれた projection の中の情報だけを根拠にすること。",
        "- ファイルを読もうとしないこと（ツールは与えられていない）。",
        "- actions の candidate_id は action_scope にあるものだけを使うこと。",
        "- action_type は、その candidate の allowed_actions の中から選ぶこと。",
        "- actionability は max_actionability を超えないこと。",
        "- technical.usable が false の銘柄は、数値が無いことを前提に扱うこと"
        "（推測して数値を作らない）。",
        "",
        "## projection",
        canonical_json(projection),
        "",
        "## 出力",
        "指定された JSON スキーマに従って構造化出力のみを返すこと。",
    ])


# ──────────────────────────────────────────────────────────────
#  Agent 出力のホスト側検証
# ──────────────────────────────────────────────────────────────

def validate_agent_output(
    output: object,
    projection: dict,
    *,
    base_dir: Path | None = None,
) -> dict:
    """Agent の構造化出力を検証し、ticker をホスト側で復元して返す。

    スキーマ検証だけでは足りない (Codex レビュー round 11):
    Agent が projection に無い銘柄を提案したり、候補の上限を超えた
    actionability へ昇格させたりできてしまう。candidate_id を唯一の参照に
    することで、別銘柄の捏造は構造的に不可能になる。

    失敗時は AgentOutputError を投げる。呼び出し元は**新しい結果を保存せず**、
    last-known-good を維持して監査ログへ隔離すること。
    """
    if not isinstance(output, dict):
        raise AgentOutputError("agent output must be a JSON object")

    allowed_top = set(OUTPUT_SCHEMA["properties"])
    extra = set(output) - allowed_top
    if extra:
        raise AgentOutputError(f"unexpected output keys: {sorted(extra)}")
    for key in OUTPUT_SCHEMA["required"]:
        if key not in output:
            raise AgentOutputError(f"missing output key: {key}")

    stance = output.get("overall_stance")
    if stance not in OUTPUT_SCHEMA["properties"]["overall_stance"]["enum"]:
        raise AgentOutputError(f"bad overall_stance: {stance!r}")

    headline = output.get("headline")
    if not isinstance(headline, str) or len(headline) > MAX_STRING_CHARS:
        raise AgentOutputError("bad headline")

    warnings = output.get("risk_warnings")
    if not isinstance(warnings, list) or len(warnings) > MAX_WARNINGS:
        raise AgentOutputError("bad risk_warnings")
    for w in warnings:
        if not isinstance(w, str) or len(w) > MAX_STRING_CHARS:
            raise AgentOutputError("bad risk_warning entry")

    scope = {e["candidate_id"]: e for e in projection["action_scope"]}
    actions = output.get("actions")
    if not isinstance(actions, list) or len(actions) > MAX_ACTIONS:
        raise AgentOutputError("bad actions")

    seen_ranks: set[int] = set()
    resolved: list[dict] = []
    for action in actions:
        if not isinstance(action, dict):
            raise AgentOutputError("action must be a dict")
        extra = set(action) - set(OUTPUT_SCHEMA["properties"]["actions"]["items"]["properties"])
        if extra:
            raise AgentOutputError(f"unexpected action keys: {sorted(extra)}")

        rank = action.get("rank")
        if not isinstance(rank, int) or rank < 1:
            raise AgentOutputError(f"bad rank: {rank!r}")
        if rank in seen_ranks:
            raise AgentOutputError(f"duplicate rank: {rank}")
        seen_ranks.add(rank)

        cid = action.get("candidate_id")
        entry = scope.get(cid)
        if entry is None:
            raise AgentOutputError(f"candidate_id not in action_scope: {cid!r}")

        action_type = action.get("action_type")
        if action_type not in entry["allowed_actions"]:
            raise AgentOutputError(
                f"action_type {action_type!r} not allowed for {cid!r} "
                f"(allowed: {entry['allowed_actions']})")

        actionability = action.get("actionability")
        if actionability not in ACTIONABILITY_ORDER:
            raise AgentOutputError(f"bad actionability: {actionability!r}")
        if ACTIONABILITY_ORDER[actionability] > ACTIONABILITY_ORDER[entry["max_actionability"]]:
            raise AgentOutputError(
                f"actionability {actionability!r} exceeds max "
                f"{entry['max_actionability']!r} for {cid!r}")

        reason = action.get("reason")
        if not isinstance(reason, str) or len(reason) > MAX_STRING_CHARS:
            raise AgentOutputError(f"bad reason for {cid!r}")

        resolved.append({
            "rank": rank,
            "candidate_id": cid,
            # ⚠️ ticker は Agent の出力からではなく projection から復元する。
            "ticker": entry["canonical_instrument_id"],
            "action_type": action_type,
            "actionability": actionability,
            "reason": reason,
        })

    if base_dir is not None:
        _assert_sources_unchanged(projection, base_dir)

    return {
        "schema_version": SCHEMA_VERSION,
        "mode": projection["mode"],
        "evaluation_as_of": projection["evaluation_as_of"],
        "projection_sha256": projection_sha256(projection),
        "headline": headline,
        "overall_stance": stance,
        "actions": sorted(resolved, key=lambda a: a["rank"]),
        "risk_warnings": list(warnings),
    }


def _assert_sources_unchanged(projection: dict, base_dir: Path) -> None:
    """projection を作ってから元データが変わっていないこと。

    変わっていたら、Agent は既に古い世界像で判断している。保存すると
    「今の state と食い違う推奨」が残るので、失敗として扱う。
    """
    base_dir = Path(base_dir)
    for name, expected in (projection.get("source_hashes") or {}).items():
        current = _file_hash(base_dir / f"{name}.json")
        if current != expected:
            raise AgentOutputError(
                f"source changed while the agent was running: {name}")


# ──────────────────────────────────────────────────────────────
#  Agent 実行 (CLI/API 共通)
# ──────────────────────────────────────────────────────────────

def build_agent_options():
    """ツールを一切与えない ClaudeAgentOptions。

    プロンプトからファイル名を消しても、Read/Bash が残っていれば Agent は
    raw ファイルへ戻れる (Codex レビュー round 11)。ツールそのものを外し、
    構造化出力だけを返させる。disallowed_tools は allowed_tools が空である
    ことの二重の担保 —— SDK の既定が将来変わっても効くようにしておく。
    """
    from claude_agent_sdk import ClaudeAgentOptions

    return ClaudeAgentOptions(
        tools=[],
        allowed_tools=[],
        disallowed_tools=["Read", "Write", "Edit", "Bash", "Glob", "Grep"],
        setting_sources=[],
        output_format=OUTPUT_SCHEMA,
        max_turns=1,
    )


class AgentProtocolViolation(AgentOutputError):
    """Agent がツールを使おうとした。ツールは与えていないので契約違反。"""


def parse_agent_result(raw: object) -> dict:
    """ResultMessage.result を JSON として取り出す。"""
    if isinstance(raw, dict):
        return raw
    if not isinstance(raw, str) or not raw.strip():
        raise AgentOutputError("agent returned no structured output")
    try:
        return json.loads(raw)
    except json.JSONDecodeError as exc:
        raise AgentOutputError(f"agent output is not valid JSON: {exc}") from exc
