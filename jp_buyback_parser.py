"""Deterministic parser for Japanese share-buyback (自己株式取得) disclosures.

攻めバックログ項目4 Phase B (docs/design_jp_event_drift_2026_07.md)。
自己株式取得の開示本文から「発行済株式総数に対する割合」を抽出し、
規模に比例した directional_score に写像する。値が高信頼で取れないときは
None を返し、LLM に推測させない (jp_guidance_parser と同じ設計原則)。

処分 (売出し・希薄化側) は tdnet_fetcher の分類段階で buyback に入らない
前提 (「自己株式の取得」のみが buyback に分類される)。
"""

from __future__ import annotations

import re
import unicodedata
from typing import Optional

PARSER_VERSION = "jp-buyback-1.0"

# 「発行済株式総数（自己株式を除く）に対する割合 2.53%」
# 「発行済株式総数に対する割合：3.1％」 等の定型行。
_RATIO_PAT = re.compile(
    r"発行済(?:み)?株式(?:の)?総数[^\n%％]{0,40}?割合[^\n0-9]{0,10}"
    r"([0-9]{1,2}(?:\.[0-9]+)?)\s*[%％]"
)

# 5% 取得でフル強度 (JP では 3% 超で大型、5% 超は稀な大規模買い)。
_FULL_STRENGTH_RATIO_PCT = 5.0
# パース崩れ対策: 発行済株式総数の 30% 超の自己株取得は現実に極めて稀。
_MAX_PLAUSIBLE_RATIO_PCT = 30.0


def _normalize(text: str) -> str:
    return unicodedata.normalize("NFKC", text or "")


def parse_buyback_ratio_pct(text: str) -> Optional[float]:
    """本文から取得上限の対発行済株式割合 (%) を返す。取れなければ None。

    「自己株式の処分」(処分=放出=希薄化方向) は取得と逆方向のため、本文に
    「処分」が出ている場合は解析しない (誤って正の buyback シグナルにしない)。
    """
    normalized = _normalize(text)
    if "自己株式" not in normalized and "自社株" not in normalized:
        return None
    if "処分" in normalized:
        return None
    match = _RATIO_PAT.search(normalized)
    if not match:
        return None
    try:
        ratio = float(match.group(1))
    except ValueError:
        return None
    if ratio <= 0 or ratio > _MAX_PLAUSIBLE_RATIO_PCT:
        return None
    return ratio


def buyback_directional_score(ratio_pct: float) -> float:
    """取得割合 → [0.2, 1.0] の正の directional_score。

    5% 以上でフル強度 1.0。シャドーブックの既存ピン留めルール
    (|ds|>=0.6 かつ confidence>=0.7) の下では、3% 以上の取得のみが
    シグナルとして発火する — 小規模な形式的取得は自然に落ちる。
    """
    return max(0.2, min(ratio_pct / _FULL_STRENGTH_RATIO_PCT, 1.0))
