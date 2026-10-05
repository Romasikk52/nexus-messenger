# language: Python 3.11, file: server.py
# Простой мессенджер. Запуск: uvicorn server:app --host 0.0.0.0 --port 8000
import time, uuid
from pathlib import Path
from typing import Dict, List
from fastapi import FastAPI, Request, WebSocket, WebSocketDisconnect
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.middleware.cors import CORSMiddleware

app = FastAPI()
app.add_middleware(CORSMiddleware, allow_origins=["*"],
                   allow_methods=["*"], allow_headers=["*"])

users: Dict[str, dict] = {}
chats: List[dict] = []

class WSManager:
    def __init__(self):
        self.conns: Dict[str, WebSocket] = {}
    async def connect(self, uid, ws):
        await ws.accept()
        self.conns[uid] = ws
    def disconnect(self, uid):
        self.conns.pop(uid, None)
    async def send(self, uid, data):
        ws = self.conns.get(uid)
        if ws:
            try: await ws.send_json(data)
            except Exception: self.conns.pop(uid, None)

ws_mgr = WSManager()

@app.post("/auth/register")
async def register(req: Request):
    b = await req.json()
    name = b.get("name", "").strip()
    if not name:
        return JSONResponse({"error": "no name"}, status_code=400)
    for u in users.values():
        if u["name"].lower() == name.lower():
            return JSONResponse({"error": "name taken"}, status_code=400)
    uid = str(uuid.uuid4())
    users[uid] = {"name": name, "password": b.get("password", "")}
    return {"id": uid, "name": name}

@app.post("/auth/login")
async def login(req: Request):
    b = await req.json()
    for uid, u in users.items():
        if u["name"] == b.get("name") and u["password"] == b.get("password"):
            return {"id": uid, "name": u["name"]}
    return JSONResponse({"error": "invalid"}, status_code=401)

@app.get("/users")
async def list_users():
    return [{"id": uid, "name": u["name"]} for uid, u in users.items()]

@app.get("/messages/{uid}")
async def get_messages(uid: str, peer: str = None):
    out = []
    for m in chats:
        if m["from"] == uid or m["to"] == uid:
            if peer is None or m["from"] == peer or m["to"] == peer:
                out.append(m)
    return out

@app.websocket("/ws/chat/{uid}")
async def ws_chat(ws: WebSocket, uid: str):
    await ws_mgr.connect(uid, ws)
    try:
        while True:
            data = await ws.receive_json()
            msg = {
                "id": str(uuid.uuid4()),
                "from": uid,
                "to": data.get("to"),
                "text": data.get("text", ""),
                "ts": time.time(),
            }
            chats.append(msg)
            if msg["to"]:
                await ws_mgr.send(msg["to"], {"type": "msg", "msg": msg})
            await ws_mgr.send(uid, {"type": "msg", "msg": msg})
    except WebSocketDisconnect:
        ws_mgr.disconnect(uid)

@app.get("/version")
async def version():
    return {"version": "1.0.0"}

@app.get("/", response_class=HTMLResponse)
async def index():
    p = Path(__file__).parent / "client.html"
    if p.exists():
        return p.read_text(encoding="utf-8")
    return "<h1>client.html не найден</h1>"