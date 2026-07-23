"""日証金 貸借取引情報(taisyaku.jp)から JP 空売りデータを自動取得する。

無料・日次更新・機械可読の 4 CSV(Shift_JIS)を取り込み、builder が読む
state ファイルに変換する。手入力ゼロで JP 空売りの借株可否/逆日歩/規制を更新できる。

取り込み元（https://www.taisyaku.jp/data/<name>）:
  - meigara.csv         貸借取引対象銘柄 → loanable(貸借銘柄区分（東証）∈{1,2})
  - shina.csv           品貸料率一覧     → 逆日歩(reverse_daily_fee) / 貸株超過株数
  - zandaka.csv         銘柄別残高一覧   → 貸借倍率(融資残/貸株残)
  - seigenichiran.csv   制限措置等一覧   → 規制(申込停止＋新規売り → short_ban)

出力（data/、いずれも gitignore 配下）:
  - jp_loanable_state.json   {generated_at, as_of, loanable_by_ticker:{<t>:bool}}
  - jsf_lending_state.json   {generated_at, tickers:{<t>:{loan_ratio,reverse_daily_fee,short_excess_shares}}}
  - jp_regulation_state.json {generated_at, tickers:{<t>:"short_ban|margin_up|caution"}}

すべて fail-closed: パース不能/取得失敗は素通りさせず、その銘柄は供給されない
（=builder 側で shortable=false のまま）。
"""

from __future__ import annotations

import argparse
import csv
import io
import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable, Optional

BASE_DIR = Path(__file__).parent
_BASE_URL = "https://www.taisyaku.jp/data/"
_FILES = ("meigara.csv", "shina.csv", "zandaka.csv", "seigenichiran.csv")


def _to_ticker(code: str) -> Optional[str]:
    code = "".join(ch for ch in str(code or "") if ch.isdigit())
    return f"{code}.T" if len(code) == 4 else None


def _num(value: str) -> Optional[float]:
    """'5.00' → 5.0、'*****' や空欄 → None。"""
    s = str(value or "").strip().replace(",", "")
    if not s or s.startswith("*"):
        return None
    try:
        return float(s)
    except ValueError:
        return None


def _find_header_idx(rows: list[list[str]], *tokens: str) -> int:
    """与えたトークンを全て含む行の index を返す(preamble 行飛ばし)。"""
    for i, row in enumerate(rows):
        joined = ",".join(row)
        if all(tok in joined for tok in tokens):
            return i
    raise ValueError(f"header row not found for tokens={tokens}")


def _read_rows(text: str) -> list[list[str]]:
    return list(csv.reader(io.StringIO(text)))


# ── 各 CSV パーサ(fail-closed)─────────────────────────────

def parse_meigara(text: str) -> dict[str, bool]:
    """貸借銘柄区分（東証）∈{1,2} → True、0 → False。"""
    rows = _read_rows(text)
    h = _find_header_idx(rows, "コード", "貸借銘柄区分")
    header = rows[h]
    code_i = header.index("コード")
    tse_i = next((i for i, c in enumerate(header) if "貸借銘柄区分" in c and "東" in c), None)
    if tse_i is None:
        raise ValueError("貸借銘柄区分（東証）列が見つからない")
    out: dict[str, bool] = {}
    for row in rows[h + 1:]:
        if len(row) <= max(code_i, tse_i):
            continue
        t = _to_ticker(row[code_i])
        if not t:
            continue
        out[t] = str(row[tse_i]).strip() in ("1", "2")
    return out


def parse_shina(text: str) -> dict[str, dict]:
    """品貸料率一覧 → 逆日歩(当日 or 前日 品貸料率>0)と貸株超過株数。"""
    rows = _read_rows(text)
    h = _find_header_idx(rows, "コード", "品貸料率")
    header = rows[h]
    code_i = header.index("コード")
    today_i = next((i for i, c in enumerate(header) if "当日品貸料率" in c), None)
    prev_i = next((i for i, c in enumerate(header) if "前日品貸料率" in c), None)
    excess_i = next((i for i, c in enumerate(header) if "貸株超過株数" in c), None)
    out: dict[str, dict] = {}
    for row in rows[h + 1:]:
        if len(row) <= code_i:
            continue
        t = _to_ticker(row[code_i])
        if not t:
            continue
        today = _num(row[today_i]) if today_i is not None and len(row) > today_i else None
        prev = _num(row[prev_i]) if prev_i is not None and len(row) > prev_i else None
        excess = _num(row[excess_i]) if excess_i is not None and len(row) > excess_i else None
        reverse = bool((today and today > 0) or (prev and prev > 0))
        out[t] = {
            "reverse_daily_fee": reverse,
            "short_excess_shares": int(excess) if excess is not None else None,
        }
    return out


def parse_zandaka(text: str) -> dict[str, dict]:
    """銘柄別残高 → 貸借倍率(融資残高株数 / 貸株残高株数)。貸株残=0 は None(踏み上げ無し)。"""
    rows = _read_rows(text)
    h = _find_header_idx(rows, "銘柄コード", "融資残高株数", "貸株残高株数")
    header = rows[h]
    code_i = header.index("銘柄コード")
    margin_i = header.index("融資残高株数")
    short_i = header.index("貸株残高株数")
    out: dict[str, dict] = {}
    for row in rows[h + 1:]:
        if len(row) <= max(code_i, margin_i, short_i):
            continue
        t = _to_ticker(row[code_i])
        if not t:
            continue
        margin = _num(row[margin_i])
        short = _num(row[short_i])
        ratio = (margin / short) if (margin is not None and short) else None
        out[t] = {"loan_ratio": round(ratio, 4) if ratio is not None else None,
                  "short_balance": int(short) if short is not None else None}
    return out


def parse_seigen(text: str) -> dict[str, str]:
    """制限措置 → 申込停止＋新規売り=short_ban / 申込制限=margin_up / 注意喚起=caution。"""
    rows = _read_rows(text)
    h = _find_header_idx(rows, "銘柄コード", "実施措置")
    header = rows[h]
    code_i = header.index("銘柄コード")
    measure_i = header.index("実施措置")
    detail_i = next((i for i, c in enumerate(header) if "実施内容" in c), None)
    out: dict[str, str] = {}
    for row in rows[h + 1:]:
        if len(row) <= max(code_i, measure_i):
            continue
        t = _to_ticker(row[code_i])
        if not t:
            continue
        measure = str(row[measure_i]).strip()
        detail = str(row[detail_i]).strip() if (detail_i is not None and len(row) > detail_i) else ""
        if "停止" in measure and ("売り" in detail or not detail):
            out[t] = "short_ban"
        elif "制限" in measure:
            out[t] = "margin_up"
        elif "注意" in measure:
            out[t] = "caution"
    return out


# ── 取得 + 書き出し ───────────────────────────────────────

def _live_fetch(name: str) -> str:
    import requests
    r = requests.get(_BASE_URL + name, timeout=30)
    r.raise_for_status()
    return r.content.decode("shift_jis", errors="replace")


def _write_json(path: Path, payload: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    tmp.replace(path)


def sync(*, base_dir: Path = BASE_DIR,
         texts: Optional[dict[str, str]] = None,
         fetcher: Optional[Callable[[str], str]] = None,
         now: Optional[datetime] = None) -> dict:
    """4 CSV を取り込み 3 state ファイルを書く。texts 指定時はそれを使う(テスト用)。"""
    now = now or datetime.now(timezone.utc)
    get = (lambda n: texts[n]) if texts is not None else (fetcher or _live_fetch)

    loanable = parse_meigara(get("meigara.csv"))
    shina = parse_shina(get("shina.csv"))
    zandaka = parse_zandaka(get("zandaka.csv"))
    regulation = parse_seigen(get("seigenichiran.csv"))

    gen = now.isoformat()
    data_dir = Path(base_dir) / "data"

    _write_json(data_dir / "jp_loanable_state.json", {
        "generated_at": gen, "source": "taisyaku.jp/meigara.csv",
        "loanable_by_ticker": loanable,
    })

    # JSF state = 逆日歩(shina) ∪ 貸借倍率(zandaka)
    jsf_tickers: dict[str, dict] = {}
    for t, v in shina.items():
        jsf_tickers.setdefault(t, {}).update(v)
    for t, v in zandaka.items():
        jsf_tickers.setdefault(t, {}).update(v)
    for t in jsf_tickers:
        jsf_tickers[t].setdefault("reverse_daily_fee", False)
    _write_json(data_dir / "jsf_lending_state.json", {
        "generated_at": gen, "source": "taisyaku.jp/shina.csv+zandaka.csv",
        "tickers": jsf_tickers,
    })

    _write_json(data_dir / "jp_regulation_state.json", {
        "generated_at": gen, "source": "taisyaku.jp/seigenichiran.csv",
        "tickers": regulation,
    })

    return {
        "loanable_count": sum(1 for v in loanable.values() if v),
        "loanable_total": len(loanable),
        "reverse_daily_fee_count": sum(1 for v in shina.values() if v.get("reverse_daily_fee")),
        "jsf_ticker_count": len(jsf_tickers),
        "short_ban_count": sum(1 for v in regulation.values() if v == "short_ban"),
        "regulation_count": len(regulation),
    }


def main() -> int:
    parser = argparse.ArgumentParser(description="日証金 taisyaku.jp から JP 空売りデータを取得")
    parser.add_argument("--from-dir", help="ローカルCSVディレクトリ(検証用)。省略時は live 取得")
    args = parser.parse_args()
    texts = None
    if args.from_dir:
        d = Path(args.from_dir)
        texts = {n: (d / n).read_text(encoding="shift_jis", errors="replace") for n in _FILES}
    summary = sync(texts=texts)
    print(json.dumps(summary, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
