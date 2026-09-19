import enum
from datetime import datetime, timezone

from sqlalchemy import DateTime, ForeignKey, Integer, String, Text
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.database import Base


class PremasterHandoffMode(str, enum.Enum):
    CURRENT_SOURCE = "current_source"
    UPLOAD_REQUEST = "upload_request"


class PremasterHandoffStatus(str, enum.Enum):
    PENDING = "pending"
    REVALIDATING = "revalidating"
    CONFIRMED = "confirmed"
    CANCELLED = "cancelled"
    SUPERSEDED = "superseded"
    INVALIDATED = "invalidated"


class PremasterHandoff(Base):
    __tablename__ = "premaster_handoffs"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, index=True)
    track_id: Mapped[int] = mapped_column(
        Integer,
        ForeignKey("tracks.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    workflow_cycle: Mapped[int] = mapped_column(Integer, nullable=False, index=True)
    source_version_id: Mapped[int | None] = mapped_column(
        Integer,
        ForeignKey("track_source_versions.id", ondelete="SET NULL"),
        nullable=True,
        index=True,
    )
    requested_by_id: Mapped[int] = mapped_column(
        Integer,
        ForeignKey("users.id"),
        nullable=False,
        index=True,
    )
    cancelled_by_id: Mapped[int | None] = mapped_column(
        Integer,
        ForeignKey("users.id"),
        nullable=True,
        index=True,
    )
    mode: Mapped[str] = mapped_column(String(24), nullable=False)
    status: Mapped[str] = mapped_column(String(24), nullable=False, index=True)
    request_note: Mapped[str | None] = mapped_column(Text, nullable=True)
    validation_error: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime,
        default=lambda: datetime.now(timezone.utc),
        nullable=False,
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime,
        default=lambda: datetime.now(timezone.utc),
        onupdate=lambda: datetime.now(timezone.utc),
        nullable=False,
    )
    confirmed_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    cancelled_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)

    track: Mapped["Track"] = relationship("Track", back_populates="premaster_handoffs")  # noqa: F821
    source_version: Mapped["TrackSourceVersion | None"] = relationship(  # noqa: F821
        "TrackSourceVersion",
        foreign_keys=[source_version_id],
    )
    requested_by: Mapped["User"] = relationship("User", foreign_keys=[requested_by_id])  # noqa: F821
    cancelled_by: Mapped["User | None"] = relationship("User", foreign_keys=[cancelled_by_id])  # noqa: F821
