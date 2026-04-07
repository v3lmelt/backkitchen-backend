from datetime import datetime, timezone

from sqlalchemy import DateTime, Float, ForeignKey, Integer, String
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.database import Base


class CommentAudio(Base):
    __tablename__ = "comment_audios"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, index=True)
    comment_id: Mapped[int] = mapped_column(Integer, ForeignKey("comments.id"), nullable=False, index=True)
    file_path: Mapped[str] = mapped_column(String(500), nullable=False)
    storage_backend: Mapped[str] = mapped_column(String(10), nullable=False, default="local")
    original_filename: Mapped[str] = mapped_column(String(255), nullable=False)
    duration: Mapped[float | None] = mapped_column(Float, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime, default=lambda: datetime.now(timezone.utc), nullable=False
    )

    comment: Mapped["Comment"] = relationship("Comment", back_populates="audios")  # noqa: F821

    def __repr__(self) -> str:
        return f"<CommentAudio(id={self.id}, comment_id={self.comment_id})>"
