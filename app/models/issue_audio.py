from datetime import datetime, timezone

from sqlalchemy import DateTime, Float, ForeignKey, Integer, String
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.database import Base


class IssueAudio(Base):
    __tablename__ = "issue_audios"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, index=True)
    issue_id: Mapped[int] = mapped_column(Integer, ForeignKey("issues.id", ondelete="CASCADE"), nullable=False, index=True)
    file_path: Mapped[str] = mapped_column(String(500), nullable=False)
    storage_backend: Mapped[str] = mapped_column(String(10), nullable=False, default="local")
    original_filename: Mapped[str] = mapped_column(String(255), nullable=False)
    duration: Mapped[float | None] = mapped_column(Float, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime, default=lambda: datetime.now(timezone.utc), nullable=False
    )

    issue: Mapped["Issue"] = relationship("Issue", back_populates="audios")  # noqa: F821

    def __repr__(self) -> str:
        return f"<IssueAudio(id={self.id}, issue_id={self.issue_id})>"
