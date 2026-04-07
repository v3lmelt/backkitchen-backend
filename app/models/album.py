from datetime import date, datetime, timezone

from sqlalchemy import Date, DateTime, ForeignKey, Integer, String, Text
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.database import Base


class Album(Base):
    __tablename__ = "albums"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, index=True)
    title: Mapped[str] = mapped_column(String(200), nullable=False)
    description: Mapped[str | None] = mapped_column(Text, nullable=True)
    cover_color: Mapped[str] = mapped_column(String(7), nullable=False, default="#8b5cf6")
    producer_id: Mapped[int | None] = mapped_column(Integer, ForeignKey("users.id"), nullable=True)
    mastering_engineer_id: Mapped[int | None] = mapped_column(
        Integer, ForeignKey("users.id"), nullable=True
    )
    release_date: Mapped[date | None] = mapped_column(Date, nullable=True)
    catalog_number: Mapped[str | None] = mapped_column(String(50), nullable=True)
    circle_id: Mapped[int | None] = mapped_column(
        Integer, ForeignKey("circles.id"), nullable=True, index=True
    )
    circle_name: Mapped[str | None] = mapped_column(String(200), nullable=True)
    genres: Mapped[str | None] = mapped_column(Text, nullable=True)  # JSON array of strings
    cover_image: Mapped[str | None] = mapped_column(String(500), nullable=True)
    checklist_template: Mapped[str | None] = mapped_column(Text, nullable=True)
    deadline: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    phase_deadlines: Mapped[str | None] = mapped_column(Text, nullable=True)
    webhook_config: Mapped[str | None] = mapped_column(Text, nullable=True)
    workflow_config: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime, default=lambda: datetime.now(timezone.utc), nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime,
        default=lambda: datetime.now(timezone.utc),
        onupdate=lambda: datetime.now(timezone.utc),
        nullable=False,
    )

    tracks: Mapped[list["Track"]] = relationship(  # noqa: F821
        "Track", back_populates="album", cascade="all, delete-orphan"
    )
    producer: Mapped["User | None"] = relationship(  # noqa: F821
        "User", foreign_keys=[producer_id]
    )
    mastering_engineer: Mapped["User | None"] = relationship(  # noqa: F821
        "User", foreign_keys=[mastering_engineer_id]
    )
    members: Mapped[list["AlbumMember"]] = relationship(  # noqa: F821
        "AlbumMember", back_populates="album", cascade="all, delete-orphan"
    )
    circle: Mapped["Circle | None"] = relationship(  # noqa: F821
        "Circle", foreign_keys=[circle_id]
    )

    def __repr__(self) -> str:
        return f"<Album(id={self.id}, title='{self.title}')>"
