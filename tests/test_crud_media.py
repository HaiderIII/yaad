"""Tests for media CRUD operations."""

import pytest
import pytest_asyncio
from sqlalchemy.ext.asyncio import AsyncSession

from src.db.crud.media import (
    create_media,
    delete_media,
    get_media,
    get_media_list,
    get_user_stats,
    update_media,
)
from src.models.media import Media, MediaStatus, MediaType
from src.models.schemas import MediaCreate, MediaUpdate, MediaTypeEnum, MediaStatusEnum
from src.models.user import User


@pytest_asyncio.fixture
async def test_user(db_session: AsyncSession) -> User:
    """Create a test user."""
    user = User(
        username="testuser",
        email="test@example.com",
        github_id="12345",
    )
    db_session.add(user)
    await db_session.commit()
    await db_session.refresh(user)
    return user


@pytest_asyncio.fixture
async def test_media(db_session: AsyncSession, test_user: User) -> Media:
    """Create a test media entry."""
    media_data = MediaCreate(
        type=MediaTypeEnum.FILM,
        title="Test Movie",
        year=2024,
        status=MediaStatusEnum.TO_CONSUME,
    )
    media = await create_media(db_session, test_user.id, media_data)
    return media


class TestCreateMedia:
    """Tests for create_media function."""

    @pytest.mark.asyncio
    async def test_create_film(self, db_session: AsyncSession, test_user: User):
        """Test creating a film."""
        media_data = MediaCreate(
            type=MediaTypeEnum.FILM,
            title="Inception",
            year=2010,
            duration_minutes=148,
            description="A mind-bending thriller",
            status=MediaStatusEnum.TO_CONSUME,
        )

        media = await create_media(db_session, test_user.id, media_data)

        assert media.id is not None
        assert media.title == "Inception"
        assert media.year == 2010
        assert media.type == MediaType.FILM
        assert media.status == MediaStatus.TO_CONSUME
        assert media.user_id == test_user.id

    @pytest.mark.asyncio
    async def test_create_book(self, db_session: AsyncSession, test_user: User):
        """Test creating a book."""
        media_data = MediaCreate(
            type=MediaTypeEnum.BOOK,
            title="1984",
            year=1949,
            page_count=328,
            status=MediaStatusEnum.FINISHED,
            rating=4.5,
        )

        media = await create_media(
            db_session,
            test_user.id,
            media_data,
            authors=["George Orwell"],
        )

        assert media.title == "1984"
        assert media.type == MediaType.BOOK
        assert media.page_count == 328
        assert media.rating == 4.5
        assert len(media.authors) == 1
        assert media.authors[0].name == "George Orwell"

    @pytest.mark.asyncio
    async def test_create_with_genres(self, db_session: AsyncSession, test_user: User):
        """Test creating media with genres."""
        media_data = MediaCreate(
            type=MediaTypeEnum.FILM,
            title="The Matrix",
            year=1999,
            status=MediaStatusEnum.FINISHED,
        )

        media = await create_media(
            db_session,
            test_user.id,
            media_data,
            genres=["Science Fiction", "Action"],
        )

        assert len(media.genres) == 2
        genre_names = {g.name for g in media.genres}
        assert "Science Fiction" in genre_names
        assert "Action" in genre_names

    @pytest.mark.asyncio
    async def test_create_with_local_title(self, db_session: AsyncSession, test_user: User):
        """Test that local_title becomes title and original title is preserved."""
        media_data = MediaCreate(
            type=MediaTypeEnum.FILM,
            title="Seven Samurai",
            local_title="Les Sept Samouraïs",
            year=1954,
            status=MediaStatusEnum.TO_CONSUME,
        )

        media = await create_media(db_session, test_user.id, media_data)

        assert media.title == "Les Sept Samouraïs"
        assert media.original_title == "Seven Samurai"


class TestGetMedia:
    """Tests for get_media function."""

    @pytest.mark.asyncio
    async def test_get_existing_media(
        self, db_session: AsyncSession, test_user: User, test_media: Media
    ):
        """Test getting an existing media."""
        media = await get_media(db_session, test_media.id, test_user.id)

        assert media is not None
        assert media.id == test_media.id
        assert media.title == test_media.title

    @pytest.mark.asyncio
    async def test_get_nonexistent_media(self, db_session: AsyncSession, test_user: User):
        """Test getting a non-existent media returns None."""
        media = await get_media(db_session, 99999, test_user.id)
        assert media is None

    @pytest.mark.asyncio
    async def test_get_other_user_media(
        self, db_session: AsyncSession, test_media: Media
    ):
        """Test that users can't access other users' media."""
        # Create another user
        other_user = User(
            username="otheruser",
            email="other@example.com",
            github_id="67890",
        )
        db_session.add(other_user)
        await db_session.commit()

        # Try to get the media as the other user
        media = await get_media(db_session, test_media.id, other_user.id)
        assert media is None


class TestGetMediaList:
    """Tests for get_media_list function."""

    @pytest.mark.asyncio
    async def test_list_empty(self, db_session: AsyncSession, test_user: User):
        """Test listing media when none exist."""
        items, total = await get_media_list(db_session, test_user.id)

        assert items == []
        assert total == 0

    @pytest.mark.asyncio
    async def test_list_with_items(
        self, db_session: AsyncSession, test_user: User
    ):
        """Test listing media with items."""
        # Create multiple media
        for i in range(5):
            await create_media(
                db_session,
                test_user.id,
                MediaCreate(
                    type=MediaTypeEnum.FILM,
                    title=f"Movie {i}",
                    year=2020 + i,
                    status=MediaStatusEnum.TO_CONSUME,
                ),
            )

        items, total = await get_media_list(db_session, test_user.id)

        assert len(items) == 5
        assert total == 5

    @pytest.mark.asyncio
    async def test_list_filter_by_type(
        self, db_session: AsyncSession, test_user: User
    ):
        """Test filtering by media type."""
        # Create films and books
        await create_media(
            db_session,
            test_user.id,
            MediaCreate(type=MediaTypeEnum.FILM, title="Film 1", status=MediaStatusEnum.TO_CONSUME),
        )
        await create_media(
            db_session,
            test_user.id,
            MediaCreate(type=MediaTypeEnum.BOOK, title="Book 1", status=MediaStatusEnum.TO_CONSUME),
        )

        films, total = await get_media_list(
            db_session, test_user.id, media_type=MediaType.FILM
        )

        assert len(films) == 1
        assert total == 1
        assert films[0].title == "Film 1"

    @pytest.mark.asyncio
    async def test_list_filter_by_status(
        self, db_session: AsyncSession, test_user: User
    ):
        """Test filtering by status."""
        await create_media(
            db_session,
            test_user.id,
            MediaCreate(type=MediaTypeEnum.FILM, title="To Watch", status=MediaStatusEnum.TO_CONSUME),
        )
        await create_media(
            db_session,
            test_user.id,
            MediaCreate(type=MediaTypeEnum.FILM, title="Watched", status=MediaStatusEnum.FINISHED),
        )

        finished, total = await get_media_list(
            db_session, test_user.id, status=MediaStatus.FINISHED
        )

        assert len(finished) == 1
        assert total == 1
        assert finished[0].title == "Watched"

    @pytest.mark.asyncio
    async def test_list_pagination(
        self, db_session: AsyncSession, test_user: User
    ):
        """Test pagination."""
        # Create 15 media items
        for i in range(15):
            await create_media(
                db_session,
                test_user.id,
                MediaCreate(
                    type=MediaTypeEnum.FILM,
                    title=f"Movie {i:02d}",
                    status=MediaStatusEnum.TO_CONSUME,
                ),
            )

        # Get first page
        page1, total = await get_media_list(
            db_session, test_user.id, page=1, page_size=10
        )
        assert len(page1) == 10
        assert total == 15

        # Get second page
        page2, _ = await get_media_list(
            db_session, test_user.id, page=2, page_size=10
        )
        assert len(page2) == 5

    @pytest.mark.asyncio
    async def test_list_search(
        self, db_session: AsyncSession, test_user: User
    ):
        """Test search functionality."""
        await create_media(
            db_session,
            test_user.id,
            MediaCreate(type=MediaTypeEnum.FILM, title="The Matrix", status=MediaStatusEnum.TO_CONSUME),
        )
        await create_media(
            db_session,
            test_user.id,
            MediaCreate(type=MediaTypeEnum.FILM, title="Inception", status=MediaStatusEnum.TO_CONSUME),
        )

        results, total = await get_media_list(
            db_session, test_user.id, search="matrix"
        )

        assert len(results) == 1
        assert results[0].title == "The Matrix"


class TestUpdateMedia:
    """Tests for update_media function."""

    @pytest.mark.asyncio
    async def test_update_title(
        self, db_session: AsyncSession, test_user: User, test_media: Media
    ):
        """Test updating media title."""
        update_data = MediaUpdate(title="Updated Title")

        updated = await update_media(
            db_session, test_media.id, test_user.id, update_data
        )

        assert updated is not None
        assert updated.title == "Updated Title"

    @pytest.mark.asyncio
    async def test_update_status_to_finished(
        self, db_session: AsyncSession, test_user: User, test_media: Media
    ):
        """Test that consumed_at is set when status changes to finished."""
        update_data = MediaUpdate(status=MediaStatusEnum.FINISHED)

        updated = await update_media(
            db_session, test_media.id, test_user.id, update_data
        )

        assert updated is not None
        assert updated.status == MediaStatus.FINISHED
        assert updated.consumed_at is not None

    @pytest.mark.asyncio
    async def test_update_rating(
        self, db_session: AsyncSession, test_user: User, test_media: Media
    ):
        """Test updating rating."""
        update_data = MediaUpdate(rating=4.5)

        updated = await update_media(
            db_session, test_media.id, test_user.id, update_data
        )

        assert updated is not None
        assert updated.rating == 4.5

    @pytest.mark.asyncio
    async def test_update_nonexistent(
        self, db_session: AsyncSession, test_user: User
    ):
        """Test updating non-existent media returns None."""
        update_data = MediaUpdate(title="New Title")

        updated = await update_media(
            db_session, 99999, test_user.id, update_data
        )

        assert updated is None


class TestDeleteMedia:
    """Tests for delete_media function."""

    @pytest.mark.asyncio
    async def test_delete_existing(
        self, db_session: AsyncSession, test_user: User, test_media: Media
    ):
        """Test deleting existing media."""
        result = await delete_media(db_session, test_media.id, test_user.id)

        assert result is True

        # Verify it's gone
        media = await get_media(db_session, test_media.id, test_user.id)
        assert media is None

    @pytest.mark.asyncio
    async def test_delete_nonexistent(
        self, db_session: AsyncSession, test_user: User
    ):
        """Test deleting non-existent media returns False."""
        result = await delete_media(db_session, 99999, test_user.id)
        assert result is False


class TestGetUserStats:
    """Tests for get_user_stats function."""

    @pytest.mark.asyncio
    async def test_stats_empty(self, db_session: AsyncSession, test_user: User):
        """Test stats when no media exists."""
        stats = await get_user_stats(db_session, test_user.id)

        assert stats["total"] == 0
        assert stats["films"] == 0
        assert stats["books"] == 0
        assert stats["in_progress"] == 0

    @pytest.mark.asyncio
    async def test_stats_with_media(
        self, db_session: AsyncSession, test_user: User
    ):
        """Test stats with various media."""
        # Create some films
        await create_media(
            db_session,
            test_user.id,
            MediaCreate(type=MediaTypeEnum.FILM, title="Film 1", status=MediaStatusEnum.FINISHED),
        )
        await create_media(
            db_session,
            test_user.id,
            MediaCreate(type=MediaTypeEnum.FILM, title="Film 2", status=MediaStatusEnum.IN_PROGRESS),
        )

        # Create a book
        await create_media(
            db_session,
            test_user.id,
            MediaCreate(type=MediaTypeEnum.BOOK, title="Book 1", status=MediaStatusEnum.TO_CONSUME),
        )

        stats = await get_user_stats(db_session, test_user.id)

        assert stats["total"] == 3
        assert stats["films"] == 2
        assert stats["books"] == 1
        assert stats["in_progress"] == 1
