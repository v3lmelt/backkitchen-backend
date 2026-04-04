from datetime import datetime, timezone

from sqlalchemy import DateTime, ForeignKey, Integer, String, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.database import Base


class Invitation(Base):
    __tablename__ = "invitations"
    __table_args__ = (UniqueConstraint("album_id", "user_id", name="uq_invitation_album_user"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True, index=True)
    album_id: Mapped[int] = mapped_column(Integer, ForeignKey("albums.id"), nullable=False, index=True)
    user_id: Mapped[int] = mapped_column(Integer, ForeignKey("users.id"), nullable=False, index=True)
    invited_by_user_id: Mapped[int] = mapped_column(Integer, ForeignKey("users.id"), nullable=False)
    status: Mapped[str] = mapped_column(String(20), nullable=False, default="pending")
    created_at: Mapped[datetime] = mapped_column(
        DateTime, default=lambda: datetime.now(timezone.utc), nullable=False
    )

    album: Mapped["Album"] = relationship("Album")  # noqa: F821
    user: Mapped["User"] = relationship("User", foreign_keys=[user_id])  # noqa: F821
    invited_by_user: Mapped["User"] = relationship("User", foreign_keys=[invited_by_user_id])  # noqa: F821
