from collections import defaultdict
from datetime import datetime
from pathlib import Path
import json
import uuid
import asyncio

from fastapi import (
    FastAPI,
    Request,
    Form,
    Depends,
    Cookie,
    WebSocket,
    WebSocketDisconnect,
    UploadFile,
    File,
    HTTPException,
    Body,
)
from fastapi.responses import HTMLResponse, RedirectResponse, FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from sqlalchemy.orm import Session
from sqlalchemy import or_, and_
from pywebpush import webpush, WebPushException

from database import Base, engine, get_db, SessionLocal
from auth import create_user, authenticate_user
from models import User, Message, PushSubscription

app = FastAPI()

Base.metadata.create_all(bind=engine)

BASE_DIR = Path(__file__).resolve().parent
STATIC_DIR = BASE_DIR / "static"
UPLOADS_DIR = BASE_DIR / "uploads"
UPLOADS_DIR.mkdir(exist_ok=True)

app.mount("/static", StaticFiles(directory="static"), name="static")
app.mount("/uploads", StaticFiles(directory="uploads"), name="uploads")
templates = Jinja2Templates(directory="templates")

VAPID_PUBLIC_KEY = "BNwWM6Ck0sZ828Jq5mb56yFN4bS8mzj3eCA3sA1bxUYl7ysSZdCRSWZvrT7V9p7m9DBBX2WwHOkBemAJfo0Ya8M"
VAPID_PRIVATE_KEY = "UN14rpjZaky3nH_LOKLRJg2H5wEHEJYBGULYvLGkhSw"
VAPID_CLAIMS = {
    "sub": "mailto:test@example.com"
}


@app.get("/manifest.webmanifest", include_in_schema=False)
async def manifest():
    return FileResponse(
        STATIC_DIR / "manifest.webmanifest",
        media_type="application/manifest+json",
    )


@app.get("/service-worker.js", include_in_schema=False)
async def service_worker():
    return FileResponse(
        STATIC_DIR / "service-worker.js",
        media_type="application/javascript",
        headers={"Service-Worker-Allowed": "/"},
    )


@app.get("/favicon.ico", include_in_schema=False)
async def favicon():
    return FileResponse(STATIC_DIR / "icon.svg", media_type="image/svg+xml")


@app.get("/apple-touch-icon", include_in_schema=False)
async def apple_touch_icon():
    return FileResponse(STATIC_DIR / "icon.svg", media_type="image/svg+xml")


@app.get("/push-public-key", include_in_schema=False)
async def push_public_key():
    return {"publicKey": VAPID_PUBLIC_KEY}


def format_message_time(dt):
    if not dt:
        return ""
    return dt.strftime("%H:%M")


def get_current_user(user_id: str | None, db: Session):
    if not user_id:
        return None
    return db.query(User).filter(User.id == int(user_id)).first()


def get_unread_count(current_user_id: int, other_user_id: int, db: Session) -> int:
    return (
        db.query(Message)
        .filter(
            Message.sender_id == other_user_id,
            Message.receiver_id == current_user_id,
            Message.is_read == False
        )
        .count()
    )


def build_message_preview(message: Message, current_user_id: int) -> str:
    if not message:
        return "Начать диалог"

    if message.message_type == "photo":
        base = "📷 Фото"
    else:
        base = (message.content or "")[:40]

    if message.sender_id == current_user_id:
        return f"Ты: {base}"
    return base


def build_dialogs_for_user(current_user: User, db: Session):
    all_users = db.query(User).filter(User.id != current_user.id).all()
    dialogs = []

    for user in all_users:
        last_message = (
            db.query(Message)
            .filter(
                or_(
                    and_(Message.sender_id == current_user.id, Message.receiver_id == user.id),
                    and_(Message.sender_id == user.id, Message.receiver_id == current_user.id),
                )
            )
            .order_by(Message.id.desc())
            .first()
        )

        preview = "Начать диалог"
        time_str = ""
        last_message_id = 0

        if last_message:
            preview = build_message_preview(last_message, current_user.id)
            time_str = format_message_time(last_message.created_at)
            last_message_id = last_message.id

        unread_count = get_unread_count(current_user.id, user.id, db)

        dialogs.append({
            "user": user,
            "last_message": preview,
            "time": time_str,
            "last_message_id": last_message_id,
            "unread_count": unread_count
        })

    dialogs.sort(key=lambda x: x["last_message_id"], reverse=True)
    return dialogs


def save_push_subscription(db: Session, user_id: int, subscription: dict):
    endpoint = subscription.get("endpoint")
    keys = subscription.get("keys", {})
    p256dh = keys.get("p256dh")
    auth = keys.get("auth")

    if not endpoint or not p256dh or not auth:
        raise HTTPException(status_code=400, detail="Некорректная push-подписка")

    existing = db.query(PushSubscription).filter(PushSubscription.endpoint == endpoint).first()

    if existing:
        existing.user_id = user_id
        existing.p256dh = p256dh
        existing.auth = auth
    else:
        new_sub = PushSubscription(
            user_id=user_id,
            endpoint=endpoint,
            p256dh=p256dh,
            auth=auth
        )
        db.add(new_sub)

    db.commit()


def remove_push_subscription(db: Session, endpoint: str):
    sub = db.query(PushSubscription).filter(PushSubscription.endpoint == endpoint).first()
    if sub:
        db.delete(sub)
        db.commit()


def send_push_to_user(db: Session, user_id: int, title: str, body: str, url: str = "/chat"):
    subscriptions = db.query(PushSubscription).filter(PushSubscription.user_id == user_id).all()

    for sub in subscriptions:
        try:
            webpush(
                subscription_info={
                    "endpoint": sub.endpoint,
                    "keys": {
                        "p256dh": sub.p256dh,
                        "auth": sub.auth,
                    },
                },
                data=json.dumps({
                    "title": title,
                    "body": body,
                    "url": url,
                }),
                vapid_private_key=VAPID_PRIVATE_KEY,
                vapid_claims=VAPID_CLAIMS,
            )
        except WebPushException as ex:
            status_code = None
            if ex.response is not None:
                status_code = ex.response.status_code

            if status_code in (404, 410):
                db.delete(sub)
                db.commit()
            else:
                print("Push error:", repr(ex))


class ConnectionManager:
    def __init__(self):
        self.active_connections = defaultdict(list)
        self.online_users = set()
        self.last_activity = {}

    async def connect(self, user_id: int, websocket: WebSocket):
        await websocket.accept()
        self.active_connections[user_id].append(websocket)

        was_offline = user_id not in self.online_users
        self.online_users.add(user_id)
        self.last_activity[user_id] = datetime.utcnow()

        if was_offline:
            await self.broadcast_presence()

    async def disconnect(self, user_id: int, websocket: WebSocket):
        if user_id in self.active_connections:
            if websocket in self.active_connections[user_id]:
                self.active_connections[user_id].remove(websocket)

            if not self.active_connections[user_id]:
                del self.active_connections[user_id]
                if user_id in self.online_users:
                    self.online_users.discard(user_id)
                    self.last_activity[user_id] = datetime.utcnow()
                    await self.broadcast_presence()

    async def update_activity(self, user_id: int):
        if user_id in self.last_activity:
            self.last_activity[user_id] = datetime.utcnow()

    async def send_to_user(self, user_id: int, data: dict):
        if user_id not in self.active_connections:
            return

        dead = []
        for ws in list(self.active_connections[user_id]):
            try:
                await ws.send_json(data)
            except Exception:
                dead.append(ws)

        changed = False
        for ws in dead:
            if user_id in self.active_connections and ws in self.active_connections[user_id]:
                self.active_connections[user_id].remove(ws)
                changed = True

        if changed and user_id in self.active_connections and not self.active_connections[user_id]:
            del self.active_connections[user_id]
            if user_id in self.online_users:
                self.online_users.discard(user_id)
                await self.broadcast_presence()

    async def broadcast_all(self, data: dict):
        dead_connections = []

        for uid, sockets in list(self.active_connections.items()):
            for ws in list(sockets):
                try:
                    await ws.send_json(data)
                except Exception:
                    dead_connections.append((uid, ws))

        changed = False
        for uid, ws in dead_connections:
            if uid in self.active_connections and ws in self.active_connections[uid]:
                self.active_connections[uid].remove(ws)
                if not self.active_connections[uid]:
                    del self.active_connections[uid]
                    if uid in self.online_users:
                        self.online_users.discard(uid)
                        changed = True

        if changed:
            await self.broadcast_presence()

    async def broadcast_presence(self):
        await self.broadcast_all({
            "type": "presence",
            "online_users": list(self.online_users),
            "last_activity": {uid: ts.isoformat() for uid, ts in self.last_activity.items()}
        })


manager = ConnectionManager()


@app.get("/", response_class=HTMLResponse)
async def home(request: Request):
    return templates.TemplateResponse(
        request=request,
        name="login.html",
        context={"error": None}
    )


@app.get("/register", response_class=HTMLResponse)
async def register_page(request: Request):
    return templates.TemplateResponse(
        request=request,
        name="register.html",
        context={"error": None, "success": None}
    )


@app.post("/register", response_class=HTMLResponse)
async def register_user(
    request: Request,
    email: str = Form(...),
    password: str = Form(...),
    db: Session = Depends(get_db)
):
    user = create_user(db, email, password)

    if user is None:
        return templates.TemplateResponse(
            request=request,
            name="register.html",
            context={
                "error": "Пользователь с таким email уже существует",
                "success": None
            }
        )

    return templates.TemplateResponse(
        request=request,
        name="register.html",
        context={
            "error": None,
            "success": "Регистрация прошла успешно. Теперь войди."
        }
    )


@app.post("/login", response_class=HTMLResponse)
async def login_user(
    request: Request,
    email: str = Form(...),
    password: str = Form(...),
    db: Session = Depends(get_db)
):
    user = authenticate_user(db, email, password)

    if user is None:
        return templates.TemplateResponse(
            request=request,
            name="login.html",
            context={"error": "Неверный email или пароль"}
        )

    response = RedirectResponse(url="/chat", status_code=303)
    response.set_cookie(key="user_id", value=str(user.id), httponly=True)
    return response


@app.get("/chat", response_class=HTMLResponse)
async def chat_page(
    request: Request,
    db: Session = Depends(get_db),
    user_id: str | None = Cookie(default=None),
    selected_user_id: int | None = None
):
    current_user = get_current_user(user_id, db)
    if not current_user:
        return RedirectResponse(url="/", status_code=303)

    messages = []
    selected_user = None

    if selected_user_id:
        selected_user = db.query(User).filter(User.id == selected_user_id).first()

        if selected_user:
            unread_messages = (
                db.query(Message)
                .filter(
                    Message.sender_id == selected_user_id,
                    Message.receiver_id == current_user.id,
                    Message.is_read == False
                )
                .all()
            )

            for msg in unread_messages:
                msg.is_read = True

            if unread_messages:
                db.commit()

            messages = (
                db.query(Message)
                .filter(
                    or_(
                        and_(Message.sender_id == current_user.id, Message.receiver_id == selected_user_id),
                        and_(Message.sender_id == selected_user_id, Message.receiver_id == current_user.id),
                    )
                )
                .order_by(Message.id.asc())
                .all()
            )

    dialogs = build_dialogs_for_user(current_user, db)
    
    last_activity = {}
    for dialog in dialogs:
        user_id_dialog = dialog["user"].id
        if user_id_dialog in manager.last_activity:
            last_activity[user_id_dialog] = manager.last_activity[user_id_dialog]

    return templates.TemplateResponse(
        request=request,
        name="chat.html",
        context={
            "current_user": current_user,
            "dialogs": dialogs,
            "messages": messages,
            "selected_user": selected_user,
            "online_users": manager.online_users,
            "last_activity": last_activity,
            "format_message_time": format_message_time,
            "vapid_public_key": VAPID_PUBLIC_KEY,
        }
    )


@app.post("/subscribe")
async def subscribe(
    subscription: dict = Body(...),
    db: Session = Depends(get_db),
    user_id: str | None = Cookie(default=None)
):
    current_user = get_current_user(user_id, db)
    if not current_user:
        raise HTTPException(status_code=401, detail="Не авторизован")

    save_push_subscription(db, current_user.id, subscription)
    return JSONResponse({"status": "ok"})


@app.post("/unsubscribe")
async def unsubscribe(
    data: dict = Body(...),
    db: Session = Depends(get_db),
    user_id: str | None = Cookie(default=None)
):
    current_user = get_current_user(user_id, db)
    if not current_user:
        raise HTTPException(status_code=401, detail="Не авторизован")

    endpoint = data.get("endpoint")
    if endpoint:
        remove_push_subscription(db, endpoint)

    return JSONResponse({"status": "ok"})


@app.post("/send-photo")
async def send_photo(
    receiver_id: int = Form(...),
    photo: UploadFile = File(...),
    db: Session = Depends(get_db),
    user_id: str | None = Cookie(default=None)
):
    current_user = get_current_user(user_id, db)
    if not current_user:
        return RedirectResponse(url="/", status_code=303)

    if not photo.content_type or not photo.content_type.startswith("image/"):
        return RedirectResponse(url=f"/chat?selected_user_id={receiver_id}", status_code=303)

    extension = Path(photo.filename or "image.jpg").suffix or ".jpg"
    safe_name = f"{uuid.uuid4().hex}{extension}"
    save_path = UPLOADS_DIR / safe_name

    with save_path.open("wb") as buffer:
        content = await photo.read()
        buffer.write(content)

    new_message = Message(
        sender_id=current_user.id,
        receiver_id=receiver_id,
        message_type="photo",
        content="",
        file_name=photo.filename or "image",
        file_path=safe_name,
        created_at=datetime.utcnow(),
        is_read=False
    )
    db.add(new_message)
    db.commit()
    db.refresh(new_message)

    unread_count_for_receiver = get_unread_count(receiver_id, current_user.id, db)

    file_url = f"/uploads/{safe_name}"
    download_url = f"/download-file/{new_message.id}"

    outgoing_to_sender = {
        "type": "message",
        "message_type": "photo",
        "sender_id": current_user.id,
        "receiver_id": receiver_id,
        "content": "",
        "time": format_message_time(new_message.created_at),
        "unread_count": 0,
        "file_url": file_url,
        "download_url": download_url,
        "file_name": new_message.file_name or "image",
        "sender_email": current_user.email
    }

    outgoing_to_receiver = {
        "type": "message",
        "message_type": "photo",
        "sender_id": current_user.id,
        "receiver_id": receiver_id,
        "content": "",
        "time": format_message_time(new_message.created_at),
        "unread_count": unread_count_for_receiver,
        "file_url": file_url,
        "download_url": download_url,
        "file_name": new_message.file_name or "image",
        "sender_email": current_user.email
    }

    await manager.send_to_user(current_user.id, outgoing_to_sender)
    await manager.send_to_user(receiver_id, outgoing_to_receiver)

    if receiver_id not in manager.online_users:
        send_push_to_user(
            db=db,
            user_id=receiver_id,
            title=f"Новое фото от {current_user.email}",
            body="📷 Фото",
            url=f"/chat?selected_user_id={current_user.id}",
        )

    return RedirectResponse(url=f"/chat?selected_user_id={receiver_id}", status_code=303)


@app.get("/download-file/{message_id}")
async def download_file(
    message_id: int,
    db: Session = Depends(get_db),
    user_id: str | None = Cookie(default=None)
):
    current_user = get_current_user(user_id, db)
    if not current_user:
        raise HTTPException(status_code=401, detail="Не авторизован")

    message = db.query(Message).filter(Message.id == message_id).first()
    if not message:
        raise HTTPException(status_code=404, detail="Файл не найден")

    if current_user.id not in [message.sender_id, message.receiver_id]:
        raise HTTPException(status_code=403, detail="Нет доступа")

    if not message.file_path:
        raise HTTPException(status_code=404, detail="Файл не найден")

    file_path = UPLOADS_DIR / message.file_path
    if not file_path.exists():
        raise HTTPException(status_code=404, detail="Файл не найден")

    return FileResponse(
        file_path,
        filename=message.file_name or file_path.name,
        media_type="application/octet-stream"
    )


@app.get("/logout")
async def logout():
    response = RedirectResponse(url="/", status_code=303)
    response.delete_cookie("user_id")
    return response


@app.websocket("/ws/{user_id}")
async def websocket_endpoint(websocket: WebSocket, user_id: int):
    await manager.connect(user_id, websocket)
    
    typing_timeout = {}
    
    try:
        await manager.send_to_user(user_id, {
            "type": "presence",
            "online_users": list(manager.online_users),
            "last_activity": {uid: ts.isoformat() for uid, ts in manager.last_activity.items()}
        })

        while True:
            data = await websocket.receive_json()
            data_type = data.get("type")
            
            await manager.update_activity(user_id)

            if data_type == "read_chat":
                other_user_id = int(data["chat_user_id"])

                db = SessionLocal()
                try:
                    unread_messages = (
                        db.query(Message)
                        .filter(
                            Message.sender_id == other_user_id,
                            Message.receiver_id == user_id,
                            Message.is_read == False
                        )
                        .all()
                    )

                    for msg in unread_messages:
                        msg.is_read = True

                    if unread_messages:
                        db.commit()
                finally:
                    db.close()

                await manager.send_to_user(user_id, {
                    "type": "read_update",
                    "chat_user_id": other_user_id
                })

            elif data_type == "typing":
                receiver_id = int(data.get("receiver_id"))
                is_typing = data.get("is_typing", False)
                
                await manager.send_to_user(receiver_id, {
                    "type": "typing",
                    "sender_id": user_id,
                    "is_typing": is_typing
                })
                
                if is_typing:
                    if user_id in typing_timeout:
                        try:
                            typing_timeout[user_id].cancel()
                        except:
                            pass
                    
                    async def clear_typing():
                        await asyncio.sleep(3)
                        await manager.send_to_user(receiver_id, {
                            "type": "typing",
                            "sender_id": user_id,
                            "is_typing": False
                        })
                        if user_id in typing_timeout:
                            del typing_timeout[user_id]
                    
                    task = asyncio.create_task(clear_typing())
                    typing_timeout[user_id] = task

            elif data_type == "message":
                receiver_id = int(data["receiver_id"])
                content = data["content"].strip()

                if not content:
                    continue

                db = SessionLocal()
                sender_email = ""
                try:
                    sender = db.query(User).filter(User.id == user_id).first()
                    sender_email = sender.email if sender else ""

                    new_message = Message(
                        sender_id=user_id,
                        receiver_id=receiver_id,
                        message_type="text",
                        content=content,
                        created_at=datetime.utcnow(),
                        is_read=False
                    )
                    db.add(new_message)
                    db.commit()
                    db.refresh(new_message)

                    unread_count_for_receiver = get_unread_count(receiver_id, user_id, db)

                    if receiver_id not in manager.online_users and sender:
                        send_push_to_user(
                            db=db,
                            user_id=receiver_id,
                            title=f"Новое сообщение от {sender.email}",
                            body=content[:120],
                            url=f"/chat?selected_user_id={user_id}",
                        )
                finally:
                    db.close()

                outgoing_to_sender = {
                    "type": "message",
                    "message_type": "text",
                    "sender_id": user_id,
                    "receiver_id": receiver_id,
                    "content": content,
                    "time": format_message_time(new_message.created_at),
                    "unread_count": 0,
                    "sender_email": sender_email
                }

                outgoing_to_receiver = {
                    "type": "message",
                    "message_type": "text",
                    "sender_id": user_id,
                    "receiver_id": receiver_id,
                    "content": content,
                    "time": format_message_time(new_message.created_at),
                    "unread_count": unread_count_for_receiver,
                    "sender_email": sender_email
                }

                await manager.send_to_user(user_id, outgoing_to_sender)
                await manager.send_to_user(receiver_id, outgoing_to_receiver)

    except WebSocketDisconnect:
        if user_id in typing_timeout:
            try:
                typing_timeout[user_id].cancel()
            except:
                pass
            del typing_timeout[user_id]
        await manager.disconnect(user_id, websocket)
    except Exception as e:
        print(f"WebSocket error: {e}")
        if user_id in typing_timeout:
            try:
                typing_timeout[user_id].cancel()
            except:
                pass
            del typing_timeout[user_id]
        await manager.disconnect(user_id, websocket)
