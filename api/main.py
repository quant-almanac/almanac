"""
ALMANAC v5.0 — FastAPI バックエンド

P0-3: localhost バインド + 書き込み系エンドポイントの X-API-Key 認証。
- host は 127.0.0.1（LAN 全開を防止）
- GET / WebSocket / /health / / は認証不要
- POST / PUT / DELETE / PATCH は X-API-Key ヘッダ必須
- API キーは ~/.config/almanac/api_key (0600) か環境変数 ALMANAC_API_KEY
  を優先し、移行期間は旧 ALMANAC 名にも fallback する
- 環境変数 ALLOW_UNAUTH=1 で認証をバイパス（段階移行用）
"""
import os
import sys
from pathlib import Path

BASE_DIR = Path(__file__).parent.parent
sys.path.insert(0, str(BASE_DIR))

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from fastapi.middleware.cors import CORSMiddleware
from almanac.runtime_config import load_api_key
from api.routes import dashboard, portfolio, risk, signals, ws, chart, rebalance, margin, nisa, admin, decision, screening, strategy, ai_analysis, actions, contributions, chat, macro, batch, agent, market, scenario, comparison, dca, tuning, performance, disclosure, today, system_status

app = FastAPI(title="ALMANAC API", version="5.0.0")

# ============================================================
# P0-3: API キー読込（環境変数 → ~/.config/almanac/api_key）
# ============================================================

API_KEY = load_api_key()
ALLOW_UNAUTH = os.environ.get("ALLOW_UNAUTH", "").strip() == "1"
# 認証不要パス（読み取り専用 or ヘルスチェック）
_AUTH_EXEMPT_PATHS = {"/", "/health", "/docs", "/openapi.json", "/redoc"}


@app.middleware("http")
async def api_key_auth(request: Request, call_next):
    """書き込み系（POST/PUT/DELETE/PATCH）のみ X-API-Key を要求。"""
    if ALLOW_UNAUTH:
        return await call_next(request)

    method = request.method.upper()
    path = request.url.path

    # 読み取り系は素通り
    if method in ("GET", "HEAD", "OPTIONS") or path in _AUTH_EXEMPT_PATHS:
        return await call_next(request)

    # WebSocket は middleware を通らない（scope['type']=='websocket'）ため無視

    if not API_KEY:
        return JSONResponse(
            status_code=503,
            content={
                "detail": (
                    "API key not configured. Set ALMANAC_API_KEY or "
                    "~/.config/almanac/api_key"
                )
            },
        )

    provided = request.headers.get("x-api-key", "")
    if provided != API_KEY:
        return JSONResponse(status_code=403, content={"detail": "Invalid or missing X-API-Key"})

    return await call_next(request)


app.add_middleware(
    CORSMiddleware,
    allow_origins=[
        "http://localhost:3000",
        "http://localhost:3001",
        "http://localhost:3100",  # dev preview (/today v5)
        "http://127.0.0.1:3000",
        "http://127.0.0.1:3001",
        "http://127.0.0.1:3100",
    ],
    allow_methods=["*"],
    allow_headers=["*", "X-API-Key"],
)

app.include_router(dashboard.router)
app.include_router(portfolio.router)
app.include_router(risk.router)
app.include_router(signals.router)
app.include_router(ws.router)
app.include_router(chart.router)
app.include_router(rebalance.router)
app.include_router(margin.router)
app.include_router(nisa.router)
app.include_router(admin.router)
app.include_router(decision.router)
app.include_router(screening.router)
app.include_router(strategy.router)
app.include_router(ai_analysis.router)
app.include_router(actions.router)
app.include_router(contributions.router)
from api.routes import cash as _cash_routes  # Fix 8B: 現金入出金 API
app.include_router(_cash_routes.router)
app.include_router(chat.router)
app.include_router(macro.router)
app.include_router(batch.router)
app.include_router(agent.router)
app.include_router(market.router)
app.include_router(scenario.router)
app.include_router(comparison.router)
app.include_router(dca.router)
app.include_router(tuning.router)
app.include_router(performance.router)  # 整理 #6: TWR / tax-lots / policy-decisions / ledger-events
app.include_router(disclosure.router)  # Phase 0: observe_only public-disclosure features (参考のみ)
app.include_router(today.router)  # /today オブシディアン・コンソール v5 合成エンドポイント
app.include_router(system_status.router)


@app.get("/")
async def root():
    return {"status": "ok", "version": "5.0.0"}


@app.get("/health")
async def health():
    return {"status": "ok", "version": "5.0.0"}


if __name__ == "__main__":
    import uvicorn
    # P0-3: 127.0.0.1 バインドに変更（LAN 全開を防止）。
    # LAN からのアクセスが必要な場合は BIND_HOST=0.0.0.0 + ALMANAC_API_KEY 設定で有効化可能。
    host = os.environ.get("BIND_HOST", "127.0.0.1")
    uvicorn.run("api.main:app", host=host, port=8000, reload=True)
