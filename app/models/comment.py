from datetime import datetime, timezone

from sqlalchemy import DateTime, ForeignKey, Integer, Text
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.database import Base


class Comment(Base):
    __tablename__ = "comments"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, index=True)
    issue_id: Mapped[int] = mapped_column(Integer, ForeignKey("issues.id"), nullable=False, index=True)
    author_id: Mapped[int] = mapped_column(Integer, ForeignKey("users.id"), nullable=False)
    content: Mapped[str] = mapped_column(Text, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime, default=lambda: datetime.now(timezone.utc), nullable=False
    )

    issue: Mapped["Issue"] = relationship("Issue", back_populates="comments")  # noqa: F821

    def __repr__(self) -> str:
        return f"<Comment(id={self.id}, issue_id={self.issue_id})>"
