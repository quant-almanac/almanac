"""
WS /ws/live
30秒ごとに guard_state + regime を push
"""
import asyncio
import json
import sys
from pathlib import Path
from fastapi import APIRouter, WebSocket, WebSocketDisconnect

router = APIRouter()
BASE_DIR = Path(__file__).parent.parent.parent
sys.path.insert(0, str(BASE_DIR))
from utils import load_json as _load_json


@router.websocket("/ws/live")
async def ws_live(websocket: WebSocket):
    await websocket.accept()
    try:
        while True:
            guard = _load_json(BASE_DIR / "guard_state.json")
            regime = _load_json(BASE_DIR / "regime_state.json")
            payload = json.dumps({"type": "update", "data": {"guard": guard, "regime": regime}})
            await websocket.send_text(payload)
            await asyncio.sleep(30)
    except WebSocketDisconnect:
        pass
    except Exception:
        pass
