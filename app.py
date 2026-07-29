import json
from datetime import datetime, timedelta
from pathlib import Path
 from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from sqlalchemy import Boolean, Column, DateTime, Integer, String, Text,
create_engine
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
 engine = create_engine(DATABASE_URL, connect_args={"check_same_thread":
False})
SessionLocal = sessionmaker(bind=engine, autoflush=False, autocommit=False)
Base = declarative_base()
 VOICE_EXPIRY = timedelta(hours=1.5)
VOICE_LIMIT = 5
VOICE_WINDOW = timedelta(hours=24)
  class ChatMessage(Base):
__tablename__ = "messages"
  id = Column(Integer, primary_key=True, index=True)
username = Column(String(128), nullable=False)
content = Column(Text, nullable=False)message_type = Column(String(20), nullable=False, default="text")
timestamp = Column(DateTime, default=datetime.utcnow)
expires_at = Column(DateTime, nullable=True)
deleted = Column(Boolean, nullable=False, default=False)
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
encoded = json.dumps(message)
for connection in list(self.active_connections):
try:
await connection.send_text(encoded)
except Exception:
disconnected.append(connection)
for connection in disconnected:
self.disconnect(connection)
  manager = ConnectionManager()
  def message_to_dict(message: ChatMessage):
expired = (
message.message_type == "voice"
and message.expires_at is not None
and message.expires_at <= datetime.utcnow()
)return {
"type": "message",
"id": message.id,
"username": message.username,
"content": "" if message.deleted or expired else message.content,
"message_type": message.message_type,
"timestamp": message.timestamp.isoformat(),
"expires_at": message.expires_at.isoformat() if message.expires_at else None,
"deleted": bool(message.deleted),
"expired": expired,
}
  def load_message_history(session):
messages = (
session.query(ChatMessage)
.order_by(ChatMessage.timestamp.asc())
.limit(200)
.all()
)
return {"type": "history", "messages": [message_to_dict(msg) for msg in
messages]}
  def save_message(session, username, content, message_type):
now = datetime.utcnow()
expires_at = now + VOICE_EXPIRY if message_type == "voice" else None
message = ChatMessage(
username=username,
content=content,
message_type=message_type,
timestamp=now,
expires_at=expires_at,
)
session.add(message)
session.commit()
session.refresh(message)
return message
  def voice_count_last_24h(session, username):
cutoff = datetime.utcnow() - VOICE_WINDOWreturn (
session.query(ChatMessage)
.filter(
ChatMessage.username == username,
ChatMessage.message_type == "voice",
ChatMessage.timestamp >= cutoff,
)
.count()
)
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
except json.JSONDecodeError:
continue
  action = payload.get("type")
username = str(payload.get("username", "Anonymous")).strip()[:128] or
"Anonymous"
  try:
if action in ("message", "text"):
content = str(payload.get("content", "")).strip()
if not content:
continue
      saved = save_message(session, username, content, "text")
await manager.broadcast(message_to_dict(saved))
elif action in ("voice", "voice_message"):
# Accept common frontend field names so the existing UI works.
content = (
payload.get("content")
or payload.get("audio")
or payload.get("audioData")
or payload.get("data")
or ""
)
content = str(content)
if not content:
await websocket.send_text(json.dumps({
"type": "error",
"code": "VOICE_EMPTY",
"message": "Voice audio was empty.",
}))
continue
if voice_count_last_24h(session, username) >= VOICE_LIMIT:
await websocket.send_text(json.dumps({
"type": "error",
"code": "VOICE_LIMIT",
"message": "You can send only 5 voice messages in 24 hours.",
}))
continue
saved = save_message(session, username, content, "voice")
await manager.broadcast(message_to_dict(saved))
elif action in ("delete", "delete_message"):
message_id = payload.get("id") or payload.get("message_id")
if not message_id:
continue
message = session.get(ChatMessage, int(message_id))
if message is None or message.username != username:
continue
   message.deleted = True
message.content = ""
session.commit()
await manager.broadcast({
"type": "message_deleted",
"id": message.id,
"message_id": message.id,
"username": username,
})
except (ValueError, TypeError):
continue
except SQLAlchemyError:
session.rollback()
await websocket.send_text(json.dumps({
"type": "error",
"code": "DATABASE_ERROR",
"message": "The message could not be saved.",
}))
except WebSocketDisconnect:
manager.disconnect(websocket)
except Exception:
manager.disconnect(websocket)
finally:
session.close()
