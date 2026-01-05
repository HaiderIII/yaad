"""Film-specific metadata model."""

from typing import TYPE_CHECKING

from sqlalchemy import BigInteger, ForeignKey, String
from sqlalchemy.orm import Mapped, mapped_column, relationship

from src.models.base import Base

if TYPE_CHECKING:
    from src.models.media import Media


class FilmMetadata(Base):
    """Extended metadata for films (local files)."""

    __tablename__ = "film_metadata"

    id: Mapped[int] = mapped_column(primary_key=True)
    media_id: Mapped[int] = mapped_column(
        ForeignKey("media.id", ondelete="CASCADE"), unique=True, index=True
    )

    # File info (for local films)
    file_path: Mapped[str | None] = mapped_column(String(1000), nullable=True)
    resolution: Mapped[str | None] = mapped_column(String(50), nullable=True)
    codec: Mapped[str | None] = mapped_column(String(100), nullable=True)
    file_size: Mapped[int | None] = mapped_column(BigInteger, nullable=True)

    # Relationship
    media: Mapped["Media"] = relationship("Media", back_populates="film_metadata")

    def __repr__(self) -> str:
        return f"<FilmMetadata(media_id={self.media_id})>"
