import json
import logging
from collections import defaultdict
from contextlib import asynccontextmanager
from datetime import datetime, timezone

from fastapi import FastAPI, HTTPException, WebSocket, WebSocketDisconnect
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles
from sqlalchemy import inspect, select, text

from app.config import settings
from app.database import Base, SessionLocal, engine
from app.models import (  # noqa: F401
    Album,
    AlbumMember,
    ChecklistItem,
    Comment,
    CommentImage,
    Invitation,
    Issue,
    IssuePhase,
    IssueSeverity,
    IssueStatus,
    IssueType,
    MasterDelivery,
    Notification,
    RejectionMode,
    Track,
    TrackSourceVersion,
    TrackStatus,
    User,
)
from app.routers import albums, auth, checklists, discussions, issues, invitations, notifications, tracks, users
from app.security import _decode_token, hash_password
from app.workflow import log_track_event


class ConnectionManager:
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
            except Exception as exc:  # noqa: BLE001
                logging.getLogger(__name__).warning("WebSocket send failed: %s", exc)
                dead.append(ws)
        for ws in dead:
            self.disconnect(track_id, ws)


manager = ConnectionManager()


def _run_sqlite_compat_migrations() -> None:
    if not settings.DATABASE_URL.startswith("sqlite"):
        return

    inspector = inspect(engine)
    columns_by_table = {
        table_name: {column["name"] for column in inspector.get_columns(table_name)}
        for table_name in inspector.get_table_names()
    }

    def add_column(table_name: str, column_name: str, definition: str) -> None:
        if table_name not in columns_by_table or column_name in columns_by_table[table_name]:
            return
        with engine.begin() as conn:
            conn.execute(text(f"ALTER TABLE {table_name} ADD COLUMN {definition}"))

    add_column("albums", "producer_id", "producer_id INTEGER")
    add_column("albums", "mastering_engineer_id", "mastering_engineer_id INTEGER")
    add_column("tracks", "submitter_id", "submitter_id INTEGER")
    add_column("tracks", "peer_reviewer_id", "peer_reviewer_id INTEGER")
    add_column("tracks", "rejection_mode", "rejection_mode VARCHAR(20)")
    add_column("tracks", "workflow_cycle", "workflow_cycle INTEGER NOT NULL DEFAULT 1")
    add_column("issues", "phase", "phase VARCHAR(20) NOT NULL DEFAULT 'peer'")
    add_column("issues", "workflow_cycle", "workflow_cycle INTEGER NOT NULL DEFAULT 1")
    add_column("issues", "source_version_id", "source_version_id INTEGER")
    add_column("issues", "master_delivery_id", "master_delivery_id INTEGER")
    add_column("checklist_items", "source_version_id", "source_version_id INTEGER")
    add_column("checklist_items", "workflow_cycle", "workflow_cycle INTEGER NOT NULL DEFAULT 1")
    add_column("comments", "is_status_note", "is_status_note BOOLEAN NOT NULL DEFAULT 0")
    add_column("tracks", "track_number", "track_number INTEGER")
    add_column("albums", "checklist_template", "checklist_template TEXT")
    add_column("albums", "deadline", "deadline DATETIME")
    add_column("albums", "phase_deadlines", "phase_deadlines TEXT")
    add_column("albums", "webhook_config", "webhook_config TEXT")
    add_column("albums", "release_date", "release_date DATE")
    add_column("albums", "catalog_number", "catalog_number VARCHAR(50)")
    add_column("albums", "circle_name", "circle_name VARCHAR(200)")
    add_column("albums", "genres", "genres TEXT")
    add_column("albums", "cover_image", "cover_image VARCHAR(500)")

    with engine.begin() as conn:
        if "users" in columns_by_table:
            conn.execute(text("UPDATE users SET role = 'member' WHERE lower(role) IN ('author', 'reviewer')"))

        if "tracks" in columns_by_table:
            conn.execute(text("UPDATE tracks SET status = 'submitted' WHERE lower(status) = 'submitted' OR status = 'SUBMITTED'"))
            conn.execute(text("UPDATE tracks SET status = 'peer_review' WHERE lower(status) = 'in_review' OR status = 'IN_REVIEW'"))
            conn.execute(text("UPDATE tracks SET status = 'peer_revision' WHERE lower(status) = 'revision' OR status = 'REVISION'"))
            conn.execute(text("UPDATE tracks SET status = 'completed' WHERE lower(status) = 'approved' OR status = 'APPROVED'"))

        if "issues" in columns_by_table:
            conn.execute(text("UPDATE issues SET issue_type = 'point' WHERE lower(issue_type) = 'point' OR issue_type = 'POINT'"))
            conn.execute(text("UPDATE issues SET issue_type = 'range' WHERE lower(issue_type) = 'range' OR issue_type = 'RANGE'"))
            conn.execute(text("UPDATE issues SET severity = 'critical' WHERE lower(severity) = 'critical' OR severity = 'CRITICAL'"))
            conn.execute(text("UPDATE issues SET severity = 'major' WHERE lower(severity) = 'major' OR severity = 'MAJOR'"))
            conn.execute(text("UPDATE issues SET severity = 'minor' WHERE lower(severity) = 'minor' OR severity = 'MINOR'"))
            conn.execute(text("UPDATE issues SET severity = 'suggestion' WHERE lower(severity) = 'suggestion' OR severity = 'SUGGESTION'"))
            conn.execute(text("UPDATE issues SET status = 'open' WHERE lower(status) = 'open' OR status = 'OPEN'"))
            conn.execute(text("UPDATE issues SET status = 'will_fix' WHERE lower(status) = 'will_fix' OR status = 'WILL_FIX'"))
            conn.execute(text("UPDATE issues SET status = 'disagreed' WHERE lower(status) = 'disagreed' OR status = 'DISAGREED'"))
            conn.execute(text("UPDATE issues SET status = 'resolved' WHERE lower(status) = 'resolved' OR status = 'RESOLVED'"))


def _backfill_workflow_data() -> None:
    db = SessionLocal()
    try:
        users = list(db.scalars(select(User).order_by(User.id)).all())
        if not users:
            return

        albums = list(db.scalars(select(Album).order_by(Album.id)).all())
        for album in albums:
            if album.producer_id is None:
                producer = next((user for user in users if user.role == "producer"), users[0])
                album.producer_id = producer.id
            if album.mastering_engineer_id is None:
                mastering_engineer = next(
                    (user for user in users if user.id != album.producer_id),
                    users[0],
                )
                album.mastering_engineer_id = mastering_engineer.id

            if not album.members:
                for user in users:
                    db.add(AlbumMember(album_id=album.id, user_id=user.id))

        tracks = list(db.scalars(select(Track).order_by(Track.id)).all())
        for track in tracks:
            album = db.get(Album, track.album_id)
            if album is None:
                continue

            if track.submitter_id is None:
                matching_user = next(
                    (
                        user
                        for user in users
                        if user.display_name.lower() == track.artist.lower()
                        or user.username.lower() == track.artist.lower()
                    ),
                    None,
                )
                fallback_user = next(
                    (user for user in users if user.id != album.producer_id),
                    users[0],
                )
                track.submitter_id = (matching_user or fallback_user).id

            if track.peer_reviewer_id is None:
                issue_author = next((issue.author_id for issue in track.issues if issue.author_id != track.submitter_id), None)
                checklist_reviewer = next((item.reviewer_id for item in track.checklist_items if item.reviewer_id != track.submitter_id), None)
                track.peer_reviewer_id = issue_author or checklist_reviewer

            for issue in track.issues:
                if not issue.workflow_cycle:
                    issue.workflow_cycle = track.workflow_cycle

            for item in track.checklist_items:
                if not item.workflow_cycle:
                    item.workflow_cycle = track.workflow_cycle

            if track.file_path and not track.source_versions:
                db.add(
                    TrackSourceVersion(
                        track_id=track.id,
                        workflow_cycle=track.workflow_cycle,
                        version_number=track.version,
                        file_path=track.file_path,
                        duration=track.duration,
                        uploaded_by_id=track.submitter_id,
                    )
                )

        db.commit()
    except Exception:
        db.rollback()
        raise
    finally:
        db.close()


def _seed_demo_data() -> None:
    db = SessionLocal()
    try:
        if db.query(User).first() is not None:
            return

        now = datetime.now(timezone.utc)
        producer = User(
            username="kira",
            display_name="Kira",
            role="producer",
            avatar_color="#f43f5e",
            email="kira@example.com",
            password=hash_password("password123"),
            created_at=now,
        )
        submitter = User(
            username="nova",
            display_name="Nova",
            role="member",
            avatar_color="#3b82f6",
            email="nova@example.com",
            password=hash_password("password123"),
            created_at=now,
        )
        mastering_engineer = User(
            username="echo",
            display_name="Echo",
            role="mastering_engineer",
            avatar_color="#10b981",
            email="echo@example.com",
            password=hash_password("password123"),
            created_at=now,
        )
        db.add_all([producer, submitter, mastering_engineer])
        db.flush()

        album = Album(
            title="BACK KITCHEN Vol.1",
            description="Demo workflow album for reviewing doujin submissions.",
            cover_color="#8b5cf6",
            producer_id=producer.id,
            mastering_engineer_id=mastering_engineer.id,
            created_at=now,
            updated_at=now,
        )
        db.add(album)
        db.flush()

        db.add_all(
            [
                AlbumMember(album_id=album.id, user_id=producer.id, created_at=now),
                AlbumMember(album_id=album.id, user_id=submitter.id, created_at=now),
                AlbumMember(album_id=album.id, user_id=mastering_engineer.id, created_at=now),
            ]
        )

        track = Track(
            title="Neon Drizzle",
            artist="Nova",
            album_id=album.id,
            submitter_id=submitter.id,
            status=TrackStatus.SUBMITTED,
            version=1,
            workflow_cycle=1,
            created_at=now,
            updated_at=now,
        )
        db.add(track)
        db.flush()
        log_track_event(db, track, submitter, "track_submitted", to_status=TrackStatus.SUBMITTED)

        db.commit()
    except Exception:
        db.rollback()
        raise
    finally:
        db.close()


@asynccontextmanager
async def lifespan(app: FastAPI):
    Base.metadata.create_all(bind=engine)
    _run_sqlite_compat_migrations()
    Base.metadata.create_all(bind=engine)
    _backfill_workflow_data()
    upload_path = settings.get_upload_path()
    (upload_path / "comment_images").mkdir(parents=True, exist_ok=True)
    (upload_path / "covers").mkdir(parents=True, exist_ok=True)
    if settings.SEED_DEMO_DATA:
        _seed_demo_data()
    yield


app = FastAPI(title=settings.APP_NAME, version="2.0.0", lifespan=lifespan)

app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.CORS_ORIGINS,
    allow_credentials=True,
    allow_methods=["GET", "POST", "PUT", "PATCH", "DELETE", "OPTIONS"],
    allow_headers=["*"],
)

app.include_router(auth.router)
app.include_router(users.router)
app.include_router(albums.router)
app.include_router(tracks.router)
app.include_router(issues.router)
app.include_router(checklists.router)
app.include_router(invitations.router)
app.include_router(notifications.router)
app.include_router(discussions.router)

try:
    upload_path = settings.get_upload_path()
    app.mount("/uploads", StaticFiles(directory=str(upload_path)), name="uploads")
except Exception:
    pass


@app.get("/api/health")
def healthcheck() -> dict[str, str]:
    return {"status": "ok"}


@app.websocket("/ws/tracks/{track_id}")
async def websocket_track(websocket: WebSocket, track_id: int, token: str | None = None) -> None:
    if token is None:
        await websocket.close(code=4001)
        return
    try:
        _decode_token(token)
    except HTTPException:
        await websocket.close(code=4001)
        return
    await manager.connect(track_id, websocket)
    try:
        while True:
            data = await websocket.receive_text()
            try:
                message = json.loads(data)
            except json.JSONDecodeError:
                message = {"type": "message", "content": data}
            message["track_id"] = track_id
            await manager.broadcast(track_id, message)
    except WebSocketDisconnect:
        manager.disconnect(track_id, websocket)
