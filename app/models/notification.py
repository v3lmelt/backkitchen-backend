from sqlalchemy import Boolean, Column, DateTime, ForeignKey, Integer, String, Text, func
from sqlalchemy.orm import relationship

from app.database import Base


class Notification(Base):
    __tablename__ = "notifications"

    id = Column(Integer, primary_key=True, index=True)
    user_id = Column(Integer, ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True)
    type = Column(String(50), nullable=False)
    title = Column(String(200), nullable=False)
    body = Column(Text, nullable=False)
    related_track_id = Column(Integer, ForeignKey("tracks.id", ondelete="SET NULL"), nullable=True)
    related_issue_id = Column(Integer, ForeignKey("issues.id", ondelete="SET NULL"), nullable=True)
    is_read = Column(Boolean, default=False, nullable=False)
    created_at = Column(DateTime, default=func.now(), nullable=False)

    user = relationship("User", foreign_keys=[user_id])
