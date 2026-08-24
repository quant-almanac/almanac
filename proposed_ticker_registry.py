"""proposed_ticker_registry.py — AI が名指しした新規銘柄をテクニカル取得対象に残す。

モデルは保有・セクターETF・主要指数・シナリオ playbook・直近スクリーナー候補の
どこにも属さない銘柄を提案できる。朝の再計算はそれを値付けしていないので
execution_readiness は technical_data_missing で必ず止める (それ自体は正しい)。

technical_signals.ensure_technical_coverage が実行内でその行を補完するが、
crontab は平日 08:30 / 12:00 / 17:05 に get_technical_context() を叩き、
compute_technical_state() は technical_state.json を**丸ごと置換**する。
ユニバースに居ない銘柄の行は毎回消えるので、補完だけでは
「朝 ready → 正午 technical_data_missing」という間欠障害になる。

このレジストリは補完に成功した銘柄を
technical_signals.CANDIDATE_UNIVERSE_FILES 経由でユニバースへ戻し、
再計算がネイティブに行を作れるようにする。

設計上の制約が2つある。

1. **ポジティブキャッシュにすること。**
   analyst._ensure_technical_state_fresh は
   `source_health.missing_count == 0` と
   `_build_ticker_universe() ⊆ cached_tickers` の両方を要求する。解決できない
   銘柄をユニバースへ登録すると両条件が恒久的に false になり、分析のたびに
   全銘柄の強制再計算がエラーも警告も無く走り続ける。よって
   「実際にテクニカル行を生成できた銘柄」だけを登録し (record の resolved)、
   再計算で落ちた銘柄は evict_unresolved で追い出す。

2. **last_seen の新しい順に並べること。**
   _build_ticker_universe は rows[:CANDIDATE_TICKERS_PER_FILE] で切る。
   古い順のままだと最新の提案が黙って落ちる — screen_results_us.json 事件
   (508e948) と同じ「静かな取りこぼし」になる。MAX_ENTRIES を消費側の
   スライスと一致させ、切り捨てが不可視にならないようにする。
"""

from __future__ import annotations

from datetime import date, datetime, timedelta
from pathlib import Path

from utils import atomic_write_json, load_json, process_lock, LockBusy

# _build_ticker_universe が既に解釈できる形 (candidates キー・ticker フィールド)
# で書く。ユニバース解析コードを新規に書かないため。
REGISTRY_FILENAME = "proposed_ticker_candidates.json"

# このレジストリファイルへの読込→更新→書込を直列化するロック名。
# technical_signals.TECHNICAL_STATE_LOCK_NAME と意図的に同じ文字列にしてある
# (utils.process_lock はロック名をそのままファイル名に使うので、同じ名前を
# 使う全呼び出しが同じ OS ロックを取り合う)。record()/evict_unresolved() の
# read-modify-write に共通ロックが無いと、両者が同時に古い内容を読んで
# 互いの更新を上書きし合う (Codex レビュー round 5 で実スレッド再現:
# eviction が旧registryを読んで書込み待ちの間に record() が割り込み登録、
# eviction 再開後の書込みでその登録が消えた)。加えて
# technical_signals.ensure_technical_coverage は、この名前のロックを
# 保持したまま record_already_locked() を直接呼ぶことで、technical_state.json
# への行追加とレジストリ登録を1つの臨界区間で完結させる —— そうしないと
# 「行はあるがレジストリに無い」窓を全再計算に観測されうる
# (Codex レビュー round 4/5 で再現)。
REGISTRY_LOCK_NAME = "technical_state"
# JSON の読み書きだけの短い区間なので、通常はミリ秒で解放される。
REGISTRY_LOCK_TIMEOUT_SECONDS = 10.0

# technical_signals.CANDIDATE_TICKERS_PER_FILE 以下に保つこと。消費側の
# スライスより大きいと、超過分が読まれないまま黙って捨てられる。
MAX_ENTRIES = 30
TTL_DAYS = 21

# yfinance の全面障害で「今日は誰も解決できなかった」だけの状態と、
# 個別銘柄が本当に上場廃止された状態を区別できない。再計算のカバレッジが
# この閾値を割った日は追い出しごとスキップし、レジストリを守る。
MIN_REBUILD_COVERAGE_FOR_EVICTION = 0.5

# coverage が閾値を超えていても、1回の再計算で1銘柄だけ取得に失敗する
# ことはある (yfinance の一時的なレート制限・単一銘柄のAPI不調)。それを
# 上場廃止と区別できないまま即時追い出すと、一時的な取得失敗のたびに
# レジストリが痩せ、翌日また resolved で再登録されるだけの往復が起きる。
# 再計算は平日3回/日 (08:30/12:00/17:05) 走るので、連続してこの回数
# 失敗して初めて追い出す。record() が resolved/継続提案のたびに 0 へ戻す
# ので、間に1回でも成功が挟まれば猶予はリセットされる。
MISSED_REBUILDS_BEFORE_EVICTION = 3

SOURCE_LABEL = "ai_proposal"


def _today(now: datetime | None) -> date:
    return (now or datetime.now()).date()


def _as_date(value: object) -> date | None:
    try:
        return date.fromisoformat(str(value)[:10])
    except Exception:
        return None


def _read_rows(base_dir: Path) -> list[dict]:
    payload = load_json(Path(base_dir) / REGISTRY_FILENAME, {})
    rows = payload.get("candidates") if isinstance(payload, dict) else payload
    if not isinstance(rows, list):
        return []
    out: list[dict] = []
    for row in rows:
        if not isinstance(row, dict):
            continue
        ticker = str(row.get("ticker") or "").strip().upper()
        if not ticker:
            continue
        try:
            missed_rebuilds = int(row.get("missed_rebuilds") or 0)
        except (TypeError, ValueError):
            missed_rebuilds = 0
        out.append({
            "ticker": ticker,
            "first_seen": str(row.get("first_seen") or ""),
            "last_seen": str(row.get("last_seen") or ""),
            "seen_count": int(row.get("seen_count") or 0),
            "source": str(row.get("source") or SOURCE_LABEL),
            # 既存フィールドに無ければ 0 (旧形式のエントリも「失敗歴なし」
            # として安全に読める)。
            "missed_rebuilds": max(0, missed_rebuilds),
        })
    return out


def _prune_and_order(rows: list[dict], *, today: date) -> list[dict]:
    """TTL を切ってから新しい順に並べ、消費側スライスと同じ幅に収める。"""
    cutoff = today - timedelta(days=TTL_DAYS)
    alive = []
    for row in rows:
        seen = _as_date(row.get("last_seen"))
        # last_seen を読めない行は破棄する。並び順を決められない行を残すと
        # 消費側スライスの切り捨てが不可視になる。
        if seen is None or seen < cutoff:
            continue
        alive.append(row)
    alive.sort(
        key=lambda r: (str(r.get("last_seen") or ""), int(r.get("seen_count") or 0), r["ticker"]),
        reverse=True,
    )
    return alive[:MAX_ENTRIES]


def _write(base_dir: Path, rows: list[dict], *, now: datetime | None) -> None:
    atomic_write_json(Path(base_dir) / REGISTRY_FILENAME, {
        "version": 1,
        "generated_at": (now or datetime.now()).isoformat(),
        "candidates": rows,
    })


def load_registered(base_dir: Path) -> dict[str, dict]:
    """ticker -> エントリ。順序は last_seen の新しい順。"""
    return {row["ticker"]: row for row in _read_rows(base_dir)}


def record(
    proposed,
    *,
    resolved: set[str],
    base_dir: Path,
    now: datetime | None = None,
) -> dict:
    """補完に成功した銘柄だけを登録する。例外は投げない。

    REGISTRY_LOCK_NAME を取得してから record_already_locked() を呼ぶ。
    呼び出し元が既にこのロックを保持している場合 (technical_signals.
    ensure_technical_coverage 等) は、代わりに record_already_locked() を
    直接呼ぶこと —— fcntl.flock は同一プロセスからの二重取得もブロックする
    ため、ここを呼ぶとロック保持中の呼び出し元ごとタイムアウトまで
    デッドロックする。
    """
    try:
        with process_lock(REGISTRY_LOCK_NAME, timeout=REGISTRY_LOCK_TIMEOUT_SECONDS):
            return record_already_locked(
                proposed, resolved=resolved, base_dir=base_dir, now=now)
    except LockBusy:
        return {"status": "lock_busy", "requested": [], "registered": []}


def record_already_locked(
    proposed,
    *,
    resolved: set[str],
    base_dir: Path,
    now: datetime | None = None,
) -> dict:
    """record() のロックフリーな本体。呼び出し元が REGISTRY_LOCK_NAME を
    既に保持していることを前提とする。単独では呼ばないこと。

    proposed は監査のための全要求。実際に登録されるのは resolved との積集合
    だけで、これはポジティブキャッシュ制約 (モジュール docstring 1.) の
    実装そのもの。解決できなかった銘柄を入れると強制再計算が恒久化する。
    """
    try:
        today = _today(now)
        stamp = today.isoformat()
        resolved_upper = {str(t or "").strip().upper() for t in (resolved or set())}
        resolved_upper.discard("")

        requested = []
        for value in (proposed or []):
            ticker = str(value or "").strip().upper()
            if ticker and ticker not in requested:
                requested.append(ticker)

        additions = [t for t in requested if t in resolved_upper]
        rows = {row["ticker"]: row for row in _read_rows(base_dir)}
        # 継続提案: 今回は resolved に入らなかった (= ensure_technical_coverage
        # が「既に行がある」として取得をスキップした) が、既に登録済みの銘柄。
        # resolved はその呼び出しで実際に取得した銘柄しか含まないので、
        # 2日目以降ずっと同じ銘柄が提案され続けても、行が既にあるせいで
        # resolved には二度と入らない。それを見て additions だけで
        # last_seen を判定すると、初日以降ずっと同じ日付のまま TTL に
        # 引っかかって消える (last_seen が「最後に新規取得できた日」に
        # なってしまい、「最後に提案された日」にならない)。
        #
        # rows に既にある = 「過去に実在確認済み」の証拠でしかなく、
        # 「今も実在する」証拠ではない。missed_rebuilds は「今も実在するか」
        # を数えるカウンタなので、過去の実在だけで continuing 扱いすると、
        # 現在 technical_state.json に行を持たない銘柄 (直近の topup も
        # 直近の全再計算も解決できなかった) が resolved 無しで再提案される
        # たびに missed_rebuilds が 0 へリセットされ、永久に猶予が尽きなく
        # なる (Codex レビュー再現: technical state に不在・topup失敗・
        # missed_rebuilds=2 のMDBが、continuing 扱いで 0 へ戻った)。
        # 「今も実在する」の唯一の直接証拠は technical_state.json 自身の
        # tickers キーなので、それを読んで requested の絞り込みに使う。
        try:
            _tech_state = load_json(Path(base_dir) / "technical_state.json", {})
            _live_tickers = (
                set((_tech_state.get("tickers") or {}).keys())
                if isinstance(_tech_state, dict) else set()
            )
        except Exception:
            _live_tickers = set()
        continuing = [
            t for t in requested
            if t not in resolved_upper and t in rows and t in _live_tickers
        ]
        for ticker in additions:
            row = rows.get(ticker)
            if row is None:
                rows[ticker] = {
                    "ticker": ticker,
                    "first_seen": stamp,
                    "last_seen": stamp,
                    "seen_count": 1,
                    "source": SOURCE_LABEL,
                    "missed_rebuilds": 0,
                }
            else:
                row["last_seen"] = stamp
                row["seen_count"] = int(row.get("seen_count") or 0) + 1
                if not row.get("first_seen"):
                    row["first_seen"] = stamp
                # 行を取得できた = 直近の再計算失敗歴を持ち越す理由がない。
                row["missed_rebuilds"] = 0
        for ticker in continuing:
            row = rows[ticker]
            row["last_seen"] = stamp
            row["seen_count"] = int(row.get("seen_count") or 0) + 1
            # continuing は「今も technical row がある」ことが前提条件
            # (record() 呼び出し側の risk-increasing 銘柄は、行があるからこそ
            # ensure_technical_coverage が取得をスキップして resolved に
            # 入らなかった)。よって additions と同じく猶予をリセットする。
            row["missed_rebuilds"] = 0

        ordered = _prune_and_order(list(rows.values()), today=today)
        if not additions and not continuing and len(ordered) == len(rows):
            # 変化なし。mtime を動かさない。
            return {"status": "noop", "requested": requested, "registered": []}
        # 追加が無くても TTL 切れは書き戻して落とす。_build_ticker_universe は
        # このファイルを直接読むので、追加のあった日にしか prune しないと、
        # 二度と提案されない銘柄が期限を過ぎてもユニバースに残り続け、
        # 毎回の再計算で無駄にダウンロードされる。
        _write(base_dir, ordered, now=now)
        kept = {row["ticker"] for row in ordered}
        return {
            "status": "ok",
            "requested": requested,
            "registered": [t for t in additions if t in kept],
            "dropped": [t for t in additions if t not in kept],
            # 新規登録ではなく last_seen だけ更新した既存銘柄。
            "continuing": [t for t in continuing if t in kept],
            "entries": len(ordered),
        }
    except Exception as exc:  # レジストリは補助。失敗しても分析を止めない。
        return {"status": "error", "error": f"{type(exc).__name__}: {exc}"}


def evict_unresolved(
    missing,
    *,
    base_dir: Path,
    rebuild_coverage: float,
    now: datetime | None = None,
) -> dict:
    """evict_unresolved_already_locked() を REGISTRY_LOCK_NAME 取得の上で呼ぶ。

    record() と共通のロックを取ることで、両者の read-modify-write が
    互いの更新を上書きしないようにする (Codex レビュー round 5 で再現済み)。
    """
    try:
        with process_lock(REGISTRY_LOCK_NAME, timeout=REGISTRY_LOCK_TIMEOUT_SECONDS):
            return evict_unresolved_already_locked(
                missing, base_dir=base_dir, rebuild_coverage=rebuild_coverage, now=now)
    except LockBusy:
        return {"status": "lock_busy", "evicted": []}


def evict_unresolved_already_locked(
    missing,
    *,
    base_dir: Path,
    rebuild_coverage: float,
    now: datetime | None = None,
) -> dict:
    """evict_unresolved() のロックフリーな本体。呼び出し元が
    REGISTRY_LOCK_NAME を既に保持していることを前提とする。

    例外は投げない。1回の再計算で欠けただけでは追い出さない —— yfinance の
    一時的な単一銘柄不調と、本当の上場廃止を区別できないため
    (MISSED_REBUILDS_BEFORE_EVICTION 参照)。連続してこの回数を欠けて
    初めて追い出す。record() が成功のたびに猶予を 0 へ戻すので、
    間に1回でも解決できればカウントは積み上がらない。

    上場廃止・改称された銘柄をユニバースに残し続けると
    _ensure_technical_state_fresh の universe_is_complete が恒久的に false に
    なり、毎回の強制再計算が無警告で走る。猶予を使い切るまでの最悪ケースは
    「強制再計算が数回余分に走ってから自己修復」で、それ以上悪化しない。
    """
    try:
        coverage = float(rebuild_coverage)
    except (TypeError, ValueError):
        coverage = 0.0
    if coverage < MIN_REBUILD_COVERAGE_FOR_EVICTION:
        # 全面障害の日に追い出すと、翌日には復活する銘柄まで消える。
        return {"status": "skipped_low_coverage", "rebuild_coverage": coverage, "evicted": []}

    try:
        today = _today(now)
        drop = {str(t or "").strip().upper() for t in (missing or [])}
        drop.discard("")
        rows = _read_rows(base_dir)
        if not rows:
            return {"status": "noop", "evicted": []}

        touched = {row["ticker"] for row in rows} & drop

        survivors = []
        evicted = []
        for row in rows:
            if row["ticker"] not in drop:
                survivors.append(row)
                continue
            missed = int(row.get("missed_rebuilds") or 0) + 1
            if missed >= MISSED_REBUILDS_BEFORE_EVICTION:
                evicted.append(row["ticker"])
                continue
            row = dict(row)
            row["missed_rebuilds"] = missed
            survivors.append(row)
        evicted = sorted(evicted)

        ordered = _prune_and_order(survivors, today=today)
        if not touched and len(ordered) == len(rows):
            # このレジストリの銘柄は1件もこの再計算で欠けておらず、
            # TTL でも何も落ちていない。カウンタも件数も変わらないので
            # 書かない (mtime を動かさない)。touched が非空なら、
            # 追い出しに至らなくても missed_rebuilds の増分を必ず書く —
            # ここを省くと猶予を跨いだカウントアップが失われ、
            # 実質「1回の欠落で即追い出し」の旧挙動へ戻ってしまう。
            return {"status": "noop", "evicted": []}
        _write(base_dir, ordered, now=now)
        return {"status": "ok", "evicted": evicted, "entries": len(ordered)}
    except Exception as exc:
        return {"status": "error", "error": f"{type(exc).__name__}: {exc}"}
