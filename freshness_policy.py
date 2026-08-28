"""Decision-input freshness contracts shared by refreshers and snapshots.

``refresh_after_hours`` is when the producer must refresh an input.
``stale_after_hours`` is when the frozen decision snapshot must reject it.
Every refreshable source must keep a positive safety margin:

    refresh_after_hours < stale_after_hours

Keeping both values in one registry prevents a daily job from skipping a
refresh at exactly the same boundary at which the downstream snapshot becomes
stale.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Optional


@dataclass(frozen=True)
class FreshnessPolicy:
    refresh_after_hours: Optional[float]
    stale_after_hours: float

    def validate(self, source: str) -> None:
        if self.stale_after_hours <= 0:
            raise ValueError(f"{source}: stale_after_hours must be positive")
        if self.refresh_after_hours is None:
            return
        if self.refresh_after_hours < 0:
            raise ValueError(f"{source}: refresh_after_hours must be non-negative")
        if self.refresh_after_hours >= self.stale_after_hours:
            raise ValueError(
                f"{source}: refresh_after_hours ({self.refresh_after_hours}) "
                f"must be less than stale_after_hours ({self.stale_after_hours})"
            )


SOURCE_FRESHNESS_POLICIES: dict[str, FreshnessPolicy] = {
    # holdings/cash には定期更新の生産者がいない (楽天CSV取込か本人の表明でしか
    # 動かない)。そこに短い壁時計の失効を課すと、時間が経つこと自体が停止条件に
    # なり、放置すれば必ず全候補が review に落ちる (2026-08: 6営業日連続0件)。
    #
    # 保有・現金を実際に狂わせるのは時間ではなく「記録されていない約定・入出金」
    # という出来事なので、停止はその証拠 (未適用の broker確認済み約定) を掴んだ
    # ときに掛ける — build_base_snapshot が holdings/cash に対して判定する。
    # 壁時計は「そろそろ照合を」という助言 (refresh) に降格し、
    # それでも何も分からない状態が続いた場合の最終防衛線として 30日を残す。
    "holdings": FreshnessPolicy(refresh_after_hours=96.0, stale_after_hours=720.0),
    "cash": FreshnessPolicy(refresh_after_hours=96.0, stale_after_hours=720.0),
    "technical": FreshnessPolicy(refresh_after_hours=4.0, stale_after_hours=8.0),
    "fx": FreshnessPolicy(refresh_after_hours=None, stale_after_hours=24.0),
    "macro": FreshnessPolicy(refresh_after_hours=12.0, stale_after_hours=24.0),
    "news": FreshnessPolicy(refresh_after_hours=6.0, stale_after_hours=12.0),
    "screening": FreshnessPolicy(refresh_after_hours=None, stale_after_hours=72.0),
    # 長期スクリーニングだけは cron が日・木の週2回 (0 7 * * 0,4)。日曜→木曜は
    # 96h 空くので、日次ソースと同じ 72h を課すと毎サイクル必ず stale になり、
    # 「最も古いものを採用」する screening カテゴリ全体を道連れにする
    # (2026-08-05: 日次2ファイルは 0.0h/13.4h と新鮮なのに、この1本が 73.6h で
    # 全発注候補が review に落ちていた)。生産者の実周期を refresh 側に明示し、
    # stale はそれを上回る値にして契約 refresh < stale を成立させる。
    "screening_long_term": FreshnessPolicy(refresh_after_hours=96.0, stale_after_hours=120.0),
    "options": FreshnessPolicy(refresh_after_hours=12.0, stale_after_hours=24.0),
    # news_topic / social_topic は平日 18:25 / 18:55 に生成され、翌営業日の
    # 朝 06:15 の統合分析 (com.almanac.ai-analysis) が消費する。
    #   平日:   18:25 生成 → 翌 06:15 消費 = 約 12h
    #   金曜分: 18:25 生成 → 月 06:15 消費 = 約 60h (週末を挟む)
    # 単純な 12h 固定にすると金曜生成分が月曜朝に必ず失効し、週明けだけ
    # 材料コンテキストが落ちる。週末を含む実スケジュールから 72h を採り、
    # refresh は日次生産者の実周期 (平日毎日) に合わせて 30h とする。
    # どちらも最終分析への補助コンテキストであり執行ゲートではないため、
    # 週末をまたいだ再利用は許容し、鮮度は run_status とともに可視化する。
    "news_topic": FreshnessPolicy(refresh_after_hours=30.0, stale_after_hours=72.0),
    "social_topic": FreshnessPolicy(refresh_after_hours=30.0, stale_after_hours=72.0),
}


def get_freshness_policy(source: str) -> FreshnessPolicy:
    try:
        policy = SOURCE_FRESHNESS_POLICIES[source]
    except KeyError as exc:
        raise KeyError(f"unregistered decision-input source: {source}") from exc
    policy.validate(source)
    return policy


def refresh_after_hours(source: str, override: float | None = None) -> float:
    """Return a validated refresh threshold for a refreshable source."""
    policy = get_freshness_policy(source)
    value = policy.refresh_after_hours if override is None else float(override)
    if value is None:
        raise ValueError(f"{source}: source has no periodic refresh contract")
    overridden = FreshnessPolicy(
        refresh_after_hours=float(value),
        stale_after_hours=policy.stale_after_hours,
    )
    overridden.validate(source)
    return float(value)


def stale_after_hours(source: str) -> float:
    return get_freshness_policy(source).stale_after_hours


def validate_all_freshness_policies() -> None:
    for source, policy in SOURCE_FRESHNESS_POLICIES.items():
        policy.validate(source)
