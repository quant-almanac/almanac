#!/usr/bin/env python
"""
sector_strength_updater.py — 11 SPDR セクター ETF の 1m / 3m モメンタム & 対 SPY 相対強度を計算し
sector_strength.json を atomic に更新する。

計算定義:
  - mom_1m  = (close[-1] / close[-22] - 1) * 100   # 約 1ヶ月（22 営業日）
  - mom_3m  = (close[-1] / close[-65] - 1) * 100   # 約 3ヶ月（65 営業日）
  - rel_1m  = mom_1m_etf - mom_1m_spy
  - rel_3m  = mom_3m_etf - mom_3m_spy
  - score   = 0.4*rel_1m + 0.6*rel_3m              # 中長期トレンド重視
  - strong  = (score > 0)

cron 例:
  30 17 * * 1-5 cd ~/portfolio-bot && run_with_secrets.sh venv/bin/python sector_strength_updater.py
"""
from __future__ import annotations

import json
import sys
from datetime import datetime
from pathlib import Path

BASE_DIR = Path(__file__).parent
OUTPUT   = BASE_DIR / "sector_strength.json"

# 11 SPDR セクター ETF（米国市場標準）
SECTOR_MAP: dict[str, str] = {
    "テクノロジー":  "XLK",
    "金融":         "XLF",
    "ヘルスケア":   "XLV",
    "消費財":       "XLY",     # consumer discretionary
    "生活必需品":   "XLP",     # consumer staples
    "エネルギー":   "XLE",
    "公益":         "XLU",
    "資本財":       "XLI",
    "素材":         "XLB",
    "不動産":       "XLRE",
    "通信":         "XLC",
}
BENCHMARK = "SPY"

# 過去取得期間（営業日 65 + バッファ 35 ≒ 100 営業日 ≒ 6ヶ月）
LOOKBACK_PERIOD = "6mo"


def _atomic_write_json(path: Path, data: dict) -> None:
    """utils.atomic_write_json があれば使用、無ければ tempfile + os.replace。"""
    try:
        from utils import atomic_write_json
        atomic_write_json(str(path), data)
        return
    except Exception:
        pass
    import os, tempfile
    fd, tmp = tempfile.mkstemp(prefix=path.stem + ".", suffix=".tmp", dir=str(path.parent))
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fp:
            json.dump(data, fp, ensure_ascii=False, indent=2)
        os.replace(tmp, path)
    except Exception:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def _calc_mom(close_series, lookback_days: int) -> float | None:
    """直近終値 / lookback_days 前終値 - 1 を % で返す。データ不足は None。"""
    if close_series is None or len(close_series) < lookback_days + 1:
        return None
    try:
        last = float(close_series.iloc[-1])
        prev = float(close_series.iloc[-(lookback_days + 1)])
        if prev <= 0:
            return None
        return round((last / prev - 1.0) * 100.0, 2)
    except Exception:
        return None


def update() -> dict:
    try:
        import yfinance as yf
    except ImportError:
        print("ERROR: yfinance がインストールされていません。", file=sys.stderr)
        sys.exit(1)

    tickers = [BENCHMARK] + list(SECTOR_MAP.values())
    print(f"[sector_strength] yfinance 一括取得: {len(tickers)} 銘柄 (期間 {LOOKBACK_PERIOD})")

    try:
        df = yf.download(
            tickers, period=LOOKBACK_PERIOD,
            interval="1d", auto_adjust=True, threads=True, progress=False,
            group_by="column",
        )
    except Exception as e:
        print(f"ERROR: yfinance.download 失敗: {e}", file=sys.stderr)
        sys.exit(2)

    # MultiIndex 列: (field, ticker) — 'Close' を抽出
    try:
        if hasattr(df.columns, "levels"):
            close_df = df["Close"]
        else:
            close_df = df  # 単一銘柄時など
    except Exception as e:
        print(f"ERROR: Close 列取得失敗: {e}", file=sys.stderr)
        sys.exit(3)

    spy_close = close_df.get(BENCHMARK)
    spy_1m = _calc_mom(spy_close, 22)
    spy_3m = _calc_mom(spy_close, 65)
    if spy_1m is None or spy_3m is None:
        print(f"ERROR: SPY モメンタム計算失敗 (1m={spy_1m}, 3m={spy_3m})", file=sys.stderr)
        sys.exit(4)
    print(f"  ベンチマーク SPY: 1m={spy_1m:+.2f}% / 3m={spy_3m:+.2f}%")

    out: dict[str, dict] = {}
    for sector_jp, etf in SECTOR_MAP.items():
        s = close_df.get(etf)
        mom_1m = _calc_mom(s, 22)
        mom_3m = _calc_mom(s, 65)
        if mom_1m is None or mom_3m is None:
            print(f"  ⚠️ {sector_jp} ({etf}): データ不足、スキップ")
            continue
        rel_1m = round(mom_1m - spy_1m, 2)
        rel_3m = round(mom_3m - spy_3m, 2)
        score  = round(0.4 * rel_1m + 0.6 * rel_3m, 2)
        out[sector_jp] = {
            "etf":    etf,
            "mom_1m": mom_1m,
            "mom_3m": mom_3m,
            "rel_1m": rel_1m,
            "rel_3m": rel_3m,
            "score":  score,
            "strong": bool(score > 0),
        }

    if not out:
        print("ERROR: 出力 0 件、保存をスキップ", file=sys.stderr)
        sys.exit(5)

    # 強さ降順でキーを並べ替える（既存形式と互換）
    out_sorted = dict(sorted(out.items(), key=lambda kv: kv[1]["score"], reverse=True))
    _atomic_write_json(OUTPUT, out_sorted)
    strong_count = sum(1 for v in out_sorted.values() if v["strong"])
    print(f"[sector_strength] {OUTPUT.name} 更新完了: {len(out_sorted)} セクター（強気 {strong_count}）")
    print(f"  最終更新: {datetime.now().isoformat(timespec='seconds')}")
    return out_sorted


if __name__ == "__main__":
    update()
