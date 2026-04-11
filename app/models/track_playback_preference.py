from datetime import datetime, timezone

from sqlalchemy import DateTime, Float, ForeignKey, Integer, String, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from app.database import Base


class TrackPlaybackPreference(Base):
    __tablename__ = "track_playback_preferences"
    __table_args__ = (
        UniqueConstraint("track_id", "user_id", "scope", name="uq_track_playback_preferences_track_user_scope"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, index=True)
    track_id: Mapped[int] = mapped_column(
        Integer, ForeignKey("tracks.id", ondelete="CASCADE"), nullable=False, index=True
    )
    user_id: Mapped[int] = mapped_column(
        Integer, ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True
    )
    scope: Mapped[str] = mapped_column(String(32), nullable=False)
    gain_db: Mapped[float] = mapped_column(Float, nullable=False, default=0.0)
    created_at: Mapped[datetime] = mapped_column(
        DateTime, default=lambda: datetime.now(timezone.utc), nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime,
        default=lambda: datetime.now(timezone.utc),
        onupdate=lambda: datetime.now(timezone.utc),
        nullable=False,
    )
