import json
from collections import defaultdict
from contextlib import asynccontextmanager
from datetime import datetime, timezone

from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles

from app.config import settings
from app.database import Base, SessionLocal, engine
from app.models.album import Album
from app.models.checklist import ChecklistItem
from app.models.comment import Comment
from app.models.comment_image import CommentImage
from app.models.issue import Issue, IssueSeverity, IssueStatus, IssueType
from app.models.track import Track, TrackStatus
from app.models.user import User
from app.routers import albums, auth, checklists, issues, tracks, users


class ConnectionManager:
    """Manages WebSocket connections grouped by track_id."""

    def __init__(self) -> None:
        self.active_connections: dict[int, list[WebSocket]] = defaultdict(list)

    async def connect(self, track_id: int, websocket: WebSocket) -> None:
        await websocket.accept()
        self.active_connections[track_id].append(websocket)

    def disconnect(self, track_id: int, websocket: WebSocket) -> None:
        self.active_connections[track_id].remove(websocket)
        if not self.active_connections[track_id]:
            del self.active_connections[track_id]

    async def broadcast(self, track_id: int, message: dict) -> None:
        payload = json.dumps(message)
        dead: list[WebSocket] = []
        for ws in self.active_connections.get(track_id, []):
            try:
                await ws.send_text(payload)
            except Exception:
                dead.append(ws)
        for ws in dead:
            self.disconnect(track_id, ws)


manager = ConnectionManager()


# ---------------------------------------------------------------------------
# Seed demo data
# ---------------------------------------------------------------------------
def _seed_demo_data() -> None:
    db = SessionLocal()
    try:
        # Only seed if database is empty
        if db.query(User).first() is not None:
            return

        now = datetime.now(timezone.utc)

        # --- Users ---
        kira = User(
            username="kira",
            display_name="Kira",
            role="producer",
            avatar_color="#f43f5e",
            created_at=now,
        )
        nova = User(
            username="nova",
            display_name="Nova",
            role="author",
            avatar_color="#3b82f6",
            created_at=now,
        )
        echo = User(
            username="echo",
            display_name="Echo",
            role="reviewer",
            avatar_color="#10b981",
            created_at=now,
        )
        db.add_all([kira, nova, echo])
        db.flush()

        # --- Album ---
        album = Album(
            title="BACK KITCHEN Vol.1",
            description="First compilation of the BACK KITCHEN doujin electronic music circle.",
            cover_color="#8b5cf6",
            created_at=now,
            updated_at=now,
        )
        db.add(album)
        db.flush()

        # --- Tracks ---
        track1 = Track(
            title="Neon Drizzle",
            artist="Kira",
            album_id=album.id,
            duration=245.0,
            bpm=128,
            status=TrackStatus.IN_REVIEW,
            version=1,
            created_at=now,
            updated_at=now,
        )
        track2 = Track(
            title="Phantom Signal",
            artist="Nova",
            album_id=album.id,
            duration=198.5,
            bpm=140,
            status=TrackStatus.REVISION,
            version=2,
            created_at=now,
            updated_at=now,
        )
        track3 = Track(
            title="Starlit Protocol",
            artist="Kira",
            album_id=album.id,
            duration=312.0,
            bpm=174,
            status=TrackStatus.APPROVED,
            version=1,
            created_at=now,
            updated_at=now,
        )
        db.add_all([track1, track2, track3])
        db.flush()

        # --- Issues for track2 (the one in revision) ---
        issue1 = Issue(
            track_id=track2.id,
            author_id=echo.id,
            title="Low-end too muddy around drop",
            description="The sub-bass and kick overlap between 1:20-1:35, causing muddiness.",
            issue_type=IssueType.RANGE,
            severity=IssueSeverity.MAJOR,
            status=IssueStatus.WILL_FIX,
            time_start=80.0,
            time_end=95.0,
            created_at=now,
            updated_at=now,
        )
        issue2 = Issue(
            track_id=track2.id,
            author_id=echo.id,
            title="Clipping on master at peak",
            description="There is audible clipping at 2:10. Reduce limiter ceiling.",
            issue_type=IssueType.POINT,
            severity=IssueSeverity.CRITICAL,
            status=IssueStatus.OPEN,
            time_start=130.0,
            time_end=None,
            created_at=now,
            updated_at=now,
        )
        db.add_all([issue1, issue2])
        db.flush()

        # --- Checklist for track3 (the approved one) ---
        checklist_labels = [
            "Mix Balance",
            "Low-End",
            "Stereo Image",
            "Loudness",
            "Format Compliance",
        ]
        for label in checklist_labels:
            ci = ChecklistItem(
                track_id=track3.id,
                reviewer_id=echo.id,
                label=label,
                passed=True,
                note=None,
                created_at=now,
            )
            db.add(ci)

        db.commit()
    except Exception:
        db.rollback()
        raise
    finally:
        db.close()


# ---------------------------------------------------------------------------
# App lifespan
# ---------------------------------------------------------------------------
@asynccontextmanager
async def lifespan(app: FastAPI):
    # Startup
    Base.metadata.create_all(bind=engine)
    upload_path = settings.get_upload_path()  # ensure uploads dir exists
    (upload_path / "comment_images").mkdir(parents=True, exist_ok=True)
    _seed_demo_data()
    yield
    # Shutdown (nothing to clean up)


# ---------------------------------------------------------------------------
# FastAPI application
# ---------------------------------------------------------------------------
app = FastAPI(
    title=settings.APP_NAME,
    version="1.0.0",
    lifespan=lifespan,
)

# CORS
app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.CORS_ORIGINS,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# Routers
app.include_router(auth.router)
app.include_router(users.router)
app.include_router(albums.router)
app.include_router(tracks.router)
app.include_router(issues.router)
app.include_router(checklists.router)

# Static file serving for uploads
try:
    upload_path = settings.get_upload_path()
    app.mount("/uploads", StaticFiles(directory=str(upload_path)), name="uploads")
except Exception:
    pass  # uploads dir will be created on startup anyway


# ---------------------------------------------------------------------------
# WebSocket endpoint
# ---------------------------------------------------------------------------
@app.websocket("/ws/tracks/{track_id}")
async def websocket_track(websocket: WebSocket, track_id: int) -> None:
    await manager.connect(track_id, websocket)
    try:
        while True:
            data = await websocket.receive_text()
            # Parse and re-broadcast the message to all connected clients
            try:
                message = json.loads(data)
            except json.JSONDecodeError:
                message = {"type": "message", "content": data}

            # Add metadata
            message["track_id"] = track_id
            await manager.broadcast(track_id, message)
    except WebSocketDisconnect:
        manager.disconnect(track_id, websocket)


# ---------------------------------------------------------------------------
# Health check
# ---------------------------------------------------------------------------
@app.get("/api/health")
def health_check() -> dict[str, str]:
    return {"status": "ok"}
