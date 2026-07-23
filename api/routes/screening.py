"""
GET /api/screening  — 長期スクリーニング結果 + ポートフォリオ最適化 + 空売り候補
POST /api/screening/run-long-term — 長期スクリーニングをバックグラウンド実行
GET /api/news-signals — ニュース感情スクリーニング結果
POST /api/screening/run-news-screener — ニューススクリーナーをバックグラウンド実行

S5B 拡張 (2026-04):
  - 各カテゴリに morning/evening の両ファイルを併載
  - composite_score 内訳（technical/fundamental/ai_conviction/win_rate）を pass-through
  - ai_source / sonnet_second_signal / earnings_imminent / news_boost / social_buzz を保持
  - 各カテゴリに meta = {updated_at, source_file, next_scheduled_at} を付与
"""
import asyncio
import json
import subprocess
import sys
from datetime import datetime, timedelta
from pathlib import Path
from fastapi import APIRouter, BackgroundTasks

router = APIRouter()
BASE_DIR = Path(__file__).parent.parent.parent

# 各 cron スケジュール（次回実行時刻計算用）
# (hour, minute, [曜日リスト or None=平日])
_CRON_SCHEDULE = {
    "screen_results.json":               (18,  0, None),    # 平日 18:00
    "screen_results_morning.json":       ( 6,  0, None),    # 平日 06:00
    "screen_results_jp.json":            (15, 30, None),    # 平日 15:30
    "short_candidates.json":             (18, 30, None),    # 平日 18:30
    "short_candidates_morning.json":     ( 6,  0, None),    # 平日 06:00
    "margin_long_candidates.json":       (19, 15, None),    # 平日 19:15
    "margin_long_candidates_morning.json": (6, 5, None),    # 平日 06:05
    "long_term_screen_results.json":     ( 7,  0, [3, 6]),  # 木(3)・日(6) 07:00
}


def _next_scheduled_at(filename: str) -> str | None:
    """ファイル名から次回 cron 実行時刻を ISO 文字列で返す（不明は None）。"""
    sch = _CRON_SCHEDULE.get(filename)
    if not sch:
        return None
    hour, minute, weekdays = sch
    now = datetime.now()
    candidate = now.replace(hour=hour, minute=minute, second=0, microsecond=0)
    for _ in range(8):
        if candidate > now:
            wd = candidate.weekday()
            if weekdays is not None:
                if wd in weekdays:
                    return candidate.isoformat(timespec="minutes")
            else:
                if wd < 5:  # 平日
                    return candidate.isoformat(timespec="minutes")
        candidate += timedelta(days=1)
    return None


def _load_json_with_meta(filename: str, default: dict) -> dict:
    """JSON ファイルを読み込み meta（updated_at / source_file / next_scheduled_at）を付与。"""
    p = BASE_DIR / filename
    if not p.exists():
        out = dict(default)
        out["meta"] = {
            "source_file":      filename,
            "updated_at":       None,
            "next_scheduled_at": _next_scheduled_at(filename),
            "exists":           False,
        }
        return out
    try:
        with open(p, encoding="utf-8") as f:
            data = json.load(f)
        if not isinstance(data, dict):
            data = {"data": data}
        data["meta"] = {
            "source_file":       filename,
            "updated_at":        datetime.fromtimestamp(p.stat().st_mtime).isoformat(timespec="seconds"),
            "next_scheduled_at": _next_scheduled_at(filename),
            "exists":            True,
        }
        return data
    except Exception as e:
        out = dict(default)
        out["error"] = str(e)
        out["meta"] = {
            "source_file":       filename,
            "updated_at":        datetime.fromtimestamp(p.stat().st_mtime).isoformat(timespec="seconds"),
            "next_scheduled_at": _next_scheduled_at(filename),
            "exists":            True,
        }
        return out


@router.get("/api/screening")
async def get_screening():
    result: dict = {}

    # ── 長期スクリーニング ──
    result["long_term"] = _load_json_with_meta(
        "long_term_screen_results.json",
        {"passed": [], "error": "long_term_screen_results.json not found"},
    )

    # ── ポートフォリオ最適化 ──
    result["optimization"] = _load_json_with_meta(
        "optimization_result.json",
        {"error": "optimization_result.json not found"},
    )

    # ── 短期スクリーニング（空売り候補） evening + morning ──
    result["short_term"] = _load_json_with_meta(
        "short_candidates.json",
        {"candidates": [], "error": "short_candidates.json not found"},
    )
    result["short_term_morning"] = _load_json_with_meta(
        "short_candidates_morning.json",
        {"candidates": [], "error": "short_candidates_morning.json not found"},
    )

    # ── 信用買い候補 evening + morning ──
    result["margin_long"] = _load_json_with_meta(
        "margin_long_candidates.json",
        {"candidates": [], "error": "margin_long_candidates.json not found"},
    )
    result["margin_long_morning"] = _load_json_with_meta(
        "margin_long_candidates_morning.json",
        {"candidates": [], "error": "margin_long_candidates_morning.json not found"},
    )

    # ── モメンタム買い候補 evening + morning + JP ──
    result["momentum_buy"] = _load_json_with_meta(
        "screen_results.json",
        {"candidates": [], "error": "screen_results.json not found"},
    )
    result["momentum_buy_morning"] = _load_json_with_meta(
        "screen_results_morning.json",
        {"candidates": [], "error": "screen_results_morning.json not found"},
    )
    result["momentum_buy_jp"] = _load_json_with_meta(
        "screen_results_jp.json",
        {"candidates": [], "error": "screen_results_jp.json not found"},
    )

    # ── Black-Litterman ビュー ──
    try:
        path = BASE_DIR / "bl_views.json"
        if path.exists():
            with open(path, encoding="utf-8") as f:
                result["bl_views"] = json.load(f)
        else:
            result["bl_views"] = {}
    except Exception as e:
        result["bl_views"] = {"error": str(e)}

    # ── BLウェイト ──
    try:
        opt = result.get("optimization", {})
        if opt and "results" in opt and "black_litterman" in opt["results"]:
            result["bl_weights"] = opt["results"]["black_litterman"].get("weights", {})
        else:
            bl_path = BASE_DIR / "optimization_result.json"
            if bl_path.exists():
                with open(bl_path, encoding="utf-8") as f:
                    opt_data = json.load(f)
                bl_res = opt_data.get("results", {}).get("black_litterman", {})
                result["bl_weights"] = bl_res.get("weights", {})
    except Exception as e:
        result["bl_weights"] = {"error": str(e)}

    # ── ニュース感情シグナル ──
    news_file = BASE_DIR / "news_signal_candidates.json"
    if news_file.exists():
        try:
            with open(news_file, encoding="utf-8") as f:
                result["news_signals"] = json.load(f)
        except Exception as e:
            result["news_signals"] = {"error": str(e)}
    else:
        result["news_signals"] = None

    # ── SNS感情 + オプション異常 ──
    social_file = BASE_DIR / "social_sentiment.json"
    if social_file.exists():
        try:
            with open(social_file, encoding="utf-8") as f:
                result["social_sentiment"] = json.load(f)
        except Exception as e:
            result["social_sentiment"] = {"error": str(e)}
    else:
        result["social_sentiment"] = None

    # ── セクター強度（朝/夕表示用バッジ） ──
    sector_file = BASE_DIR / "sector_strength.json"
    if sector_file.exists():
        try:
            with open(sector_file, encoding="utf-8") as f:
                sd = json.load(f)
            result["sector_strength"] = {
                "data":       sd,
                "updated_at": datetime.fromtimestamp(sector_file.stat().st_mtime).isoformat(timespec="seconds"),
            }
        except Exception as e:
            result["sector_strength"] = {"error": str(e)}
    else:
        result["sector_strength"] = None

    # ── ハーネス A/B 比較サマリー（あれば） ──
    harness_file = BASE_DIR / "harness_compare_state.json"
    if harness_file.exists():
        try:
            with open(harness_file, encoding="utf-8") as f:
                result["harness_compare"] = json.load(f)
        except Exception:
            pass

    return result


@router.get("/api/screening/signal-history")
async def get_signal_history():
    """シグナル履歴と勝率統計を返す"""
    result: dict = {}

    # シグナル履歴
    try:
        path = BASE_DIR / "signal_history.json"
        if path.exists():
            with open(path, encoding="utf-8") as f:
                history = json.load(f)
            # 最新30件のみ返す
            result["history"] = sorted(history, key=lambda r: r.get("date", ""), reverse=True)[:30]
        else:
            result["history"] = []
    except Exception as e:
        result["history"] = []
        result["history_error"] = str(e)

    # 勝率統計
    try:
        path = BASE_DIR / "signal_stats.json"
        if path.exists():
            with open(path, encoding="utf-8") as f:
                result["stats"] = json.load(f)
        else:
            result["stats"] = None
    except Exception as e:
        result["stats"] = None

    return result


@router.get("/api/screening/harness-compare")
async def get_harness_compare(days: int = 7):
    """legacy vs deepseek の A/B 勝率比較（compare_harness.py をオンザフライ実行）。"""
    try:
        venv_python = str(BASE_DIR / "venv" / "bin" / "python")
        script = str(BASE_DIR / "compare_harness.py")
        proc = subprocess.run(
            [venv_python, script, "--days", str(int(days)), "--json"],
            cwd=str(BASE_DIR),
            capture_output=True,
            text=True,
            timeout=30,
        )
        if proc.returncode != 0:
            return {"error": proc.stderr.strip() or f"exit {proc.returncode}"}
        return json.loads(proc.stdout)
    except Exception as e:
        return {"error": str(e)}


@router.post("/api/screening/run-long-term")
async def run_long_term_scan():
    """長期スクリーニングをバックグラウンドプロセスで実行する。"""
    try:
        venv_python = str(BASE_DIR / "venv" / "bin" / "python")
        script = str(BASE_DIR / "long_term_screener.py")
        subprocess.Popen(
            [venv_python, script],
            cwd=str(BASE_DIR),
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        return {"message": "長期スクリーニングをバックグラウンドで開始しました（完了まで数分かかります）"}
    except Exception as e:
        return {"message": f"エラー: {e}"}


@router.post("/api/screening/submit-batch")
async def submit_ai_batch_endpoint():
    """既存の long_term_screen_results.json から AI テーゼバッチを送信する。"""
    try:
        venv_python = str(BASE_DIR / "venv" / "bin" / "python")
        script = str(BASE_DIR / "long_term_screener.py")
        subprocess.Popen(
            [venv_python, script, "submit-batch"],
            cwd=str(BASE_DIR),
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        return {"message": "AI テーゼバッチ送信を開始しました（long_term_batch_state.json に状態が保存されます）"}
    except Exception as e:
        return {"message": f"エラー: {e}"}


@router.post("/api/screening/run-short-term")
async def run_short_term_scan(morning: bool = False, us_only: bool = False, jp_only: bool = False):
    """短期スクリーニング（モメンタム + 空売り候補）をバックグラウンド実行する。

    クエリ:
      morning=true  → screener.py / short_screener.py / margin_long_screener.py に --morning
      us_only=true  → --us-only
      jp_only=true  → --jp-only（screener.py のみ）
    """
    try:
        venv_python = str(BASE_DIR / "venv" / "bin" / "python")
        errors = []

        common_flags = []
        if morning:
            common_flags.append("--morning")
        if us_only:
            common_flags.append("--us-only")

        # モメンタムスクリーナー
        try:
            args = [venv_python, str(BASE_DIR / "screener.py")] + common_flags
            if jp_only:
                args.append("--jp-only")
            subprocess.Popen(
                args,
                cwd=str(BASE_DIR),
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
        except Exception as e:
            errors.append(f"screener: {e}")

        # 空売りスクリーナー
        try:
            subprocess.Popen(
                [venv_python, str(BASE_DIR / "short_screener.py")] + common_flags,
                cwd=str(BASE_DIR),
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
        except Exception as e:
            errors.append(f"short_screener: {e}")

        if errors:
            return {"message": f"一部エラー: {'; '.join(errors)}"}
        flag_label = " ".join(common_flags) or "(夕方フル)"
        return {"message": f"短期スクリーニング {flag_label} をバックグラウンドで開始しました"}
    except Exception as e:
        return {"message": f"エラー: {e}"}


@router.get("/api/news-signals")
async def get_news_signals():
    """ニュース感情スクリーニング結果を返す"""
    news_file = BASE_DIR / "news_signal_candidates.json"
    if not news_file.exists():
        return {"candidates": [], "generated_at": None, "error": "未生成（news_screener.py を実行してください）"}
    with open(news_file, encoding="utf-8") as f:
        return json.load(f)


@router.post("/api/screening/run-news-screener")
async def run_news_screener(background_tasks: BackgroundTasks):
    """ニューススクリーナーをバックグラウンド実行する"""
    # P1-15: 連打や複数プロセス起動を file lock で防ぐ（旧実装は無防備で multi-spawn 可能）
    from utils import process_lock, is_locked, LockBusy
    if is_locked("news_screener"):
        return {"ok": False, "message": "ニューススクリーナーは既に実行中"}
    def _run():
        try:
            with process_lock("news_screener"):
                subprocess.run(
                    [str(BASE_DIR / "venv" / "bin" / "python"), str(BASE_DIR / "news_screener.py")],
                    cwd=str(BASE_DIR),
                )
        except LockBusy:
            pass  # 別プロセス取得済み
    background_tasks.add_task(_run)
    return {"ok": True, "message": "ニューススクリーナー開始"}


@router.get("/api/social-sentiment")
async def get_social_sentiment():
    """SNS感情 + オプション異常スクリーニング結果を返す"""
    social_file = BASE_DIR / "social_sentiment.json"
    if not social_file.exists():
        return {
            "stocktwits": {},
            "options_unusual": [],
            "top_bullish": [],
            "top_bearish": [],
            "generated_at": None,
            "error": "未生成（social_screener.py を実行してください）",
        }
    with open(social_file, encoding="utf-8") as f:
        return json.load(f)


@router.post("/api/screening/run-social-screener")
async def run_social_screener(background_tasks: BackgroundTasks):
    """SNS/オプションスクリーナーをバックグラウンド実行する"""
    from utils import process_lock, is_locked, LockBusy
    if is_locked("social_screener"):
        return {"ok": False, "message": "SNS スクリーナーは既に実行中"}
    def _run():
        try:
            with process_lock("social_screener"):
                subprocess.run(
                    [str(BASE_DIR / "venv" / "bin" / "python"), str(BASE_DIR / "social_screener.py")],
                    cwd=str(BASE_DIR),
                )
        except LockBusy:
            pass
    background_tasks.add_task(_run)
    return {"ok": True, "message": "SNS/オプションスクリーナー開始"}
