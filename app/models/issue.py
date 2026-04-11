import enum
from datetime import datetime, timezone

from sqlalchemy import DateTime, Enum, Float, ForeignKey, Integer, String, Text
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.database import Base


class MarkerType(str, enum.Enum):
    POINT = "point"
    RANGE = "range"


class IssueSeverity(str, enum.Enum):
    CRITICAL = "critical"
    MAJOR = "major"
    MINOR = "minor"
    SUGGESTION = "suggestion"


class IssueStatus(str, enum.Enum):
    OPEN = "open"
    PENDING_DISCUSSION = "pending_discussion"
    DISAGREED = "disagreed"
    RESOLVED = "resolved"


class IssuePhase(str, enum.Enum):
    PEER = "peer"
    PRODUCER = "producer"
    MASTERING = "mastering"
    FINAL_REVIEW = "final_review"


class Issue(Base):
    __tablename__ = "issues"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, index=True)
    track_id: Mapped[int] = mapped_column(
        Integer, ForeignKey("tracks.id"), nullable=False, index=True
    )
    author_id: Mapped[int] = mapped_column(Integer, ForeignKey("users.id"), nullable=False, index=True)
    phase: Mapped[str] = mapped_column(
        String(50),
        nullable=False,
        default=IssuePhase.PEER.value,
    )
    workflow_cycle: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    source_version_id: Mapped[int | None] = mapped_column(
        Integer, ForeignKey("track_source_versions.id"), nullable=True, index=True
    )
    master_delivery_id: Mapped[int | None] = mapped_column(
        Integer, ForeignKey("master_deliveries.id"), nullable=True, index=True
    )
    title: Mapped[str] = mapped_column(String(200), nullable=False)
    description: Mapped[str] = mapped_column(Text, nullable=False)
    severity: Mapped[IssueSeverity] = mapped_column(
        Enum(IssueSeverity, values_callable=lambda items: [item.value for item in items]),
        nullable=False,
        default=IssueSeverity.MAJOR,
    )
    status: Mapped[IssueStatus] = mapped_column(
        Enum(IssueStatus, values_callable=lambda items: [item.value for item in items]),
        nullable=False,
        default=IssueStatus.OPEN,
        index=True,
    )
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
    markers: Mapped[list["IssueMarker"]] = relationship(
        "IssueMarker", back_populates="issue", cascade="all, delete-orphan",
        order_by="IssueMarker.id",
    )
    audios: Mapped[list["IssueAudio"]] = relationship(  # noqa: F821
        "IssueAudio", back_populates="issue", cascade="all, delete-orphan",
        order_by="IssueAudio.id",
    )

    def __repr__(self) -> str:
        return f"<Issue(id={self.id}, title='{self.title}', status='{self.status}')>"


class IssueMarker(Base):
    __tablename__ = "issue_markers"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, index=True)
    issue_id: Mapped[int] = mapped_column(
        Integer, ForeignKey("issues.id", ondelete="CASCADE"), nullable=False, index=True
    )
    marker_type: Mapped[MarkerType] = mapped_column(
        Enum(MarkerType, values_callable=lambda items: [item.value for item in items]),
        nullable=False,
        default=MarkerType.POINT,
    )
    time_start: Mapped[float] = mapped_column(Float, nullable=False)
    time_end: Mapped[float | None] = mapped_column(Float, nullable=True)

    issue: Mapped["Issue"] = relationship("Issue", back_populates="markers")

    def __repr__(self) -> str:
        return f"<IssueMarker(id={self.id}, issue_id={self.issue_id}, type='{self.marker_type}', start={self.time_start})>"
