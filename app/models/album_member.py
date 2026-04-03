from datetime import datetime, timezone

from sqlalchemy import DateTime, ForeignKey, Integer, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.database import Base


class AlbumMember(Base):
    __tablename__ = "album_members"
    __table_args__ = (UniqueConstraint("album_id", "user_id", name="uq_album_member"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True, index=True)
    album_id: Mapped[int] = mapped_column(
        Integer, ForeignKey("albums.id"), nullable=False, index=True
    )
    user_id: Mapped[int] = mapped_column(
        Integer, ForeignKey("users.id"), nullable=False, index=True
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime, default=lambda: datetime.now(timezone.utc), nullable=False
    )

    album: Mapped["Album"] = relationship("Album", back_populates="members")  # noqa: F821
    user: Mapped["User"] = relationship("User")  # noqa: F821
