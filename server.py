# language: Python 3.11, file: server.py
# Nexus Messenger v3. Регистрация по номеру ИЛИ юзернейму. Звонки, группы, файлы.
# Запуск: uvicorn server:app --host 0.0.0.0 --port 8000

import asyncio, json, os, re, time, uuid
from pathlib import Path
from typing import Dict, List
from fastapi import FastAPI, Request, WebSocket, WebSocketDisconnect, UploadFile, File
from fastapi.responses import HTMLResponse, JSONResponse, FileResponse
from fastapi.middleware.cors import CORSMiddleware

app = FastAPI()
app.add_middleware(CORSMiddleware, allow_origins=["*"],
                   allow_methods=["*"], allow_headers=["*"])

UPLOAD_DIR = Path("uploads"); UPLOAD_DIR.mkdir(exist_ok=True)

# ==================== ХРАНИЛИЩЕ ====================
users: Dict[str, dict] = {}
by_phone: Dict[str, str] = {}
by_username: Dict[str, str] = {}
friends: Dict[str, set] = {}
groups: Dict[str, dict] = {}
messages: List[dict] = []
calls: Dict[str, dict] = {}

# ==================== WEBSOCKET МЕНЕДЖЕР ====================
class WSManager:
    def __init__(self): self.conns: Dict[str, WebSocket] = {}
    async def connect(self, uid, ws):
        await ws.accept(); self.conns[uid] = ws
    def disconnect(self, uid): self.conns.pop(uid, None)
    async def send(self, uid, data):
        ws = self.conns.get(uid)
        if ws:
            try: await ws.send_json(data)
            except Exception: self.conns.pop(uid, None)
    async def broadcast(self, data, exclude=None):
        for uid in list(self.conns.keys()):
            if uid != exclude: await self.send(uid, data)

ws_mgr = WSManager()

# ==================== ВАЛИДАЦИЯ ====================
def valid_phone(p: str) -> bool:
    return bool(re.match(r"^\+?\d{10,15}$", p.replace(" ", "").replace("-", "").replace("(", "").replace(")", "")))

def valid_username(u: str) -> bool:
    return bool(re.match(r"^[a-zA-Z0-9_]{3,20}$", u))

# ==================== АУТЕНТИФИКАЦИЯ ====================
@app.post("/auth/register")
async def register(req: Request):
    b = await req.json()
    phone = (b.get("phone") or "").strip()
    password = b.get("password", "")
    username = (b.get("username") or "").strip().lstrip("@")

    if not phone and not username:
        return JSONResponse({"error": "Укажи номер телефона или юзернейм"}, status_code=400)

    if phone:
        if not valid_phone(phone):
            return JSONResponse({"error": "Неверный формат номера"}, status_code=400)
        if phone in by_phone:
            return JSONResponse({"error": "Этот номер уже зарегистрирован"}, status_code=400)

    if username:
        if not valid_username(username):
            return JSONResponse({"error": "Юзернейм: 3-20 символов, только a-z 0-9 _"}, status_code=400)
        if username in by_username:
            return JSONResponse({"error": "Этот юзернейм занят"}, status_code=400)

    if not username:
        username = "user_" + str(uuid.uuid4())[:6]
        while username in by_username:
            username = "user_" + str(uuid.uuid4())[:6]

    if len(password) < 4:
        return JSONResponse({"error": "Пароль минимум 4 символа"}, status_code=400)

    uid = str(uuid.uuid4())
    users[uid] = {
        "phone": phone, "password": password, "username": username,
        "avatar": "", "online": True, "created": time.time(),
    }
    if phone: by_phone[phone] = uid
    by_username[username] = uid
    friends[uid] = set()

    return {"id": uid, "username": username, "phone": phone}

@app.post("/auth/login")
async def login(req: Request):
    b = await req.json()
    login_id = (b.get("login") or "").strip()
    password = b.get("password", "")

    if not login_id or not password:
        return JSONResponse({"error": "Заполни оба поля"}, status_code=400)

    uid = None
    clean = login_id.lstrip("@")
    # ищем как username
    for u, i in by_username.items():
        if u.lower() == clean.lower():
            uid = i
            break
    # ищем как телефон
    if not uid:
        ph = clean.replace(" ", "").replace("-", "").replace("(", "").replace(")", "")
        uid = by_phone.get(ph) or by_phone.get("+" + ph) or by_phone.get(ph[1:] if ph.startswith("+") else ph)

    if not uid or users[uid]["password"] != password:
        return JSONResponse({"error": "Неверный логин или пароль"}, status_code=401)

    users[uid]["online"] = True
    return {"id": uid, "username": users[uid]["username"], "phone": users[uid]["phone"]}

@app.get("/user/{uid}")
async def get_user(uid: str):
    u = users.get(uid)
    if not u: return JSONResponse({"error": "not found"}, status_code=404)
    return {"id": uid, "username": u["username"], "avatar": u["avatar"], "online": u.get("online", False)}

@app.post("/user/{uid}/username")
async def change_username(uid: str, req: Request):
    b = await req.json()
    new = (b.get("username") or "").strip().lstrip("@")
    if not valid_username(new):
        return JSONResponse({"error": "Некорректный юзернейм"}, status_code=400)
    if new.lower() in [u.lower() for u in by_username if by_username[u] != uid]:
        return JSONResponse({"error": "Занят"}, status_code=400)
    old = users[uid]["username"]
    by_username.pop(old, None)
    by_username[new] = uid
    users[uid]["username"] = new
    return {"ok": True, "username": new}

# ==================== ПОИСК ====================
@app.get("/search")
async def search(q: str = ""):
    q = q.strip().lstrip("@")
    if not q: return []
    results = []
    for uid, u in users.items():
        if q.lower() in u["username"].lower() or q in u["phone"]:
            p = u["phone"]
            masked = (p[:4] + "***" + p[-2:]) if len(p) > 6 else (p or "—")
            results.append({
                "id": uid, "username": u["username"],
                "phone": masked, "online": u.get("online", False),
            })
    return results[:20]

# ==================== ДРУЗЬЯ ====================
@app.get("/friends/{uid}")
async def get_friends(uid: str):
    out = []
    for fid in friends.get(uid, set()):
        u = users.get(fid)
        if u:
            out.append({"id": fid, "username": u["username"],
                        "avatar": u["avatar"], "online": u.get("online", False)})
    return out

@app.post("/friends/{uid}/add")
async def add_friend(uid: str, req: Request):
    b = await req.json()
    target = b.get("target")
    if not target or target not in users:
        return JSONResponse({"error": "Не найден"}, status_code=404)
    if target == uid:
        return JSONResponse({"error": "Это ты"}, status_code=400)
    friends.setdefault(uid, set()).add(target)
    friends.setdefault(target, set()).add(uid)
    await ws_mgr.send(target, {"type": "friend_added", "from": uid})
    return {"ok": True}

# ==================== ГРУППЫ ====================
@app.post("/groups/create")
async def create_group(req: Request):
    b = await req.json()
    owner = b.get("owner")
    name = (b.get("name") or "Группа").strip()[:50]
    members = b.get("members", [])
    if owner not in users:
        return JSONResponse({"error": "no owner"}, status_code=400)
    gid = str(uuid.uuid4())
    member_set = set(members) | {owner}
    groups[gid] = {"name": name, "owner": owner, "members": member_set,
                   "avatar": "", "created": time.time()}
    for m in member_set:
        await ws_mgr.send(m, {"type": "group_created", "gid": gid, "name": name})
    return {"gid": gid, "name": name}

@app.get("/groups/{uid}")
async def get_groups(uid: str):
    out = []
    for gid, g in groups.items():
        if uid in g["members"]:
            out.append({"id": gid, "name": g["name"], "avatar": g["avatar"],
                        "count": len(g["members"])})
    return out

# ==================== СООБЩЕНИЯ ====================
@app.get("/messages/{uid}")
async def get_messages(uid: str, peer: str = None, gid: str = None):
    out = []
    for m in messages:
        if gid and m.get("gid") == gid:
            out.append(m)
        elif not gid and ((m["from"] == uid and m["to"] == peer) or
                          (m["from"] == peer and m["to"] == uid)):
            out.append(m)
    return out[-200:]

# ==================== ФАЙЛЫ ====================
@app.post("/upload")
async def upload(file: UploadFile = File(...)):
    fid = str(uuid.uuid4())
    ext = Path(file.filename).suffix
    path = UPLOAD_DIR / f"{fid}{ext}"
    with path.open("wb") as f:
        f.write(await file.read())
    return {"fid": fid, "name": file.filename, "url": f"/files/{fid}{ext}",
            "size": path.stat().st_size}

@app.get("/files/{name}")
async def get_file(name: str):
    p = UPLOAD_DIR / name
    if p.exists(): return FileResponse(p)
    return JSONResponse({"error": "not found"}, status_code=404)

# ==================== WEBSOCKET ====================
@app.websocket("/ws/chat/{uid}")
async def ws_chat(ws: WebSocket, uid: str):
    if uid not in users:
        await ws.close(); return
    await ws_mgr.connect(uid, ws)
    users[uid]["online"] = True
    await ws_mgr.broadcast({"type": "presence", "uid": uid, "online": True}, exclude=uid)

    try:
        while True:
            data = await ws.receive_json()
            t = data.get("type", "msg")

            if t == "msg":
                m = {
                    "id": str(uuid.uuid4()), "from": uid,
                    "to": data.get("to"), "gid": data.get("gid"),
                    "text": data.get("text", ""),
                    "type": data.get("msg_type", "text"),
                    "file": data.get("file"),
                    "ts": time.time(),
                }
                messages.append(m)
                if m["gid"]:
                    g = groups.get(m["gid"])
                    if g:
                        for member in g["members"]:
                            if member != uid:
                                await ws_mgr.send(member, {"type": "msg", "msg": m})
                elif m["to"]:
                    await ws_mgr.send(m["to"], {"type": "msg", "msg": m})
                await ws_mgr.send(uid, {"type": "msg", "msg": m})

            elif t == "typing":
                target = data.get("to") or data.get("gid")
                if target:
                    await ws_mgr.send(target, {"type": "typing", "from": uid})

            elif t in ("call_offer", "call_answer", "call_ice", "call_end", "call_reject"):
                target = data.get("to")
                data["from"] = uid
                if t == "call_offer":
                    cid = data.get("call_id") or str(uuid.uuid4())
                    calls[cid] = {"from": uid, "to": target,
                                  "type": data.get("call_type", "audio"),
                                  "state": "ringing", "started": time.time()}
                    data["call_id"] = cid
                await ws_mgr.send(target, data)

    except WebSocketDisconnect:
        ws_mgr.disconnect(uid)
        users[uid]["online"] = False
        await ws_mgr.broadcast({"type": "presence", "uid": uid, "online": False}, exclude=uid)

# ==================== HTML ====================
@app.get("/", response_class=HTMLResponse)
async def index():
    p = Path(__file__).parent / "client.html"
    if p.exists():
        return p.read_text(encoding="utf-8")
    return "<h1>client.html не найден</h1>"

if __name__ == "__main__":
    import uvicorn
    print("[*] Nexus v3 на http://0.0.0.0:8000")
    uvicorn.run(app, host="0.0.0.0", port=8000, log_level="warning")