import json
from datetime import datetime
from pathlib import Path

from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from sqlalchemy import Column, DateTime, Integer, String, Text, create_engine
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import declarative_base, sessionmaker

BASE_DIR = Path(__file__).resolve().parent
DATABASE_URL = f"sqlite:///{BASE_DIR / 'chat.db'}"

app = FastAPI(title="Wechat Real-Time Chat")
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

app.mount("/static", StaticFiles(directory=BASE_DIR / "static"), name="static")

engine = create_engine(DATABASE_URL, connect_args={"check_same_thread": False})
SessionLocal = sessionmaker(bind=engine, autoflush=False, autocommit=False)
Base = declarative_base()


class ChatMessage(Base):
    __tablename__ = "messages"

    id = Column(Integer, primary_key=True, index=True)
    username = Column(String(128), nullable=False)
    content = Column(Text, nullable=False)
    timestamp = Column(DateTime, default=datetime.utcnow)


Base.metadata.create_all(bind=engine)


class ConnectionManager:
    def __init__(self):
        # map websocket -> username (username may be None until client sends join)
        self.active_connections: dict[WebSocket, str | None] = {}

    async def connect(self, websocket: WebSocket):
        await websocket.accept()
        self.active_connections[websocket] = None

    def disconnect(self, websocket: WebSocket):
        if websocket in self.active_connections:
            del self.active_connections[websocket]

    async def broadcast(self, message: dict):
        disconnected = []
        for connection in list(self.active_connections.keys()):
            try:
                await connection.send_text(json.dumps(message))
            except Exception:
                disconnected.append(connection)
        for connection in disconnected:
            self.disconnect(connection)

    def set_username(self, websocket: WebSocket, username: str | None):
        if websocket in self.active_connections:
            self.active_connections[websocket] = username

    def get_user_list(self) -> list[str]:
        return sorted([u for u in set(self.active_connections.values() or []) if u])


manager = ConnectionManager()


def get_db_session():
    session = SessionLocal()
    try:
        yield session
    finally:
        session.close()


def load_message_history(session):
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


def save_message(session, username: str, content: str):
    message = ChatMessage(username=username, content=content, timestamp=datetime.utcnow())
    session.add(message)
    session.commit()
    session.refresh(message)
    return message


@app.get("/")
async def root():
    return FileResponse(BASE_DIR / "static" / "index.html")


@app.websocket("/ws")
async def websocket_endpoint(websocket: WebSocket):
    await manager.connect(websocket)
    session = SessionLocal()
    try:
        history_payload = load_message_history(session)
        for payload in history_payload:
            await websocket.send_text(json.dumps(payload))

        while True:
            raw_data = await websocket.receive_text()
            try:
                payload = json.loads(raw_data)
                msg_type = payload.get("type")

                # Handle New Messages
                if msg_type == "message":
                    username = payload.get("username", "Anonymous").strip() or "Anonymous"
                    content = payload.get("content", "").strip()
                    if not content:
                        continue

                    saved_message = save_message(session, username=username, content=content)
                    message_data = {
                        "type": "message",
                        "id": saved_message.id, 
                        "username": saved_message.username,
                        "content": saved_message.content,
                        "timestamp": saved_message.timestamp.isoformat(),
                    }
                    await manager.broadcast(message_data)

                # Handle join (presence)
                elif msg_type == "join":
                    username = (payload.get("username") or "").strip() or None
                    manager.set_username(websocket, username)
                    # broadcast updated presence list
                    await manager.broadcast({"type": "presence", "users": manager.get_user_list()})

                # Typing indicator (forward to clients)
                elif msg_type == "typing":
                    # payload should include 'username' and optional 'to' and 'state' (start/stop)
                    await manager.broadcast(payload)

                # Handle Message Deletions
                elif msg_type == "delete":
                    msg_id = payload.get("id")
                    req_username = payload.get("username")
                    
                    if msg_id and req_username:
                        # Find the message in the database
                        msg = session.query(ChatMessage).filter(ChatMessage.id == msg_id).first()
                        
                        # Only allow deletion if the user requesting it is the one who sent it
                        if msg and msg.username == req_username:
                            session.delete(msg)
                            session.commit()
                            
                            # Broadcast the deletion instruction to all users globally
                            await manager.broadcast({
                                "type": "delete",
                                "id": msg_id
                            })
                
                # Forward any other payloads (signaling / call events) to connected clients
                else:
                    # For real-time features like WebRTC signaling, simply broadcast the payload
                    # Clients are expected to filter by username/target as needed.
                    await manager.broadcast(payload)
            except json.JSONDecodeError:
                continue
            except SQLAlchemyError:
                session.rollback()
                continue
    except WebSocketDisconnect:
        manager.disconnect(websocket)
        # broadcast updated presence
        try:
            await manager.broadcast({"type": "presence", "users": manager.get_user_list()})
        except Exception:
            pass
    except Exception:
        manager.disconnect(websocket)
        try:
            await manager.broadcast({"type": "presence", "users": manager.get_user_list()})
        except Exception:
            pass
    finally:
        session.close()
