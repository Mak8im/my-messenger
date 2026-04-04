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
import logging
import traceback
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
)
from fastapi.exceptions import RequestValidationError
from fastapi.exception_handlers import http_exception_handler, request_validation_exception_handler
from fastapi.responses import HTMLResponse, RedirectResponse, FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from sqlalchemy.orm import Session
from sqlalchemy import or_, and_, text
from pywebpush import webpush, WebPushException

from database import Base, engine, get_db, SessionLocal
from auth import create_user, authenticate_user
from models import User, Message, PushSubscription, ChatPin, UserSession

app = FastAPI()

Base.metadata.create_all(bind=engine)


def _migrate_sqlite_schema():
    """Добавляет колонки в существующую SQLite БД (если их ещё нет)."""
    try:
        if engine.dialect.name != "sqlite":
            return
        with engine.connect() as conn:
            for stmt in (
                "ALTER TABLE messages ADD COLUMN edited_at DATETIME",
                "ALTER TABLE messages ADD COLUMN deleted_for_sender BOOLEAN NOT NULL DEFAULT 0",
                "ALTER TABLE messages ADD COLUMN deleted_for_receiver BOOLEAN NOT NULL DEFAULT 0",
                "ALTER TABLE messages ADD COLUMN reply_to_id INTEGER REFERENCES messages(id)",
            ):
                try:
                    conn.execute(text(stmt))
                    conn.commit()
                except Exception:
                    conn.rollback()
    except Exception as e:
        print("Migration warning:", e)


_migrate_sqlite_schema()


def normalize_chat_pair(a: int, b: int) -> tuple[int, int]:
    return (min(a, b), max(a, b))


def message_visible_for_user(m: Message, viewer_id: int) -> bool:
    if m.sender_id == viewer_id:
        return not m.deleted_for_sender
    if m.receiver_id == viewer_id:
        return not m.deleted_for_receiver
    return False


def format_stars_amount(v: float | str | None) -> str:
    try:
        x = float(v or 0)
    except (TypeError, ValueError):
        return str(v or "0")
    t = f"{x:.3f}".rstrip("0").rstrip(".")
    return t if t else "0"


def stars_display_label(u: User) -> str:
    t = (u.username or "").strip()
    if t:
        return t if t.startswith("@") else f"@{t}"
    return (u.email or "").strip() or "Пользователь"


def stars_transfer_banner(sender: User, amount: float | str) -> str:
    return f"{stars_display_label(sender)} отправил(а) {format_stars_amount(amount)}"


def build_reply_preview(m: Message) -> str:
    if not m:
        return ""
    if m.message_type == "photo":
        return "📷 Фото"
    if m.message_type == "video":
        return "🎬 Видео"
    if m.message_type == "voice":
        return "🎤 Голосовое"
    if m.message_type == "stars":
        return f"⭐ {format_stars_amount(m.content)}"
    return (m.content or "")[:120]


def last_visible_message_between(db: Session, current_user_id: int, peer_id: int) -> Optional[Message]:
    msgs = (
        db.query(Message)
        .filter(
            or_(
                and_(Message.sender_id == current_user_id, Message.receiver_id == peer_id),
                and_(Message.sender_id == peer_id, Message.receiver_id == current_user_id),
            )
        )
        .order_by(Message.id.desc())
        .limit(200)
        .all()
    )
    for m in msgs:
        if message_visible_for_user(m, current_user_id):
            return m
    return None

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

app_log = logging.getLogger("uvicorn.error")


@app.exception_handler(Exception)
async def unhandled_exception_handler(request: Request, exc: Exception):
    if isinstance(exc, HTTPException):
        return await http_exception_handler(request, exc)
    if isinstance(exc, RequestValidationError):
        return await request_validation_exception_handler(request, exc)

    app_log.exception("Необработанная ошибка: %s %s", request.method, request.url.path)
    tb = traceback.format_exc()
    debug = os.environ.get("MESSENGER_DEBUG", "").lower() in ("1", "true", "yes")
    reason = str(exc).strip() or type(exc).__name__

    if request.url.path.startswith("/api/"):
        payload = {"error": "Internal Server Error", "reason": reason}
        if debug:
            payload["traceback"] = tb
        return JSONResponse(status_code=500, content=payload)

    accept = request.headers.get("accept") or ""
    if "text/html" not in accept and "*/*" not in accept and "application/json" in accept:
        payload = {"error": "Internal Server Error", "reason": reason}
        if debug:
            payload["traceback"] = tb
        return JSONResponse(status_code=500, content=payload)

    return templates.TemplateResponse(
        request=request,
        name="error_server.html",
        status_code=500,
        context={
            "error_title": "Ошибка сервера",
            "error_summary": "Запрос не удалось выполнить из-за внутренней ошибки. Ниже указана причина — её можно переслать разработчику.",
            "error_reason": reason,
            "show_traceback": debug,
            "traceback": tb,
        },
    )


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


def default_device_name_from_ua(user_agent: str | None) -> str:
    ua = (user_agent or "").lower()
    if "iphone" in ua:
        return "iPhone"
    if "ipad" in ua:
        return "iPad"
    if "android" in ua:
        return "Android"
    if "windows" in ua:
        return "Windows"
    if "mac os" in ua or "macintosh" in ua:
        return "Mac"
    if "linux" in ua:
        return "Linux"
    return "Браузер"


def client_label_from_ua(user_agent: str | None) -> str:
    ua = (user_agent or "").strip()
    if not ua:
        return "Веб-клиент"
    low = ua.lower()
    if "edg/" in low:
        return "Microsoft Edge"
    if "chrome/" in low and "chromium" not in low:
        return "Google Chrome"
    if "firefox/" in low:
        return "Firefox"
    if "safari/" in low and "chrome" not in low:
        return "Safari"
    if "telegram" in low:
        return "Telegram"
    return ua[:80] if len(ua) > 80 else ua


def session_platform_kind(user_agent: str | None) -> str:
    ua = (user_agent or "").lower()
    if "iphone" in ua or "ipad" in ua or ("mac os" in ua and "mobile" in ua):
        return "apple"
    if "android" in ua:
        return "android"
    return "desktop"


def format_session_meta_line(sess: UserSession) -> str:
    parts = []
    if sess.ip_address:
        parts.append(sess.ip_address)
    if sess.last_activity:
        if sess.last_activity.tzinfo is None:
            la = sess.last_activity.replace(tzinfo=timezone.utc)
        else:
            la = sess.last_activity
        local_la = la + timedelta(hours=3)
        parts.append(local_la.strftime("%d.%m.%Y %H:%M"))
    return " • ".join(parts) if parts else "—"


def attach_session_cookie(response, request: Request, token: str, max_age: int):
    is_secure = request.url.scheme == "https"
    response.set_cookie(
        key="session_token",
        value=token,
        httponly=True,
        max_age=max_age,
        secure=is_secure,
        samesite="lax",
        path="/",
    )


def create_browser_session(db: Session, user: User, request: Request) -> tuple[str, UserSession]:
    token = uuid.uuid4().hex
    ua = (request.headers.get("user-agent") or "")[:4000]
    ip = request.client.host if request.client else None
    now = datetime.now(timezone.utc)
    sess = UserSession(
        user_id=user.id,
        session_token=token,
        device_name=default_device_name_from_ua(ua),
        user_agent=ua or None,
        ip_address=(ip[:64] if ip else None),
        last_activity=now,
        created_at=now,
    )
    db.add(sess)
    db.commit()
    return token, sess


def get_current_user(user_id: str | None, session_token: str | None, db: Session):
    if not user_id:
        return None
    try:
        uid = int(user_id)
    except (ValueError, TypeError):
        return None
    user = db.query(User).filter(User.id == uid).first()
    if not user:
        return None
    if session_token:
        sess = (
            db.query(UserSession)
            .filter(UserSession.session_token == session_token, UserSession.user_id == uid)
            .first()
        )
        if not sess:
            return None
    return user


def get_unread_count(current_user_id: int, other_user_id: int, db: Session) -> int:
    return (
        db.query(Message)
        .filter(
            Message.sender_id == other_user_id,
            Message.receiver_id == current_user_id,
            Message.is_read == False,
            Message.deleted_for_receiver == False,
        )
        .count()
    )


def build_message_preview(message: Message, current_user_id: int) -> str:
    if not message:
        return "Начать диалог"

    if message.message_type == "photo":
        base = "📷 Фото"
    elif message.message_type == "video":
        base = "🎬 Видео"
    elif message.message_type == "voice":
        base = "🎤 Голосовое"
    elif message.message_type == "stars":
        base = f"⭐ {format_stars_amount(message.content)}"
    else:
        base = (message.content or "")[:40]

    if message.sender_id == current_user_id:
        return f"Ты: {base}"
    return base


def build_dialogs_for_user(current_user: User, db: Session, online_users: set):
    all_users = db.query(User).filter(User.id != current_user.id).all()
    dialogs = []

    for user in all_users:
        last_message = last_visible_message_between(db, current_user.id, user.id)

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

    async def connect(self, user_id: int, websocket: WebSocket, db: Session) -> bool:
        user_id = int(user_id)
        await websocket.accept()
        tok = websocket.cookies.get("session_token")
        if tok:
            row = (
                db.query(UserSession)
                .filter(UserSession.session_token == tok, UserSession.user_id == user_id)
                .first()
            )
            if not row:
                try:
                    await websocket.close(code=1008)
                except Exception:
                    pass
                return False
        self.active_connections[user_id].append(websocket)
        
        user = db.query(User).filter(User.id == user_id).first()
        if user:
            user.last_activity = datetime.now(timezone.utc)
            db.commit()
        
        was_offline = user_id not in self.online_users
        self.online_users.add(user_id)
        
        if was_offline:
            await self.broadcast_presence(db)
        return True

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
        uid = int(user_id)
        if uid not in self.active_connections:
            return

        dead = []
        for ws in list(self.active_connections[uid]):
            try:
                await ws.send_json(data)
            except Exception:
                dead.append(ws)

        for ws in dead:
            if uid in self.active_connections and ws in self.active_connections[uid]:
                self.active_connections[uid].remove(ws)

        if uid in self.active_connections and not self.active_connections[uid]:
            del self.active_connections[uid]

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
        self.online_users.discard(user_id)

        try:
            db.query(UserSession).filter(UserSession.user_id == user_id).delete(synchronize_session=False)
            user = db.query(User).filter(User.id == user_id).first()
            if user:
                user.last_activity = datetime.now(timezone.utc)
            db.commit()
        except Exception:
            try:
                db.rollback()
            except Exception:
                pass

        try:
            await self.broadcast_presence(db)
        except Exception:
            pass

        if user_id in self.active_connections:
            for ws in list(self.active_connections[user_id]):
                try:
                    await ws.close()
                except Exception:
                    pass
            try:
                del self.active_connections[user_id]
            except Exception:
                pass


manager = ConnectionManager()


@app.get("/", response_class=HTMLResponse)
async def home(
    request: Request,
    user_id: str | None = Cookie(default=None),
    session_token: str | None = Cookie(default=None),
    db: Session = Depends(get_db)
):
    if user_id:
        user = get_current_user(user_id, session_token, db)
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

    if remember_me:
        max_age = 30 * 24 * 60 * 60
    else:
        max_age = 7 * 24 * 60 * 60

    is_secure = request.url.scheme == "https"
    response = RedirectResponse(url="/chat", status_code=303)
    response.set_cookie(
        key="user_id",
        value=str(user.id),
        httponly=True,
        max_age=max_age,
        secure=is_secure,
        samesite="lax",
        path="/",
    )
    token, _ = create_browser_session(db, user, request)
    attach_session_cookie(response, request, token, max_age)

    return response


@app.get("/chat", response_class=HTMLResponse)
async def chat_page(
    request: Request,
    db: Session = Depends(get_db),
    user_id: str | None = Cookie(default=None),
    session_token: str | None = Cookie(default=None),
    selected_user_id: int | None = None
):
    current_user = get_current_user(user_id, session_token, db)
    if not current_user:
        return RedirectResponse(url="/", status_code=303)

    db.refresh(current_user)
    
    current_user.last_activity = datetime.now(timezone.utc)
    db.commit()

    messages = []
    selected_user = None
    pinned_message = None
    pinned_preview = ""

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
                    Message.is_read == False,
                    Message.deleted_for_receiver == False,
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

            raw_messages = (
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
            messages = [m for m in raw_messages if message_visible_for_user(m, current_user.id)]
            reply_ids = [m.reply_to_id for m in messages if m.reply_to_id]
            reply_map = {}
            if reply_ids:
                for rp in db.query(Message).filter(Message.id.in_(reply_ids)).all():
                    reply_map[rp.id] = rp
            for m in messages:
                m.reply_parent = reply_map.get(m.reply_to_id) if m.reply_to_id else None

    if selected_user:
        lo, hi = normalize_chat_pair(current_user.id, selected_user.id)
        pin = (
            db.query(ChatPin)
            .filter(ChatPin.user_low_id == lo, ChatPin.user_high_id == hi)
            .first()
        )
        if pin:
            pm = db.query(Message).filter(Message.id == pin.message_id).first()
            if pm and message_visible_for_user(pm, current_user.id):
                pinned_message = pm
                pinned_preview = build_reply_preview(pm)
                if pm.message_type == "text" and (pm.content or "").strip():
                    pinned_preview = (pm.content or "")[:80]
            else:
                db.delete(pin)
                db.commit()

    dialogs = build_dialogs_for_user(current_user, db, manager.online_users)
    
    selected_user_status = "Не в сети"
    if selected_user:
        if selected_user.id in manager.online_users:
            selected_user_status = "В сети"
        else:
            selected_user_status = format_last_seen(selected_user.last_activity)
    
    can_backup = current_user.email in ALLOWED_BACKUP_EMAILS

    response = templates.TemplateResponse(
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
            "pinned_message": pinned_message,
            "pinned_preview": pinned_preview,
            "peer_display_name": (selected_user.full_name or selected_user.email) if selected_user else "",
            "stars_transfer_banner": stars_transfer_banner,
            "format_stars_amount": format_stars_amount,
        }
    )
    if current_user and not request.cookies.get("session_token"):
        boot_max = 30 * 24 * 60 * 60
        token, _ = create_browser_session(db, current_user, request)
        attach_session_cookie(response, request, token, boot_max)
    return response


# ========== ПРОФИЛЬ API ==========

@app.get("/api/profile")
async def get_profile(
    user_id: str | None = Cookie(default=None),
    session_token: str | None = Cookie(default=None),
    db: Session = Depends(get_db)
):
    current_user = get_current_user(user_id, session_token, db)
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
    session_token: str | None = Cookie(default=None),
    db: Session = Depends(get_db)
):
    current_user = get_current_user(user_id, session_token, db)
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
    session_token: str | None = Cookie(default=None),
    db: Session = Depends(get_db)
):
    current_user = get_current_user(user_id, session_token, db)
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
    session_token: str | None = Cookie(default=None),
    db: Session = Depends(get_db)
):
    current_user = get_current_user(user_id, session_token, db)
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
    session_token: str | None = Cookie(default=None),
    db: Session = Depends(get_db)
):
    current_user = get_current_user(user_id, session_token, db)
    if not current_user:
        raise HTTPException(status_code=401, detail="Не авторизован")
    
    sound = current_user.notification_sound or "default"
    return {"sound": sound}


@app.put("/api/notification-sound")
async def set_notification_sound(
    sound: str = Body(..., embed=True),
    user_id: str | None = Cookie(default=None),
    session_token: str | None = Cookie(default=None),
    db: Session = Depends(get_db)
):
    current_user = get_current_user(user_id, session_token, db)
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
    session_token: str | None = Cookie(default=None),
    db: Session = Depends(get_db)
):
    current_user = get_current_user(user_id, session_token, db)
    if not current_user:
        raise HTTPException(status_code=401, detail="Не авторизован")
    
    return {"stars": current_user.stars or 0.0}


@app.post("/api/stars/click")
async def click_star(
    user_id: str | None = Cookie(default=None),
    session_token: str | None = Cookie(default=None),
    db: Session = Depends(get_db)
):
    current_user = get_current_user(user_id, session_token, db)
    if not current_user:
        raise HTTPException(status_code=401, detail="Не авторизован")
    
    current_user.stars = (current_user.stars or 0.0) + 0.001
    db.commit()
    
    return {"stars": current_user.stars}


@app.post("/api/stars/send")
async def api_send_stars(
    payload: dict = Body(...),
    user_id: str | None = Cookie(default=None),
    session_token: str | None = Cookie(default=None),
    db: Session = Depends(get_db),
):
    current_user = get_current_user(user_id, session_token, db)
    if not current_user:
        raise HTTPException(status_code=401, detail="Не авторизован")

    receiver_id = int(payload.get("receiver_id"))
    try:
        amount = float(payload.get("amount"))
    except (TypeError, ValueError):
        raise HTTPException(status_code=400, detail="Некорректная сумма")

    if receiver_id == current_user.id:
        raise HTTPException(status_code=400, detail="Нельзя отправить звёзды самому себе")
    if amount < 0.1 - 1e-9:
        raise HTTPException(status_code=400, detail="Минимум 0.1 звезды")

    amount = round(amount, 3)
    receiver = db.query(User).filter(User.id == receiver_id).first()
    if not receiver:
        raise HTTPException(status_code=404, detail="Пользователь не найден")

    balance = float(current_user.stars or 0.0)
    if balance + 1e-9 < amount:
        raise HTTPException(status_code=400, detail="Недостаточно звёзд")

    current_user.stars = balance - amount
    receiver.stars = float(receiver.stars or 0.0) + amount

    new_message = Message(
        sender_id=current_user.id,
        receiver_id=receiver_id,
        message_type="stars",
        content=str(amount),
        created_at=datetime.now(timezone.utc),
        is_read=False,
        is_delivered=True,
    )
    db.add(new_message)
    db.commit()
    db.refresh(new_message)

    unread_count_for_receiver = get_unread_count(receiver_id, current_user.id, db)
    banner = stars_transfer_banner(current_user, amount)
    base_out = {
        "type": "message",
        "message_type": "stars",
        "message_id": new_message.id,
        "sender_id": current_user.id,
        "receiver_id": receiver_id,
        "content": str(amount),
        "stars_banner": banner,
        "time": format_message_time(new_message.created_at),
        "sender_email": current_user.email,
        "is_read": False,
        "is_delivered": True,
        "reply_to_id": None,
        "reply_preview": "",
    }
    await manager.send_to_user(current_user.id, {**base_out, "unread_count": 0})
    await manager.send_to_user(receiver_id, {**base_out, "unread_count": unread_count_for_receiver})

    receiver_was_offline = receiver_id not in manager.online_users
    if receiver_was_offline:
        send_push_to_user(
            db=db,
            user_id=receiver_id,
            title=f"Звёзды от {stars_display_label(current_user)}",
            body=banner,
            url=f"/chat?selected_user_id={current_user.id}",
        )

    return {"ok": True, "stars": float(current_user.stars or 0.0)}


# ========== ДРУГИЕ РОУТЫ ==========

@app.post("/subscribe")
async def subscribe(
    subscription: dict = Body(...),
    db: Session = Depends(get_db),
    user_id: str | None = Cookie(default=None),
    session_token: str | None = Cookie(default=None),
):
    current_user = get_current_user(user_id, session_token, db)
    if not current_user:
        raise HTTPException(status_code=401, detail="Не авторизован")

    save_push_subscription(db, current_user.id, subscription)
    return JSONResponse({"status": "ok"})


@app.post("/unsubscribe")
async def unsubscribe(
    data: dict = Body(...),
    db: Session = Depends(get_db),
    user_id: str | None = Cookie(default=None),
    session_token: str | None = Cookie(default=None),
):
    current_user = get_current_user(user_id, session_token, db)
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
    user_id: str | None = Cookie(default=None),
    session_token: str | None = Cookie(default=None),
):
    current_user = get_current_user(user_id, session_token, db)
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


@app.post("/send-video")
async def send_video(
    receiver_id: int = Form(...),
    video: UploadFile = File(...),
    db: Session = Depends(get_db),
    user_id: str | None = Cookie(default=None),
    session_token: str | None = Cookie(default=None),
):
    current_user = get_current_user(user_id, session_token, db)
    if not current_user:
        return RedirectResponse(url="/", status_code=303)

    ct = (video.content_type or "").lower()
    fn = (video.filename or "").lower()
    if not ct.startswith("video/") and not any(
        fn.endswith(x) for x in (".mp4", ".webm", ".mov", ".m4v", ".ogv")
    ):
        return RedirectResponse(url=f"/chat?selected_user_id={receiver_id}", status_code=303)

    if fn.endswith(".webm"):
        extension = ".webm"
    elif fn.endswith(".mov"):
        extension = ".mov"
    else:
        extension = ".mp4"

    safe_name = f"{uuid.uuid4().hex}{extension}"
    save_path = UPLOADS_DIR / safe_name

    with save_path.open("wb") as buffer:
        content = await video.read()
        if len(content) > 80 * 1024 * 1024:
            return RedirectResponse(url=f"/chat?selected_user_id={receiver_id}", status_code=303)
        buffer.write(content)

    new_message = Message(
        sender_id=current_user.id,
        receiver_id=receiver_id,
        message_type="video",
        content="",
        file_name=video.filename or "video",
        file_path=safe_name,
        created_at=datetime.now(timezone.utc),
        is_read=False,
        is_delivered=False,
    )
    db.add(new_message)
    db.commit()
    db.refresh(new_message)

    unread_count_for_receiver = get_unread_count(receiver_id, current_user.id, db)
    file_url = f"/uploads/{safe_name}"
    download_url = f"/download-file/{new_message.id}"

    outgoing_to_sender = {
        "type": "message",
        "message_type": "video",
        "message_id": new_message.id,
        "sender_id": current_user.id,
        "receiver_id": receiver_id,
        "content": "",
        "time": format_message_time(new_message.created_at),
        "unread_count": 0,
        "file_url": file_url,
        "download_url": download_url,
        "file_name": new_message.file_name or "video",
        "sender_email": current_user.email,
        "is_read": False,
        "is_delivered": False,
    }
    outgoing_to_receiver = {
        **outgoing_to_sender,
        "unread_count": unread_count_for_receiver,
    }

    await manager.send_to_user(current_user.id, outgoing_to_sender)
    await manager.send_to_user(receiver_id, outgoing_to_receiver)

    new_message.is_delivered = True
    db.commit()

    await manager.send_to_user(current_user.id, {
        "type": "delivered",
        "message_id": new_message.id,
        "is_delivered": True,
    })

    if receiver_id not in manager.online_users:
        send_push_to_user(
            db=db,
            user_id=receiver_id,
            title=f"Видео от {current_user.email}",
            body="🎬 Видео",
            url=f"/chat?selected_user_id={current_user.id}",
        )

    return RedirectResponse(url=f"/chat?selected_user_id={receiver_id}", status_code=303)


@app.post("/send-voice")
async def send_voice(
    receiver_id: int = Form(...),
    voice: UploadFile = File(...),
    db: Session = Depends(get_db),
    user_id: str | None = Cookie(default=None),
    session_token: str | None = Cookie(default=None),
):
    current_user = get_current_user(user_id, session_token, db)
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
    user_id: str | None = Cookie(default=None),
    session_token: str | None = Cookie(default=None),
):
    current_user = get_current_user(user_id, session_token, db)
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


@app.post("/api/messages/delete")
async def api_delete_message(
    payload: dict = Body(...),
    user_id: str | None = Cookie(default=None),
    session_token: str | None = Cookie(default=None),
    db: Session = Depends(get_db),
):
    current_user = get_current_user(user_id, session_token, db)
    if not current_user:
        raise HTTPException(status_code=401, detail="Не авторизован")
    message_id = int(payload.get("message_id"))
    scope = (payload.get("scope") or "me").lower()
    if scope not in ("me", "everyone"):
        raise HTTPException(status_code=400, detail="Некорректный scope")

    msg = db.query(Message).filter(Message.id == message_id).first()
    if not msg:
        raise HTTPException(status_code=404, detail="Сообщение не найдено")
    if current_user.id not in (msg.sender_id, msg.receiver_id):
        raise HTTPException(status_code=403, detail="Нет доступа")

    if scope == "everyone":
        if msg.sender_id != current_user.id:
            raise HTTPException(status_code=403, detail="Нельзя удалить у всех")
        sid = msg.sender_id
        rid = msg.receiver_id
        lo, hi = normalize_chat_pair(sid, rid)
        pin = db.query(ChatPin).filter(ChatPin.user_low_id == lo, ChatPin.user_high_id == hi).first()
        if pin and pin.message_id == msg.id:
            db.delete(pin)
        if msg.file_path:
            fp = UPLOADS_DIR / msg.file_path
            if fp.exists():
                try:
                    fp.unlink()
                except OSError:
                    pass
        db.delete(msg)
        db.commit()
        deleted_payload = {
            "type": "message_deleted",
            "message_id": int(message_id),
            "scope": "everyone",
            "sender_id": int(sid),
            "receiver_id": int(rid),
        }
        await manager.send_to_user(sid, deleted_payload)
        await manager.send_to_user(rid, deleted_payload)
        return {"ok": True}

    if msg.sender_id == current_user.id:
        msg.deleted_for_sender = True
    else:
        msg.deleted_for_receiver = True
    db.commit()
    return {"ok": True}


@app.put("/api/messages/{message_id}")
async def api_edit_message(
    message_id: int,
    payload: dict = Body(...),
    user_id: str | None = Cookie(default=None),
    session_token: str | None = Cookie(default=None),
    db: Session = Depends(get_db),
):
    current_user = get_current_user(user_id, session_token, db)
    if not current_user:
        raise HTTPException(status_code=401, detail="Не авторизован")
    content = (payload.get("content") or "").strip()
    if not content:
        raise HTTPException(status_code=400, detail="Пустой текст")

    msg = db.query(Message).filter(Message.id == message_id).first()
    if not msg:
        raise HTTPException(status_code=404, detail="Сообщение не найдено")
    if msg.sender_id != current_user.id:
        raise HTTPException(status_code=403, detail="Нет доступа")
    if msg.message_type != "text":
        raise HTTPException(status_code=400, detail="Можно редактировать только текст")

    msg.content = content
    msg.edited_at = datetime.now(timezone.utc)
    db.commit()

    edited_time = format_message_time(msg.edited_at)
    out = {
        "type": "message_edited",
        "message_id": msg.id,
        "content": msg.content,
        "edited_time": edited_time,
    }
    await manager.send_to_user(msg.sender_id, out)
    await manager.send_to_user(msg.receiver_id, out)
    return {"ok": True, "edited_time": edited_time}


@app.post("/api/chat/pin")
async def api_pin_message(
    payload: dict = Body(...),
    user_id: str | None = Cookie(default=None),
    session_token: str | None = Cookie(default=None),
    db: Session = Depends(get_db),
):
    current_user = get_current_user(user_id, session_token, db)
    if not current_user:
        raise HTTPException(status_code=401, detail="Не авторизован")
    peer_id = int(payload.get("peer_id"))
    message_id = payload.get("message_id")
    if message_id is not None and message_id != "":
        message_id = int(message_id)
    else:
        message_id = None

    lo, hi = normalize_chat_pair(current_user.id, peer_id)
    pin = db.query(ChatPin).filter(ChatPin.user_low_id == lo, ChatPin.user_high_id == hi).first()

    if message_id is None:
        if pin:
            db.delete(pin)
            db.commit()
        await manager.send_to_user(current_user.id, {"type": "pin_updated", "peer_id": peer_id, "message_id": None, "preview": ""})
        await manager.send_to_user(peer_id, {"type": "pin_updated", "peer_id": current_user.id, "message_id": None, "preview": ""})
        return {"ok": True}

    msg = db.query(Message).filter(Message.id == message_id).first()
    if not msg:
        raise HTTPException(status_code=404, detail="Сообщение не найдено")
    if msg.sender_id not in (current_user.id, peer_id) or msg.receiver_id not in (current_user.id, peer_id):
        raise HTTPException(status_code=400, detail="Сообщение не из этого чата")

    preview = build_reply_preview(msg)
    if msg.message_type == "text" and (msg.content or "").strip():
        preview = (msg.content or "")[:80]

    if pin:
        pin.message_id = message_id
    else:
        db.add(
            ChatPin(
                user_low_id=lo,
                user_high_id=hi,
                message_id=message_id,
            )
        )
    db.commit()

    pin_payload = {"type": "pin_updated", "message_id": message_id, "preview": preview}
    await manager.send_to_user(current_user.id, {**pin_payload, "peer_id": peer_id})
    await manager.send_to_user(peer_id, {**pin_payload, "peer_id": current_user.id})
    return {"ok": True}


@app.post("/api/chat/clear")
async def api_clear_chat(
    payload: dict = Body(...),
    user_id: str | None = Cookie(default=None),
    session_token: str | None = Cookie(default=None),
    db: Session = Depends(get_db),
):
    current_user = get_current_user(user_id, session_token, db)
    if not current_user:
        raise HTTPException(status_code=401, detail="Не авторизован")
    peer_id = int(payload.get("peer_id"))
    scope = (payload.get("scope") or "me").lower()
    if scope not in ("me", "both"):
        raise HTTPException(status_code=400, detail="Некорректный scope")
    if peer_id == current_user.id:
        raise HTTPException(status_code=400, detail="Некорректный собеседник")
    other = db.query(User).filter(User.id == peer_id).first()
    if not other:
        raise HTTPException(status_code=404, detail="Пользователь не найден")

    pair_filter = or_(
        and_(Message.sender_id == current_user.id, Message.receiver_id == peer_id),
        and_(Message.sender_id == peer_id, Message.receiver_id == current_user.id),
    )
    msgs = db.query(Message).filter(pair_filter).all()

    if scope == "me":
        for m in msgs:
            if m.sender_id == current_user.id:
                m.deleted_for_sender = True
            else:
                m.deleted_for_receiver = True
        db.commit()
        return {"ok": True, "scope": "me"}

    lo, hi = normalize_chat_pair(current_user.id, peer_id)
    pin = db.query(ChatPin).filter(ChatPin.user_low_id == lo, ChatPin.user_high_id == hi).first()
    if pin:
        db.delete(pin)
    for m in msgs:
        if m.file_path:
            fp = UPLOADS_DIR / m.file_path
            if fp.exists():
                try:
                    fp.unlink()
                except OSError:
                    pass
        db.delete(m)
    db.commit()
    await manager.send_to_user(peer_id, {"type": "chat_cleared", "peer_id": current_user.id, "scope": "both"})
    await manager.send_to_user(current_user.id, {"type": "chat_cleared", "peer_id": peer_id, "scope": "both"})
    return {"ok": True, "scope": "both"}


def _session_api_dict(sess: UserSession, current_token: str | None) -> dict:
    return {
        "id": sess.id,
        "device_name": sess.device_name,
        "client_label": client_label_from_ua(sess.user_agent),
        "platform": session_platform_kind(sess.user_agent),
        "meta_line": format_session_meta_line(sess),
        "is_current": bool(current_token and sess.session_token == current_token),
    }


@app.get("/api/sessions")
async def api_list_sessions(
    user_id: str | None = Cookie(default=None),
    session_token: str | None = Cookie(default=None),
    db: Session = Depends(get_db),
):
    current_user = get_current_user(user_id, session_token, db)
    if not current_user:
        raise HTTPException(status_code=401, detail="Не авторизован")
    rows = (
        db.query(UserSession)
        .filter(UserSession.user_id == current_user.id)
        .order_by(UserSession.last_activity.desc())
        .all()
    )
    if session_token:
        cur = next((s for s in rows if s.session_token == session_token), None)
        if cur:
            cur.last_activity = datetime.now(timezone.utc)
            db.commit()
    return {"sessions": [_session_api_dict(s, session_token) for s in rows]}


@app.patch("/api/sessions/{session_id}")
async def api_rename_session(
    session_id: int,
    payload: dict = Body(...),
    user_id: str | None = Cookie(default=None),
    session_token: str | None = Cookie(default=None),
    db: Session = Depends(get_db),
):
    current_user = get_current_user(user_id, session_token, db)
    if not current_user:
        raise HTTPException(status_code=401, detail="Не авторизован")
    name = (payload.get("device_name") or "").strip()
    if not name or len(name) > 120:
        raise HTTPException(status_code=400, detail="Некорректное имя устройства")
    sess = (
        db.query(UserSession)
        .filter(UserSession.id == session_id, UserSession.user_id == current_user.id)
        .first()
    )
    if not sess:
        raise HTTPException(status_code=404, detail="Сеанс не найден")
    sess.device_name = name
    db.commit()
    return {"ok": True}


@app.delete("/api/sessions/{session_id}")
async def api_revoke_session(
    session_id: int,
    user_id: str | None = Cookie(default=None),
    session_token: str | None = Cookie(default=None),
    db: Session = Depends(get_db),
):
    current_user = get_current_user(user_id, session_token, db)
    if not current_user:
        raise HTTPException(status_code=401, detail="Не авторизован")
    sess = (
        db.query(UserSession)
        .filter(UserSession.id == session_id, UserSession.user_id == current_user.id)
        .first()
    )
    if not sess:
        raise HTTPException(status_code=404, detail="Сеанс не найден")
    if session_token and sess.session_token == session_token:
        raise HTTPException(status_code=400, detail="Текущий сеанс нельзя завершить здесь — выйди из аккаунта")
    db.delete(sess)
    db.commit()
    return {"ok": True}


@app.post("/api/sessions/terminate-others")
async def api_terminate_other_sessions(
    user_id: str | None = Cookie(default=None),
    session_token: str | None = Cookie(default=None),
    db: Session = Depends(get_db),
):
    current_user = get_current_user(user_id, session_token, db)
    if not current_user:
        raise HTTPException(status_code=401, detail="Не авторизован")
    if not session_token:
        raise HTTPException(status_code=400, detail="Для этой операции нужна привязка сеанса. Перезайди в аккаунт")
    n = (
        db.query(UserSession)
        .filter(
            UserSession.user_id == current_user.id,
            UserSession.session_token != session_token,
        )
        .delete(synchronize_session=False)
    )
    db.commit()
    return {"ok": True, "terminated": n}


@app.get("/logout")
async def logout(request: Request, db: Session = Depends(get_db)):
    raw = request.cookies.get("user_id")
    try:
        if raw is not None and str(raw).strip() != "":
            uid = int(raw)
            await manager.force_logout_user(uid, db)
    except (ValueError, TypeError):
        pass
    except Exception:
        try:
            db.rollback()
        except Exception:
            pass

    out = RedirectResponse(url="/", status_code=303)
    out.delete_cookie(key="user_id", path="/")
    out.delete_cookie(key="session_token", path="/")
    return out


@app.websocket("/ws/{user_id}")
async def websocket_endpoint(websocket: WebSocket, user_id: int):
    db = SessionLocal()
    try:
        connected = await manager.connect(user_id, websocket, db)
        if not connected:
            return

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

                reply_to_id = data.get("reply_to_id")
                if reply_to_id is not None:
                    reply_to_id = int(reply_to_id)
                else:
                    reply_to_id = None

                sender_email = ""
                try:
                    sender = db.query(User).filter(User.id == user_id).first()
                    sender_email = sender.email if sender else ""

                    if reply_to_id:
                        rp = db.query(Message).filter(Message.id == reply_to_id).first()
                        if not rp or rp.sender_id not in (user_id, receiver_id) or rp.receiver_id not in (user_id, receiver_id):
                            reply_to_id = None

                    new_message = Message(
                        sender_id=user_id,
                        receiver_id=receiver_id,
                        message_type="text",
                        content=content,
                        created_at=datetime.now(timezone.utc),
                        is_read=False,
                        is_delivered=False,
                        reply_to_id=reply_to_id,
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

                reply_preview = ""
                if reply_to_id:
                    rp = db.query(Message).filter(Message.id == reply_to_id).first()
                    if rp:
                        reply_preview = build_reply_preview(rp)

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
                    "is_delivered": True,
                    "reply_to_id": reply_to_id,
                    "reply_preview": reply_preview,
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
                    "is_delivered": True,
                    "reply_to_id": reply_to_id,
                    "reply_preview": reply_preview,
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
    session_token: str | None = Cookie(default=None),
    db: Session = Depends(get_db)
):
    current_user = get_current_user(user_id, session_token, db)
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
    session_token: str | None = Cookie(default=None),
    db: Session = Depends(get_db)
):
    current_user = get_current_user(user_id, session_token, db)
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
