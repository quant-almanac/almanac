import os
import anthropic
import yfinance as yf
import requests
from datetime import datetime, timedelta
import json
from event_calendar import filter_by_events, check_event_risk
from sector_rotation import filter_by_sector_strength, save_sector_report
from earnings_season import get_season_config
from generate_dashboard import generate as update_dashboard
import time
from utils import init_yfinance_timeout

init_yfinance_timeout()

# 設定
TELEGRAM_TOKEN = os.environ.get('TELEGRAM_TOKEN', '')
TELEGRAM_CHAT_ID = os.environ.get('TELEGRAM_CHAT_ID', '')
client = anthropic.Anthropic()

# DeepSeek V3 クライアント（DEEPSEEK_API_KEY 設定時のみ有効）
# openai パッケージ経由で OpenAI 互換 API を使用
# 未設定時は Haiku にフォールバック
_deepseek_client = None
def _get_deepseek():
    global _deepseek_client
    if _deepseek_client is None:
        try:
            from utils import load_environment_secrets
            load_environment_secrets()
        except Exception:
            pass
        key = os.environ.get("DEEPSEEK_API_KEY", "")
        if key:
            try:
                from openai import OpenAI as _OpenAI
                _deepseek_client = _OpenAI(
                    api_key=key,
                    base_url="https://api.deepseek.com",
                )
            except ImportError:
                print("[DeepSeek] openai パッケージ未インストール。pip install openai")
    return _deepseek_client

def _deepseek_transport(*, base_url: str, api_key: str, model_id: str,
                        system: str, user: str, max_tokens: int, temperature: float):
    """Transport for almanac.llm_safety.call_external_llm that reuses the cached
    DeepSeek client (avoids constructing a new OpenAI client per call)."""
    ds = _get_deepseek()
    resp = ds.chat.completions.create(
        model=model_id,
        messages=[
            {"role": "system", "content": system},
            {"role": "user",   "content": user},
        ],
        max_tokens=max_tokens,
        temperature=temperature,
    )
    content = resp.choices[0].message.content or ""
    usage = getattr(resp, "usage", None)
    return content, {
        "input_tokens": getattr(usage, "prompt_tokens", None),
        "output_tokens": getattr(usage, "completion_tokens", None),
    }


def _call_fast_llm(system: str, user: str, max_tokens: int = 200) -> str | None:
    """
    Bull/Bear/Risk の短文生成に使う軽量 LLM。
    DeepSeek V3 は almanac.llm_safety.call_external_llm（public_market_context）経由で
    送信し、公開市場情報のみであることを保証する。未設定/失敗時は Haiku にフォールバック。
    """
    ds = _get_deepseek()
    if ds:
        try:
            from almanac.llm_safety import Payload, call_external_llm
            res = call_external_llm(
                Payload(kind="public_market_context", system=system, user=user),
                base_url="https://api.deepseek.com",
                api_key=os.environ.get("DEEPSEEK_API_KEY", ""),
                model_id="deepseek-chat",
                role="analyzer_debate",
                max_tokens=max_tokens,
                # P3-16: deterministic モード時は 0、通常時は 0.3
                temperature=__import__('utils').get_llm_temperature(default=0.3),
                transport=_deepseek_transport,
            )
            return res.content
        except Exception as e:
            print(f"  [DeepSeek] フォールバック中: {type(e).__name__}: {e}")
    # Haiku フォールバック
    def _haiku_call():
        return client.messages.create(
            model=HAIKU_MODEL_ID,
            max_tokens=max_tokens,
            system=system,
            messages=[{"role": "user", "content": user}],
        )

    started = time.monotonic()
    response = safe_api_call(_haiku_call)
    if response:
        _log_anthropic_usage(
            role="analyzer_haiku_fallback",
            model=HAIKU_MODEL_ID,
            max_tokens=max_tokens,
            started=started,
            prompt_chars=len(system) + len(user),
            response=response,
        )
        return response.content[0].text
    _log_anthropic_usage(
        role="analyzer_haiku_fallback",
        model=HAIKU_MODEL_ID,
        max_tokens=max_tokens,
        started=started,
        prompt_chars=len(system) + len(user),
        status="error",
        cost_usd=0.0,
    )
    return None

RESULTS_FILE = os.path.expanduser("~/portfolio-bot/screen_results.json")
TICKERS_FILE = os.path.expanduser("~/portfolio-bot/tickers.json")

# Batch API タイムアウト（秒）。小バッチ(30件以下)は通常1〜3分で完了する。
BATCH_TIMEOUT_SECONDS = 3600
HAIKU_MODEL_ID = "claude-haiku-4-5-20251001"


def _append_llm_call_log(row: dict) -> None:
    try:
        from analyst.llm_client import _append_llm_call_log as _append
        _append(row)
    except Exception:
        pass


def _log_anthropic_usage(
    *,
    role: str,
    model: str,
    max_tokens: int,
    started: float,
    prompt_chars: int,
    response=None,
    status: str = "ok",
    use_tool: bool = False,
    error: Exception | None = None,
    **extra,
) -> None:
    usage = getattr(response, "usage", None)
    row = {
        "ts": time.strftime("%Y-%m-%dT%H:%M:%S"),
        "role": role,
        "model": model,
        "use_tool": use_tool,
        "max_tokens": max_tokens,
        "elapsed_sec": round(time.monotonic() - started, 2),
        "prompt_chars": prompt_chars,
        "status": status,
        **extra,
    }
    if response is not None:
        row.update({
            "stop_reason": getattr(response, "stop_reason", None),
            "content_types": [getattr(block, "type", None) for block in getattr(response, "content", [])],
            "input_tokens": getattr(usage, "input_tokens", None),
            "output_tokens": getattr(usage, "output_tokens", None),
        })
    if error is not None:
        row.update({
            "error_type": type(error).__name__,
            "error": str(error)[:500],
            "cost_usd": 0.0,
        })
    _append_llm_call_log(row)


def _log_anthropic_batch_usage(
    *,
    role: str,
    batch_id: str | None,
    status: str,
    started: float,
    **extra,
) -> None:
    row = {
        "ts": time.strftime("%Y-%m-%dT%H:%M:%S"),
        "role": role,
        "model": HAIKU_MODEL_ID,
        "use_tool": False,
        "batch": True,
        "max_tokens": extra.pop("max_tokens", 200),
        "elapsed_sec": round(time.monotonic() - started, 2),
        "batch_id": batch_id,
        "status": status,
        **extra,
    }
    _append_llm_call_log(row)


def _extract_message_usage(message) -> tuple[int | None, int | None]:
    usage = getattr(message, "usage", None)
    return (
        getattr(usage, "input_tokens", None),
        getattr(usage, "output_tokens", None),
    )

# Opus 最終判断 Tool Use スキーマ（正規表現 JSON パースを完全置き換え）
_OPUS_JUDGMENT_TOOL = {
    "name": "submit_judgment",
    "description": "銘柄の最終投資判断を構造化JSONで提出する",
    "input_schema": {
        "type": "object",
        "properties": {
            "signal":         {"type": "string", "enum": ["買い", "様子見", "見送り"]},
            "score":          {"type": "number",  "minimum": 1, "maximum": 5},
            "entry_price":    {"description": "推奨エントリー価格"},
            "target_price":   {"description": "目標株価"},
            "stop_loss":      {"description": "損切りライン"},
            "reason":         {"type": "string"},
            "holding_period": {"type": "string"},
        },
        "required": ["signal", "score", "entry_price", "target_price",
                     "stop_loss", "reason", "holding_period"],
    },
}

def send_telegram(message):
    if not TELEGRAM_TOKEN or not TELEGRAM_CHAT_ID:
        print('[WARN] TELEGRAM_TOKEN/CHAT_ID 未設定 — 通知スキップ')
        return
    url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
    try:
        requests.post(url, data={
            "chat_id": TELEGRAM_CHAT_ID,
            "text": message,
            "parse_mode": "HTML"
        }, timeout=15)
    except requests.RequestException as e:
        print(f'[WARN] Telegram送信失敗: {e}')

def get_macro_score():
    """マクロ環境スコアを計算（0-10）+ 地合い情報
    FRED API が設定済みの場合、イールドカーブ・CPI・FF金利をスコアに反映する。
    """
    try:
        vix = float(yf.Ticker("^VIX").fast_info['lastPrice'])
        usdjpy = float(yf.Ticker("JPY=X").fast_info['lastPrice'])
        tnx = float(yf.Ticker("^TNX").fast_info['lastPrice'])

        spy_hist = yf.Ticker("SPY").history(period="3mo")
        spy_price = float(spy_hist['Close'].iloc[-1])
        spy_ma50 = float(spy_hist['Close'].rolling(50).mean().iloc[-1])
        spy_above = spy_price > spy_ma50
        spy_5d_chg = (spy_price - float(spy_hist['Close'].iloc[-6])) / float(spy_hist['Close'].iloc[-6]) * 100

        nk_hist = yf.Ticker("^N225").history(period="3mo")
        nk_price = float(nk_hist['Close'].iloc[-1])
        nk_ma50 = float(nk_hist['Close'].rolling(50).mean().iloc[-1])
        nk_above = nk_price > nk_ma50

        score = 10
        if vix > 30:
            score = 0
        elif vix > 25:
            score -= 4
        elif vix > 20:
            score -= 2
        if tnx > 5.0: score -= 4
        elif tnx > 4.5: score -= 2
        if not spy_above: score -= 2
        if spy_5d_chg < -2: score -= 2

        # ── FRED マクロ指標による追加調整 ──────────────────────────
        try:
            from macro_fetcher import get_macro_context
            macro_ctx = get_macro_context()
            adj = macro_ctx.get("macro_adj", 0)
            if score > 0:  # サーキットブレーカー時は加算しない
                score = max(0, score + adj)
            macro_note = ""
            if macro_ctx.get("yield_inverted"):
                macro_note += " 逆イールド⚠️"
            cpi = macro_ctx.get("cpi_yoy")
            if cpi and cpi > 4.0:
                macro_note += f" CPI{cpi:.1f}%⚠️"
        except Exception:
            macro_note = ""
        # ────────────────────────────────────────────────────────────

        if score >= 8:
            market_condition = f"強気（全戦略有効）{macro_note}"
        elif score >= 5:
            market_condition = f"中立（逆張り・イベント中心）{macro_note}"
        elif score >= 2:
            market_condition = f"弱気（逆張りのみ・小ロット）{macro_note}"
        else:
            market_condition = f"危険（サーキットブレーカー）{macro_note}"

        return score, vix, usdjpy, tnx, spy_above, nk_above, market_condition
    except Exception:
        return 5, 0, 0, 0, True, True, "取得失敗"

def get_stock_data(ticker):
    """銘柄データ取得"""
    try:
        stock = yf.Ticker(ticker)
        hist = stock.history(period="3mo")
        if hist.empty:
            return None
        
        current_price = hist['Close'].iloc[-1]
        prev_price = hist['Close'].iloc[-2]
        change_pct = (current_price - prev_price) / prev_price * 100
        
        # モメンタム計算
        mom_1m = (current_price - hist['Close'].iloc[-22]) / hist['Close'].iloc[-22] * 100
        mom_3m = (current_price - hist['Close'].iloc[0]) / hist['Close'].iloc[0] * 100
        
        # RSI計算
        delta = hist['Close'].diff()
        gain = delta.where(delta > 0, 0).rolling(14).mean()
        loss = -delta.where(delta < 0, 0).rolling(14).mean()
        rs = gain / loss
        rsi = 100 - (100 / (1 + rs.iloc[-1]))
        
        # 出来高比率
        avg_volume = hist['Volume'].iloc[-20:].mean()
        current_volume = hist['Volume'].iloc[-1]
        volume_ratio = current_volume / avg_volume
        
        return {
            "ticker": ticker,
            "price": round(current_price, 2),
            "change_pct": round(change_pct, 2),
            "rsi": round(rsi, 1),
            "mom_1m": round(mom_1m, 1),
            "mom_3m": round(mom_3m, 1),
            "volume_ratio": round(volume_ratio, 2)
        }
    except:
        return None

def screen_candidates(stocks):
    """一次フィルタリング：市場環境に合わせた条件"""
    candidates = []
    for data in stocks:
        if data is None:
            continue
        if data['rsi'] < 25:
            candidates.append(data)
        elif data['rsi'] < 35 and data['volume_ratio'] > 1.0:
            candidates.append(data)
        elif abs(data['mom_1m']) > 8 and data['volume_ratio'] > 1.1:
            candidates.append(data)
    candidates.sort(key=lambda x: x['rsi'])
    return candidates[:5]


def safe_api_call(func, retries=3, wait=15):
    for i in range(retries):
        try:
            return func()
        except anthropic.RateLimitError:
            if i < retries - 1:
                print(f"  レートリミット。{wait}秒待機...")
                time.sleep(wait)
            else:
                return None
        except Exception as e:
            print(f"  APIエラー: {e}")
            return None

def get_news_headlines(ticker, max_items=3):
    """yfinanceからニュースヘッドラインを取得"""
    try:
        news = yf.Ticker(ticker).news
        if not news:
            return ""
        headlines = []
        for item in news[:max_items]:
            # 新構造: item['content']['title']
            title = item.get('content', {}).get('title', '') or item.get('title', '')
            if title:
                headlines.append(f"・{title}")
        return "\n".join(headlines)
    except:
        return ""

def _build_context_str(stock_data: dict, macro_info: tuple) -> str:
    """analyze_with_agents のコンテキスト文字列を構築（バッチAPI共有化）"""
    ticker = stock_data['ticker']
    strategy = stock_data.get('strategy', '')
    atr_pct = stock_data.get('atr_pct', '-')
    stop_loss_atr = stock_data.get('stop_loss_atr', '-')
    strategy_notes = {
        '逆張り':            '売られすぎからのリバウンド狙い。地合いが悪い場合は信頼性低下に注意。',
        'モメンタム':         '強トレンドへの乗り。高値掴みリスクに注意し素早い損切りを推奨。',
        'ギャップダウン':     '当日急落からの短期リバウンド狙い。ニュース内容が最重要。一時的パニックか業績悪化かを見極めること。',
        'イベントドリブン後': '決算後の急落への対応。悪材料の継続性を見極めること。1回限りの失望売りかどうかが鍵。',
        'イベントドリブン前': '決算前の仕込み。決算リスクを十分考慮すること。',
    }
    strategy_note = strategy_notes.get(strategy, '')
    news_str = ""
    if strategy in ['ギャップダウン', 'イベントドリブン後', 'イベントドリブン前']:
        headlines = get_news_headlines(ticker)
        if headlines:
            news_str = f"\n【直近ニュース】\n{headlines}"
        else:
            news_str = "\n【直近ニュース】取得できませんでした"
    return (
        f"銘柄: {ticker}\n"
        f"戦略: {strategy} — {strategy_note}\n"
        f"現在値: ${stock_data['price']} (前日比 {stock_data['change_pct']}%)\n"
        f"RSI: {stock_data['rsi']}\n"
        f"出来高比率: {stock_data['volume_ratio']}倍\n"
        f"ATR: 株価の{atr_pct}% | ストップロス目安: ${stop_loss_atr}（2×ATR）\n"
        f"1ヶ月モメンタム: {stock_data['mom_1m']}%\n"
        f"3ヶ月モメンタム: {stock_data['mom_3m']}%\n"
        f"VIX: {macro_info[1]:.1f}\n"
        f"ドル円: {macro_info[2]:.1f}\n"
        f"米10年債利回り: {macro_info[3]:.2f}%{news_str}"
    )


def _run_batch(requests_list: list, label: str = "") -> "dict | None":
    """
    Batch API に requests_list を投入して完了を待ち、結果を返す共通ヘルパー。
    タイムアウトまたはエラー時は None を返す。
    """
    try:
        started = time.monotonic()
        batch = client.messages.batches.create(requests=requests_list)
        _log_anthropic_batch_usage(
            role="analyzer_batch_submit",
            batch_id=batch.id,
            status="submitted",
            started=started,
            batch_status=getattr(batch, "processing_status", None),
            request_count=len(requests_list),
            label=label,
            cost_usd=0.0,
        )
        print(f"  📦 Batch 投入完了{(' '+label) if label else ''} ({len(requests_list)}件, ID={batch.id})")
    except Exception as e:
        _log_anthropic_batch_usage(
            role="analyzer_batch_submit",
            batch_id=None,
            status="error",
            started=started if "started" in locals() else time.monotonic(),
            request_count=len(requests_list),
            label=label,
            cost_usd=0.0,
            error_type=type(e).__name__,
            error=str(e)[:500],
        )
        print(f"  ⚠️ Batch 投入失敗 ({e}) → フォールバック")
        return None

    deadline = time.time() + BATCH_TIMEOUT_SECONDS
    while time.time() < deadline:
        try:
            status = client.messages.batches.retrieve(batch.id)
        except Exception as e:
            print(f"  ⚠️ Batch status 取得エラー ({e}) → フォールバック")
            return None
        if status.processing_status == "ended":
            results: dict = {}
            for r in client.messages.batches.results(batch.id):
                if r.result.type == "succeeded":
                    started = time.monotonic()
                    message = r.result.message
                    results[r.custom_id] = message.content[0].text
                    input_tokens, output_tokens = _extract_message_usage(message)
                    _log_anthropic_batch_usage(
                        role="analyzer_batch_result",
                        batch_id=batch.id,
                        status="ok",
                        started=started,
                        custom_id=r.custom_id,
                        label=label,
                        stop_reason=getattr(message, "stop_reason", None),
                        content_types=[getattr(block, "type", None) for block in getattr(message, "content", [])],
                        input_tokens=input_tokens,
                        output_tokens=output_tokens,
                    )
                else:
                    _log_anthropic_batch_usage(
                        role="analyzer_batch_result",
                        batch_id=batch.id,
                        status=getattr(r.result, "type", "error"),
                        started=time.monotonic(),
                        custom_id=r.custom_id,
                        label=label,
                        cost_usd=0.0,
                    )
            print(f"  ✅ Batch 完了{(' '+label) if label else ''} ({len(results)}/{len(requests_list)}件成功)")
            return results
        remaining = int(deadline - time.time())
        print(f"  ⏳ Batch 処理中… (残{remaining}秒)")
        time.sleep(20)

    print(f"  ⚠️ Batch タイムアウト ({BATCH_TIMEOUT_SECONDS}s) → フォールバック")
    return None


def _batch_debate_haiku(candidates: list, macro_info: tuple) -> "dict | None":
    """
    全候補の Bull / Bear / Risk 議論を Haiku × Batch API で一括処理（2段階）。

    Stage1: Bull + Bear（2N件）を一括処理
    Stage2: Risk（N件、Stage1の Bull/Bear 結果を含めて品質向上）を一括処理

    - Haiku 価格 + バッチ50%割引 → Sonnet逐次比で最大94%コスト削減
    - Stage1タイムアウト時は None を返す → 呼び出し元が逐次 Haiku フォールバックを実行
    - Stage2タイムアウト時は Stage1結果のみ返す → Risk は逐次Haikuフォールバック

    返り値: {"bull-AAPL": "...", "bear-AAPL": "...", "risk-AAPL": "...", ...}
    """
    # ── Stage1: Bull + Bear（並列実行可能）──────────────────────────────────
    # Anthropic Batch API は custom_id に '^[a-zA-Z0-9_-]{1,64}$' を要求するため
    # ticker の '.' '^' を '_' に置換（'6770.T' → '6770_T'）
    def _safe_id(t: str) -> str:
        return t.replace('.', '_').replace('^', '_').replace('=', '_')

    stage1_requests = []
    for cdata in candidates:
        ticker = cdata['ticker']
        sticker = _safe_id(ticker)
        context = _build_context_str(cdata, macro_info)
        stage1_requests.append({
            "custom_id": f"bull-{sticker}",
            "params": {
                "model": "claude-haiku-4-5-20251001",
                "max_tokens": 200,
                "system": "あなたは強気派アナリストです。この銘柄を今買うべき理由だけを3つ挙げてください。簡潔に。",
                "messages": [{"role": "user", "content": context}],
            },
        })
        stage1_requests.append({
            "custom_id": f"bear-{sticker}",
            "params": {
                "model": "claude-haiku-4-5-20251001",
                "max_tokens": 200,
                "system": "あなたは慎重派アナリストです。この銘柄を今買ってはいけない理由だけを3つ挙げてください。簡潔に。",
                "messages": [{"role": "user", "content": context}],
            },
        })

    stage1_results = _run_batch(stage1_requests, label="Stage1(Bull+Bear)")
    if stage1_results is None:
        return None

    # ── Stage2: Risk（Bull+Bear 結果を含めて品質向上）────────────────────────
    stage2_requests = []
    for cdata in candidates:
        ticker = cdata['ticker']
        sticker = _safe_id(ticker)
        context = _build_context_str(cdata, macro_info)
        bull_text = stage1_results.get(f"bull-{sticker}", "（取得失敗）")
        bear_text = stage1_results.get(f"bear-{sticker}", "（取得失敗）")
        risk_context = (
            f"{context}\n\n"
            f"強気派の意見:\n{bull_text}\n\n"
            f"慎重派の意見:\n{bear_text}"
        )
        stage2_requests.append({
            "custom_id": f"risk-{sticker}",
            "params": {
                "model": "claude-haiku-4-5-20251001",
                "max_tokens": 200,
                "system": (
                    "あなたはリスク管理の専門家です。"
                    "強気派・慎重派の議論を踏まえ、この銘柄投資の最悪シナリオと"
                    "見落とされがちなリスクを3点指摘してください。簡潔に。"
                ),
                "messages": [{"role": "user", "content": risk_context}],
            },
        })

    stage2_results = _run_batch(stage2_requests, label="Stage2(Risk)")
    if stage2_results is None:
        print("  ⚠️ Stage2タイムアウト → Risk は逐次Haikuにフォールバック")
        return stage1_results  # Stage1分だけ返す（Riskは呼び出し元でフォールバック）

    return {**stage1_results, **stage2_results}


def analyze_with_agents(stock_data, macro_info, batch_results: "dict | None" = None):
    """
    マルチエージェント分析。

    batch_results が渡された場合（_batch_debate_haiku() 成功時）:
      Bull/Bear/Risk テキストをバッチ結果から取得し Opus 最終判断のみ実行。
    batch_results が None の場合（タイムアウト or 投入失敗時）:
      従来通り Haiku で Bull/Bear/Risk を逐次呼び出し。

    Opus 最終判断は Tool Use (_OPUS_JUDGMENT_TOOL) で構造化 JSON を強制出力。
    """
    ticker = stock_data['ticker']
    context = _build_context_str(stock_data, macro_info)

    # ── Bull / Bear / Risk（バッチ結果優先、フォールバック: 逐次 Haiku）──────────
    # custom_id は '.' 等を '_' に置換してあるため lookup も同じ変換で行う
    sticker = ticker.replace('.', '_').replace('^', '_').replace('=', '_')
    bull = batch_results.get(f"bull-{sticker}") if batch_results else None
    bear = batch_results.get(f"bear-{sticker}") if batch_results else None
    skeptic = batch_results.get(f"risk-{sticker}") if batch_results else None

    if not bull:
        bull = _call_fast_llm(
            "あなたは強気派アナリストです。この銘柄を今買うべき理由だけを3つ挙げてください。簡潔に。",
            context,
        )
        if not bull:
            return None
        time.sleep(1)

    if not bear:
        bear = _call_fast_llm(
            "あなたは慎重派アナリストです。この銘柄を今買ってはいけない理由だけを3つ挙げてください。簡潔に。",
            context,
        )
        if not bear:
            return None
        time.sleep(1)

    if not skeptic:
        skeptic = _call_fast_llm(
            "あなたはリスク管理の専門家です。この銘柄投資の最悪シナリオと見落とされがちなリスクを3点指摘してください。簡潔に。",
            f"{context}\n\n強気派:\n{bull}\n\n慎重派:\n{bear}",
        )
        if not skeptic:
            return None
        time.sleep(1)

    # ── Opus 最終判断（Tool Use で構造化 JSON を強制出力 / 正規表現パース廃止）──
    opus_user = (
        f"{context}\n\n"
        f"強気派:\n{bull}\n\n"
        f"慎重派:\n{bear}\n\n"
        f"リスク派:\n{skeptic}"
    )
    def _opus_call():
        # model_router 経由で Opus 4.8 に昇格。ALMANAC_BUDGET_MODE=eco で Sonnet に降格可能。
        try:
            from model_router import get_model as _get_model
            _model_id = _get_model("final_synthesis")
        except ImportError:
            _model_id = "claude-opus-4-8"
        return client.messages.create(
            model=_model_id,
            max_tokens=800,
            system=(
                "あなたはヘッジファンドのポートフォリオマネージャーです。"
                "3つのアナリストの意見を総合して最終判断を submit_judgment ツールで提出してください。"
            ),
            tools=[_OPUS_JUDGMENT_TOOL],
            tool_choice={"type": "tool", "name": "submit_judgment"},
            messages=[{"role": "user", "content": opus_user}],
        )

    started = time.monotonic()
    response = safe_api_call(_opus_call)
    if response:
        model_id = getattr(response, "model", None)
        if not model_id:
            try:
                from model_router import get_model as _get_model
                model_id = _get_model("final_synthesis")
            except ImportError:
                model_id = "claude-opus-4-8"
        _log_anthropic_usage(
            role="analyzer_final_judgment",
            model=model_id,
            max_tokens=800,
            started=started,
            prompt_chars=len(opus_user),
            response=response,
            use_tool=True,
            ticker=ticker,
        )
        for block in response.content:
            if block.type == "tool_use":
                return block.input
    else:
        try:
            from model_router import get_model as _get_model
            model_id = _get_model("final_synthesis")
        except ImportError:
            model_id = "claude-opus-4-8"
        _log_anthropic_usage(
            role="analyzer_final_judgment",
            model=model_id,
            max_tokens=800,
            started=started,
            prompt_chars=len(opus_user),
            status="error",
            use_tool=True,
            ticker=ticker,
            cost_usd=0.0,
        )
    print(f"Opus Tool Use 失敗 ({ticker}): リトライ上限到達")
    return None

def get_position_size(price_usd, opus_score=3.5):
    """口座残高とOpusスコアからポジションサイズを計算（ケリー基準的）"""
    try:
        filepath = os.path.expanduser('~/portfolio-bot/account.json')
        if not os.path.exists(filepath):
            return None
        with open(filepath) as f:
            info = json.load(f)
        balance = info.get('balance', 0)
        base_ratio = info.get('risk_per_trade', 0.1)
        if balance == 0:
            return None

        # Opusスコアに応じてポジションサイズを調整
        # スコア5 → 15% / スコア4 → 12% / スコア3.5 → 10% / スコア3 → 7%
        score_multiplier = {
            5:   1.5,
            4:   1.2,
            3.5: 1.0,
            3:   0.7,
        }
        score_key = min(score_multiplier.keys(), key=lambda x: abs(x - float(opus_score)))
        adjusted_ratio = base_ratio * score_multiplier[score_key]
        adjusted_ratio = min(adjusted_ratio, 0.20)  # 最大20%上限

        usdjpy = yf.Ticker("JPY=X").fast_info['lastPrice']
        price_jpy = price_usd * usdjpy
        max_amount = balance * adjusted_ratio
        shares = int(max_amount / price_jpy)
        return {
            "shares": max(1, shares),
            "amount_jpy": int(price_jpy * max(1, shares)),
            "usdjpy": round(usdjpy, 1),
            "risk_ratio": round(adjusted_ratio * 100, 1)
        }
    except:
        return None

def format_signal_message(stock_data, judgment, macro_info):
    """Telegram通知メッセージを整形"""
    signal_emoji = {"買い": "🟢", "様子見": "🟡", "見送り": "🔴"}.get(judgment['signal'], "⚪")
    
    # ポジションサイズ計算
    pos = get_position_size(stock_data['price'], opus_score=judgment.get('score', 3.5))
    pos_str = f"\n💴 推奨株数: {pos['shares']}株（約¥{pos['amount_jpy']:,}）" if pos else ""
    
    return f"""
{signal_emoji} <b>{stock_data['ticker']} - {judgment['signal']}</b>
━━━━━━━━━━━━━━
💰 現在値: ${stock_data['price']} ({stock_data['change_pct']:+.1f}%)
📊 RSI: {stock_data['rsi']} | 出来高: {stock_data['volume_ratio']}倍

{pos_str}
🎯 エントリー: ${judgment.get('entry_price', '-')}
📈 目標株価: ${judgment.get('target_price', '-')}
🛑 損切りライン: ${judgment.get('stop_loss', '-')}
⏱ 保有期間: {judgment.get('holding_period', '-')}

💡 判断理由:
{judgment.get('reason', '-')}

⭐ 信頼度: {'★' * int(judgment.get('score', 0))}{'☆' * (5 - int(judgment.get('score', 0)))} ({judgment.get('score', 0)}/5)
━━━━━━━━━━━━━━
🌍 VIX: {macro_info[1]:.1f} | ドル円: {macro_info[2]:.1f}
"""


def _extract_prev_delta_state(prev: dict) -> dict:
    """Extract delta baseline from both current and legacy analysis JSON shapes."""
    if not isinstance(prev, dict):
        prev = {}
    synthesis = prev.get("synthesis") if isinstance(prev.get("synthesis"), dict) else {}

    _pt = prev.get("portfolio_total")
    if isinstance(_pt, dict):
        prev_total = _pt.get("total_jpy")
    elif isinstance(_pt, (int, float)):
        prev_total = float(_pt)
    else:
        prev_total = None

    market_meta = prev.get("market_meta") if isinstance(prev.get("market_meta"), dict) else {}
    if not market_meta:
        market_meta = (
            synthesis.get("market_meta_snapshot")
            if isinstance(synthesis.get("market_meta_snapshot"), dict)
            else {}
        )
    prev_vix = market_meta.get("vix")
    prev_regime = (
        prev.get("scenario_key")
        or (
            (prev.get("scenario") or {}).get("regime")
            if isinstance(prev.get("scenario"), dict)
            else None
        )
        or market_meta.get("regime")
        or synthesis.get("scenario_key")
    )
    dca = prev.get("dca_signals") if isinstance(prev.get("dca_signals"), dict) else {}
    if not dca:
        dca = synthesis.get("dca_signals") if isinstance(synthesis.get("dca_signals"), dict) else {}

    return {
        "portfolio_total": float(prev_total) if isinstance(prev_total, (int, float)) else None,
        "vix": prev_vix,
        "regime": prev_regime,
        "active_tranche": dca.get("active_tranche"),
    }


def is_market_hours():
    """米国市場が開いているか判定（日本時間）"""
    from datetime import datetime
    import pytz
    
    et = pytz.timezone('America/New_York')
    now_et = datetime.now(et)
    
    # 週末は除外
    if now_et.weekday() >= 5:
        return False, "週末のため市場クローズ"
    
    # 市場時間 9:30-16:00 ET
    open_time = now_et.replace(hour=9, minute=30, second=0)
    close_time = now_et.replace(hour=16, minute=0, second=0)
    
    if open_time <= now_et <= close_time:
        return True, "市場オープン中"
    elif now_et < open_time:
        return False, f"市場未開場（開場まで{int((open_time - now_et).seconds/60)}分）"
    else:
        return False, "市場クローズ済み"

def main(*, force_evening: bool = False, now: datetime | None = None):
    now = now or datetime.now()
    if now.hour >= 12 and not force_evening:
        print(
            f"[{now.strftime('%H:%M:%S')}] evening commentary skipped "
            "(use --force-evening for a manual run)"
        )
        return {"status": "skipped_evening_commentary"}
    print(f"[{now.strftime('%H:%M:%S')}] 分析開始...")
    
    # 市場時間チェック（参考表示のみ）
    market_open, market_status = is_market_hours()
    print(f"市場状態: {market_status}")
    # ALMANAC: telegram disabled — ai_analysis only
    # send_telegram(f"🤖 <b>分析開始</b> {datetime.now().strftime('%Y/%m/%d %H:%M')}")

    # マクロ環境チェック
    macro = get_macro_score()
    macro_score, macro_vix, macro_usdjpy, macro_tnx, spy_above, nk_above, market_condition = macro
    if macro_score == 0:
        msg = f"⚠️ <b>サーキットブレーカー発動</b>\nVIX {macro_vix:.1f} が30を超えています。本日の売買シグナルを停止します。"
        # ALMANAC: telegram disabled — ai_analysis only
        # send_telegram(msg)
        return

    # 地合いに応じてシグナル閾値を調整
    if macro_score >= 8:
        signal_threshold = 3.5
    elif macro_score >= 5:
        signal_threshold = 3.8
    else:
        signal_threshold = 4.2

    # 決算シーズン調整
    season = get_season_config()
    signal_threshold += season['signal_threshold_delta']
    signal_threshold = max(3.0, signal_threshold)  # 最低3.0
    print(f"地合い: {market_condition}（スコア{macro_score}）")
    print(f"決算シーズン: {season['label']}")
    print(f"シグナル閾値: {signal_threshold}")

    # セクターレポート保存
    try:
        save_sector_report()
    except:
        pass

    # スクリーニング結果を読み込む
    import screener as sc
    print("全市場スクリーニング実行中...")
    screened, market_meta, meta_text = sc.run_full_screen()
    
    if not screened:
        # ALMANAC: telegram disabled — ai_analysis only
        # send_telegram("📊 本日の候補銘柄なし。様子見が推奨されます。")
        return

    # 新設計：優先度付き候補をそのまま使用
    # イベントドリブン前のみevent_calendarフィルタを適用
    event_pre = [s for s in screened if s.get('strategy') == 'イベントドリブン前']
    others = [s for s in screened if s.get('strategy') != 'イベントドリブン前']
    ep_filtered = filter_by_events(event_pre) if event_pre else []
    selected = others + ep_filtered

    # セクターローテーション：強いセクターを優先
    selected = filter_by_sector_strength(selected, top_n=4)

    # 決算シーズン中はイベントドリブンを先頭に
    if season['event_driven_priority']:
        event_types = [s for s in selected if 'イベントドリブン' in s.get('strategy','')]
        other_types = [s for s in selected if 'イベントドリブン' not in s.get('strategy','')]
        selected = event_types + other_types

    selected = selected[:10]

    candidates = []
    for s in selected:
        data = get_stock_data(s["ticker"])
        if data:
            data['event_risks'] = s.get('event_risks', [])
            data['strategy'] = s.get('strategy', '逆張り')
            data['atr_pct'] = s.get('atr_pct', '')
            data['stop_loss_atr'] = s.get('stop_loss_atr', '')
            data['reason'] = s.get('reason', '')
            candidates.append(data)
    print(f"候補銘柄: {[(c['ticker'], c['strategy']) for c in candidates]}")

    if not candidates:
        # ALMANAC: telegram disabled — ai_analysis only
        # send_telegram("📊 本日の候補銘柄なし。様子見が推奨されます。")
        return

    # ── Batch API で Bull/Bear/Risk を全候補同時処理（Haiku × 3N 件）────────────
    # バッチ成功: 逐次 Sonnet 比で最大94%コスト削減（Haiku価格 + バッチ50%割引）
    # バッチ失敗 / タイムアウト: 逐次 Haiku にフォールバック（品質変わらず）
    print("📦 Batch API で Bull/Bear/Risk を全候補同時投入中…")
    batch_results = _batch_debate_haiku(candidates, macro)
    if batch_results:
        print(f"  → バッチ完了: {len(batch_results)}件の議論テキスト取得")
    else:
        print("  → バッチ未完了: 逐次 Haiku にフォールバック")

    # マルチエージェント分析
    signals_sent = 0
    watch_list = []  # 閾値未満だが参考になる候補を収集
    for stock in candidates:
        if signals_sent >= 5:  # 1日最大5シグナル
            break

        print(f"{stock['ticker']} 分析中（Opus 最終判断）…")
        try:
            judgment = analyze_with_agents(stock, macro, batch_results=batch_results)
        except Exception as e:
            print(f"分析エラー ({stock['ticker']}): {e}")
            judgment = None

        if judgment and float(judgment.get('score', 0)) >= signal_threshold and judgment.get('signal') == '買い':
            msg = format_signal_message(stock, judgment, macro)
            # ALMANAC: telegram disabled — ai_analysis only
            # send_telegram(msg)
            # シグナルをログに保存
            try:
                log_path = os.path.expanduser('~/portfolio-bot/signals_log.json')
                logs = {}
                if os.path.exists(log_path):
                    with open(log_path) as f:
                        logs = json.load(f)
                logs[stock['ticker']] = {
                    'entry_price': judgment.get('entry_price'),
                    'target_price': judgment.get('target_price'),
                    'stop_loss': judgment.get('stop_loss'),
                    'reason': judgment.get('reason'),
                    'holding_period': judgment.get('holding_period'),
                    'score': judgment.get('score'),
                    'signal_date': datetime.now().strftime('%Y-%m-%d %H:%M')
                }
                with open(log_path, 'w') as f:
                    json.dump(logs, f, indent=2, ensure_ascii=False)
            except Exception as e:
                print(f"シグナルログ保存エラー: {e}")
            signals_sent += 1
        elif judgment:
            # 閾値未満でもウォッチリストに追加
            watch_list.append({
                "ticker": stock['ticker'],
                "signal": judgment.get('signal', '様子見'),
                "score": judgment.get('score', 0),
                "reason": (judgment.get('reason') or '')[:80],
            })

    if signals_sent == 0:
        # レジーム情報（destructured済みの spy_above / nk_above を使用）
        bear = not spy_above and not nk_above
        regime_note = "⚠️ SPY・NK ともに MA50 割れ（弱気レジーム）" if bear else ""

        if watch_list:
            # ウォッチ候補がある場合は詳細を送信
            watch_list.sort(key=lambda x: float(x['score']), reverse=True)
            lines = [f"📊 <b>本日のシグナル結果</b>（閾値 {signal_threshold:.1f}/5）"]
            if regime_note:
                lines.append(regime_note)
            lines.append("─ 候補なし（買いシグナル未達）")
            for w in watch_list[:3]:
                star = "★" * int(w['score']) + "☆" * (5 - int(w['score']))
                lines.append(f"👁 {w['ticker']} [{w['signal']}] {star} ({w['score']}/5)\n   {w['reason']}")
            # ALMANAC: telegram disabled — ai_analysis only
            # send_telegram("\n".join(lines))
        else:
            # ALMANAC: telegram disabled — ai_analysis only
            # send_telegram(f"📊 本日は候補銘柄なし。{regime_note or '様子見を推奨します。'}")
            pass
    
    print(f"分析完了。{signals_sent}件のシグナルを送信しました。")

def _run_delta_only():
    """
    Part D: 軽量 delta 監視モード。

    フル分析は 06:00 JST の 1 本に集約し、7:30 / 17:00 cron はこの mode で
    「前回の fullanalysis (ai_portfolio_analysis.json) から材料変化があるか」を
    Haiku 相当の軽量呼び出しでチェックし、しきい値超えのときだけ Telegram 通知。

    判定ロジック（決定的・LLM 依存無し）:
      - portfolio value vs 前回: ±1.5% 以上動いたら通知
      - VIX  vs 前回: ±15% 以上動いたら通知
      - regime 変化（bull/neutral/bear）があれば必ず通知
      - DCA ラダーの active_tranche が変化したら必ず通知
    材料が無ければサイレント（コスト 0）。
    """
    import json as _json
    from pathlib import Path as _P
    import os as _os

    base = _P(__file__).parent
    prev_file = base / "ai_portfolio_analysis.json"
    dca_file  = base / "bottom_fishing_signals.json"

    if not prev_file.exists():
        print("[delta] no prior ai_portfolio_analysis.json → skip (initial run)")
        return

    try:
        prev = _json.loads(prev_file.read_text(encoding="utf-8"))
    except Exception as e:
        print(f"[delta] cannot parse prev json: {e}")
        return

    prev_state = _extract_prev_delta_state(prev)
    prev_total = prev_state["portfolio_total"]
    prev_vix = prev_state["vix"]
    if prev_vix is None:
        # macro_state.json fallback
        try:
            _ms = base / "macro_state.json"
            if _ms.exists():
                prev_vix = (_json.loads(_ms.read_text(encoding="utf-8")) or {}).get("vix")
        except Exception:
            pass
    prev_regime = prev_state["regime"]

    # 現在値を取得
    try:
        from portfolio_manager import build_portfolio_snapshot
        cur_total = float(
            build_portfolio_snapshot(fetch_missing_sectors=False).get("total_jpy", 0) or 0
        )
    except Exception as e:
        print(f"[delta] portfolio fetch failed: {e}")
        cur_total = None

    try:
        import yfinance as _yf
        vt = _yf.Ticker("^VIX").history(period="2d")
        cur_vix = float(vt["Close"].iloc[-1]) if not vt.empty else None
    except Exception:
        cur_vix = None

    # 現在 regime （最軽量: macro_state.json の regime フィールドを参照）
    cur_regime = None
    try:
        _ms = base / "macro_state.json"
        if _ms.exists():
            cur_regime = (_json.loads(_ms.read_text(encoding="utf-8")) or {}).get("regime")
    except Exception:
        pass

    cur_tranche = None
    if dca_file.exists():
        try:
            cur_tranche = _json.loads(dca_file.read_text(encoding="utf-8")).get("active_tranche")
        except Exception:
            cur_tranche = None
    prev_tranche = prev_state["active_tranche"]

    alerts = []
    if prev_total and cur_total:
        pct = (cur_total - prev_total) / prev_total
        if abs(pct) >= 0.015:
            alerts.append(f"ポートフォリオ {pct*100:+.2f}% (¥{prev_total:,.0f} → ¥{cur_total:,.0f})")
    if prev_vix and cur_vix:
        pct = (cur_vix - prev_vix) / prev_vix
        if abs(pct) >= 0.15:
            alerts.append(f"VIX {pct*100:+.1f}% ({prev_vix:.1f} → {cur_vix:.1f})")
    if cur_tranche != prev_tranche:
        alerts.append(f"DCA tranche 変化: {prev_tranche} → {cur_tranche}")
    if prev_regime and cur_regime and prev_regime != cur_regime:
        alerts.append(f"Regime 変化: {prev_regime} → {cur_regime}")

    if alerts:
        msg = "🔔 <b>Delta Check</b>\n" + "\n".join(f"• {a}" for a in alerts) \
              + "\n\n→ 次回 06:00 のフル分析まで新規重要材料なし判定でもよいが、変化が大きいため手動 refresh 推奨"
        try:
            # ALMANAC: telegram disabled — ai_analysis only
            # send_telegram(msg)
            pass
        except Exception:
            pass
        print(f"[delta] {len(alerts)} alerts sent")
    else:
        print("[delta] no material change; silent")


def regen_stale_signals(min_days_stale: int = 30) -> dict:
    """
    P3-12: 保有銘柄のうち signals_log.json の signal_date が `min_days_stale` 日以上
    古いものを検出し、analyze_with_agents で再生成して上書きする。

    weekly cron (日曜 7:30) からトリガすることを想定。
    NVDA 67 日放置のような active_signal の硬直化を防ぐ。

    Returns:
        {"scanned": int, "regenerated": int, "skipped": int,
         "stale_tickers": [str], "errors": {ticker: msg}}
    """
    log_path = os.path.expanduser('~/portfolio-bot/signals_log.json')
    holdings_path = os.path.expanduser('~/portfolio-bot/holdings.json')

    result = {
        "scanned": 0, "regenerated": 0, "skipped": 0,
        "stale_tickers": [], "errors": {},
    }

    if not os.path.exists(log_path):
        print("[regen-stale] signals_log.json なし — skip")
        return result
    try:
        with open(log_path) as f:
            logs = json.load(f) or {}
    except Exception as e:
        print(f"[regen-stale] signals_log 読込エラー: {e}")
        return result

    held_tickers = set()
    if os.path.exists(holdings_path):
        try:
            with open(holdings_path) as f:
                hd = json.load(f) or {}
            held_tickers = {t for t in hd.keys() if not t.startswith("CASH")}
        except Exception:
            pass

    cutoff = datetime.now() - timedelta(days=min_days_stale)
    stale: list[str] = []
    for ticker, entry in logs.items():
        result["scanned"] += 1
        if held_tickers and ticker not in held_tickers:
            result["skipped"] += 1
            continue
        sd = (entry or {}).get("signal_date") or ""
        try:
            dt = datetime.strptime(sd[:16], "%Y-%m-%d %H:%M")
        except Exception:
            try:
                dt = datetime.strptime(sd[:10], "%Y-%m-%d")
            except Exception:
                stale.append(ticker)
                continue
        if dt < cutoff:
            stale.append(ticker)

    result["stale_tickers"] = stale
    if not stale:
        print(f"[regen-stale] {result['scanned']}件スキャン — 古いシグナルなし")
        return result

    print(f"[regen-stale] {len(stale)}件の古いシグナルを再生成: {stale}")
    macro = None
    try:
        macro = get_macro_score()
    except Exception as e:
        print(f"[regen-stale] macro 取得失敗 — フォールバック: {e}")
        macro = (5, 18.0, 150.0, 4.2, True, True, "neutral")

    for ticker in stale:
        try:
            data = get_stock_data(ticker)
            if not data:
                result["errors"][ticker] = "get_stock_data returned None"
                continue
            judgment = analyze_with_agents(data, macro)
            if not judgment:
                result["errors"][ticker] = "analyze_with_agents returned None"
                continue
            logs[ticker] = {
                "entry_price":    judgment.get("entry_price"),
                "target_price":   judgment.get("target_price"),
                "stop_loss":      judgment.get("stop_loss"),
                "reason":         judgment.get("reason"),
                "holding_period": judgment.get("holding_period"),
                "score":          judgment.get("score"),
                "signal":         judgment.get("signal"),
                "signal_date":    datetime.now().strftime("%Y-%m-%d %H:%M"),
                "regen_source":   "regen_stale_signals",
            }
            result["regenerated"] += 1
            print(f"  ✓ {ticker} 再生成完了 (score={judgment.get('score')})")
        except Exception as e:
            result["errors"][ticker] = str(e)[:300]
            print(f"  ✗ {ticker} 再生成失敗: {e}")

    if result["regenerated"] > 0:
        try:
            with open(log_path, 'w') as f:
                json.dump(logs, f, indent=2, ensure_ascii=False)
        except Exception as e:
            print(f"[regen-stale] signals_log 書込エラー: {e}")

    return result


if __name__ == "__main__":
    import sys as _sys
    delta_only  = "--delta-only" in _sys.argv
    regen_stale = "regen-stale" in _sys.argv
    force_evening = "--force-evening" in _sys.argv

    # P2-9: ヘルスチェック用ハートビート
    try:
        from utils import heartbeat
    except Exception:
        heartbeat = None
    try:
        if regen_stale:
            r = regen_stale_signals()
            print(f"[regen-stale] done: {r}")
            if heartbeat:
                heartbeat('analyzer_regen_stale', 'ok',
                          f"regen={r['regenerated']}/{r['scanned']}")
        elif delta_only:
            _run_delta_only()
            if heartbeat:
                heartbeat('analyzer_delta', 'ok')
        else:
            main(force_evening=force_evening)
            if heartbeat:
                heartbeat('analyzer', 'ok')
    except Exception as _e:
        if heartbeat:
            tag = 'analyzer_regen_stale' if regen_stale else (
                'analyzer_delta' if delta_only else 'analyzer'
            )
            heartbeat(tag, 'error', str(_e)[:500])
        raise
