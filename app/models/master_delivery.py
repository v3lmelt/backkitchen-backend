from datetime import datetime, timezone

from sqlalchemy import DateTime, ForeignKey, Integer, String
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.database import Base


class MasterDelivery(Base):
    __tablename__ = "master_deliveries"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, index=True)
    track_id: Mapped[int] = mapped_column(
        Integer, ForeignKey("tracks.id"), nullable=False, index=True
    )
    workflow_cycle: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    delivery_number: Mapped[int] = mapped_column(Integer, nullable=False)
    file_path: Mapped[str] = mapped_column(String(500), nullable=False)
    storage_backend: Mapped[str] = mapped_column(String(10), nullable=False, default="local")
    uploaded_by_id: Mapped[int | None] = mapped_column(
        Integer, ForeignKey("users.id"), nullable=True, index=True
    )
    producer_approved_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    submitter_approved_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime, default=lambda: datetime.now(timezone.utc), nullable=False
    )

    track: Mapped["Track"] = relationship("Track", back_populates="master_deliveries")  # noqa: F821
    uploaded_by: Mapped["User | None"] = relationship("User")  # noqa: F821
