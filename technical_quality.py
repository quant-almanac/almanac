"""technical_quality.py — テクニカル行の品質契約を1箇所に集約する。

technical_state.json の各行を「数値を判断根拠にしてよいか」で分類する。
副作用も重い依存も持たない純粋モジュールにしてある —— 以前この述語を
scenario_engine に置いていたが、あちらは import 時に alert 経由で yfinance を
読み、グローバル timeout を書き換える (Codex レビュー round 9 で実測)。
品質契約は「読む側すべて」が通るべきものなので、どこからでも安全に import
できる必要がある。

## 分類

- **usable**: 数値を判断根拠にしてよい
- **degraded**: 数値は出してよいが、基準日を明示し review 扱いにする
- **unusable**: 数値を一切出さない

## 判定 (fail-closed)

以前の述語は「明示的に blocked」「明示的に rebuild_unresolved」だけを
拒否していたため、フィールドが欠損した行・未知の値を持つ行・
freshness_status が stale の行がすべて素通りしていた
(Codex レビュー round 9 で、品質・鮮度フィールドのない RSI=10 の行が
シナリオ条件を成立させ AI へも再注入されるのを再現)。

判断根拠にしてよいのは「良好であると明示的に分かっている行」だけにする:

- data_quality_status が "ok" 以外 (欠損・未知値を含む) -> unusable
- rebuild_unresolved -> unusable
  (直近の全再計算が取得できず、前回取得分の凍結値をそのまま持っている行)
- freshness_status:
    "fresh"                          -> usable
    "degraded"                       -> degraded (1セッション遅延)
    "stale" / "unknown" / 欠損 / 未知 -> unusable
"""

from __future__ import annotations

USABLE = "usable"
DEGRADED = "degraded"
UNUSABLE = "unusable"

# 数値を出してよい分類。degraded を含むかは呼び出し側が選ぶ。
_NUMERIC_OK = {USABLE, DEGRADED}


def classify_quality_axis(row: object) -> tuple[str, str | None]:
    """**品質軸だけ**を見る。rebuild_unresolved も freshness も見ない。

    軸を分けてあるのは、引き継ぎ行 (rebuild_unresolved) で無視してよいのが
    「保存済みの freshness_status」だけだから。品質軸まで一緒に飛ばすと、
    「corporate action で blocked → 次の再計算が取得失敗 → その行が
    carry-forward」という並びで独立した品質 block が消える
    (Codex レビュー round 11 で再現)。
    """
    if not isinstance(row, dict) or not row:
        return UNUSABLE, "technical_row_missing"
    quality = row.get("data_quality_status")
    if quality != "ok":
        # "blocked" だけでなく、欠損・未知値もここで落とす。
        return UNUSABLE, (
            "data_quality_blocked" if quality == "blocked" else "data_quality_unknown"
        )
    return USABLE, None


def classify_freshness_axis(row: object) -> tuple[str, str | None]:
    """**保存済みの鮮度軸だけ**を見る。品質も rebuild_unresolved も見ない。"""
    if not isinstance(row, dict) or not row:
        return UNUSABLE, "technical_row_missing"
    freshness = row.get("freshness_status")
    if freshness == "fresh":
        return USABLE, None
    if freshness == "degraded":
        return DEGRADED, "technical_data_degraded"
    return UNUSABLE, (
        "technical_data_stale" if freshness == "stale" else "technical_freshness_unknown"
    )


def classify_technical_row(row: object) -> tuple[str, str | None]:
    """(分類, 理由コード) を返す。理由コードは unusable/degraded のときだけ。

    両軸 + rebuild_unresolved を合成した「この行の数値を使ってよいか」の
    総合判定。個別に評価したい呼び出し元 (execution_readiness) は
    classify_quality_axis / classify_freshness_axis を直接使うこと。
    """
    if not isinstance(row, dict) or not row:
        return UNUSABLE, "technical_row_missing"

    if row.get("rebuild_unresolved"):
        # 行はあるが、直近の全再計算はこの銘柄を取得できていない。
        # 品質軸の方が重い問題なので、そちらが unusable ならそれを優先して
        # 報告する (理由コードを取り違えないため)。
        _q_verdict, _q_reason = classify_quality_axis(row)
        if _q_verdict == UNUSABLE:
            return UNUSABLE, _q_reason
        return UNUSABLE, "rebuild_unresolved"

    quality_verdict, quality_reason = classify_quality_axis(row)
    if quality_verdict == UNUSABLE:
        return UNUSABLE, quality_reason
    return classify_freshness_axis(row)


def technical_row_is_usable(row: object, *, allow_degraded: bool = True) -> bool:
    """条件判定の根拠にしてよいか。

    allow_degraded=True (既定) は「1セッション遅延なら数値を出してよい」。
    既存の execution_readiness も degraded は blocked ではなく review 扱いに
    しているので、既定をそちらに合わせてある。数値を一切許さない経路だけが
    False を渡すこと。
    """
    verdict, _ = classify_technical_row(row)
    return verdict in _NUMERIC_OK if allow_degraded else verdict == USABLE


def usable_technical_row(tickers_data: object, ticker: object, *,
                         allow_degraded: bool = True) -> dict:
    """判定に使える行だけを返す。使えなければ空 dict。"""
    if not isinstance(tickers_data, dict) or not ticker:
        return {}
    row = tickers_data.get(str(ticker))
    if not isinstance(row, dict):
        return {}
    return row if technical_row_is_usable(row, allow_degraded=allow_degraded) else {}


def describe_unusable(row: object) -> dict:
    """使えない行を、数値を伏せた説明用の dict にする。

    理由と基準日は必ず残す —— これが落ちると、プロンプト側で
    「price=None RSI=None」とだけ出て、なぜ欠けているのか分からなくなる
    (Codex レビュー round 9)。
    """
    verdict, reason = classify_technical_row(row)
    out = {
        "usable": False,
        "reason": reason or "unknown",
        "data_as_of": row.get("data_as_of") if isinstance(row, dict) else None,
    }
    if verdict == DEGRADED:
        out["usable"] = True
    return out


def format_for_prompt(row: object) -> str:
    """行1つをプロンプト向けの短い文字列にする。

    使える行は数値、使えない行は理由と基準日。呼び出し側が個別に
    f-string を組むと、使えない行が「price=None RSI=None」に化けて
    理由が消える (実際そうなっていた)。整形もここに集約する。
    """
    verdict, reason = classify_technical_row(row)
    if verdict == UNUSABLE:
        as_of = (row.get("data_as_of") if isinstance(row, dict) else None) or "不明"
        return f"指標判定不能({reason}, 基準日 {as_of})"

    assert isinstance(row, dict)  # USABLE/DEGRADED は必ず dict
    parts = [
        f"price={row.get('price')}",
        f"RSI={row.get('rsi')}",
        f"5d={row.get('change_5d_pct')}%",
        f"20d={row.get('change_20d_pct')}%",
        f"vol={row.get('volume_ratio')}",
        f"signal={row.get('composite_signal')}",
    ]
    text = " ".join(parts)
    if verdict == DEGRADED:
        text += f" ※1セッション遅延(基準日 {row.get('data_as_of') or '不明'})"
    return text
