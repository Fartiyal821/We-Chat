"""
Wechat Real-Time Chat Application
Backend WebSocket server with message persistence, user presence, and typing indicators.
"""

import json
from datetime import datetime
from pathlib import Path

from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from sqlalchemy import Column, DateTime, Integer, String, Text, create_engine, func
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import declarative_base, sessionmaker

# ==================== CONFIGURATION ====================
BASE_DIR = Path(__file__).resolve().parent
DATABASE_URL = f"sqlite:///{BASE_DIR / 'chat.db'}"

# FastAPI setup
app = FastAPI(title="Wechat Real-Time Chat")
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)
app.mount("/static", StaticFiles(directory=BASE_DIR / "static"), name="static")

# Database setup
engine = create_engine(DATABASE_URL, connect_args={"check_same_thread": False})
SessionLocal = sessionmaker(bind=engine, autoflush=False, autocommit=False)
Base = declarative_base()


# ==================== DATABASE MODELS ====================
class ChatMessage(Base):
    """Chat message stored in database."""
    __tablename__ = "messages"

    id = Column(Integer, primary_key=True, index=True)
    username = Column(String(128), nullable=False)
    content = Column(Text, nullable=False)
    timestamp = Column(DateTime, default=datetime.utcnow)


Base.metadata.create_all(bind=engine)


# ==================== CONNECTION MANAGER ====================
class ConnectionManager:
    """Manages active WebSocket connections and broadcasts messages."""

    def __init__(self):
        """Map WebSocket -> username (None until user joins)."""
        self.active_connections: dict[WebSocket, str | None] = {}

    async def connect(self, websocket: WebSocket):
        """Accept and register a new WebSocket connection."""
        await websocket.accept()
        self.active_connections[websocket] = None

    def disconnect(self, websocket: WebSocket):
        """Remove a disconnected WebSocket."""
        if websocket in self.active_connections:
            del self.active_connections[websocket]

    async def broadcast(self, message: dict):
        """Send message to all connected clients; remove failed connections."""
        disconnected = []
        for connection in list(self.active_connections.keys()):
            try:
                await connection.send_text(json.dumps(message))
            except Exception:
                disconnected.append(connection)

        for connection in disconnected:
            self.disconnect(connection)

    def set_username(self, websocket: WebSocket, username: str | None):
        """Set or update username for a connection."""
        if websocket in self.active_connections:
            self.active_connections[websocket] = username

    def get_user_list(self) -> list[str]:
        """Get sorted list of unique online usernames."""
        return sorted([u for u in set(self.active_connections.values() or []) if u])


manager = ConnectionManager()


# ==================== DATABASE HELPERS ====================
def get_db_session():
    """Create and yield a database session."""
    session = SessionLocal()
    try:
        yield session
    finally:
        session.close()


def load_message_history(session) -> list[dict]:
    """Load recent chat messages from database (limit: 200)."""
    messages = session.query(ChatMessage).order_by(ChatMessage.timestamp.asc()).limit(200).all()
    return [
        {
            "type": "history",
            "messages": [
                {
                    "id": msg.id,
                    "username": msg.username,
                    "content": msg.content,
                    "timestamp": msg.timestamp.isoformat(),
                }
                for msg in messages
            ],
        }
    ]


def save_message(session, username: str, content: str) -> ChatMessage:
    """Save a new chat message to database."""
    message = ChatMessage(username=username, content=content, timestamp=datetime.utcnow())
    session.add(message)
    session.commit()
    session.refresh(message)
    return message


# ==================== ROUTES ====================
@app.get("/")
async def root():
    """Serve the main HTML page."""
    return FileResponse(BASE_DIR / "static" / "index.html")


@app.get("/stats")
async def stats():
    """Return server statistics for debugging and monitoring."""
    session = SessionLocal()
    try:
        message_count = session.query(func.count(ChatMessage.id)).scalar() or 0
        online_users = manager.get_user_list()
        return {
            "status": "ok",
            "server_time": datetime.utcnow().isoformat() + "Z",
            "online_count": len(online_users),
            "online_users": online_users,
            "total_messages": message_count,
        }
    finally:
        session.close()


# ==================== WEBSOCKET ENDPOINT ====================
@app.websocket("/ws")
async def websocket_endpoint(websocket: WebSocket):
    """
    Main WebSocket handler for real-time chat, presence, typing, and call signaling.

    Supported message types:
      - message: Save and broadcast chat message
      - join: Register user presence in online list
      - typing: Broadcast typing indicator (start/stop)
      - delete: Delete a message (authorized by sender only)
      - webrtc_*: Forward WebRTC signaling for video/audio calls
    """
    await manager.connect(websocket)
    session = SessionLocal()

    try:
        # Send message history to new client
        history_payload = load_message_history(session)
        for payload in history_payload:
            await websocket.send_text(json.dumps(payload))

        # ========== Main Message Loop ==========
        while True:
            raw_data = await websocket.receive_text()
            try:
                payload = json.loads(raw_data)
                msg_type = payload.get("type")

                # ===== Chat Message =====
                if msg_type == "message":
                    username = payload.get("username", "Anonymous").strip() or "Anonymous"
                    content = payload.get("content", "").strip()

                    if not content:
                        continue

                    # Save and broadcast
                    saved_message = save_message(session, username=username, content=content)
                    message_data = {
                        "type": "message",
                        "id": saved_message.id,
                        "username": saved_message.username,
                        "content": saved_message.content,
                        "timestamp": saved_message.timestamp.isoformat(),
                    }
                    await manager.broadcast(message_data)

                # ===== User Presence (Join) =====
                elif msg_type == "join":
                    username = (payload.get("username") or "").strip() or None
                    manager.set_username(websocket, username)
                    # Broadcast updated online list to all clients
                    await manager.broadcast({"type": "presence", "users": manager.get_user_list()})

                # ===== Typing Indicator =====
                elif msg_type == "typing":
                    # Forward typing state (start/stop) to all clients
                    await manager.broadcast(payload)

                # ===== Message Deletion =====
                elif msg_type == "delete":
                    msg_id = payload.get("id")
                    req_username = payload.get("username")

                    if msg_id and req_username:
                        # Find message in database
                        msg = session.query(ChatMessage).filter(ChatMessage.id == msg_id).first()

                        # Only owner can delete
                        if msg and msg.username == req_username:
                            session.delete(msg)
                            session.commit()

                            # Broadcast deletion to all clients
                            await manager.broadcast({"type": "delete", "id": msg_id})

                # ===== Other (WebRTC Signaling, etc.) =====
                else:
                    # Broadcast any unhandled message type (calls, signaling)
                    await manager.broadcast(payload)

            except json.JSONDecodeError:
                # Skip malformed JSON
                continue
            except SQLAlchemyError:
                # Rollback on database error
                session.rollback()
                continue

    except WebSocketDisconnect:
        # Client disconnected
        manager.disconnect(websocket)
        try:
            # Notify others of updated presence
            await manager.broadcast({"type": "presence", "users": manager.get_user_list()})
        except Exception:
            pass

    except Exception:
        # Unexpected error
        manager.disconnect(websocket)
        try:
            await manager.broadcast({"type": "presence", "users": manager.get_user_list()})
        except Exception:
            pass

    finally:
        session.close()
