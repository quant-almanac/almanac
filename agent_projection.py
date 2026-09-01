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
from zoneinfo import ZoneInfo

from technical_quality import DEGRADED, USABLE, classify_technical_row
from utils import load_json

SCHEMA_VERSION = "agent_projection_v1"
MODES = ("default", "risk", "nisa")

#: 現時点で実行してよいモード。
#:
#: risk と NISA は projection の判断材料がまだ正しくない (Codex レビュー
#: round 14)。
#:   - risk: 証券会社評価額が 2026-07-28 時点で、行単位の最新価格/NAV・
#:     評価基準日・評価完全性を持たない。今日の集中リスクとして扱えない。
#:   - nisa: 残枠が owner 別 attestation・基準日後の約定/注文・
#:     wallet 制約・積立予約・名義間移動禁止を反映していない。
#:     到達可能額としては attestation まで 0 扱いが正しい。
#: 直るまでは **実行させない**。projection が作れてしまうと、いずれ
#: 誰かが呼ぶ。
ENABLED_MODES = ("default",)


class ModeDisabledError(ValueError):
    """このモードは現在無効。"""

# Agent 出力の上限。長大な文字列でプロンプト/保存を膨らませない。
MAX_STRING_CHARS = 2000
MAX_ACTIONS = 20
MAX_WARNINGS = 20

# 銘柄あたりの候補上限 (プロンプト長の制御)。
MAX_CANDIDATES = 40

# actionability の順序。Agent はこれを超えて昇格できない。
ACTIONABILITY_ORDER = {"blocked": 0, "watch_only": 1, "review": 2}

#: stance の攻撃度。補助 Agent はこれを正式分析より上げられない。
STANCE_ORDER = {"defensive": 0, "neutral": 1,
                "moderately_aggressive": 2, "aggressive": 3}

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


#: 正式分析がこれより古ければ default モードを走らせない。
ANALYSIS_MAX_AGE_HOURS = 24.0

#: 時計ずれの許容幅。これを超えて未来の as_of は壊れているとみなす。
FUTURE_TOLERANCE_HOURS = 1.0


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


class RequiredInputError(ProjectionError):
    """必須入力が欠損・破損している。"""


def read_hashed_json(path: Path, default, *, required: bool = False):
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
    except OSError as exc:
        if required:
            raise RequiredInputError(f"required input missing: {Path(path).name}") from exc
        return default, None
    digest = hashlib.sha256(raw).hexdigest()
    try:
        return json.loads(raw.decode("utf-8")), digest
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        if required:
            # ⚠️ 破損した bytes の hash だけを残して既定値で続行しては
            # ならない。空の分析が last-known-good を上書きできてしまう
            # (Codex レビュー round 13 で再現)。
            raise RequiredInputError(
                f"required input is not valid JSON: {Path(path).name}") from exc
        return default, digest


# ──────────────────────────────────────────────────────────────
#  projection の構築
# ──────────────────────────────────────────────────────────────

def _recomputed_freshness(row: object, now: datetime, *, ticker: str = "") -> str | None:
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

        # ⚠️ ticker は引数で受け取る。technical_state.json の行は
        # ticker フィールドを持たない (実測 0/72) ので、行から取ると空文字に
        # なり _session_lag が既定の NYSE カレンダーで判定してしまう。
        # JP 銘柄が米国カレンダーで評価され、1日古い行が fresh に見える
        # (Codex レビュー round 13)。
        lag = _session_lag(
            ticker,
            _date.fromisoformat(str(as_of)[:10]),
            expected=_last_completed_session(ticker, now=now),
        )
        return _freshness_status(lag)
    except Exception:
        return "unknown"


def _technical_projection(row: object, *, now: datetime | None = None,
                          ticker: str = "") -> dict:
    """テクニカル行を projection 形へ。使えない行には数値を入れない。"""
    verdict, reason = classify_technical_row(row)
    if verdict in (USABLE, DEGRADED) and now is not None:
        # 保存済み判定を通っても、今のラグで見直して stale なら落とす。
        live = _recomputed_freshness(row, now, ticker=ticker)
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


def _exposure_rows(held: list[str], holdings: object, tech_rows: object,
                   *, fx_usdjpy: float | None) -> list[dict]:
    """銘柄ごとの **JPY 建て** 評価額と構成比。

    ⚠️ 通貨を混ぜて合計してはならない。以前は USD の shares×USD価格 と
    JPY の shares×円価格 をそのまま足しており、ある1銘柄が 94% 超という
    無意味な比率になっていた (Codex レビュー round 13)。

    評価額は証券会社照合済みの `broker_position_value_jpy` を第一候補に
    する —— これは既に JPY で、投信 (テクニカル行を持たない) にも入って
    いる。無い行だけ price×shares(×FX) で補い、それも無ければ
    `valuation_available: false` にして推測値を入れない。
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
            # 上場・決済通貨。経済的な通貨エクスポージャ (look-through)
            # ではない —— 例えば米国株を組み入れた円建て投信は JPY と出る。
            "listing_currency": row.get("currency"),
            "_jpy": 0.0,
            "_value_rows": 0,
            "_rows_without_value": 0,
            "_rows_without_as_of": 0,
            "_broker_rows": 0,
            "_other_rows": 0,
            "_oldest_as_of": None,
        })
        # 同じ銘柄が複数口座に分かれている場合は合算する (口座名は出さない)。
        entry["shares"] += shares
        # ⚠️ 口座ごとに JPY 評価額のフィールドが違う。片方だけ見ると、
        # 同一銘柄の別口座ぶんが丸ごと分母から落ちる (レビューで、
        # 複数銘柄にわたる百万円規模の欠落を実測)。
        # 行ごとに「JPY 額を持っているか」を数え、持たない行があれば
        # 完全性フラグを落とす。
        row_jpy = None
        used_field = None
        for field in ("broker_position_value_jpy", "current_value_jpy"):
            try:
                value = row.get(field)
                if value is not None:
                    row_jpy = float(value)
                    used_field = field
                    break
            except (TypeError, ValueError):
                continue
        if row_jpy is None:
            entry["_rows_without_value"] += 1
        else:
            entry["_jpy"] += row_jpy
            entry["_value_rows"] += 1
            if used_field == "broker_position_value_jpy":
                entry["_broker_rows"] += 1
            else:
                entry["_other_rows"] += 1
            as_of = row.get("broker_cost_basis_as_of") or row.get("source_as_of")
            if as_of:
                previous = entry.get("_oldest_as_of")
                entry["_oldest_as_of"] = (
                    min(previous, str(as_of)) if previous else str(as_of))
            else:
                entry["_rows_without_as_of"] += 1

    tech = tech_rows if isinstance(tech_rows, dict) else {}
    for ticker, entry in rows.items():
        value_rows = entry.pop("_value_rows")
        missing_rows = entry.pop("_rows_without_value")
        rows_without_as_of = entry.pop("_rows_without_as_of")
        broker_rows = entry.pop("_broker_rows")
        other_rows = entry.pop("_other_rows")
        jpy_total = entry.pop("_jpy")
        oldest_as_of = entry.pop("_oldest_as_of")
        if value_rows:
            entry["market_value_jpy"] = round(jpy_total, 2)
            entry["valuation_available"] = True
            # ⚠️ 金額の完全性・時点の完全性・source を **分けて** 持つ。
            # 一括の valuation_complete だと、評価基準日が欠けている行を
            # 検知できず、混在した source も一律に見えてしまう
            # (レビューで指摘)。
            entry["valuation_source"] = (
                "broker_reconciled" if broker_rows and not other_rows
                else "mixed" if broker_rows and other_rows
                else "position_value")
            entry["valuation_as_of"] = oldest_as_of
            entry["amount_complete"] = missing_rows == 0
            entry["as_of_complete"] = rows_without_as_of == 0
            if missing_rows:
                entry["rows_without_valuation"] = missing_rows
            if rows_without_as_of:
                entry["rows_without_as_of"] = rows_without_as_of
            continue
        price = None
        raw = tech.get(ticker)
        if isinstance(raw, dict) and classify_technical_row(raw)[0] in (USABLE, DEGRADED):
            try:
                price = float(raw.get("price"))
            except (TypeError, ValueError):
                price = None
        currency = str(entry.get("listing_currency") or "").upper()
        if price is None:
            entry["valuation_available"] = False
            continue
        if currency == "JPY":
            entry["market_value_jpy"] = round(entry["shares"] * price, 2)
        elif currency == "USD" and fx_usdjpy:
            entry["market_value_jpy"] = round(entry["shares"] * price * fx_usdjpy, 2)
        else:
            # 換算レートが無い通貨は金額を出さない。混ぜるより欠く方が安全。
            entry["valuation_available"] = False
            continue
        entry["valuation_source"] = "price_times_shares"
        entry["valuation_available"] = True
        entry["valuation_as_of"] = (
            raw.get("data_as_of") if isinstance(raw, dict) else None)
        entry["amount_complete"] = True
        entry["as_of_complete"] = entry["valuation_as_of"] is not None

    valued = [e for e in rows.values() if e.get("valuation_available")]
    total = sum(e["market_value_jpy"] for e in valued) or 0.0
    for entry in valued:
        entry["weight_pct"] = (
            round(entry["market_value_jpy"] / total * 100.0, 2) if total else None
        )
    return sorted(rows.values(), key=lambda e: e["canonical_instrument_id"])


def _listing_currency_mix(exposures: list[dict]) -> dict:
    """**上場・決済通貨**ベースの構成比 (JPY 換算額で按分)。

    ⚠️ 経済的な通貨エクスポージャではない。円建てで米国株を持つ投信は
    JPY に数えられる。look-through を実装していないので、名前で
    そうと分かるようにしてある (Codex レビュー round 13)。
    """
    totals: dict[str, float] = {}
    for entry in exposures:
        if not entry.get("valuation_available"):
            continue
        currency = str(entry.get("listing_currency") or "unknown")
        totals[currency] = totals.get(currency, 0.0) + entry["market_value_jpy"]
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

    ⚠️ フィールド名は本番の nisa_portfolio.json に実在するものだけを読む。
    以前は tsumitate_remaining / growth_remaining / lifetime_remaining を
    探しており、実ファイルには1つも無いので owner_count だけが渡っていた
    (Codex レビュー round 13)。**予定を差し引いた後の残枠** を使う ——
    予定分を二重に使える額として見せない。
    """
    if not isinstance(nisa, dict):
        return {}
    owners = [k for k in nisa if k not in _NISA_NON_OWNER_KEYS
              and isinstance(nisa.get(k), dict)]
    out: dict = {
        "last_updated": nisa.get("last_updated"),
        "owner_count": len(owners),
        "basis": "after_planned",
    }
    buckets = {
        "tsumitate_remaining_after_planned_jpy": (
            "tsumitate_remaining_after_planned",),
        "growth_remaining_after_planned_jpy": (
            "growth_remaining_after_planned",),
        "lifetime_remaining_jpy": (
            "lifetime_remaining_screen", "growth_lifetime_remaining_screen"),
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


def _assert_fresh_enough(payload: object, *, now: datetime, label: str,
                         max_age_hours: float) -> None:
    """入力の as_of が古すぎないこと。読めなければ古いものとして扱う。"""
    raw = None
    if isinstance(payload, dict):
        for key in ("as_of", "generated_at", "updated_at", "last_updated"):
            if payload.get(key):
                raw = payload[key]
                break
    if not raw:
        raise RequiredInputError(f"{label}: no as_of timestamp to check freshness")
    try:
        stamp = datetime.fromisoformat(str(raw).replace("Z", "+00:00"))
    except ValueError as exc:
        raise RequiredInputError(f"{label}: unreadable as_of {raw!r}") from exc
    if stamp.tzinfo is None:
        # ⚠️ 本番の as_of は "YYYY-MM-DD HH:MM" 形式の **JST の naive 時刻**。
        # UTC として解釈すると9時間ぶん未来にずれ、24h を超えた分析が
        # 制限を素通りする (レビューで実測)。
        stamp = stamp.replace(tzinfo=ZoneInfo("Asia/Tokyo"))
    age_hours = (now - stamp).total_seconds() / 3600.0
    if age_hours > max_age_hours:
        raise RequiredInputError(
            f"{label} is stale: {age_hours:.1f}h old (limit {max_age_hours:.0f}h)")
    if age_hours < -FUTURE_TOLERANCE_HOURS:
        # 未来の as_of は時計ずれか壊れた書き込み。素通しすると
        # 「どれだけ古くても通る」状態になる。
        raise RequiredInputError(
            f"{label} is in the future by {-age_hours:.1f}h — refusing to trust it")


def _is_cash_like(ticker: str, row: object) -> bool:
    """現金相当（現金ウォレット・MMF）か。

    これだけが projection から完全に外れる。口座経路名
    と残高が Agent のプロンプトへ出るのを防ぐ (Codex レビュー round 12)。

    判定は行の型で行う。ticker 名のパターンに頼ると、新しい現金経路が
    増えたときに素通りする。
    """
    if isinstance(row, dict):
        if row.get("investment_type") == "cash":
            return True
        if str(row.get("asset_type") or "") in {"cash", "money_market_fund"}:
            return True
    # 行が無い文脈 (正式 action の ticker だけを見るとき) 用の保険。
    return bool(ticker) and (ticker.startswith("CASH_") or "MMF" in ticker)


def _has_market_data(ticker: str) -> bool:
    """テクニカル指標を引ける銘柄か。

    ⚠️ これは「投資対象か」ではない。`is_pseudo_market_ticker` は
    「yfinance へ送れない」という意味で、投信も True を返す。以前これを
    投資対象の判定に流用しており、実在するコア投信が risk 集計から
    丸ごと消えて比率が狂っていた (レビューで再現)。
    テクニカルを引くかどうかだけに使うこと。
    """
    from pseudo_tickers import is_pseudo_market_ticker

    return bool(ticker) and not is_pseudo_market_ticker(ticker)


def _is_investable(ticker: str, row: object) -> bool:
    """投資対象として projection に載せてよい行か。

    現金相当だけを外す。市場データを引けない投信は**投資対象**なので
    残す —— 集中リスクの分母から落ちると比率が全部狂う。
    """
    return bool(ticker) and not _is_cash_like(ticker, row)


def _official_actions(analysis: object) -> list[dict]:
    """正式分析の priority_actions を ticker -> 判断 に。

    Agent の action_scope はこれを土台にする。保有しているだけの銘柄へ
    add/trim を自動付与すると、正式分析が一度も評価していない銘柄に
    Agent が売買方向を出せてしまう (Codex レビュー round 12)。
    """
    synthesis = analysis.get("synthesis", {}) if isinstance(analysis, dict) else {}
    out: list[dict] = []
    for index, act in enumerate(synthesis.get("priority_actions") or []):
        if not isinstance(act, dict):
            continue
        ticker = str(act.get("ticker") or "").strip()
        if not ticker or not _is_investable(ticker, None):
            continue
        out.append({
            "ticker": ticker,
            "action_type": str(act.get("type") or act.get("action_type") or "").lower(),
            # ⚠️ 欠損・未知値は blocked。review へ昇格させると、正式分析が
            # 判定していない候補に Agent が提案を出せる (Codex レビュー
            # round 14)。実データは4件とも明示値を持つが契約として閉じる。
            "readiness": str(act.get("execution_readiness") or "blocked").lower(),
            # ⚠️ 実データでは recommendation_id が全件 null なので、
            # 入力配列の安定 index を併用する。ticker を dict キーにすると
            # 同一銘柄の2判断が1件へ **上書き** される
            # (Codex レビュー round 13)。
            "recommendation_id": str(act.get("id") or act.get("recommendation_id") or ""),
            "index": index,
        })
    return out


#: 正式判断の readiness を Agent の天井へ写す。
#: blocked は blocked のまま —— Agent が review へ上げてはならない。
_READINESS_TO_CEILING = {"ready": "review", "review": "review", "blocked": "blocked"}


def _ceiling_for_readiness(readiness: str) -> str:
    """未知の readiness 値も blocked にする (allow-list)。"""
    return _READINESS_TO_CEILING.get(readiness, "blocked")


def _scope_for_official(ticker: str, verdict: dict, tech: dict) -> dict:
    """正式判断1件ぶんの action_scope エントリ。

    allowed_actions は **元の方向 + 見送り系** だけ。trim を add へ反転
    させない。天井は正式 readiness とテクニカル品質の厳しい方。
    """
    direction = verdict["action_type"]
    allowed = [a for a in (direction, "watch", "hold") if a]
    ceiling = _ceiling_for_readiness(verdict["readiness"])
    if not tech.get("usable"):
        # ⚠️ 上書きではなく **厳しい方** を採る。以前は無条件代入で、
        # blocked の候補が watch_only へ **緩和** されていた
        # (Codex レビュー round 13 で再現: blocked + unusable -> watch_only)。
        ceiling = min(ceiling, "watch_only", key=lambda c: ACTIONABILITY_ORDER[c])
    if ceiling == "blocked":
        # blocked は提案そのものを許さない。見るだけ。
        allowed = ["watch"]
    return {
        # candidate_id は ticker ではなく **正式判断の識別子**。同じ銘柄に
        # 複数の推奨がある場合に取り違えないため。
        "candidate_id": (f"candidate:{verdict['recommendation_id']}"
                         if verdict.get("recommendation_id")
                         else f"candidate:{verdict.get('index', 0)}:{ticker}:{direction or 'na'}"),
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
    if mode not in ENABLED_MODES:
        raise ModeDisabledError(
            f"mode {mode!r} is disabled: its projection inputs are not yet "
            f"trustworthy (see ENABLED_MODES)")
    base_dir = Path(base_dir)
    now = now or datetime.now(timezone.utc)

    # 内容とハッシュは必ず同じ bytes から作る (read_hashed_json 参照)。
    tech_state, tech_hash = read_hashed_json(
        base_dir / "technical_state.json", {}, required=True)
    holdings, holdings_hash = read_hashed_json(
        base_dir / "holdings.json", {}, required=True)
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
        # 正式分析は default モードの唯一の判断材料。欠損・破損・stale の
        # まま進むと、空の scope で「アクション無し」を保存して
        # last-known-good を消す (Codex レビュー round 13)。
        analysis, analysis_hash = read_hashed_json(
            base_dir / "ai_portfolio_analysis.json", {}, required=True)
        source_hashes["ai_portfolio_analysis"] = analysis_hash
        _assert_fresh_enough(analysis, now=now, label="ai_portfolio_analysis",
                             max_age_hours=ANALYSIS_MAX_AGE_HOURS)
        synthesis = analysis.get("synthesis", {}) if isinstance(analysis, dict) else {}
        portfolio_context["overall_stance"] = synthesis.get("overall_stance")
        # Agent はこれより攻撃側の stance を返せない (validate_agent_output)。
        portfolio_context["max_overall_stance"] = synthesis.get("overall_stance")

        official = _official_actions(analysis)
        for verdict in official:
            ticker = verdict["ticker"]
            tech = _technical_projection(tech_rows.get(ticker), now=now, ticker=ticker)
            scope = _scope_for_official(ticker, verdict, tech)
            if any(e["candidate_id"] == scope["candidate_id"] for e in action_scope):
                # 安定 index を入れてもなお衝突するなら入力が壊れている。
                # 上書きせず失敗させる。
                raise RequiredInputError(
                    f"duplicate candidate_id from official actions: {scope['candidate_id']}")
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
        account, account_hash = read_hashed_json(base_dir / "account.json", {})
        source_hashes["account"] = account_hash
        try:
            fx = float((account or {}).get("fx_rate_usdjpy") or 0.0) or None
        except (TypeError, ValueError):
            fx = None
        exposures = _exposure_rows(held_tickers, holdings, tech_rows, fx_usdjpy=fx)
        if exposures:
            portfolio_context["exposures"] = exposures
            portfolio_context["listing_currency_mix_pct"] = _listing_currency_mix(exposures)
            portfolio_context["fx_usdjpy_used"] = fx
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
            tech = _technical_projection(tech_rows.get(ticker), now=now, ticker=ticker)
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
    if not projection["action_scope"]:
        # 候補ゼロの projection を Agent へ渡しても、返るのは空の分析だけで、
        # それが保存されると last-known-good を消す。入力側の異常として扱う。
        raise RequiredInputError(
            f"{mode}: no candidates in scope — refusing to run the agent")
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
    # ⚠️ 補助 Agent は正式分析より攻撃側へ上げられない。構造化 action を
    # 縛っても stance が自由なら、そこから攻撃的な解釈が下流へ伝わる
    # (Codex レビュー round 13)。
    official_stance = (projection.get("portfolio_context") or {}).get("max_overall_stance")
    if official_stance in STANCE_ORDER and stance in STANCE_ORDER:
        if STANCE_ORDER[stance] > STANCE_ORDER[official_stance]:
            raise AgentOutputError(
                f"overall_stance {stance!r} is more aggressive than the official "
                f"{official_stance!r}")

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
        # headline と risk_warnings は自由文で、scope の外の銘柄に触れうる。
        # 構造化 action へ昇格させないことを、読む側が分かる形で残す。
        "commentary_is_non_actionable": True,
        "mode": projection["mode"],
        "evaluation_as_of": projection["evaluation_as_of"],
        "projection_sha256": projection_sha256(projection),
        "headline": headline,
        "overall_stance": stance,
        "actions": sorted(resolved, key=lambda a: a["rank"]),
        "risk_warnings": list(warnings),
    }


def save_verified_result(path: Path, verified: dict, *, as_of: str) -> bool:
    """検証済み結果を保存する。既存が新しければ書かない (CAS)。

    CLI と API が同時に走ると、遅く終わった **古い** run が新しい結果を
    上書きしうる (Codex レビュー round 13)。保存直前に既存の
    evaluation_as_of を読み、自分の方が古ければ書かずに False を返す。
    """
    from utils import atomic_write_json

    path = Path(path)
    try:
        existing = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        existing = None
    if isinstance(existing, dict):
        previous = existing.get("evaluation_as_of")
        if previous and str(previous) >= str(verified.get("evaluation_as_of") or ""):
            return False
    atomic_write_json(path, {**verified, "as_of": as_of})
    return True


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

#: CLI と API が同じ名前で取る。projection 生成から保存までを直列化する。
AGENT_RUN_LOCK_NAME = "agent_run"
AGENT_RUN_LOCK_TIMEOUT_SECONDS = 5.0

def resolve_agent_model() -> str:
    """Agent が使うモデル ID を model_router から解決する。

    ⚠️ ここへ直接 ID を書かない。以前 ID を固定しており、router が指す
    モデルと食い違って意図せず旧モデルを使っていた (レビューで指摘)。
    モデル ID の一元管理は model_router。
    """
    try:
        from model_router import get_model
        # ⚠️ MODEL_REGISTRY を直接引かない。eco/premium プロファイルと
        # role override を迂回してしまう (レビューで指摘)。
        return get_model("agent_sdk_run")
    except Exception:
        # router を引けない環境では固定せず SDK 既定へ落とさず、
        # 明示的に失敗させる —— 課金経路で「どのモデルか不明」は許さない。
        raise ProjectionError("cannot resolve the agent model from model_router")
AGENT_MAX_BUDGET_USD = 0.50
#: StructuredOutput は内部的には tool_use -> tool_result で配信される。
#: 1回目の出力が JSON Schema に合わない場合、CLI は tool_result で誤りを
#: モデルへ返し、次の1ターンで自己修正する。max_turns=1 だとこの安全な
#: schema 再試行を error_max_turns で打ち切る（2026-09-01 本番で実測）。
#: 実ツールは引き続き0件、費用上限も別に固定しているため、許すのは
#: schema 修正用の追加1ターンだけとする。
AGENT_MAX_TURNS = 2


def build_agent_options_kwargs() -> dict:
    """ClaudeAgentOptions へ渡す kwargs を素の dict として組み立てる。

    ⚠️ 安全契約 (ツール禁止・構造化出力の強制・model/予算の明示) を
    検証するテストが、この dict を直接検査できるようにするために
    `ClaudeAgentOptions(...)` の呼び出しから分離してある。

    以前は4件の安全契約テストが `pytest.importorskip("claude_agent_sdk")`
    で SDK 未インストール環境ではまるごと skip されており、claude-agent-sdk
    が requirements.txt に含まれていなかったため CI ではこの契約が一度も
    検証されていなかった (レビューで指摘: Round 11-13 で塞いだはずの
    核心的な安全契約が CI 上は無検証だった)。
    ⚠️ 「CI 向けの Linux wheel が無いから SDK を入れられない」という
    のは誤りだった —— claude-agent-sdk 0.1.50 は PyPI に
    manylinux_2_17_x86_64/aarch64 wheel を公開しており、CI へ普通に
    インストールできる (レビューで指摘・PyPI のファイル一覧で確認)。
    現在は requirements.txt に追加済みで CI でも実際にインストールされる。
    この dict 分離自体は SDK オブジェクトの内部表現へ依存せず契約を検査
    できるという設計上の利点が残るので維持している —— SDK の有無に
    関わらず、SDK オブジェクトのプロパティ名や型に依存しない、より
    直接的な検査になる。
    """
    return {
        "tools": [],
        "allowed_tools": [],
        "disallowed_tools": ["Read", "Write", "Edit", "Bash", "Glob", "Grep"],
        "setting_sources": [],
        # ⚠️ SDK は {"type": "json_schema", "schema": ...} の形でしか
        # --json-schema を CLI へ渡さない。素のスキーマを渡すと **黙って
        # 無視され**、本番だけ自由形式出力になる
        # (Codex レビュー round 12: 生成コマンドに --json-schema が無かった)。
        "output_format": {"type": "json_schema", "schema": OUTPUT_SCHEMA},
        "max_turns": AGENT_MAX_TURNS,
        # 課金経路なので model と上限を明示する。既定任せにしない
        # (Codex レビュー round 13: model=None / max_budget_usd=None だった)。
        "model": resolve_agent_model(),
        "max_budget_usd": AGENT_MAX_BUDGET_USD,
        # ⚠️ 明示しないと、SDK 同梱の CLI が claude-sonnet-5 向けに
        # レガシーな thinking.type=enabled をデフォルト注入し、API が
        # 400 (invalid_request_error) で拒否する (レビューで指摘・実測:
        # 隔離ライブの初回試行で再現)。同梱 CLI のバージョン (claude-agent-sdk
        # のバージョンに紐づく) に関わらず、明示的に adaptive を要求する。
        "thinking": {"type": "adaptive"},
        "effort": "medium",
    }


def build_agent_options():
    """ツールを一切与えない ClaudeAgentOptions。

    プロンプトからファイル名を消しても、Read/Bash が残っていれば Agent は
    raw ファイルへ戻れる (Codex レビュー round 11)。ツールそのものを外し、
    構造化出力だけを返させる。disallowed_tools は allowed_tools が空である
    ことの二重の担保 —— SDK の既定が将来変わっても効くようにしておく。
    """
    from claude_agent_sdk import ClaudeAgentOptions

    return ClaudeAgentOptions(**build_agent_options_kwargs())


class AgentProtocolViolation(AgentOutputError):
    """Agent がツールを使おうとした。ツールは与えていないので契約違反。"""


# ⚠️ output_format={"type": "json_schema", ...} を要求すると、SDK 同梱の
# CLI はこの名前の ToolUseBlock を使って構造化出力を配信する
# (tools=[] で実ツールを一切与えなくても現れる)。SDK 0.1.50/0.2.145・
# claude-haiku-4-5-20251001/claude-sonnet-5 のいずれの組み合わせでも
# 再現・実測で確認済み — SDK のバージョンやモデルに依存しない、
# 構造化出力の配信機構そのもの。
# この名前を「禁止したツールの使用」と区別しないと、構造化出力を要求する
# 限り**成功する run が存在し得ない** (毎回 AgentProtocolViolation として
# 誤検知される)。実際に本番コードはこの区別を一度もしておらず、隔離ライブの
# 検証で初めて発覚した (レビューで発見)。
STRUCTURED_OUTPUT_TOOL_NAME = "StructuredOutput"


def assert_no_forbidden_tool_use(block) -> None:
    """block が禁止ツールの使用なら AgentProtocolViolation を送出する。

    SDK 自身が構造化出力の配信に使う STRUCTURED_OUTPUT_TOOL_NAME だけは
    実際のツール使用ではないため対象外にする。CLI (portfolio_agent.py) と
    API (api/routes/agent.py) の両方がこの1関数だけを呼ぶことで、
    どちらか片方だけこの区別を持って食い違う事態を避ける
    (呼び出し側は `isinstance(block, ToolUseBlock)` を確認してから渡す —
    この関数自体は claude_agent_sdk の型に依存しないよう `block.name` の
    duck typing のみで判定する)。
    """
    if block.name == STRUCTURED_OUTPUT_TOOL_NAME:
        return
    raise AgentProtocolViolation(f"agent attempted tool use: {block.name}")


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
