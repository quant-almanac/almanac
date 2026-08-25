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
    """入力ファイルの内容ハッシュ。"""
    try:
        return hashlib.sha256(path.read_bytes()).hexdigest()
    except OSError:
        return None


def read_hashed_json(path: Path, default):
    """**同じ bytes から** JSON と SHA-256 を作る。

    ⚠️ 内容を load_json で読み、ハッシュを別途 read_bytes で取ると、その間に
    ファイルが差し替わったとき「古い内容を projection しながら新しい
    ファイルのハッシュを記録する」ことが起きる。source-unchanged 検査は
    通ってしまうので、世代の食い違いに気づけない
    (Codex レビュー round 12 で再現: projection 内 price=100 /
    現在ファイル price=999 / 検査 passed)。
    """
    try:
        raw = Path(path).read_bytes()
    except OSError:
        return default, None
    digest = hashlib.sha256(raw).hexdigest()
    try:
        return json.loads(raw.decode("utf-8")), digest
    except (UnicodeDecodeError, json.JSONDecodeError):
        return default, digest


# ──────────────────────────────────────────────────────────────
#  projection の構築
# ──────────────────────────────────────────────────────────────

def _recomputed_freshness(row: object, now: datetime) -> str | None:
    """保存済みの freshness_status ではなく、data_as_of から今のラグを引き直す。

    ⚠️ 保存値は「その行を計算した時点」の判定で、時間が経っても変わらない。
    evaluation_as_of を hash に入れるだけでは鮮度判定に使われず、
    data_as_of=2026-01-01 / freshness_status=fresh の行が8か月後でも
    usable のままになる (Codex レビュー round 12 で再現)。
    """
    if not isinstance(row, dict):
        return None
    as_of = row.get("data_as_of")
    if not as_of:
        return "unknown"
    try:
        from datetime import date as _date

        from technical_signals import (
            _freshness_status, _last_completed_session, _session_lag,
        )

        ticker = str(row.get("ticker") or "")
        lag = _session_lag(
            ticker,
            _date.fromisoformat(str(as_of)[:10]),
            expected=_last_completed_session(ticker, now=now),
        )
        return _freshness_status(lag)
    except Exception:
        return "unknown"


def _technical_projection(row: object, *, now: datetime | None = None) -> dict:
    """テクニカル行を projection 形へ。使えない行には数値を入れない。"""
    verdict, reason = classify_technical_row(row)
    if verdict in (USABLE, DEGRADED) and now is not None:
        # 保存済み判定を通っても、今のラグで見直して stale なら落とす。
        live = _recomputed_freshness(row, now)
        if live in {"stale", "unknown"}:
            return {
                "usable": False,
                "reason": ("technical_data_stale" if live == "stale"
                           else "technical_freshness_unknown"),
                "data_as_of": row.get("data_as_of") if isinstance(row, dict) else None,
            }
        if live == "degraded":
            verdict, reason = DEGRADED, "technical_data_degraded"
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


def _exposure_rows(held: list[str], holdings: object, tech_rows: object) -> list[dict]:
    """銘柄ごとの評価額と構成比。集中リスクの判断に数量だけでは足りない。

    価格はテクニカル行から取る (使える行だけ)。取れない銘柄は金額を出さず
    `valuation_available: false` にする —— 推測値を入れると、Agent が
    根拠のある比率として読んでしまう。
    """
    rows: dict[str, dict] = {}
    for key, row in (holdings.items() if isinstance(holdings, dict) else []):
        if not isinstance(row, dict):
            continue
        ticker = str(row.get("ticker") or key or "").strip()
        if ticker not in held:
            continue
        try:
            shares = float(row.get("shares") or 0.0)
        except (TypeError, ValueError):
            shares = 0.0
        entry = rows.setdefault(ticker, {
            "canonical_instrument_id": ticker,
            "shares": 0.0,
            "currency": row.get("currency"),
        })
        # 同じ銘柄が複数口座に分かれている場合は合算する (口座名は出さない)。
        entry["shares"] += shares

    tech = tech_rows if isinstance(tech_rows, dict) else {}
    for ticker, entry in rows.items():
        price = None
        raw = tech.get(ticker)
        if isinstance(raw, dict) and classify_technical_row(raw)[0] in (USABLE, DEGRADED):
            try:
                price = float(raw.get("price"))
            except (TypeError, ValueError):
                price = None
        if price is None:
            entry["valuation_available"] = False
        else:
            entry["valuation_available"] = True
            entry["market_value_native"] = round(entry["shares"] * price, 2)

    valued = [e for e in rows.values() if e.get("valuation_available")]
    total = sum(e["market_value_native"] for e in valued) or 0.0
    for entry in valued:
        entry["weight_pct_of_valued"] = (
            round(entry["market_value_native"] / total * 100.0, 2) if total else None
        )
    return sorted(rows.values(), key=lambda e: e["canonical_instrument_id"])


def _currency_mix(exposures: list[dict]) -> dict:
    """通貨別の構成比。世帯・口座ではなく通貨だけで集計する。"""
    totals: dict[str, float] = {}
    for entry in exposures:
        if not entry.get("valuation_available"):
            continue
        currency = str(entry.get("currency") or "unknown")
        totals[currency] = totals.get(currency, 0.0) + entry["market_value_native"]
    grand = sum(totals.values())
    if not grand:
        return {}
    return {k: round(v / grand * 100.0, 2) for k, v in sorted(totals.items())}


#: nisa_portfolio.json のうち owner ではないトップレベルキー。
#: ⚠️ 「last_updated 以外は全部 owner」と数えると、将来 metadata が
#: 増えたときに誤カウントする (Codex レビュー round 12)。
_NISA_NON_OWNER_KEYS = {"last_updated", "as_of", "generated_at", "version",
                        "schema_version", "source", "meta", "metadata"}


def _nisa_capacity(nisa: object) -> dict:
    """枠の残量を世帯合算で。owner 名も口座経路も出さない。

    枠消化戦略を立てるには残枠が要る。owner_count だけでは何も決められない
    (Codex レビュー round 12)。金額は合算値のみで、誰の枠かは出さない。
    """
    if not isinstance(nisa, dict):
        return {}
    owners = [k for k in nisa if k not in _NISA_NON_OWNER_KEYS
              and isinstance(nisa.get(k), dict)]
    out: dict = {"last_updated": nisa.get("last_updated"), "owner_count": len(owners)}
    buckets = {
        "annual_tsumitate_remaining_jpy": ("tsumitate_remaining", "annual_tsumitate_remaining"),
        "annual_growth_remaining_jpy": ("growth_remaining", "annual_growth_remaining"),
        "lifetime_remaining_jpy": ("lifetime_remaining", "remaining_lifetime"),
    }
    for out_key, source_keys in buckets.items():
        total = 0.0
        found = False
        for owner in owners:
            row = nisa[owner]
            for candidate in source_keys:
                if candidate in row:
                    try:
                        total += float(row[candidate] or 0.0)
                        found = True
                    except (TypeError, ValueError):
                        pass
                    break
        if found:
            out[out_key] = round(total)
    return out


def _is_investable(ticker: str, row: object) -> bool:
    """この行を「投資候補」として扱ってよいか。

    ⚠️ 文字列の deny-list ではなく構造で判定する。holdings.json には現金
    ウォレット (CASH_JPY_SBI_WIFE 等)・MMF・投信の疑似ティッカーが通常の
    保有行と同じ形で並んでおり、素通しすると口座経路・世帯構成・現金残高が
    そのまま Agent のプロンプトへ出る (Codex レビュー round 12 で全3モード
    から CASH_JPY_SBI_WIFE / SBI / GS_MMF_USD の漏洩を再現)。

    判定は既存の権威ある集合をそのまま使う —— ここで独自の一覧を作ると、
    新しい現金経路が増えたときにこちらだけ更新漏れになる。
    """
    from pseudo_tickers import is_pseudo_market_ticker
    from technical_signals import SKIP_TICKERS

    if not ticker or ticker in SKIP_TICKERS or is_pseudo_market_ticker(ticker):
        return False
    if isinstance(row, dict):
        if row.get("investment_type") == "cash":
            return False
        if str(row.get("asset_type") or "") in {"cash", "money_market_fund"}:
            return False
    return True


def _official_actions(analysis: object) -> dict[str, dict]:
    """正式分析の priority_actions を ticker -> 判断 に。

    Agent の action_scope はこれを土台にする。保有しているだけの銘柄へ
    add/trim を自動付与すると、正式分析が一度も評価していない銘柄に
    Agent が売買方向を出せてしまう (Codex レビュー round 12)。
    """
    synthesis = analysis.get("synthesis", {}) if isinstance(analysis, dict) else {}
    out: dict[str, dict] = {}
    for act in (synthesis.get("priority_actions") or []):
        if not isinstance(act, dict):
            continue
        ticker = str(act.get("ticker") or "").strip()
        if not ticker or not _is_investable(ticker, None):
            continue
        out[ticker] = {
            "action_type": str(act.get("type") or act.get("action_type") or "").lower(),
            "readiness": str(act.get("execution_readiness") or "review").lower(),
            "recommendation_id": str(act.get("id") or act.get("recommendation_id") or ""),
        }
    return out


#: 正式判断の readiness を Agent の天井へ写す。
#: blocked は blocked のまま —— Agent が review へ上げてはならない。
_READINESS_TO_CEILING = {"ready": "review", "review": "review", "blocked": "blocked"}


def _scope_for_official(ticker: str, verdict: dict, tech: dict) -> dict:
    """正式判断1件ぶんの action_scope エントリ。

    allowed_actions は **元の方向 + 見送り系** だけ。trim を add へ反転
    させない。天井は正式 readiness とテクニカル品質の厳しい方。
    """
    direction = verdict["action_type"]
    allowed = [a for a in (direction, "watch", "hold") if a]
    ceiling = _READINESS_TO_CEILING.get(verdict["readiness"], "blocked")
    if not tech.get("usable"):
        ceiling = "watch_only"
    if ceiling == "blocked":
        # blocked は提案そのものを許さない。見るだけ。
        allowed = ["watch"]
    return {
        # candidate_id は ticker ではなく **正式判断の識別子**。同じ銘柄に
        # 複数の推奨がある場合に取り違えないため。
        "candidate_id": (f"candidate:{verdict['recommendation_id']}"
                         if verdict["recommendation_id"]
                         else f"candidate:{ticker}:{direction or 'na'}"),
        "canonical_instrument_id": ticker,
        "allowed_actions": list(dict.fromkeys(allowed)),
        "max_actionability": ceiling,
    }


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

    # 内容とハッシュは必ず同じ bytes から作る (read_hashed_json 参照)。
    tech_state, tech_hash = read_hashed_json(base_dir / "technical_state.json", {})
    holdings, holdings_hash = read_hashed_json(base_dir / "holdings.json", {})
    tech_rows = tech_state.get("tickers", {}) if isinstance(tech_state, dict) else {}

    source_hashes = {"technical_state": tech_hash, "holdings": holdings_hash}

    portfolio_context: dict = {}
    market_context: dict = {}
    candidates: list[dict] = []
    action_scope: list[dict] = []

    # 投資候補になりうる保有だけを context に載せる。現金・MMF・投信の
    # 疑似ティッカーは候補にも明細にも出さない。
    held_tickers: list[str] = []
    for key, row in (holdings.items() if isinstance(holdings, dict) else []):
        if not isinstance(row, dict):
            continue
        ticker = str(row.get("ticker") or key or "").strip()
        if not _is_investable(ticker, row):
            continue
        projected = _holding_projection(ticker, row)
        if projected:
            held_tickers.append(ticker)
            portfolio_context.setdefault("holdings", []).append(projected)
    held_tickers = list(dict.fromkeys(held_tickers))

    if mode == "default":
        analysis, analysis_hash = read_hashed_json(
            base_dir / "ai_portfolio_analysis.json", {})
        source_hashes["ai_portfolio_analysis"] = analysis_hash
        synthesis = analysis.get("synthesis", {}) if isinstance(analysis, dict) else {}
        portfolio_context["overall_stance"] = synthesis.get("overall_stance")

        official = _official_actions(analysis)
        for ticker, verdict in official.items():
            tech = _technical_projection(tech_rows.get(ticker), now=now)
            scope = _scope_for_official(ticker, verdict, tech)
            action_scope.append(scope)
            candidates.append({
                "candidate_id": scope["candidate_id"],
                "canonical_instrument_id": ticker,
                "held": ticker in held_tickers,
                "official_action_type": verdict["action_type"],
                "official_readiness": verdict["readiness"],
                "technical": tech,
            })

    elif mode == "risk":
        for name in ("guard_state", "macro_state"):
            payload, digest = read_hashed_json(base_dir / f"{name}.json", {})
            source_hashes[name] = digest
            if not isinstance(payload, dict):
                continue
            if name == "guard_state":
                market_context["guardrails"] = {
                    k: payload.get(k) for k in
                    ("daily_pnl_pct", "monthly_pnl_pct", "portfolio_value")
                    if k in payload
                }
            else:
                market_context["macro"] = {
                    k: payload.get(k) for k in ("fed_rate", "yield_10y", "unemp_rate")
                    if k in payload
                }
        # 集中リスクを論じるには数量だけでは足りない (Codex レビュー
        # round 12)。評価額と構成比を projection 側で計算して渡す。
        exposures = _exposure_rows(held_tickers, holdings, tech_rows)
        if exposures:
            portfolio_context["exposures"] = exposures
            portfolio_context["currency_mix_pct"] = _currency_mix(exposures)
        for row in exposures:
            ticker = row["canonical_instrument_id"]
            action_scope.append({
                "candidate_id": f"candidate:{ticker}:risk",
                "canonical_instrument_id": ticker,
                # リスク側は縮小方向と見送りだけ。増やす提案はさせない。
                "allowed_actions": ["trim", "hold", "watch"],
                "max_actionability": "review",
            })
            candidates.append({
                "candidate_id": f"candidate:{ticker}:risk",
                "canonical_instrument_id": ticker,
                "held": True,
            })

    else:  # nisa
        nisa, nisa_hash = read_hashed_json(base_dir / "nisa_portfolio.json", {})
        source_hashes["nisa_portfolio"] = nisa_hash
        portfolio_context["nisa"] = _nisa_capacity(nisa)
        screen, screen_hash = read_hashed_json(
            base_dir / "long_term_screen_results.json", {})
        source_hashes["long_term_screen_results"] = screen_hash
        # 合格リストのキーは "passed" ("candidates" ではない)。名前を
        # 間違えても既定値が返るだけで候補が丸ごと空になる。
        rows = screen.get("passed") if isinstance(screen, dict) else None
        for row in (rows or [])[:MAX_CANDIDATES]:
            if not isinstance(row, dict):
                continue
            ticker = str(row.get("ticker") or "").strip()
            if not _is_investable(ticker, row):
                continue
            tech = _technical_projection(tech_rows.get(ticker), now=now)
            cid = f"candidate:{ticker}:nisa"
            candidates.append({
                "candidate_id": cid,
                "canonical_instrument_id": ticker,
                "held": ticker in held_tickers,
                "technical": tech,
            })
            action_scope.append({
                "candidate_id": cid,
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
    seen_candidates: set[str] = set()
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
        # 同じ候補について複数の提案を出させない。相反する方向を並べられると
        # どちらを採るかがホスト側で決まらない (Codex レビュー round 12)。
        if cid in seen_candidates:
            raise AgentOutputError(f"duplicate candidate: {cid!r}")
        seen_candidates.add(cid)

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

    # rank は 1..N の連番。1, 99 のような飛びを許すと「順位」ではなく
    # 任意のラベルになり、表示側の並びが意味を持たなくなる。
    if seen_ranks and seen_ranks != set(range(1, len(seen_ranks) + 1)):
        raise AgentOutputError(f"ranks must be 1..N without gaps: {sorted(seen_ranks)}")

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
        # ⚠️ SDK は {"type": "json_schema", "schema": ...} の形でしか
        # --json-schema を CLI へ渡さない。素のスキーマを渡すと **黙って
        # 無視され**、本番だけ自由形式出力になる
        # (Codex レビュー round 12: 生成コマンドに --json-schema が無かった)。
        output_format={"type": "json_schema", "schema": OUTPUT_SCHEMA},
        max_turns=1,
    )


class AgentProtocolViolation(AgentOutputError):
    """Agent がツールを使おうとした。ツールは与えていないので契約違反。"""


def parse_agent_result(message: object) -> dict:
    """ResultMessage から構造化出力を取り出す。

    ⚠️ structured_output を優先し、無ければ fail-closed。result 文字列を
    当てにすると、スキーマが効いていないとき (SDK へ渡す形を間違えている
    等) に自由形式のテキストをそのまま受け入れてしまう
    (Codex レビュー round 12)。
    後方互換のため dict / JSON 文字列も受けるが、それはテスト用の経路で、
    本番は structured_output を通る。
    """
    structured = getattr(message, "structured_output", None)
    if isinstance(structured, dict):
        return structured
    if structured is not None:
        raise AgentOutputError(
            f"structured_output has unexpected type: {type(structured).__name__}")

    if isinstance(message, dict):
        return message
    if isinstance(message, str):
        if not message.strip():
            raise AgentOutputError("agent returned no structured output")
        try:
            return json.loads(message)
        except json.JSONDecodeError as exc:
            raise AgentOutputError(f"agent output is not valid JSON: {exc}") from exc

    raise AgentOutputError(
        "agent returned no structured output (structured_output is missing)")
