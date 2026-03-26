from sqlalchemy import Column, Integer, String, ForeignKey, Text, DateTime, Boolean, Float
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


class PushSubscription(Base):
    __tablename__ = "push_subscriptions"

    id = Column(Integer, primary_key=True, index=True)
    user_id = Column(Integer, ForeignKey("users.id"), nullable=False, index=True)

    endpoint = Column(Text, nullable=False, unique=True)
    p256dh = Column(Text, nullable=False)
    auth = Column(Text, nullable=False)
