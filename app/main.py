import asyncio
import json
import logging
from contextlib import asynccontextmanager

from alembic import command as alembic_command
from alembic.config import Config as AlembicConfig
from fastapi import FastAPI, HTTPException, Request, WebSocket, WebSocketDisconnect, status
from app.ws_manager import manager as track_manager
from app.ws_manager import notification_manager
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles
from sqlalchemy import inspect, select

from app.config import settings
from app.database import Base, SessionLocal, engine
from app.models import (  # noqa: F401
    Album,
    AlbumMember,
    AdminAuditLog,
    ChecklistItem,
    Circle,
    CircleInviteCode,
    CircleMember,
    Comment,
    CommentImage,
    EmailVerificationToken,
    Invitation,
    Issue,
    IssuePhase,
    IssueSeverity,
    IssueStatus,
    MasterDelivery,
    Notification,
    RejectionMode,
    Track,
    TrackComposer,
    TrackExternalComposer,
    TrackPlaybackPreference,
    TrackSourceVersion,
    TrackStatus,
    User,
)
from app.routers import admin as admin_router
from app.routers import albums, auth, checklists, circles, discussions, issues, invitations, notifications, tracks, users, workflow, workflow_templates
from app.security import _decode_token, _resolve_websocket_user
from app.services.maintenance import _backfill_workflow_data, _periodic_cleanup, _seed_demo_data
from app.track_permissions import ensure_track_visibility

logger = logging.getLogger(__name__)
ws_logger = logging.getLogger("app.websocket")


async def _close_websocket(websocket: WebSocket, code: int) -> None:
    try:
        await websocket.close(code=code)
    except RuntimeError:
        # The connection may already have been closed by the ASGI server.
        return


async def _authorize_track_websocket(websocket: WebSocket, track_id: int, token: str | None) -> int | None:
    if token is None:
        ws_logger.info("track_ws_rejected track_id=%s reason=missing_token", track_id)
        await _close_websocket(websocket, 4001)
        return None
    try:
        payload = _decode_token(token)
    except HTTPException:
        ws_logger.info("track_ws_rejected track_id=%s reason=invalid_token", track_id)
        await _close_websocket(websocket, 4001)
        return None

    user_id: int | None = None
    db = None
    try:
        db = SessionLocal()
        user_id, user, reason = _resolve_websocket_user(db, payload)
        if user is None:
            ws_logger.info("track_ws_rejected track_id=%s user_id=%s reason=%s", track_id, user_id, reason)
            await _close_websocket(websocket, 4001)
            return None

        track = db.get(Track, track_id)
        if track is None:
            ws_logger.info("track_ws_rejected track_id=%s user_id=%s reason=missing_track", track_id, user_id)
            await _close_websocket(websocket, 4001)
            return None

        try:
            ensure_track_visibility(track, user, db)
        except HTTPException as exc:
            close_code = 4003 if exc.status_code == status.HTTP_403_FORBIDDEN else 4001
            reason = "forbidden" if close_code == 4003 else "inaccessible_track"
            ws_logger.info("track_ws_rejected track_id=%s user_id=%s reason=%s", track_id, user_id, reason)
            await _close_websocket(websocket, close_code)
            return None
        return user_id
    except Exception:
        ws_logger.exception("track_ws_rejected track_id=%s user_id=%s reason=unexpected_error", track_id, user_id)
        await _close_websocket(websocket, 1011)
        return None
    finally:
        if db is not None:
            db.close()


async def _authorize_notification_websocket(websocket: WebSocket, token: str | None) -> int | None:
    if token is None:
        ws_logger.info("notification_ws_rejected reason=missing_token")
        await _close_websocket(websocket, 4001)
        return None
    try:
        payload = _decode_token(token)
    except HTTPException:
        ws_logger.info("notification_ws_rejected reason=invalid_token")
        await _close_websocket(websocket, 4001)
        return None

    user_id: int | None = None
    db = None
    try:
        db = SessionLocal()
        user_id, user, reason = _resolve_websocket_user(db, payload)
        if user is None:
            ws_logger.info("notification_ws_rejected user_id=%s reason=%s", user_id, reason)
            await _close_websocket(websocket, 4001)
            return None
        return user_id
    except Exception:
        ws_logger.exception("notification_ws_rejected user_id=%s reason=unexpected_error", user_id)
        await _close_websocket(websocket, 1011)
        return None
    finally:
        if db is not None:
            db.close()


def _run_alembic_upgrade() -> None:
    """Run alembic upgrade head to apply pending migrations."""
    import os

    alembic_cfg = AlembicConfig(os.path.join(os.path.dirname(__file__), "..", "alembic.ini"))
    alembic_cfg.set_main_option("script_location", os.path.join(os.path.dirname(__file__), "..", "alembic"))

    inspector = inspect(engine)
    user_tables = {
        table_name
        for table_name in inspector.get_table_names()
        if table_name != "alembic_version"
    }
    if not user_tables:
        Base.metadata.create_all(bind=engine)
        alembic_command.stamp(alembic_cfg, "head")
        return

    alembic_command.upgrade(alembic_cfg, "head")


@asynccontextmanager
async def lifespan(app: FastAPI):
    _run_alembic_upgrade()
    _backfill_workflow_data()
    upload_path = settings.get_upload_path()
    (upload_path / "comment_images").mkdir(parents=True, exist_ok=True)
    (upload_path / "covers").mkdir(parents=True, exist_ok=True)
    if settings.SEED_DEMO_DATA:
        _seed_demo_data()
    if settings.INITIAL_ADMIN_EMAIL:
        db = SessionLocal()
        try:
            user = db.scalars(select(User).where(User.email == settings.INITIAL_ADMIN_EMAIL)).first()
            if user and not user.is_admin:
                user.is_admin = True
                db.commit()
        finally:
            db.close()
    cleanup_task = asyncio.create_task(_periodic_cleanup())
    try:
        yield
    finally:
        cleanup_task.cancel()
        try:
            await cleanup_task
        except asyncio.CancelledError:
            pass


app = FastAPI(title=settings.APP_NAME, version="2.0.0", lifespan=lifespan)

app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.CORS_ORIGINS,
    allow_credentials=True,
    allow_methods=["GET", "POST", "PUT", "PATCH", "DELETE", "OPTIONS"],
    allow_headers=["*"],
)


# Catch any non-HTTPException thrown from a handler, log the full traceback,
# and return a structured JSON 500.  Starlette's default ServerErrorMiddleware
# returns a bare "Internal Server Error" plain-text body that is easy to miss
# in logs; this handler guarantees the stack trace lands in journald so we
# can diagnose intermittent failures (e.g. SQLite lock contention).
_unhandled_logger = logging.getLogger("app.unhandled")


@app.exception_handler(Exception)
async def _unhandled_exception_handler(request: Request, exc: Exception) -> JSONResponse:
    _unhandled_logger.exception(
        "Unhandled exception on %s %s",
        request.method,
        request.url.path,
    )
    return JSONResponse(
        status_code=500,
        content={"detail": "Internal server error.", "path": request.url.path},
    )


app.include_router(auth.router)
app.include_router(users.router)
app.include_router(admin_router.router)
app.include_router(circles.router)
app.include_router(albums.router)
app.include_router(tracks.router)
app.include_router(issues.router)
app.include_router(checklists.router)
app.include_router(invitations.router)
app.include_router(notifications.router)
app.include_router(discussions.router)
app.include_router(workflow_templates.router)
app.include_router(workflow.router)

try:
    upload_path = settings.get_upload_path()
    app.mount("/uploads", StaticFiles(directory=str(upload_path)), name="uploads")
except Exception:
    logging.getLogger(__name__).warning(
        "Failed to mount /uploads static files — uploaded audio will be inaccessible",
        exc_info=True,
    )


@app.get("/api/health")
def healthcheck() -> dict[str, str]:
    return {"status": "ok"}


@app.get("/api/config")
def app_config():
    return {"r2_enabled": settings.R2_ENABLED}


@app.websocket("/ws/tracks/{track_id}")
async def websocket_track(websocket: WebSocket, track_id: int, token: str | None = None) -> None:
    user_id = await _authorize_track_websocket(websocket, track_id, token)
    if user_id is None:
        return

    connected = await track_manager.connect(track_id, websocket)
    if not connected:
        ws_logger.warning("track_ws_rejected track_id=%s user_id=%s reason=capacity", track_id, user_id)
        return
    ws_logger.info("track_ws_connected track_id=%s user_id=%s", track_id, user_id)
    try:
        while True:
            data = await websocket.receive_text()
            try:
                message = json.loads(data)
            except json.JSONDecodeError:
                message = {"type": "message", "content": data}
            message["track_id"] = track_id
            await track_manager.broadcast(track_id, message)
    except WebSocketDisconnect:
        track_manager.disconnect(track_id, websocket)
        ws_logger.info("track_ws_disconnected track_id=%s user_id=%s", track_id, user_id)


@app.websocket("/ws/notifications")
async def websocket_notifications(websocket: WebSocket, token: str | None = None) -> None:
    user_id = await _authorize_notification_websocket(websocket, token)
    if user_id is None:
        return

    connected = await notification_manager.connect(user_id, websocket)
    if not connected:
        ws_logger.warning("notification_ws_rejected user_id=%s reason=capacity", user_id)
        return
    ws_logger.info("notification_ws_connected user_id=%s", user_id)
    try:
        while True:
            await websocket.receive_text()
    except WebSocketDisconnect:
        notification_manager.disconnect(user_id, websocket)
        ws_logger.info("notification_ws_disconnected user_id=%s", user_id)
