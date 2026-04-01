from sqlalchemy import Column, Integer, String, ForeignKey, Text, DateTime, Boolean, Float, UniqueConstraint
from database import Base
from datetime import datetime


class User(Base):
    __tablename__ = "users"

    id = Column(Integer, primary_key=True, index=True)
    email = Column(String, unique=True, index=True, nullable=False)
    password = Column(String, nullable=False)
    last_activity = Column(DateTime(timezone=True), default=datetime.utcnow, nullable=True)
    
    full_name = Column(String, nullable=True, default="")
    username = Column(String, unique=True, nullable=True, index=True)
    bio = Column(Text, nullable=True, default="")
    avatar = Column(String, nullable=True)
    
    # Настройки уведомлений
    notification_sound = Column(String, nullable=True, default="default")
    
    # Звёзды (кликер)
    stars = Column(Float, nullable=True, default=0.0)


class Message(Base):
    __tablename__ = "messages"

    id = Column(Integer, primary_key=True, index=True)
    sender_id = Column(Integer, ForeignKey("users.id"), nullable=False)
    receiver_id = Column(Integer, ForeignKey("users.id"), nullable=False)

    message_type = Column(String, default="text", nullable=False)
    content = Column(Text, nullable=True)

    file_name = Column(String, nullable=True)
    file_path = Column(String, nullable=True)

    created_at = Column(DateTime(timezone=True), default=datetime.utcnow, nullable=False)
    is_read = Column(Boolean, default=False, nullable=False)
    is_delivered = Column(Boolean, default=False, nullable=False)

    edited_at = Column(DateTime(timezone=True), nullable=True)
    deleted_for_sender = Column(Boolean, default=False, nullable=False)
    deleted_for_receiver = Column(Boolean, default=False, nullable=False)
    reply_to_id = Column(Integer, ForeignKey("messages.id"), nullable=True)


class ChatPin(Base):
    __tablename__ = "chat_pins"
    __table_args__ = (UniqueConstraint("user_low_id", "user_high_id", name="uq_chat_pin_pair"),)

    id = Column(Integer, primary_key=True, index=True)
    user_low_id = Column(Integer, ForeignKey("users.id"), nullable=False)
    user_high_id = Column(Integer, ForeignKey("users.id"), nullable=False)
    message_id = Column(Integer, ForeignKey("messages.id"), nullable=False)


class PushSubscription(Base):
    __tablename__ = "push_subscriptions"

    id = Column(Integer, primary_key=True, index=True)
    user_id = Column(Integer, ForeignKey("users.id"), nullable=False, index=True)

    endpoint = Column(Text, nullable=False, unique=True)
    p256dh = Column(Text, nullable=False)
    auth = Column(Text, nullable=False)


class UserSession(Base):
    __tablename__ = "user_sessions"

    id = Column(Integer, primary_key=True, index=True)
    user_id = Column(Integer, ForeignKey("users.id"), nullable=False, index=True)
    session_token = Column(String(64), unique=True, nullable=False, index=True)
    device_name = Column(String(128), nullable=False, default="Устройство")
    user_agent = Column(Text, nullable=True)
    ip_address = Column(String(64), nullable=True)
    last_activity = Column(DateTime(timezone=True), default=datetime.utcnow, nullable=True)
    created_at = Column(DateTime(timezone=True), default=datetime.utcnow, nullable=True)
