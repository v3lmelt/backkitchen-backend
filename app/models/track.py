import enum
from datetime import datetime, timezone

from sqlalchemy import DateTime, Enum, Float, ForeignKey, Integer, String
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.database import Base


class TrackStatus(str, enum.Enum):
    SUBMITTED = "submitted"
    PEER_REVIEW = "peer_review"
    PEER_REVISION = "peer_revision"
    PRODUCER_MASTERING_GATE = "producer_mastering_gate"
    MASTERING = "mastering"
    MASTERING_REVISION = "mastering_revision"
    FINAL_REVIEW = "final_review"
    COMPLETED = "completed"
    REJECTED = "rejected"


class RejectionMode(str, enum.Enum):
    FINAL = "final"
    RESUBMITTABLE = "resubmittable"


class Track(Base):
    __tablename__ = "tracks"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, index=True)
    title: Mapped[str] = mapped_column(String(200), nullable=False)
    artist: Mapped[str] = mapped_column(String(100), nullable=False)
    album_id: Mapped[int] = mapped_column(
        Integer, ForeignKey("albums.id"), nullable=False, index=True
    )
    submitter_id: Mapped[int | None] = mapped_column(
        Integer, ForeignKey("users.id"), nullable=True, index=True
    )
    peer_reviewer_id: Mapped[int | None] = mapped_column(
        Integer, ForeignKey("users.id"), nullable=True, index=True
    )
    file_path: Mapped[str | None] = mapped_column(String(500), nullable=True)
    storage_backend: Mapped[str] = mapped_column(String(10), nullable=False, default="local")
    duration: Mapped[float | None] = mapped_column(Float, nullable=True)
    bpm: Mapped[int | None] = mapped_column(Integer, nullable=True)
    track_number: Mapped[int | None] = mapped_column(Integer, nullable=True)
    status: Mapped[TrackStatus] = mapped_column(
        Enum(TrackStatus, values_callable=lambda items: [item.value for item in items]),
        nullable=False,
        default=TrackStatus.SUBMITTED,
    )
    rejection_mode: Mapped[RejectionMode | None] = mapped_column(
        Enum(RejectionMode, values_callable=lambda items: [item.value for item in items]),
        nullable=True,
    )
    version: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    workflow_cycle: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    created_at: Mapped[datetime] = mapped_column(
        DateTime, default=lambda: datetime.now(timezone.utc), nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime,
        default=lambda: datetime.now(timezone.utc),
        onupdate=lambda: datetime.now(timezone.utc),
        nullable=False,
    )

    album: Mapped["Album"] = relationship("Album", back_populates="tracks")  # noqa: F821
    submitter: Mapped["User | None"] = relationship(  # noqa: F821
        "User", foreign_keys=[submitter_id]
    )
    peer_reviewer: Mapped["User | None"] = relationship(  # noqa: F821
        "User", foreign_keys=[peer_reviewer_id]
    )
    issues: Mapped[list["Issue"]] = relationship(  # noqa: F821
        "Issue", back_populates="track", cascade="all, delete-orphan"
    )
    checklist_items: Mapped[list["ChecklistItem"]] = relationship(  # noqa: F821
        "ChecklistItem", back_populates="track", cascade="all, delete-orphan"
    )
    source_versions: Mapped[list["TrackSourceVersion"]] = relationship(  # noqa: F821
        "TrackSourceVersion",
        back_populates="track",
        cascade="all, delete-orphan",
        order_by="TrackSourceVersion.version_number",
    )
    master_deliveries: Mapped[list["MasterDelivery"]] = relationship(  # noqa: F821
        "MasterDelivery",
        back_populates="track",
        cascade="all, delete-orphan",
        order_by="MasterDelivery.delivery_number",
    )
    workflow_events: Mapped[list["WorkflowEvent"]] = relationship(  # noqa: F821
        "WorkflowEvent",
        back_populates="track",
        cascade="all, delete-orphan",
        order_by="WorkflowEvent.created_at",
    )
    discussions: Mapped[list["TrackDiscussion"]] = relationship(  # noqa: F821
        "TrackDiscussion",
        back_populates="track",
        cascade="all, delete-orphan",
        order_by="TrackDiscussion.created_at",
    )

    def __repr__(self) -> str:
        return f"<Track(id={self.id}, title='{self.title}', status='{self.status}')>"
