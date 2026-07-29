import json
import re
from datetime import datetime, timedelta
from pathlib import Path

from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from sqlalchemy import Boolean, Column, DateTime, Integer, String, Text, create_engine
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import declarative_base, sessionmaker

BASE_DIR = Path(__file__).resolve().parent
DATABASE_URL = f"sqlite:///{BASE_DIR / 'chat.db'}"
VOICE_EXPIRY = timedelta(hours=1.5)
VOICE_DAILY_LIMIT = 5
BAN_DURATION = timedelta(hours=24)

# Keep this list editable for your community rules.
ABUSIVE_WORDS = {"abuseword1", "abuseword2"}

app = FastAPI(title="Wechat Real-Time Chat")
app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_methods=["*"], allow_headers=["*"])
app.mount("/static", StaticFiles(directory=BASE_DIR / "static"), name="static")

engine = create_engine(DATABASE_URL, connect_args={"check_same_thread": False})
SessionLocal = sessionmaker(bind=engine, autoflush=False, autocommit=False)
Base = declarative_base()


class ChatMessage(Base):
    __tablename__ = "messages"

    id = Column(Integer, primary_key=True, index=True)
    username = Column(String(128), nullable=False)
    content = Column(Text, nullable=False)
    message_type = Column(String(20), default="text", nullable=False)
    deleted = Column(Boolean, default=False, nullable=False)
    expires_at = Column(DateTime, nullable=True)
    timestamp = Column(DateTime, default=datetime.utcnow)


class UserModeration(Base):
    __tablename__ = "user_moderation"

    username = Column(String(128), primary_key=True)
    warnings = Column(Integer, default=0, nullable=False)
    banned_until = Column(DateTime, nullable=True)


Base.metadata.create_all(bind=engine)


class ConnectionManager:
    def __init__(self):
        self.active_connections: list[WebSocket] = []

    async def connect(self, websocket: WebSocket):
        await websocket.accept()
        self.active_connections.append(websocket)

    def disconnect(self, websocket: WebSocket):
        if websocket in self.active_connections:
            self.active_connections.remove(websocket)

    async def broadcast(self, message: dict):
        disconnected = []
        for connection in list(self.active_connections):
            try:
                await connection.send_text(json.dumps(message))
            except Exception:
                disconnected.append(connection)
        for connection in disconnected:
            self.disconnect(connection)


manager = ConnectionManager()


def serialize_message(msg):
    expired = msg.expires_at is not None and msg.expires_at <= datetime.utcnow()
    return {
        "id": msg.id,
        "username": msg.username,
        "content": "" if msg.deleted or expired else msg.content,
        "type": "voice" if msg.message_type == "voice" else "message",
        "message_type": msg.message_type,
        "deleted": bool(msg.deleted),
        "expired": expired,
        "timestamp": msg.timestamp.isoformat(),
        "expires_at": msg.expires_at.isoformat() if msg.expires_at else None,
    }


def load_message_history(session):
    now = datetime.utcnow()
    messages = (
        session.query(ChatMessage)
        .order_by(ChatMessage.timestamp.asc())
        .limit(200)
        .all()
    )
    return {"type": "history", "messages": [serialize_message(m) for m in messages]}


def contains_abuse(content: str) -> bool:
    words = set(re.findall(r"\b[\w']+\b", content.lower()))
    return bool(words & ABUSIVE_WORDS)


def moderation_status(session, username):
    record = session.get(UserModeration, username)
    if record and record.banned_until and record.banned_until > datetime.utcnow():
        return record.banned_until
    return None


def register_abuse(session, username):
    record = session.get(UserModeration, username)
    if not record:
        record = UserModeration(username=username, warnings=0)
        session.add(record)
    record.warnings += 1
    if record.warnings >= 2:
        record.banned_until = datetime.utcnow() + BAN_DURATION
        record.warnings = 0
        session.commit()
        return record.banned_until
    session.commit()
    return None


def voice_count(session, username):
    since = datetime.utcnow() - timedelta(hours=24)
    return session.query(ChatMessage).filter(
        ChatMessage.username == username,
        ChatMessage.message_type == "voice",
        ChatMessage.timestamp >= since,
    ).count()


@app.get("/")
async def root():
    return FileResponse(BASE_DIR / "static" / "index.html")


@app.websocket("/ws")
async def websocket_endpoint(websocket: WebSocket):
    await manager.connect(websocket)
    session = SessionLocal()
    try:
        await websocket.send_text(json.dumps(load_message_history(session)))

        while True:
            raw_data = await websocket.receive_text()
            try:
                payload = json.loads(raw_data)
                action = payload.get("type")
                username = str(payload.get("username", "Anonymous")).strip()[:128] or "Anonymous"

                if action == "delete":
                    message_id = payload.get("id")
                    message = session.get(ChatMessage, message_id)
                    if message and message.username == username and not message.deleted:
                        message.deleted = True
                        session.commit()
                        await manager.broadcast({
                            "type": "delete",
                            "id": message.id,
                            "username": username,
                        })
                    continue

                if action not in {"message", "voice"}:
                    continue

                banned_until = moderation_status(session, username)
                if banned_until:
                    await websocket.send_text(json.dumps({
                        "type": "ban",
                        "banned_until": banned_until.isoformat(),
                        "message": "You are banned for 24 hours. You can view chat but cannot send messages.",
                    }))
                    continue

                content = str(payload.get("content", "")).strip()
                if not content:
                    continue

                if action == "message" and contains_abuse(content):
                    banned_until = register_abuse(session, username)
                    if banned_until:
                        await websocket.send_text(json.dumps({
                            "type": "ban",
                            "banned_until": banned_until.isoformat(),
                            "message": "You are banned for 24 hours. You can view chat but cannot send messages.",
                        }))
                    else:
                        await websocket.send_text(json.dumps({
                            "type": "warning",
                            "message": "Don't use these words. If you use abusive words again, you will be banned for 24 hours.",
                        }))
                    continue

                if action == "voice":
                    if voice_count(session, username) >= VOICE_DAILY_LIMIT:
                        await websocket.send_text(json.dumps({
                            "type": "voice_limit",
                            "message": "You can share only 5 voice messages in 24 hours.",
                        }))
                        continue
                    expires_at = datetime.utcnow() + VOICE_EXPIRY
                    message_type = "voice"
                else:
                    expires_at = None
                    message_type = "text"

                message = ChatMessage(
                    username=username,
                    content=content,
                    message_type=message_type,
                    expires_at=expires_at,
                    timestamp=datetime.utcnow(),
                )
                session.add(message)
                session.commit()
                session.refresh(message)
                await manager.broadcast(serialize_message(message))

            except json.JSONDecodeError:
                continue
            except SQLAlchemyError:
                session.rollback()
                continue
    except WebSocketDisconnect:
        manager.disconnect(websocket)
    except Exception:
        manager.disconnect(websocket)
    finally:
        session.close()
