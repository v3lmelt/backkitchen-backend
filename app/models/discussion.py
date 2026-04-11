from datetime import datetime, timezone

from sqlalchemy import DateTime, ForeignKey, Integer, String, Text
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.database import Base


class TrackDiscussion(Base):
    __tablename__ = "track_discussions"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, index=True)
    track_id: Mapped[int] = mapped_column(
        Integer, ForeignKey("tracks.id"), nullable=False, index=True
    )
    author_id: Mapped[int] = mapped_column(
        Integer, ForeignKey("users.id"), nullable=False
    )
    content: Mapped[str] = mapped_column(Text, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime, default=lambda: datetime.now(timezone.utc), nullable=False
    )
    edited_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True, default=None)

    track: Mapped["Track"] = relationship("Track", back_populates="discussions")  # noqa: F821
    author: Mapped["User"] = relationship("User", foreign_keys=[author_id])  # noqa: F821
    images: Mapped[list["TrackDiscussionImage"]] = relationship(
        "TrackDiscussionImage",
        back_populates="discussion",
        cascade="all, delete-orphan",
    )


class TrackDiscussionImage(Base):
    __tablename__ = "track_discussion_images"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, index=True)
    discussion_id: Mapped[int] = mapped_column(
        Integer, ForeignKey("track_discussions.id"), nullable=False, index=True
    )
    file_path: Mapped[str] = mapped_column(String(500), nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime, default=lambda: datetime.now(timezone.utc), nullable=False
    )

    discussion: Mapped["TrackDiscussion"] = relationship(
        "TrackDiscussion", back_populates="images"
    )
