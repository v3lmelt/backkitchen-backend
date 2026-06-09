from datetime import datetime, timezone

from sqlalchemy import DateTime, ForeignKey, Integer, String, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.database import Base


class TrackComposer(Base):
    __tablename__ = "track_composers"
    __table_args__ = (UniqueConstraint("track_id", "user_id", name="uq_track_composer"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True, index=True)
    track_id: Mapped[int] = mapped_column(
        Integer, ForeignKey("tracks.id", ondelete="CASCADE"), nullable=False, index=True
    )
    user_id: Mapped[int] = mapped_column(
        Integer, ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime, default=lambda: datetime.now(timezone.utc), nullable=False
    )

    track: Mapped["Track"] = relationship("Track", back_populates="composer_links")  # noqa: F821
    user: Mapped["User"] = relationship("User")  # noqa: F821


class TrackExternalComposer(Base):
    __tablename__ = "track_external_composers"
    __table_args__ = (UniqueConstraint("track_id", "name", name="uq_track_external_composer_name"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True, index=True)
    track_id: Mapped[int] = mapped_column(
        Integer, ForeignKey("tracks.id", ondelete="CASCADE"), nullable=False, index=True
    )
    name: Mapped[str] = mapped_column(String(100), nullable=False)
    sort_order: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    created_at: Mapped[datetime] = mapped_column(
        DateTime, default=lambda: datetime.now(timezone.utc), nullable=False
    )

    track: Mapped["Track"] = relationship("Track", back_populates="external_composer_links")  # noqa: F821
