from datetime import datetime, timezone

from sqlalchemy import DateTime, Float, ForeignKey, Integer, String, Text
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.database import Base


class SourceFollowupRequest(Base):
    __tablename__ = "source_followup_requests"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, index=True)
    track_id: Mapped[int] = mapped_column(
        Integer, ForeignKey("tracks.id", ondelete="CASCADE"), nullable=False, index=True
    )
    requested_by_id: Mapped[int] = mapped_column(
        Integer, ForeignKey("users.id"), nullable=False, index=True
    )
    decided_by_id: Mapped[int | None] = mapped_column(
        Integer, ForeignKey("users.id"), nullable=True, index=True
    )
    applied_source_version_id: Mapped[int | None] = mapped_column(
        Integer, ForeignKey("track_source_versions.id"), nullable=True
    )
    previous_status: Mapped[str] = mapped_column(String(50), nullable=False)
    target_stage_id: Mapped[str | None] = mapped_column(String(50), nullable=True)
    reason: Mapped[str] = mapped_column(Text, nullable=False)
    status: Mapped[str] = mapped_column(String(20), nullable=False, default="pending", index=True)
    staged_file_path: Mapped[str] = mapped_column(String(500), nullable=False)
    staged_storage_backend: Mapped[str] = mapped_column(String(10), nullable=False, default="local")
    staged_duration: Mapped[float | None] = mapped_column(Float, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime, default=lambda: datetime.now(timezone.utc), nullable=False
    )
    decided_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)

    track: Mapped["Track"] = relationship(  # noqa: F821
        "Track",
        back_populates="source_followup_requests",
        foreign_keys=[track_id],
    )
    requested_by: Mapped["User"] = relationship("User", foreign_keys=[requested_by_id])  # noqa: F821
    decided_by: Mapped["User | None"] = relationship("User", foreign_keys=[decided_by_id])  # noqa: F821
    applied_source_version: Mapped["TrackSourceVersion | None"] = relationship(  # noqa: F821
        "TrackSourceVersion",
        foreign_keys=[applied_source_version_id],
    )
