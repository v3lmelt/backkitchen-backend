from datetime import datetime, timezone

from sqlalchemy import DateTime, Float, ForeignKey, Integer, String, Text
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.database import Base


class TrackSourceVersion(Base):
    __tablename__ = "track_source_versions"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, index=True)
    track_id: Mapped[int] = mapped_column(
        Integer, ForeignKey("tracks.id"), nullable=False, index=True
    )
    workflow_cycle: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    version_number: Mapped[int] = mapped_column(Integer, nullable=False)
    file_path: Mapped[str] = mapped_column(String(500), nullable=False)
    storage_backend: Mapped[str] = mapped_column(String(10), nullable=False, default="local")
    duration: Mapped[float | None] = mapped_column(Float, nullable=True)
    uploaded_by_id: Mapped[int | None] = mapped_column(
        Integer, ForeignKey("users.id"), nullable=True, index=True
    )
    revision_notes: Mapped[str | None] = mapped_column(Text, nullable=True)
    expires_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime, default=lambda: datetime.now(timezone.utc), nullable=False
    )

    track: Mapped["Track"] = relationship("Track", back_populates="source_versions")  # noqa: F821
    uploaded_by: Mapped["User | None"] = relationship("User")  # noqa: F821
