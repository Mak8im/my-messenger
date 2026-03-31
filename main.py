from collections import defaultdict
from datetime import datetime, timedelta, timezone
from pathlib import Path
import json
import uuid
import asyncio
import shutil
import re
import zipfile
import tempfile
import os
from typing import Optional
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
AVATARS_DIR = BASE_DIR / "avatars"

UPLOADS_DIR.mkdir(exist_ok=True)
AVATARS_DIR.mkdir(exist_ok=True)

app.mount("/static", StaticFiles(directory="static"), name="static")
app.mount("/uploads", StaticFiles(directory="uploads"), name="uploads")
app.mount("/avatars", StaticFiles(directory="avatars"), name="avatars")

templates = Jinja2Templates(directory="templates")

VAPID_PUBLIC_KEY = "BNwWM6Ck0sZ828Jq5mb56yFN4bS8mzj3eCA3sA1bxUYl7ysSZdCRSWZvrT7V9p7m9DBBX2WwHOkBemAJfo0Ya8M"
VAPID_PRIVATE_KEY = "UN14rpjZaky3nH_LOKLRJg2H5wEHEJYBGULYvLGkhSw"
VAPID_CLAIMS = {
    "sub": "mailto:test@example.com"
}

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
    """Форматирует время с прибавлением +3 часа"""
    if not dt:
        return ""
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    local_dt = dt + timedelta(hours=3)
    return local_dt.strftime("%H:%M")


def format_last_seen(last_activity):
    if not last_activity:
        return "Не в сети"

    if isinstance(last_activity, str):
        try:
            last_activity = datetime.fromisoformat(last_activity.replace('Z', '+00:00'))
        except:
            return "Не в сети"

    if last_activity.tzinfo is None:
        last_activity = last_activity.replace(tzinfo=timezone.utc)

    now = datetime.now(timezone.utc)
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
    try:
        return db.query(User).filter(User.id == int(user_id)).first()
    except:
        return None


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
    elif message.message_type == "voice":
        base = "🎤 Голосовое"
    else:
        base = (message.content or "")[:40]

    if message.sender_id == current_user_id:
        return f"Ты: {base}"
    return base


def build_dialogs_for_user(current_user: User, db: Session, online_users: set):
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
        
        is_online = user.id in online_users
        
        if is_online:
            status_text = "В сети"
        else:
            status_text = format_last_seen(user.last_activity)
        
        display_name = user.full_name if user.full_name else user.email
        if user.username:
            display_name = f"{display_name} ({user.username})"
        
        dialogs.append({
            "user": user,
            "last_message": preview,
            "time": time_str,
            "last_message_id": last_message_id,
            "unread_count": unread_count,
            "is_online": is_online,
            "status_text": status_text,
            "display_name": display_name,
            "avatar_url": f"/avatars/{user.avatar}" if user.avatar else None
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

    async def connect(self, user_id: int, websocket: WebSocket, db: Session):
        await websocket.accept()
        self.active_connections[user_id].append(websocket)
        
        user = db.query(User).filter(User.id == user_id).first()
        if user:
            user.last_activity = datetime.now(timezone.utc)
            db.commit()
        
        was_offline = user_id not in self.online_users
        self.online_users.add(user_id)
        
        if was_offline:
            await self.broadcast_presence(db)

    async def disconnect(self, user_id: int, websocket: WebSocket, db: Session):
        if user_id in self.active_connections:
            if websocket in self.active_connections[user_id]:
                self.active_connections[user_id].remove(websocket)

            if not self.active_connections[user_id]:
                del self.active_connections[user_id]
                if user_id in self.online_users:
                    self.online_users.remove(user_id)
                    user = db.query(User).filter(User.id == user_id).first()
                    if user:
                        user.last_activity = datetime.now(timezone.utc)
                        db.commit()
                    await self.broadcast_presence(db)

    async def update_activity(self, user_id: int, db: Session):
        user = db.query(User).filter(User.id == user_id).first()
        if user:
            user.last_activity = datetime.now(timezone.utc)
            db.commit()
        
        if user_id not in self.online_users:
            self.online_users.add(user_id)
            await self.broadcast_presence(db)

    async def send_to_user(self, user_id: int, data: dict):
        if user_id not in self.active_connections:
            return

        dead = []
        for ws in list(self.active_connections[user_id]):
            try:
                await ws.send_json(data)
            except Exception:
                dead.append(ws)

        for ws in dead:
            if user_id in self.active_connections and ws in self.active_connections[user_id]:
                self.active_connections[user_id].remove(ws)

        if user_id in self.active_connections and not self.active_connections[user_id]:
            del self.active_connections[user_id]

    async def broadcast_all(self, data: dict):
        dead_connections = []

        for uid, sockets in list(self.active_connections.items()):
            for ws in list(sockets):
                try:
                    await ws.send_json(data)
                except Exception:
                    dead_connections.append((uid, ws))

        for uid, ws in dead_connections:
            if uid in self.active_connections and ws in self.active_connections[uid]:
                self.active_connections[uid].remove(ws)
                if not self.active_connections[uid]:
                    del self.active_connections[uid]

    async def broadcast_presence(self, db: Session):
        all_users = db.query(User).all()
        status_data = {}
        for user in all_users:
            if user.last_activity:
                last_activity_str = user.last_activity.isoformat()
                if not last_activity_str.endswith('Z') and '+' not in last_activity_str:
                    last_activity_str += 'Z'
            else:
                last_activity_str = None
                
            status_data[user.id] = {
                "is_online": user.id in self.online_users,
                "last_activity": last_activity_str
            }
        
        await self.broadcast_all({
            "type": "presence",
            "user_status": status_data
        })
    
    async def force_logout_user(self, user_id: int, db: Session):
        if user_id in self.online_users:
            self.online_users.remove(user_id)
        
        user = db.query(User).filter(User.id == user_id).first()
        if user:
            user.last_activity = datetime.now(timezone.utc)
            db.commit()
        
        await self.broadcast_presence(db)
        
        if user_id in self.active_connections:
            for ws in list(self.active_connections[user_id]):
                try:
                    await ws.close()
                except:
                    pass
            del self.active_connections[user_id]


manager = ConnectionManager()


@app.get("/", response_class=HTMLResponse)
async def home(
    request: Request,
    user_id: str | None = Cookie(default=None),
    db: Session = Depends(get_db)
):
    if user_id:
        user = get_current_user(user_id, db)
        if user:
            return RedirectResponse(url="/chat", status_code=303)
    
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
    identifier: str = Form(...),
    password: str = Form(...),
    remember_me: bool = Form(False),
    db: Session = Depends(get_db)
):
    user = authenticate_user(db, identifier, password)

    if user is None:
        return templates.TemplateResponse(
            request=request,
            name="login.html",
            context={"error": "Неверный email или имя пользователя"}
        )

    user.last_activity = datetime.now(timezone.utc)
    db.commit()

    response = RedirectResponse(url="/chat", status_code=303)
    
    if remember_me:
        max_age = 30 * 24 * 60 * 60
    else:
        max_age = 7 * 24 * 60 * 60
    
    expires = datetime.now(timezone.utc) + timedelta(seconds=max_age)
    
    is_secure = request.url.scheme == "https"
    
    response.set_cookie(
        key="user_id",
        value=str(user.id),
        httponly=True,
        max_age=max_age,
        expires=expires,
        secure=is_secure,
        samesite="lax",
        path="/"
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

    db.refresh(current_user)
    
    current_user.last_activity = datetime.now(timezone.utc)
    db.commit()

    messages = []
    selected_user = None

    if selected_user_id:
        selected_user = db.query(User).filter(User.id == selected_user_id).first()
        if selected_user:
            db.refresh(selected_user)

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
                
                for msg in unread_messages:
                    if msg.sender_id in manager.online_users:
                        await manager.send_to_user(msg.sender_id, {
                            "type": "read",
                            "message_id": msg.id,
                            "is_read": True
                        })

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

    dialogs = build_dialogs_for_user(current_user, db, manager.online_users)
    
    selected_user_status = "Не в сети"
    if selected_user:
        if selected_user.id in manager.online_users:
            selected_user_status = "В сети"
        else:
            selected_user_status = format_last_seen(selected_user.last_activity)
    
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


# ========== ПРОФИЛЬ API ==========

@app.get("/api/profile")
async def get_profile(
    user_id: str | None = Cookie(default=None),
    db: Session = Depends(get_db)
):
    current_user = get_current_user(user_id, db)
    if not current_user:
        raise HTTPException(status_code=401, detail="Не авторизован")
    
    return {
        "id": current_user.id,
        "email": current_user.email,
        "full_name": current_user.full_name or "",
        "username": current_user.username or "",
        "bio": current_user.bio or "",
        "avatar": f"/avatars/{current_user.avatar}" if current_user.avatar else None
    }


@app.put("/api/profile")
async def update_profile(
    full_name: Optional[str] = Form(None),
    username: Optional[str] = Form(None),
    bio: Optional[str] = Form(None),
    user_id: str | None = Cookie(default=None),
    db: Session = Depends(get_db)
):
    current_user = get_current_user(user_id, db)
    if not current_user:
        raise HTTPException(status_code=401, detail="Не авторизован")
    
    if full_name is not None:
        current_user.full_name = full_name.strip()
    
    if bio is not None:
        current_user.bio = bio.strip()
    
    if username is not None:
        username = username.strip()
        if username and not username.startswith("@"):
            username = "@" + username
        
        if username:
            clean_username = username[1:]
            if not re.match(r'^[a-zA-Z0-9_]+$', clean_username):
                raise HTTPException(status_code=400, detail="Имя пользователя может содержать только буквы, цифры и подчеркивание")
            
            existing = db.query(User).filter(User.username == username, User.id != current_user.id).first()
            if existing:
                raise HTTPException(status_code=400, detail="Имя пользователя уже занято")
        
        current_user.username = username if username else None
    
    db.commit()
    
    return {
        "id": current_user.id,
        "email": current_user.email,
        "full_name": current_user.full_name or "",
        "username": current_user.username or "",
        "bio": current_user.bio or "",
        "avatar": f"/avatars/{current_user.avatar}" if current_user.avatar else None
    }


@app.post("/api/avatar")
async def upload_avatar(
    avatar: UploadFile = File(...),
    user_id: str | None = Cookie(default=None),
    db: Session = Depends(get_db)
):
    current_user = get_current_user(user_id, db)
    if not current_user:
        raise HTTPException(status_code=401, detail="Не авторизован")
    
    if not avatar.content_type or not avatar.content_type.startswith("image/"):
        raise HTTPException(status_code=400, detail="Файл должен быть изображением")
    
    if current_user.avatar:
        old_path = AVATARS_DIR / current_user.avatar
        if old_path.exists():
            old_path.unlink()
    
    extension = Path(avatar.filename or "avatar.jpg").suffix
    safe_name = f"avatar_{current_user.id}_{uuid.uuid4().hex}{extension}"
    save_path = AVATARS_DIR / safe_name
    
    with save_path.open("wb") as buffer:
        content = await avatar.read()
        buffer.write(content)
    
    current_user.avatar = safe_name
    db.commit()
    
    return {"avatar_url": f"/avatars/{safe_name}"}


@app.delete("/api/avatar")
async def delete_avatar(
    user_id: str | None = Cookie(default=None),
    db: Session = Depends(get_db)
):
    current_user = get_current_user(user_id, db)
    if not current_user:
        raise HTTPException(status_code=401, detail="Не авторизован")
    
    if current_user.avatar:
        avatar_path = AVATARS_DIR / current_user.avatar
        if avatar_path.exists():
            avatar_path.unlink()
        current_user.avatar = None
        db.commit()
    
    return {"status": "ok"}


@app.get("/api/user/{user_id_or_username}")
async def get_user_info(
    user_id_or_username: str,
    db: Session = Depends(get_db)
):
    user = None
    if user_id_or_username.isdigit():
        user = db.query(User).filter(User.id == int(user_id_or_username)).first()
    
    if not user:
        username = user_id_or_username
        if not username.startswith("@"):
            username = "@" + username
        user = db.query(User).filter(User.username == username).first()
    
    if not user:
        raise HTTPException(status_code=404, detail="Пользователь не найден")
    
    return {
        "id": user.id,
        "email": user.email,
        "full_name": user.full_name or "",
        "username": user.username or "",
        "bio": user.bio or "",
        "avatar": f"/avatars/{user.avatar}" if user.avatar else None
    }


# ========== НАСТРОЙКИ УВЕДОМЛЕНИЙ ==========

@app.get("/api/notification-sound")
async def get_notification_sound(
    user_id: str | None = Cookie(default=None),
    db: Session = Depends(get_db)
):
    current_user = get_current_user(user_id, db)
    if not current_user:
        raise HTTPException(status_code=401, detail="Не авторизован")
    
    sound = current_user.notification_sound or "default"
    return {"sound": sound}


@app.put("/api/notification-sound")
async def set_notification_sound(
    sound: str = Body(..., embed=True),
    user_id: str | None = Cookie(default=None),
    db: Session = Depends(get_db)
):
    current_user = get_current_user(user_id, db)
    if not current_user:
        raise HTTPException(status_code=401, detail="Не авторизован")
    
    allowed_sounds = ["default", "nicesound", "tg", "vk", "icq", "facebook", "samsung", "apple"]
    if sound not in allowed_sounds:
        raise HTTPException(status_code=400, detail="Недопустимый звук")
    
    current_user.notification_sound = sound
    db.commit()
    
    return {"sound": sound}


# ========== ЗВЁЗДЫ (КЛИКЕР) ==========

@app.get("/api/stars")
async def get_stars(
    user_id: str | None = Cookie(default=None),
    db: Session = Depends(get_db)
):
    current_user = get_current_user(user_id, db)
    if not current_user:
        raise HTTPException(status_code=401, detail="Не авторизован")
    
    return {"stars": current_user.stars or 0.0}


@app.post("/api/stars/click")
async def click_star(
    user_id: str | None = Cookie(default=None),
    db: Session = Depends(get_db)
):
    current_user = get_current_user(user_id, db)
    if not current_user:
        raise HTTPException(status_code=401, detail="Не авторизован")
    
    current_user.stars = (current_user.stars or 0.0) + 0.001
    db.commit()
    
    return {"stars": current_user.stars}


# ========== ДРУГИЕ РОУТЫ ==========

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
        created_at=datetime.now(timezone.utc),
        is_read=False,
        is_delivered=False
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
        "message_id": new_message.id,
        "sender_id": current_user.id,
        "receiver_id": receiver_id,
        "content": "",
        "time": format_message_time(new_message.created_at),
        "unread_count": 0,
        "file_url": file_url,
        "download_url": download_url,
        "file_name": new_message.file_name or "image",
        "sender_email": current_user.email,
        "is_read": False,
        "is_delivered": False
    }

    outgoing_to_receiver = {
        "type": "message",
        "message_type": "photo",
        "message_id": new_message.id,
        "sender_id": current_user.id,
        "receiver_id": receiver_id,
        "content": "",
        "time": format_message_time(new_message.created_at),
        "unread_count": unread_count_for_receiver,
        "file_url": file_url,
        "download_url": download_url,
        "file_name": new_message.file_name or "image",
        "sender_email": current_user.email,
        "is_read": False,
        "is_delivered": False
    }

    await manager.send_to_user(current_user.id, outgoing_to_sender)
    await manager.send_to_user(receiver_id, outgoing_to_receiver)

    new_message.is_delivered = True
    db.commit()
    
    await manager.send_to_user(current_user.id, {
        "type": "delivered",
        "message_id": new_message.id,
        "is_delivered": True
    })

    if receiver_id not in manager.online_users:
        send_push_to_user(
            db=db,
            user_id=receiver_id,
            title=f"Новое фото от {current_user.email}",
            body="📷 Фото",
            url=f"/chat?selected_user_id={current_user.id}",
        )

    return RedirectResponse(url=f"/chat?selected_user_id={receiver_id}", status_code=303)


@app.post("/send-voice")
async def send_voice(
    receiver_id: int = Form(...),
    voice: UploadFile = File(...),
    db: Session = Depends(get_db),
    user_id: str | None = Cookie(default=None)
):
    current_user = get_current_user(user_id, db)
    if not current_user:
        raise HTTPException(status_code=401, detail="Не авторизован")

    if not voice.content_type or not voice.content_type.startswith("audio/"):
        raise HTTPException(status_code=400, detail="Файл должен быть аудио")

    ct = (voice.content_type or "").lower()
    fn = (voice.filename or "").lower()

    if (
        "mp4" in ct
        or "m4a" in ct
        or "aac" in ct
        or fn.endswith(".m4a")
        or fn.endswith(".mp4")
        or fn.endswith(".aac")
    ):
        extension = ".m4a"
    elif "webm" in ct or fn.endswith(".webm"):
        extension = ".webm"
    elif "mpeg" in ct or fn.endswith(".mp3"):
        extension = ".mp3"
    elif "ogg" in ct or fn.endswith(".ogg") or "opus" in ct:
        extension = ".ogg"
    elif "wav" in ct or fn.endswith(".wav"):
        extension = ".wav"
    else:
        extension = ".webm"

    safe_name = f"voice_{uuid.uuid4().hex}{extension}"
    save_path = UPLOADS_DIR / safe_name

    with save_path.open("wb") as buffer:
        content = await voice.read()
        buffer.write(content)

    new_message = Message(
        sender_id=current_user.id,
        receiver_id=receiver_id,
        message_type="voice",
        content="",
        file_name=voice.filename or "voice",
        file_path=safe_name,
        created_at=datetime.now(timezone.utc),
        is_read=False,
        is_delivered=False
    )
    db.add(new_message)
    db.commit()
    db.refresh(new_message)

    unread_count_for_receiver = get_unread_count(receiver_id, current_user.id, db)

    file_url = f"/uploads/{safe_name}"
    download_url = f"/download-file/{new_message.id}"

    outgoing_to_sender = {
        "type": "message",
        "message_type": "voice",
        "message_id": new_message.id,
        "sender_id": current_user.id,
        "receiver_id": receiver_id,
        "content": "",
        "time": format_message_time(new_message.created_at),
        "unread_count": 0,
        "file_url": file_url,
        "download_url": download_url,
        "file_name": new_message.file_name or "voice",
        "sender_email": current_user.email,
        "is_read": False,
        "is_delivered": False
    }

    outgoing_to_receiver = {
        "type": "message",
        "message_type": "voice",
        "message_id": new_message.id,
        "sender_id": current_user.id,
        "receiver_id": receiver_id,
        "content": "",
        "time": format_message_time(new_message.created_at),
        "unread_count": unread_count_for_receiver,
        "file_url": file_url,
        "download_url": download_url,
        "file_name": new_message.file_name or "voice",
        "sender_email": current_user.email,
        "is_read": False,
        "is_delivered": False
    }

    await manager.send_to_user(current_user.id, outgoing_to_sender)
    await manager.send_to_user(receiver_id, outgoing_to_receiver)

    new_message.is_delivered = True
    db.commit()
    
    await manager.send_to_user(current_user.id, {
        "type": "delivered",
        "message_id": new_message.id,
        "is_delivered": True
    })

    if receiver_id not in manager.online_users:
        send_push_to_user(
            db=db,
            user_id=receiver_id,
            title=f"Новое голосовое от {current_user.email}",
            body="🎤 Голосовое сообщение",
            url=f"/chat?selected_user_id={current_user.id}",
        )

    return JSONResponse({"status": "ok", "message_id": new_message.id})


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
async def logout(
    response: Response,
    user_id: str | None = Cookie(default=None),
    db: Session = Depends(get_db)
):
    if user_id:
        uid = int(user_id)
        await manager.force_logout_user(uid, db)
    
    response = RedirectResponse(url="/", status_code=303)
    response.delete_cookie("user_id", path="/")
    return response


@app.websocket("/ws/{user_id}")
async def websocket_endpoint(websocket: WebSocket, user_id: int):
    db = SessionLocal()
    try:
        await manager.connect(user_id, websocket, db)
        
        typing_timeout = {}
        
        all_users = db.query(User).all()
        status_data = {}
        for user in all_users:
            if user.last_activity:
                last_activity_str = user.last_activity.isoformat()
                if not last_activity_str.endswith('Z') and '+' not in last_activity_str:
                    last_activity_str += 'Z'
            else:
                last_activity_str = None
                
            status_data[user.id] = {
                "is_online": user.id in manager.online_users,
                "last_activity": last_activity_str
            }
        
        await manager.send_to_user(user_id, {
            "type": "presence",
            "user_status": status_data
        })

        while True:
            data = await websocket.receive_json()
            data_type = data.get("type")
            
            await manager.update_activity(user_id, db)

            if data_type == "read_chat":
                other_user_id = int(data["chat_user_id"])

                try:
                    all_messages_from_other = (
                        db.query(Message)
                        .filter(
                            Message.sender_id == other_user_id,
                            Message.receiver_id == user_id
                        )
                        .all()
                    )
                    
                    updated_ids = []
                    for msg in all_messages_from_other:
                        if not msg.is_read:
                            msg.is_read = True
                            updated_ids.append(msg.id)

                    if updated_ids:
                        db.commit()
                        
                        for msg_id in updated_ids:
                            await manager.send_to_user(other_user_id, {
                                "type": "read",
                                "message_id": msg_id,
                                "is_read": True
                            })
                except Exception as e:
                    print(f"Read error: {e}")

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

                sender_email = ""
                try:
                    sender = db.query(User).filter(User.id == user_id).first()
                    sender_email = sender.email if sender else ""

                    new_message = Message(
                        sender_id=user_id,
                        receiver_id=receiver_id,
                        message_type="text",
                        content=content,
                        created_at=datetime.now(timezone.utc),
                        is_read=False,
                        is_delivered=False
                    )
                    db.add(new_message)
                    db.commit()
                    db.refresh(new_message)

                    unread_count_for_receiver = get_unread_count(receiver_id, user_id, db)
                    
                    new_message.is_delivered = True
                    db.commit()

                    if receiver_id not in manager.online_users and sender:
                        send_push_to_user(
                            db=db,
                            user_id=receiver_id,
                            title=f"Новое сообщение от {sender.email}",
                            body=content[:120],
                            url=f"/chat?selected_user_id={user_id}",
                        )
                except Exception as e:
                    print(f"Error saving message: {e}")
                    continue

                outgoing_to_sender = {
                    "type": "message",
                    "message_type": "text",
                    "message_id": new_message.id,
                    "sender_id": user_id,
                    "receiver_id": receiver_id,
                    "content": content,
                    "time": format_message_time(new_message.created_at),
                    "unread_count": 0,
                    "sender_email": sender_email,
                    "is_read": False,
                    "is_delivered": True
                }

                outgoing_to_receiver = {
                    "type": "message",
                    "message_type": "text",
                    "message_id": new_message.id,
                    "sender_id": user_id,
                    "receiver_id": receiver_id,
                    "content": content,
                    "time": format_message_time(new_message.created_at),
                    "unread_count": unread_count_for_receiver,
                    "sender_email": sender_email,
                    "is_read": False,
                    "is_delivered": True
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
        await manager.disconnect(user_id, websocket, db)
    except Exception as e:
        print(f"WebSocket error: {e}")
        if user_id in typing_timeout:
            try:
                typing_timeout[user_id].cancel()
            except:
                pass
            del typing_timeout[user_id]
        await manager.disconnect(user_id, websocket, db)
    finally:
        db.close()


# ========== BACKUP ФУНКЦИИ ==========

def check_backup_permission(current_user: User):
    if current_user.email not in ALLOWED_BACKUP_EMAILS:
        raise HTTPException(status_code=403, detail="Доступ запрещён. Только для администраторов.")


@app.get("/download-backup")
async def download_backup(
    user_id: str | None = Cookie(default=None), 
    db: Session = Depends(get_db)
):
    current_user = get_current_user(user_id, db)
    if not current_user:
        raise HTTPException(status_code=401, detail="Не авторизован")
    
    check_backup_permission(current_user)
    
    db_path = BASE_DIR / "messenger.db"
    
    if not db_path.exists():
        raise HTTPException(status_code=404, detail="Файл базы данных не найден")
    
    temp_zip = tempfile.NamedTemporaryFile(suffix=".zip", delete=False)
    temp_zip.close()
    
    with zipfile.ZipFile(temp_zip.name, 'w', zipfile.ZIP_DEFLATED) as zipf:
        zipf.write(db_path, arcname="messenger.db")
        
        if AVATARS_DIR.exists():
            for file in AVATARS_DIR.iterdir():
                if file.is_file():
                    zipf.write(file, arcname=f"avatars/{file.name}")
    
    backup_name = f"messenger_backup_{datetime.now(timezone.utc).strftime('%Y%m%d_%H%M%S')}.zip"
    
    return FileResponse(
        path=temp_zip.name,
        filename=backup_name,
        media_type="application/zip",
        headers={"Content-Disposition": f"attachment; filename={backup_name}"}
    )


@app.post("/restore-backup")
async def restore_backup(
    backup_file: UploadFile = File(...),
    user_id: str | None = Cookie(default=None),
    db: Session = Depends(get_db)
):
    current_user = get_current_user(user_id, db)
    if not current_user:
        raise HTTPException(status_code=401, detail="Не авторизован")
    
    check_backup_permission(current_user)
    
    if not backup_file.filename.endswith('.zip'):
        raise HTTPException(status_code=400, detail="Файл должен быть zip-архивом")
    
    temp_dir = tempfile.mkdtemp()
    temp_zip_path = Path(temp_dir) / "backup.zip"
    
    try:
        with open(temp_zip_path, "wb") as f:
            content = await backup_file.read()
            f.write(content)
        
        with zipfile.ZipFile(temp_zip_path, 'r') as zipf:
            zipf.extractall(temp_dir)
        
        db_path = BASE_DIR / "messenger.db"
        if db_path.exists():
            backup_db = BASE_DIR / f"messenger_backup_auto_{datetime.now(timezone.utc).strftime('%Y%m%d_%H%M%S')}.db"
            shutil.copy(db_path, backup_db)
        
        if AVATARS_DIR.exists():
            backup_avatars = BASE_DIR / f"avatars_backup_{datetime.now(timezone.utc).strftime('%Y%m%d_%H%M%S')}"
            if backup_avatars.exists():
                shutil.rmtree(backup_avatars)
            shutil.copytree(AVATARS_DIR, backup_avatars)
        
        extracted_db = Path(temp_dir) / "messenger.db"
        if extracted_db.exists():
            shutil.copy(extracted_db, db_path)
        
        extracted_avatars = Path(temp_dir) / "avatars"
        if extracted_avatars.exists():
            if AVATARS_DIR.exists():
                shutil.rmtree(AVATARS_DIR)
            AVATARS_DIR.mkdir(exist_ok=True)
            for file in extracted_avatars.iterdir():
                if file.is_file():
                    shutil.copy(file, AVATARS_DIR / file.name)
        
        return JSONResponse({
            "status": "success",
            "message": "База данных и аватары восстановлены. Обновите страницу."
        })
    
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Ошибка при восстановлении: {str(e)}")
    finally:
        if Path(temp_dir).exists():
            shutil.rmtree(temp_dir, ignore_errors=True)
