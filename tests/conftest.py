import copy
import json
import sys
from collections.abc import Callable, Iterator
from pathlib import Path
from types import SimpleNamespace

import pytest
from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import Session, sessionmaker

BACKEND_ROOT = Path(__file__).resolve().parents[1]
if str(BACKEND_ROOT) not in sys.path:
    sys.path.insert(0, str(BACKEND_ROOT))

from app.config import settings
from app.database import Base, get_db
from app.models.album import Album
from app.models.album_member import AlbumMember
from app.models.checklist import ChecklistItem
from app.models.issue import Issue, IssueMarker, IssuePhase, IssueSeverity, IssueStatus, MarkerType
from app.models.master_delivery import MasterDelivery
from app.models.track import RejectionMode, Track
from app.models.track_source_version import TrackSourceVersion
from app.models.user import User
from app.models.invitation import Invitation
from app.models.notification import Notification
from app.routers import admin, albums, auth, checklists, circles, discussions, invitations, issues, notifications, tracks, users
from app.security import create_access_token
from app.workflow_defaults import DEFAULT_WORKFLOW_CONFIG


@pytest.fixture
def db_engine(tmp_path: Path):
    db_path = tmp_path / "test.db"
    engine = create_engine(
        f"sqlite:///{db_path}",
        connect_args={"check_same_thread": False},
        echo=False,
    )
    Base.metadata.create_all(bind=engine)
    yield engine
    Base.metadata.drop_all(bind=engine)
    engine.dispose()


@pytest.fixture
def session_factory(db_engine):
    return sessionmaker(autocommit=False, autoflush=False, bind=db_engine)


@pytest.fixture
def db_session(session_factory) -> Iterator[Session]:
    session = session_factory()
    try:
        yield session
    finally:
        session.close()


@pytest.fixture
def upload_dir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    path = tmp_path / "uploads"
    monkeypatch.setattr(settings, "UPLOAD_DIR", str(path))
    path.mkdir(parents=True, exist_ok=True)
    return path


@pytest.fixture
def client(
    session_factory,
    upload_dir: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> Iterator[TestClient]:
    monkeypatch.setattr(
        tracks,
        "extract_audio_metadata",
        lambda _path: SimpleNamespace(duration=123.4, bitrate=None, sample_rate=None),
    )
    monkeypatch.setattr(auth, "send_verification_email", lambda *_args, **_kwargs: None)

    def override_get_db() -> Iterator[Session]:
        session = session_factory()
        try:
            yield session
        finally:
            session.close()

    app = FastAPI()
    app.include_router(auth.router)
    app.include_router(users.router)
    app.include_router(albums.router)
    app.include_router(tracks.router)
    app.include_router(issues.router)
    app.include_router(checklists.router)
    app.include_router(invitations.router)
    app.include_router(notifications.router)
    app.include_router(admin.router)
    app.include_router(circles.router)
    app.include_router(discussions.router)
    app.dependency_overrides[get_db] = override_get_db
    app.mount("/uploads", StaticFiles(directory=str(upload_dir)), name="uploads")

    with TestClient(app) as test_client:
        yield test_client


class Factory:
    def __init__(self, session: Session, upload_dir: Path):
        self.session = session
        self.upload_dir = upload_dir
        self._counter = 0

    def _next(self, prefix: str) -> str:
        self._counter += 1
        return f"{prefix}-{self._counter}"

    def _audio_file(self, stem: str | None = None, ext: str = ".wav") -> str:
        filename = f"{stem or self._next('audio')}{ext}"
        path = self.upload_dir / filename
        path.write_bytes(b"RIFFdemo")
        return str(path)

    def user(
        self,
        *,
        role: str = "member",
        username: str | None = None,
        display_name: str | None = None,
        email: str | None = None,
        is_admin: bool = False,
        email_verified: bool = True,
    ) -> User:
        effective_role = "member" if role == "mastering_engineer" else role
        key = username or self._next(role)
        user = User(
            username=key,
            display_name=display_name or key.title(),
            email=email or f"{key}@example.com",
            role=effective_role,
            avatar_color="#123456",
            password="pw",
            is_admin=is_admin,
            email_verified=email_verified,
        )
        self.session.add(user)
        self.session.commit()
        self.session.refresh(user)
        return user

    def album(
        self,
        *,
        producer: User,
        mastering_engineer: User,
        members: list[User] | None = None,
        title: str | None = None,
        workflow_config: dict | None = None,
    ) -> Album:
        effective_workflow = (
            workflow_config
            if workflow_config is not None
            else copy.deepcopy(DEFAULT_WORKFLOW_CONFIG)
        )
        album = Album(
            title=title or self._next("album"),
            description="test album",
            cover_color="#abcdef",
            producer_id=producer.id,
            mastering_engineer_id=mastering_engineer.id,
            workflow_config=json.dumps(effective_workflow, ensure_ascii=False),
        )
        self.session.add(album)
        self.session.commit()
        self.session.refresh(album)

        for user in members or []:
            self.session.add(AlbumMember(album_id=album.id, user_id=user.id))
        self.session.commit()
        self.session.refresh(album)
        return album

    def track(
        self,
        *,
        album: Album,
        submitter: User,
        status: str = "intake",
        peer_reviewer: User | None = None,
        rejection_mode: RejectionMode | None = None,
        version: int = 1,
        workflow_cycle: int = 1,
        create_source_version: bool = True,
        file_path: str | None = None,
    ) -> Track:
        audio_path = file_path or self._audio_file()
        track = Track(
            title=self._next("track"),
            artist=submitter.display_name,
            album_id=album.id,
            submitter_id=submitter.id,
            peer_reviewer_id=peer_reviewer.id if peer_reviewer else None,
            file_path=audio_path,
            duration=100.0,
            bpm=174,
            status=status,
            rejection_mode=rejection_mode,
            version=version,
            workflow_cycle=workflow_cycle,
        )
        self.session.add(track)
        self.session.commit()
        self.session.refresh(track)

        if create_source_version and audio_path:
            self.source_version(track=track, uploaded_by=submitter, version_number=version, workflow_cycle=workflow_cycle, file_path=audio_path)

        self.session.refresh(track)
        return track

    def source_version(
        self,
        *,
        track: Track,
        uploaded_by: User,
        version_number: int | None = None,
        workflow_cycle: int | None = None,
        file_path: str | None = None,
    ) -> TrackSourceVersion:
        version = TrackSourceVersion(
            track_id=track.id,
            workflow_cycle=workflow_cycle or track.workflow_cycle,
            version_number=version_number or track.version,
            file_path=file_path or self._audio_file(),
            duration=track.duration,
            uploaded_by_id=uploaded_by.id,
        )
        self.session.add(version)
        self.session.commit()
        self.session.refresh(version)
        self.session.refresh(track)
        return version

    def master_delivery(
        self,
        *,
        track: Track,
        uploaded_by: User,
        delivery_number: int = 1,
        workflow_cycle: int | None = None,
        file_path: str | None = None,
    ) -> MasterDelivery:
        delivery = MasterDelivery(
            track_id=track.id,
            workflow_cycle=workflow_cycle or track.workflow_cycle,
            delivery_number=delivery_number,
            file_path=file_path or self._audio_file(stem=self._next("master"), ext=".mp3"),
            uploaded_by_id=uploaded_by.id,
        )
        self.session.add(delivery)
        self.session.commit()
        self.session.refresh(delivery)
        self.session.refresh(track)
        return delivery

    def issue(
        self,
        *,
        track: Track,
        author: User,
        phase: IssuePhase,
        status: IssueStatus = IssueStatus.OPEN,
        source_version_id: int | None = None,
        master_delivery_id: int | None = None,
        workflow_cycle: int | None = None,
        marker_type: MarkerType = MarkerType.POINT,
    ) -> Issue:
        markers = [
            IssueMarker(
                marker_type=marker_type,
                time_start=12.3,
                time_end=18.0 if marker_type == MarkerType.RANGE else None,
            )
        ]
        from app.workflow import next_issue_local_number

        issue = Issue(
            track_id=track.id,
            local_number=next_issue_local_number(self.session, track.id),
            author_id=author.id,
            phase=phase,
            workflow_cycle=workflow_cycle or track.workflow_cycle,
            source_version_id=source_version_id,
            master_delivery_id=master_delivery_id,
            title=self._next("issue"),
            description="issue description",
            severity=IssueSeverity.MAJOR,
            status=status,
            markers=markers,
        )
        self.session.add(issue)
        self.session.commit()
        self.session.refresh(issue)
        return issue

    def notification(
        self,
        *,
        user: User,
        type: str = "track_status_changed",
        title: str = "Test notification",
        body: str = "Test body",
        is_read: bool = False,
        related_track_id: int | None = None,
        related_issue_id: int | None = None,
    ) -> Notification:
        notif = Notification(
            user_id=user.id,
            type=type,
            title=title,
            body=body,
            is_read=is_read,
            related_track_id=related_track_id,
            related_issue_id=related_issue_id,
        )
        self.session.add(notif)
        self.session.commit()
        self.session.refresh(notif)
        return notif

    def invitation(
        self,
        *,
        album: Album,
        user: User,
        invited_by: User,
        invitation_status: str = "pending",
    ) -> Invitation:
        inv = Invitation(
            album_id=album.id,
            user_id=user.id,
            invited_by_user_id=invited_by.id,
            status=invitation_status,
        )
        self.session.add(inv)
        self.session.commit()
        self.session.refresh(inv)
        return inv

    def checklist(
        self,
        *,
        track: Track,
        reviewer: User,
        source_version_id: int,
        label: str = "Balance",
        passed: bool = True,
        note: str | None = None,
    ) -> ChecklistItem:
        item = ChecklistItem(
            track_id=track.id,
            reviewer_id=reviewer.id,
            source_version_id=source_version_id,
            workflow_cycle=track.workflow_cycle,
            label=label,
            passed=passed,
            note=note,
        )
        self.session.add(item)
        self.session.commit()
        self.session.refresh(item)
        return item


@pytest.fixture
def factory(db_session: Session, upload_dir: Path) -> Factory:
    return Factory(db_session, upload_dir)


@pytest.fixture
def auth_headers() -> Callable[[User], dict[str, str]]:
    def make_headers(user: User) -> dict[str, str]:
        return {"Authorization": f"Bearer {create_access_token(user)}"}

    return make_headers
