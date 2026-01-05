"""Book-specific metadata model."""

from typing import TYPE_CHECKING

from sqlalchemy import JSON, Float, ForeignKey, String
from sqlalchemy.orm import Mapped, mapped_column, relationship

from src.models.base import Base

if TYPE_CHECKING:
    from src.models.media import Media


class BookMetadata(Base):
    """Extended metadata for books (Kobo sync)."""

    __tablename__ = "book_metadata"

    id: Mapped[int] = mapped_column(primary_key=True)
    media_id: Mapped[int] = mapped_column(
        ForeignKey("media.id", ondelete="CASCADE"), unique=True, index=True
    )

    # Kobo sync info
    kobo_id: Mapped[str | None] = mapped_column(String(100), nullable=True, index=True)
    progress_percent: Mapped[float | None] = mapped_column(Float, nullable=True)
    annotations: Mapped[list | None] = mapped_column(JSON, nullable=True)

    # Book-specific
    isbn: Mapped[str | None] = mapped_column(String(20), nullable=True, index=True)
    publisher: Mapped[str | None] = mapped_column(String(255), nullable=True)

    # Relationship
    media: Mapped["Media"] = relationship("Media", back_populates="book_metadata")

    def __repr__(self) -> str:
        return f"<BookMetadata(media_id={self.media_id})>"
