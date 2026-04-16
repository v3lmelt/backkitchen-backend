from datetime import datetime, timezone

from sqlalchemy import DateTime, ForeignKey, Integer, String, Text
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.database import Base


class AdminAuditLog(Base):
    __tablename__ = "admin_audit_logs"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, index=True)
    actor_user_id: Mapped[int | None] = mapped_column(Integer, ForeignKey("users.id"), nullable=True, index=True)
    action: Mapped[str] = mapped_column(String(100), nullable=False, index=True)
    entity_type: Mapped[str] = mapped_column(String(50), nullable=False, index=True)
    entity_id: Mapped[int | None] = mapped_column(Integer, nullable=True, index=True)
    summary: Mapped[str | None] = mapped_column(String(500), nullable=True)
    reason: Mapped[str | None] = mapped_column(Text, nullable=True)
    before_state: Mapped[str | None] = mapped_column(Text, nullable=True)
    after_state: Mapped[str | None] = mapped_column(Text, nullable=True)
    target_user_id: Mapped[int | None] = mapped_column(Integer, ForeignKey("users.id"), nullable=True, index=True)
    album_id: Mapped[int | None] = mapped_column(Integer, ForeignKey("albums.id"), nullable=True, index=True)
    track_id: Mapped[int | None] = mapped_column(Integer, ForeignKey("tracks.id"), nullable=True, index=True)
    circle_id: Mapped[int | None] = mapped_column(Integer, ForeignKey("circles.id"), nullable=True, index=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime, default=lambda: datetime.now(timezone.utc), nullable=False, index=True
    )

    actor = relationship("User", foreign_keys=[actor_user_id])
    target_user = relationship("User", foreign_keys=[target_user_id])
