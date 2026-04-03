import enum
from datetime import datetime, timezone

from sqlalchemy import DateTime, Enum, Float, ForeignKey, Integer, String, Text
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.database import Base


class IssueType(str, enum.Enum):
    POINT = "point"
    RANGE = "range"


class IssueSeverity(str, enum.Enum):
    CRITICAL = "critical"
    MAJOR = "major"
    MINOR = "minor"
    SUGGESTION = "suggestion"


class IssueStatus(str, enum.Enum):
    OPEN = "open"
    WILL_FIX = "will_fix"
    DISAGREED = "disagreed"
    RESOLVED = "resolved"


class Issue(Base):
    __tablename__ = "issues"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, index=True)
    track_id: Mapped[int] = mapped_column(Integer, ForeignKey("tracks.id"), nullable=False, index=True)
    author_id: Mapped[int] = mapped_column(Integer, ForeignKey("users.id"), nullable=False)
    title: Mapped[str] = mapped_column(String(200), nullable=False)
    description: Mapped[str] = mapped_column(Text, nullable=False)
    issue_type: Mapped[IssueType] = mapped_column(
        Enum(IssueType), nullable=False, default=IssueType.POINT
    )
    severity: Mapped[IssueSeverity] = mapped_column(
        Enum(IssueSeverity), nullable=False, default=IssueSeverity.MAJOR
    )
    status: Mapped[IssueStatus] = mapped_column(
        Enum(IssueStatus), nullable=False, default=IssueStatus.OPEN
    )
    time_start: Mapped[float] = mapped_column(Float, nullable=False)
    time_end: Mapped[float | None] = mapped_column(Float, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime, default=lambda: datetime.now(timezone.utc), nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime,
        default=lambda: datetime.now(timezone.utc),
        onupdate=lambda: datetime.now(timezone.utc),
        nullable=False,
    )

    track: Mapped["Track"] = relationship("Track", back_populates="issues")  # noqa: F821
    comments: Mapped[list["Comment"]] = relationship(  # noqa: F821
        "Comment", back_populates="issue", cascade="all, delete-orphan"
    )

    def __repr__(self) -> str:
        return f"<Issue(id={self.id}, title='{self.title}', status='{self.status}')>"
