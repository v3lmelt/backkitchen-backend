import enum
from datetime import datetime, timezone

from sqlalchemy import DateTime, Enum, Float, ForeignKey, Integer, String
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.database import Base


class TrackStatus(str, enum.Enum):
    SUBMITTED = "submitted"
    IN_REVIEW = "in_review"
    REVISION = "revision"
    APPROVED = "approved"


class Track(Base):
    __tablename__ = "tracks"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, index=True)
    title: Mapped[str] = mapped_column(String(200), nullable=False)
    artist: Mapped[str] = mapped_column(String(100), nullable=False)
    album_id: Mapped[int] = mapped_column(Integer, ForeignKey("albums.id"), nullable=False, index=True)
    file_path: Mapped[str | None] = mapped_column(String(500), nullable=True)
    duration: Mapped[float | None] = mapped_column(Float, nullable=True)
    bpm: Mapped[int | None] = mapped_column(Integer, nullable=True)
    status: Mapped[TrackStatus] = mapped_column(
        Enum(TrackStatus), nullable=False, default=TrackStatus.SUBMITTED
    )
    version: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
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
    issues: Mapped[list["Issue"]] = relationship(  # noqa: F821
        "Issue", back_populates="track", cascade="all, delete-orphan"
    )
    checklist_items: Mapped[list["ChecklistItem"]] = relationship(  # noqa: F821
        "ChecklistItem", back_populates="track", cascade="all, delete-orphan"
    )

    def __repr__(self) -> str:
        return f"<Track(id={self.id}, title='{self.title}', status='{self.status}')>"
