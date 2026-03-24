from collections import defaultdict
from datetime import datetime, timedelta
from pathlib import Path
import json
import uuid
import asyncio
import shutil

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
    Response
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

# Список email, которым разрешён backup
ALLOWED_BACKUP_EMAILS = ["maksim13leo@gmail.com", "televisor13leo@gmail.com"]


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


def format_last_seen(last_activity: datetime) -> str:
    """Форматирует время последней активности в читаемый вид"""
    if not last_activity:
        return "Не в сети"
    
    now = datetime.utcnow()
    diff = now - last_activity
    seconds = diff.total_seconds()
    
    if seconds < 60:
        return "был(а) только что"
    elif seconds < 3600:
        minutes = int(seconds // 60)
        return f"был(а) {minutes} мин назад"
    elif seconds < 86400:
        hours = int(seconds // 3600)
        return f"был(а) {hours} ч назад"
    elif seconds < 172800:
        return "был(а) вчера"
    else:
        days = int(seconds // 86400)
        return f"был(а) {days} дн назад"


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


def build_dialogs_for_user(current_user: User, db: Session, user_status_map: dict):
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
        
        # Получаем статус пользователя из map
        status_info = user_status_map.get(user.id, {})
        is_online = status_info.get("is_online", False)
        last_activity = status_info.get("last_activity")
        
        if is_online:
            status_text = "В сети"
        else:
            status_text = format_last_seen(last_activity) if last_activity else "Не в сети"
        
        dialogs.append({
            "user": user,
            "last_message": preview,
            "time": time_str,
            "last_message_id": last_message_id,
            "unread_count": unread_count,
            "is_online": is_online,
            "status_text": status_text
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
        self.user_status = {}  # {user_id: {"is_online": bool, "last_activity": datetime}}

    async def connect(self, user_id: int, websocket: WebSocket):
        await websocket.accept()
        self.active_connections[user_id].append(websocket)

        was_offline = not self.user_status.get(user_id, {}).get("is_online", False)
        
        self.user_status[user_id] = {
            "is_online": True,
            "last_activity": datetime.utcnow()
        }

        if was_offline:
            # Отправляем обновление статуса всем
            await self.broadcast_presence()

    async def disconnect(self, user_id: int, websocket: WebSocket):
        if user_id in self.active_connections:
            if websocket in self.active_connections[user_id]:
                self.active_connections[user_id].remove(websocket)

            if not self.active_connections[user_id]:
                del self.active_connections[user_id]
                if user_id in self.user_status:
                    # Пользователь вышел, обновляем статус
                    self.user_status[user_id]["is_online"] = False
                    self.user_status[user_id]["last_activity"] = datetime.utcnow()
                    # Отправляем обновление статуса всем
                    await self.broadcast_presence()

    async def force_logout(self, user_id: int):
        """Принудительно выйти из аккаунта и закрыть все сокеты"""
        if user_id in self.active_connections:
            # Закрываем все сокеты пользователя
            for ws in list(self.active_connections[user_id]):
                try:
                    await ws.close()
                except:
                    pass
            del self.active_connections[user_id]
        
        if user_id in self.user_status:
            self.user_status[user_id]["is_online"] = False
            self.user_status[user_id]["last_activity"] = datetime.utcnow()
            await self.broadcast_presence()

    async def update_activity(self, user_id: int):
        if user_id in self.user_status:
            old_status = self.user_status[user_id]["is_online"]
            self.user_status[user_id]["last_activity"] = datetime.utcnow()
            
            # Если пользователь был оффлайн, но совершил действие — включаем онлайн и уведомляем всех
            if not old_status:
                self.user_status[user_id]["is_online"] = True
                await self.broadcast_presence()

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
            if user_id in self.user_status:
                self.user_status[user_id]["is_online"] = False
                self.user_status[user_id]["last_activity"] = datetime.utcnow()
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
                    if uid in self.user_status:
                        self.user_status[uid]["is_online"] = False
                        self.user_status[uid]["last_activity"] = datetime.utcnow()
                        changed = True

        if changed:
            await self.broadcast_presence()

    async def broadcast_presence(self):
        """Отправляем всем пользователям актуальные статусы"""
        status_data = {}
        for uid, status in self.user_status.items():
            status_data[uid] = {
                "is_online": status["is_online"],
                "last_activity": status["last_activity"].isoformat() if status["last_activity"] else None
            }
        
        await self.broadcast_all({
            "type": "presence",
            "user_status": status_data
        })
    
    def get_user_status_map(self):
        """Возвращает словарь со статусами для использования в шаблоне"""
        result = {}
        for uid, status in self.user_status.items():
            result[uid] = {
                "is_online": status["is_online"],
                "last_activity": status["last_activity"]
            }
        return result


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
    remember_me: bool = Form(False),
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
    
    # Устанавливаем куку с user_id
    if remember_me:
        # Запомнить на 30 дней
        expires = datetime.utcnow() + timedelta(days=30)
        response.set_cookie(
            key="user_id",
            value=str(user.id),
            httponly=True,
            expires=expires
        )
    else:
        # Сессионная кука (закроется при закрытии браузера)
        response.set_cookie(
            key="user_id",
            value=str(user.id),
            httponly=True
        )
    
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

    # Получаем актуальные статусы пользователей
    user_status_map = manager.get_user_status_map()
    
    dialogs = build_dialogs_for_user(current_user, db, user_status_map)
    
    # Статус выбранного пользователя
    selected_user_status = "Не в сети"
    if selected_user:
        status_info = user_status_map.get(selected_user.id, {})
        if status_info.get("is_online"):
            selected_user_status = "В сети"
        else:
            last_activity = status_info.get("last_activity")
            selected_user_status = format_last_seen(last_activity) if last_activity else "Не в сети"
    
    can_backup = current_user.email in ALLOWED_BACKUP_EMAILS

    return templates.TemplateResponse(
        request=request,
        name="chat.html",
        context={
            "current_user": current_user,
            "dialogs": dialogs,
            "messages": messages,
            "selected_user": selected_user,
            "selected_user_status": selected_user_status,
            "format_message_time": format_message_time,
            "format_last_seen": format_last_seen,
            "vapid_public_key": VAPID_PUBLIC_KEY,
            "can_backup": can_backup,
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

    receiver_status = manager.user_status.get(receiver_id, {})
    if not receiver_status.get("is_online", False):
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
async def logout(user_id: str | None = Cookie(default=None)):
    # Принудительно завершаем сессию пользователя
    if user_id:
        try:
            await manager.force_logout(int(user_id))
        except:
            pass
    
    response = RedirectResponse(url="/", status_code=303)
    response.delete_cookie("user_id")
    return response


@app.websocket("/ws/{user_id}")
async def websocket_endpoint(websocket: WebSocket, user_id: int):
    await manager.connect(user_id, websocket)
    
    typing_timeout = {}
    
    try:
        # Отправляем текущие статусы новому пользователю
        status_data = {}
        for uid, status in manager.user_status.items():
            status_data[uid] = {
                "is_online": status["is_online"],
                "last_activity": status["last_activity"].isoformat() if status["last_activity"] else None
            }
        
        await manager.send_to_user(user_id, {
            "type": "presence",
            "user_status": status_data
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

                    receiver_status = manager.user_status.get(receiver_id, {})
                    if not receiver_status.get("is_online", False) and sender:
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


# ========== BACKUP ФУНКЦИИ (только для разрешённых email) ==========

def check_backup_permission(current_user: User):
    """Проверяет, имеет ли пользователь право на backup"""
    if current_user.email not in ALLOWED_BACKUP_EMAILS:
        raise HTTPException(status_code=403, detail="Доступ запрещён. Только для администраторов.")


@app.get("/download-backup")
async def download_backup(
    user_id: str | None = Cookie(default=None), 
    db: Session = Depends(get_db)
):
    """Скачать файл базы данных (только для разрешённых email)"""
    current_user = get_current_user(user_id, db)
    if not current_user:
        raise HTTPException(status_code=401, detail="Не авторизован")
    
    check_backup_permission(current_user)
    
    db_path = BASE_DIR / "messenger.db"
    
    if not db_path.exists():
        raise HTTPException(status_code=404, detail="Файл базы данных не найден")
    
    backup_name = f"messenger_backup_{datetime.now().strftime('%Y%m%d_%H%M%S')}.db"
    
    return FileResponse(
        path=db_path,
        filename=backup_name,
        media_type="application/octet-stream"
    )


@app.post("/restore-backup")
async def restore_backup(
    backup_file: UploadFile = File(...),
    user_id: str | None = Cookie(default=None),
    db: Session = Depends(get_db)
):
    """Восстановить базу данных из backup файла (только для разрешённых email)"""
    current_user = get_current_user(user_id, db)
    if not current_user:
        raise HTTPException(status_code=401, detail="Не авторизован")
    
    check_backup_permission(current_user)
    
    if not backup_file.filename.endswith('.db'):
        raise HTTPException(status_code=400, detail="Файл должен иметь расширение .db")
    
    db_path = BASE_DIR / "messenger.db"
    
    # Создаём резервную копию текущей БД на всякий случай
    if db_path.exists():
        backup_path = BASE_DIR / f"messenger_backup_auto_{datetime.now().strftime('%Y%m%d_%H%M%S')}.db"
        shutil.copy(db_path, backup_path)
    
    try:
        content = await backup_file.read()
        with open(db_path, "wb") as f:
            f.write(content)
        
        return JSONResponse({
            "status": "success",
            "message": "База данных восстановлена. Обновите страницу."
        })
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Ошибка при восстановлении: {str(e)}")
