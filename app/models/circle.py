from datetime import datetime, timezone

from sqlalchemy import Boolean, DateTime, ForeignKey, Integer, String, Text, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.database import Base


class Circle(Base):
    __tablename__ = "circles"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, index=True)
    name: Mapped[str] = mapped_column(String(200), nullable=False)
    description: Mapped[str | None] = mapped_column(Text, nullable=True)
    website: Mapped[str | None] = mapped_column(String(200), nullable=True)
    logo_url: Mapped[str | None] = mapped_column(String, nullable=True)
    created_by: Mapped[int] = mapped_column(Integer, ForeignKey("users.id"), nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime, default=lambda: datetime.now(timezone.utc), nullable=False
    )

    created_by_user = relationship("User", foreign_keys=[created_by])
    members = relationship("CircleMember", back_populates="circle", cascade="all, delete-orphan")
    invite_codes = relationship("CircleInviteCode", back_populates="circle", cascade="all, delete-orphan")


class CircleMember(Base):
    __tablename__ = "circle_members"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, index=True)
    circle_id: Mapped[int] = mapped_column(Integer, ForeignKey("circles.id"), nullable=False, index=True)
    user_id: Mapped[int] = mapped_column(Integer, ForeignKey("users.id"), nullable=False, index=True)
    role: Mapped[str] = mapped_column(String(30), nullable=False, default="member")
    joined_at: Mapped[datetime] = mapped_column(
        DateTime, default=lambda: datetime.now(timezone.utc), nullable=False
    )

    __table_args__ = (UniqueConstraint("circle_id", "user_id", name="uq_circle_member"),)

    circle = relationship("Circle", back_populates="members")
    user = relationship("User", foreign_keys=[user_id])


class CircleInviteCode(Base):
    __tablename__ = "circle_invite_codes"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, index=True)
    circle_id: Mapped[int] = mapped_column(Integer, ForeignKey("circles.id"), nullable=False)
    code: Mapped[str] = mapped_column(String(16), unique=True, index=True, nullable=False)
    role: Mapped[str] = mapped_column(String(30), nullable=False, default="member")
    created_by: Mapped[int] = mapped_column(Integer, ForeignKey("users.id"), nullable=False)
    expires_at: Mapped[datetime] = mapped_column(DateTime, nullable=False)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime, default=lambda: datetime.now(timezone.utc), nullable=False
    )

    circle = relationship("Circle", back_populates="invite_codes")
    created_by_user = relationship("User", foreign_keys=[created_by])
