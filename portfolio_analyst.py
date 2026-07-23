"""
AI ポートフォリオ分析エンジン v3（モジュール化済み）
─────────────────────────────────────────────────────
このファイルは後方互換のためのラッパーです。
実装は analyst/ パッケージ内に分割されています:
  analyst/cache.py         — キャッシュ管理
  analyst/llm_client.py    — Claude API クライアント
  analyst/data_gatherer.py — データ収集
  analyst/__init__.py      — run_analysis / get_cached / send_to_telegram

CLI:
  python portfolio_analyst.py            # キャッシュがあれば再利用
  python portfolio_analyst.py --force    # 強制再分析
  python portfolio_analyst.py --telegram # 再分析 + Telegram送信
  python portfolio_analyst.py --json     # JSON出力
"""
import json
import sys
from pathlib import Path

# analyst パッケージから公開 API を再エクスポート
from analyst import run_analysis, get_cached, send_to_telegram  # noqa: F401
from utils import heartbeat

# 後方互換: 直接インポートされるケース用に定数・内部関数を公開
from analyst.cache import (  # noqa: F401
    CACHE_PATH, HISTORY_PATH, CACHE_HOURS,
    is_cache_valid as _is_cache_valid,
    load_json as _load_json,
    save_cache as _save_cache,
    load_history_context as _load_history_context,
)
from analyst.llm_client import (  # noqa: F401
    call_claude as _call_claude,
    _SUBMIT_TOOL, _SYSTEM_SONNET, _GEO_KEYWORDS,
)
from analyst.data_gatherer import (  # noqa: F401
    gather_data as _gather_data,
)

BASE_DIR = Path(__file__).parent


def main(argv: list[str] | None = None) -> int:
    args = list(sys.argv[1:] if argv is None else argv)
    force     = "--force"    in args
    send_tg   = "--telegram" in args
    show_json = "--json"     in args

    try:
        result = run_analysis(force=force)
    except Exception as e:
        heartbeat("portfolio_analyst", "error", str(e)[:500])
        raise

    if send_tg:
        send_to_telegram(result)

    synthesis = result.get("synthesis", {}) if isinstance(result, dict) else {}
    actions = synthesis.get("priority_actions", []) if isinstance(synthesis, dict) else []
    heartbeat(
        "portfolio_analyst",
        "ok",
        None,
        extra={
            "as_of": result.get("as_of") if isinstance(result, dict) else None,
            "priority_actions": len(actions) if isinstance(actions, list) else None,
        },
    )

    if show_json:
        print(json.dumps(result, ensure_ascii=False, indent=2))
    else:
        print("\n" + "─" * 60)
        print(f"📊 {synthesis.get('morning_brief_headline', 'No headline')}")
        print(f"\n{synthesis.get('morning_brief_detail', '')}")
        print(f"\n🌍 {synthesis.get('geopolitical_note', '-')}")
        print(f"\n🎯 週間テーマ: {synthesis.get('weekly_theme', '-')}")

        if actions:
            print("\n── 優先アクション ──")
            for a in actions[:5]:
                urg = {"high": "🔴", "medium": "🟡", "low": "🟢"}.get(a.get("urgency", "low"), "⚪")
                print(f"  {urg} [{a.get('tier','?')}] {a.get('action','')}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
