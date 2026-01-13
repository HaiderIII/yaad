"""YouTube-specific metadata model."""

from typing import TYPE_CHECKING

from sqlalchemy import ForeignKey, String
from sqlalchemy.orm import Mapped, mapped_column, relationship

from src.models.base import Base

if TYPE_CHECKING:
    from src.models.media import Media


class YouTubeMetadata(Base):
    """Extended metadata for YouTube videos."""

    __tablename__ = "youtube_metadata"

    id: Mapped[int] = mapped_column(primary_key=True)
    media_id: Mapped[int] = mapped_column(
        ForeignKey("media.id", ondelete="CASCADE"), unique=True, index=True
    )

    # YouTube-specific
    video_id: Mapped[str] = mapped_column(String(20), nullable=False, index=True)
    channel_name: Mapped[str | None] = mapped_column(String(255), nullable=True)
    channel_id: Mapped[str | None] = mapped_column(String(50), nullable=True)
    playlist_item_id: Mapped[str | None] = mapped_column(String(100), nullable=True)

    # Archive info (Phase 7)
    archived_path: Mapped[str | None] = mapped_column(String(1000), nullable=True)

    # Relationship
    media: Mapped["Media"] = relationship("Media", back_populates="youtube_metadata")

    def __repr__(self) -> str:
        return f"<YouTubeMetadata(video_id={self.video_id})>"
