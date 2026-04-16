from datetime import datetime, timezone

from sqlalchemy import Boolean, DateTime, Integer, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from app.database import Base


class User(Base):
    __tablename__ = "users"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, index=True)
    username: Mapped[str] = mapped_column(String(50), unique=True, nullable=False, index=True)
    display_name: Mapped[str] = mapped_column(String(100), nullable=False)
    role: Mapped[str] = mapped_column(String(20), nullable=False, default="member")
    avatar_color: Mapped[str] = mapped_column(String(7), nullable=False, default="#6366f1")
    avatar_image: Mapped[str | None] = mapped_column(String(500), nullable=True, default=None)
    email: Mapped[str | None] = mapped_column(String(254), unique=True, nullable=True, index=True)
    password: Mapped[str | None] = mapped_column(String(255), nullable=True)
    email_verified: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    is_admin: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    admin_role: Mapped[str] = mapped_column(String(20), nullable=False, default="none", server_default="none")
    suspended_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True, default=None)
    suspension_reason: Mapped[str | None] = mapped_column(Text, nullable=True, default=None)
    session_version: Mapped[int] = mapped_column(Integer, nullable=False, default=1, server_default="1")
    created_at: Mapped[datetime] = mapped_column(
        DateTime, default=lambda: datetime.now(timezone.utc), nullable=False
    )
    feishu_contact: Mapped[str | None] = mapped_column(String(100), nullable=True, default=None)
    deleted_at: Mapped[datetime | None] = mapped_column(
        DateTime, nullable=True, default=None, index=True
    )

    def __repr__(self) -> str:
        return f"<User(id={self.id}, username='{self.username}', role='{self.role}')>"
