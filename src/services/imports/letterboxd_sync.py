"""Letterboxd sync service via RSS and scraping."""

import logging
import re
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime
from xml.etree import ElementTree

import httpx
from bs4 import BeautifulSoup

logger = logging.getLogger(__name__)

LETTERBOXD_BASE = "https://letterboxd.com"


@dataclass
class LetterboxdFilm:
    """Film data from Letterboxd."""

    title: str
    year: int | None
    letterboxd_uri: str
    rating: float | None = None
    watched_date: datetime | None = None
    rewatch: bool = False
    liked: bool = False


@dataclass
class FriendRating:
    """A friend's rating for a film."""

    username: str
    rating: float | None
    liked: bool = False
    review_exists: bool = False


class LetterboxdSyncService:
    """Sync films from Letterboxd via RSS and scraping."""

    def __init__(self) -> None:
        self.client = httpx.AsyncClient(
            headers={
                "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"
            },
            follow_redirects=True,
            timeout=30.0,
        )

    async def close(self) -> None:
        """Close the HTTP client."""
        await self.client.aclose()

    async def validate_username(self, username: str) -> bool:
        """Check if a Letterboxd username exists."""
        try:
            response = await self.client.head(f"{LETTERBOXD_BASE}/{username}/")
            return response.status_code == 200
        except httpx.HTTPError:
            return False

    # ==================== RSS FEED ====================

    async def fetch_rss(self, username: str) -> list[LetterboxdFilm]:
        """Fetch recent films from user's RSS feed.

        Returns ~50 most recent diary entries.
        """
        url = f"{LETTERBOXD_BASE}/{username}/rss/"
        try:
            response = await self.client.get(url)
            response.raise_for_status()
        except httpx.HTTPError as e:
            logger.error(f"Failed to fetch RSS for {username}: {e}")
            return []

        return self._parse_rss(response.text)

    def _parse_rss(self, xml_content: str) -> list[LetterboxdFilm]:
        """Parse Letterboxd RSS feed."""
        films = []
        try:
            root = ElementTree.fromstring(xml_content)
            channel = root.find("channel")
            if channel is None:
                return films

            for item in channel.findall("item"):
                film = self._parse_rss_item(item)
                if film:
                    films.append(film)

        except ElementTree.ParseError as e:
            logger.error(f"Failed to parse RSS XML: {e}")

        return films

    def _parse_rss_item(self, item: ElementTree.Element) -> LetterboxdFilm | None:
        """Parse a single RSS item."""
        # Namespaces used by Letterboxd RSS
        ns = {"letterboxd": "https://letterboxd.com"}

        title_elem = item.find("letterboxd:filmTitle", ns)
        year_elem = item.find("letterboxd:filmYear", ns)
        rating_elem = item.find("letterboxd:memberRating", ns)
        watched_elem = item.find("letterboxd:watchedDate", ns)
        rewatch_elem = item.find("letterboxd:rewatch", ns)
        link_elem = item.find("link")

        if title_elem is None or title_elem.text is None:
            return None

        title = title_elem.text
        year = int(year_elem.text) if year_elem is not None and year_elem.text else None

        rating = None
        if rating_elem is not None and rating_elem.text:
            try:
                rating = float(rating_elem.text)
            except ValueError:
                pass

        watched_date = None
        if watched_elem is not None and watched_elem.text:
            try:
                watched_date = datetime.strptime(watched_elem.text, "%Y-%m-%d")
            except ValueError:
                pass

        rewatch = rewatch_elem is not None and rewatch_elem.text == "Yes"
        letterboxd_uri = link_elem.text if link_elem is not None and link_elem.text else ""

        return LetterboxdFilm(
            title=title,
            year=year,
            letterboxd_uri=letterboxd_uri,
            rating=rating,
            watched_date=watched_date,
            rewatch=rewatch,
        )

    # ==================== SCRAPING ====================

    async def scrape_watchlist(
        self,
        username: str,
        progress_callback: Callable[[int, int | None], None] | None = None,
    ) -> list[LetterboxdFilm]:
        """Scrape all films from user's watchlist.

        Args:
            username: Letterboxd username
            progress_callback: Optional callback(current, total) for progress

        Returns:
            List of films in watchlist (no ratings or watched dates)
        """
        films: list[LetterboxdFilm] = []
        page = 1
        max_pages = 100  # Safety limit

        while page <= max_pages:
            url = f"{LETTERBOXD_BASE}/{username}/watchlist/page/{page}/"
            try:
                response = await self.client.get(url)
                if response.status_code == 404:
                    break
                response.raise_for_status()
            except httpx.HTTPError as e:
                logger.error(f"Failed to fetch watchlist page {page}: {e}")
                break

            page_films = self._parse_watchlist_page(response.text)
            if not page_films:
                break

            films.extend(page_films)

            if progress_callback:
                progress_callback(len(films), None)

            page += 1

        logger.info(f"Found {len(films)} films in watchlist")
        return films

    def _parse_watchlist_page(self, html: str) -> list[LetterboxdFilm]:
        """Parse a watchlist page to extract film data.

        Watchlist pages use poster-container structure similar to /films/ pages.
        """
        films = []
        soup = BeautifulSoup(html, "html.parser")

        # Try poster-container structure first (standard film grid)
        posters = soup.select("li.poster-container")
        for poster in posters:
            try:
                film_div = poster.select_one("div.film-poster")
                if not film_div:
                    continue

                slug = film_div.get("data-film-slug", "")
                # Title might be in alt text of image or data attribute
                img = film_div.select_one("img")
                title = img.get("alt", "") if img else ""

                if not title:
                    # Fallback to data attribute
                    title = film_div.get("data-film-name", "")

                if not title or not slug:
                    continue

                # Parse year from title if present
                year = None
                year_match = re.search(r"\((\d{4})\)$", title)
                if year_match:
                    year = int(year_match.group(1))
                    title = title[: year_match.start()].strip()

                films.append(
                    LetterboxdFilm(
                        title=title,
                        year=year,
                        letterboxd_uri=f"{LETTERBOXD_BASE}/film/{slug}/",
                        rating=None,  # Watchlist items don't have ratings
                        watched_date=None,
                        rewatch=False,
                        liked=False,
                    )
                )

            except Exception as e:
                logger.warning(f"Failed to parse watchlist item: {e}")
                continue

        # If no poster-container found, try react-component divs
        if not films:
            react_divs = soup.select("div.react-component[data-item-slug]")
            for div in react_divs:
                try:
                    slug = div.get("data-item-slug", "")
                    title = div.get("data-item-name", "")

                    if not title or not slug:
                        continue

                    # Parse year from title
                    year = None
                    year_match = re.search(r"\((\d{4})\)$", title)
                    if year_match:
                        year = int(year_match.group(1))
                        title = title[: year_match.start()].strip()

                    films.append(
                        LetterboxdFilm(
                            title=title,
                            year=year,
                            letterboxd_uri=f"{LETTERBOXD_BASE}/film/{slug}/",
                            rating=None,
                            watched_date=None,
                            rewatch=False,
                            liked=False,
                        )
                    )

                except Exception as e:
                    logger.warning(f"Failed to parse watchlist react item: {e}")
                    continue

        return films

    async def scrape_all_films(
        self,
        username: str,
        include_ratings: bool = True,
        progress_callback: Callable[[int, int | None], None] | None = None,
    ) -> list[LetterboxdFilm]:
        """Scrape all films from user's profile.

        Combines diary entries (with watch dates) and ratings page (all rated films)
        to get the most complete list possible.

        Args:
            username: Letterboxd username
            include_ratings: Whether to include ratings
            progress_callback: Optional callback(current, total) for progress

        Returns:
            List of all films with ratings and watch dates
        """
        films: dict[str, LetterboxdFilm] = {}  # Use dict to dedupe by slug

        # 1. First scrape diary for films with watch dates
        page = 1
        max_pages = 200

        while page <= max_pages:
            url = f"{LETTERBOXD_BASE}/{username}/films/diary/page/{page}/"
            try:
                response = await self.client.get(url)
                if response.status_code == 404:
                    break
                response.raise_for_status()
            except httpx.HTTPError as e:
                logger.error(f"Failed to fetch diary page {page}: {e}")
                break

            page_films = self._parse_diary_page(response.text)
            if not page_films:
                break

            for film in page_films:
                slug = self._extract_slug(film.letterboxd_uri)
                if slug and slug not in films:
                    films[slug] = film

            if progress_callback:
                progress_callback(len(films), None)

            page += 1

        diary_count = len(films)
        logger.info(f"Found {diary_count} films from diary")

        # 2. Then scrape ratings page for films without diary entries
        page = 1
        while page <= max_pages:
            url = f"{LETTERBOXD_BASE}/{username}/films/ratings/page/{page}/"
            try:
                response = await self.client.get(url)
                if response.status_code == 404:
                    break
                response.raise_for_status()
            except httpx.HTTPError as e:
                logger.error(f"Failed to fetch ratings page {page}: {e}")
                break

            page_films = self._parse_ratings_page(response.text)
            if not page_films:
                break

            # Add films not already in diary
            for film in page_films:
                slug = self._extract_slug(film.letterboxd_uri)
                if slug and slug not in films:
                    films[slug] = film

            if progress_callback:
                progress_callback(len(films), None)

            page += 1

        logger.info(f"Total: {len(films)} films ({diary_count} from diary, {len(films) - diary_count} from ratings)")
        return list(films.values())

    def _parse_diary_page(self, html: str) -> list[LetterboxdFilm]:
        """Parse a diary page to extract film data with ratings and dates."""
        films = []
        soup = BeautifulSoup(html, "html.parser")

        rows = soup.select("tr.diary-entry-row")
        for row in rows:
            try:
                # Get film details from col-production td
                prod_td = row.select_one("td.col-production")
                if not prod_td:
                    continue

                # Get data from the react-component div
                react_div = prod_td.select_one("div.react-component")
                if react_div:
                    # Extract from data attributes (most reliable)
                    title = react_div.get("data-item-name", "")
                    slug = react_div.get("data-item-slug", "")
                    film_link = react_div.get("data-item-link", "")

                    # Parse year from title like "Boy & the World (2013)"
                    year = None
                    year_match = re.search(r"\((\d{4})\)$", title)
                    if year_match:
                        year = int(year_match.group(1))
                        title = title[: year_match.start()].strip()
                else:
                    # Fallback: get from h2 link
                    title_link = prod_td.select_one("h2.name a")
                    if not title_link:
                        continue
                    title = title_link.get_text(strip=True)
                    film_link = f"/film/{title_link.get('href', '').split('/')[-2]}/"
                    slug = film_link.replace("/film/", "").strip("/")
                    year = None

                if not title:
                    continue

                # Year from col-releaseyear td (backup)
                if not year:
                    year_td = row.select_one("td.col-releaseyear")
                    if year_td:
                        year_link = year_td.select_one("a")
                        if year_link:
                            year_text = year_link.get_text(strip=True)
                            if year_text.isdigit():
                                year = int(year_text)

                # Rating from col-rating td
                rating = None
                rating_td = row.select_one("td.col-rating")
                if rating_td:
                    # Rating is stored in input value (0-10 scale)
                    rating_input = rating_td.select_one("input.rateit-field")
                    if rating_input:
                        try:
                            rating_value = int(rating_input.get("value", 0))
                            if rating_value > 0:
                                rating = rating_value / 2.0  # Convert to 0.5-5.0 scale
                        except (ValueError, TypeError):
                            pass

                # Watch date from col-daydate and col-monthdate
                watched_date = None
                day_td = row.select_one("td.col-daydate")
                if day_td:
                    date_link = day_td.select_one("a.daydate")
                    if date_link:
                        date_href = date_link.get("href", "")
                        # Format: /username/diary/films/for/2025/12/27/
                        date_match = re.search(r"/for/(\d{4})/(\d{1,2})/(\d{1,2})/", date_href)
                        if date_match:
                            try:
                                watched_date = datetime(
                                    int(date_match.group(1)),
                                    int(date_match.group(2)),
                                    int(date_match.group(3)),
                                )
                            except ValueError:
                                pass

                # Liked - check col-like td for active state
                liked_td = row.select_one("td.col-like")
                liked = liked_td is not None and "icon-status-off" not in liked_td.get(
                    "class", []
                )

                # Rewatch - check col-rewatch td for active state
                rewatch_td = row.select_one("td.col-rewatch")
                rewatch = rewatch_td is not None and "icon-status-off" not in rewatch_td.get(
                    "class", []
                )

                # Build film object
                if not slug and film_link:
                    slug = film_link.replace("/film/", "").strip("/")

                films.append(
                    LetterboxdFilm(
                        title=title,
                        year=year,
                        letterboxd_uri=f"{LETTERBOXD_BASE}/film/{slug}/",
                        rating=rating,
                        watched_date=watched_date,
                        rewatch=rewatch,
                        liked=liked,
                    )
                )

            except Exception as e:
                logger.warning(f"Failed to parse diary row: {e}")
                continue

        return films

    def _parse_ratings_page(self, html: str) -> list[LetterboxdFilm]:
        """Parse a /films/ratings/ page to extract film data with ratings.

        This page uses react-component divs with data attributes.
        """
        films = []
        soup = BeautifulSoup(html, "html.parser")

        # Find all react-component divs with film data
        react_divs = soup.select("div.react-component[data-item-slug]")
        for div in react_divs:
            try:
                slug = div.get("data-item-slug", "")
                title = div.get("data-item-name", "")

                if not title or not slug:
                    continue

                # Parse year from title like "The Bad Guys 2 (2025)"
                year = None
                year_match = re.search(r"\((\d{4})\)$", title)
                if year_match:
                    year = int(year_match.group(1))
                    title = title[: year_match.start()].strip()

                # Find rating - look for next span.rating sibling or nearby
                rating = None
                # The rating is in a span.rating near the react-component
                parent = div.find_parent("li") or div.find_parent("div")
                if parent:
                    rating_span = parent.select_one("span.rating")
                    if rating_span:
                        rating = self._parse_star_rating(rating_span.get_text(strip=True))

                films.append(
                    LetterboxdFilm(
                        title=title,
                        year=year,
                        letterboxd_uri=f"{LETTERBOXD_BASE}/film/{slug}/",
                        rating=rating,
                        watched_date=None,  # No watch date from ratings page
                        rewatch=False,
                        liked=False,
                    )
                )

            except Exception as e:
                logger.warning(f"Failed to parse ratings item: {e}")
                continue

        return films

    def _parse_films_page(self, html: str) -> list[LetterboxdFilm]:
        """Parse a /films/ page to extract film data."""
        films = []
        soup = BeautifulSoup(html, "html.parser")

        # Find all film posters
        posters = soup.select("li.poster-container")
        for poster in posters:
            film_div = poster.select_one("div.film-poster")
            if not film_div:
                continue

            # Extract data attributes
            slug = film_div.get("data-film-slug", "")
            title = film_div.get("data-film-name", "")

            # Year might be in the title or we need to fetch it separately
            year = None
            year_match = re.search(r"\((\d{4})\)$", title)
            if year_match:
                year = int(year_match.group(1))
                title = title[: year_match.start()].strip()

            # Check for rating in the overlay
            rating = None
            rating_span = poster.select_one("span.rating")
            if rating_span:
                rating_text = rating_span.get_text(strip=True)
                rating = self._parse_star_rating(rating_text)

            if title:
                films.append(
                    LetterboxdFilm(
                        title=title,
                        year=year,
                        letterboxd_uri=f"{LETTERBOXD_BASE}/film/{slug}/",
                        rating=rating,
                    )
                )

        return films

    async def _scrape_ratings(self, username: str) -> list[dict]:
        """Scrape all ratings from /films/ratings/ pages."""
        ratings = []
        page = 1

        while True:
            url = f"{LETTERBOXD_BASE}/{username}/films/ratings/page/{page}/"
            try:
                response = await self.client.get(url)
                if response.status_code == 404:
                    break
                response.raise_for_status()
            except httpx.HTTPError:
                break

            soup = BeautifulSoup(response.text, "html.parser")
            posters = soup.select("li.poster-container")

            if not posters:
                break

            for poster in posters:
                film_div = poster.select_one("div.film-poster")
                rating_span = poster.select_one("span.rating")

                if film_div and rating_span:
                    slug = film_div.get("data-film-slug", "")
                    rating_text = rating_span.get_text(strip=True)
                    rating = self._parse_star_rating(rating_text)

                    if slug and rating:
                        ratings.append({"slug": slug, "rating": rating})

            page += 1

        return ratings

    async def _scrape_diary(self, username: str, max_pages: int = 50) -> list[dict]:
        """Scrape diary entries with watch dates."""
        entries = []
        page = 1

        while page <= max_pages:
            url = f"{LETTERBOXD_BASE}/{username}/films/diary/page/{page}/"
            try:
                response = await self.client.get(url)
                if response.status_code == 404:
                    break
                response.raise_for_status()
            except httpx.HTTPError:
                break

            soup = BeautifulSoup(response.text, "html.parser")
            rows = soup.select("tr.diary-entry-row")

            if not rows:
                break

            for row in rows:
                # Film slug
                film_td = row.select_one("td.td-film-details")
                if not film_td:
                    continue

                film_div = film_td.select_one("div.film-poster")
                if not film_div:
                    continue

                slug = film_div.get("data-film-slug", "")

                # Watch date
                date_td = row.select_one("td.td-calendar")
                watched_date = None
                if date_td:
                    date_link = date_td.select_one("a")
                    if date_link:
                        href = date_link.get("href", "")
                        # Format: /username/films/diary/for/2024/01/15/
                        date_match = re.search(r"/for/(\d{4})/(\d{2})/(\d{2})/", href)
                        if date_match:
                            try:
                                watched_date = datetime(
                                    int(date_match.group(1)),
                                    int(date_match.group(2)),
                                    int(date_match.group(3)),
                                )
                            except ValueError:
                                pass

                # Rating
                rating_td = row.select_one("td.td-rating")
                rating = None
                if rating_td:
                    rating_span = rating_td.select_one("span.rating")
                    if rating_span:
                        rating = self._parse_star_rating(rating_span.get_text(strip=True))

                # Liked
                liked = row.select_one("td.td-like span.icon-liked") is not None

                if slug:
                    entries.append(
                        {
                            "slug": slug,
                            "watched_date": watched_date,
                            "rating": rating,
                            "liked": liked,
                        }
                    )

            page += 1

        return entries

    def _parse_star_rating(self, rating_text: str) -> float | None:
        """Convert star symbols to numeric rating (0.5-5.0 scale)."""
        if not rating_text:
            return None

        # Count full stars (★) and half stars (½)
        full_stars = rating_text.count("★")
        half_stars = rating_text.count("½")

        if full_stars == 0 and half_stars == 0:
            return None

        return full_stars + (0.5 * half_stars)

    def _extract_slug(self, uri: str) -> str | None:
        """Extract film slug from Letterboxd URI."""
        match = re.search(r"/film/([^/]+)/?", uri)
        return match.group(1) if match else None


    # ==================== FRIENDS RATINGS ====================

    async def get_following(self, username: str) -> list[str]:
        """Get list of usernames that a user follows.

        Args:
            username: Letterboxd username

        Returns:
            List of usernames being followed
        """
        following: list[str] = []
        page = 1
        max_pages = 20  # Safety limit (most users follow < 500 people)

        while page <= max_pages:
            url = f"{LETTERBOXD_BASE}/{username}/following/page/{page}/"
            try:
                response = await self.client.get(url)
                if response.status_code == 404:
                    break
                response.raise_for_status()
            except httpx.HTTPError as e:
                logger.error(f"Failed to fetch following page {page}: {e}")
                break

            page_following = self._parse_following_page(response.text)
            if not page_following:
                break

            following.extend(page_following)
            page += 1

        logger.info(f"Found {len(following)} users that {username} follows")
        return following

    def _parse_following_page(self, html: str) -> list[str]:
        """Parse a following page to extract usernames."""
        usernames = []
        soup = BeautifulSoup(html, "html.parser")

        # Following page uses table with profile links
        profile_links = soup.select("a.name")
        for link in profile_links:
            href = link.get("href", "")
            # Format: /username/
            match = re.match(r"^/([^/]+)/$", href)
            if match:
                usernames.append(match.group(1))

        return usernames

    async def get_friends_ratings_for_film(
        self,
        film_slug: str,
        friends: list[str] | None = None,
        username: str | None = None,
    ) -> list[FriendRating]:
        """Get ratings from friends for a specific film.

        Args:
            film_slug: The film slug (e.g., 'dune-part-two')
            friends: List of friend usernames to check. If None, will fetch following list.
            username: If friends is None, fetch following list for this user.

        Returns:
            List of FriendRating for friends who have rated this film
        """
        if friends is None:
            if username is None:
                return []
            friends = await self.get_following(username)

        if not friends:
            return []

        # Convert to set for O(1) lookup, lowercase for case-insensitive matching
        friends_set = {f.lower() for f in friends}

        # Scrape the members page for this film
        ratings: list[FriendRating] = []
        page = 1
        max_pages = 50  # Check up to 50 pages of members

        while page <= max_pages:
            url = f"{LETTERBOXD_BASE}/film/{film_slug}/members/page/{page}/"
            try:
                response = await self.client.get(url)
                if response.status_code == 404:
                    break
                response.raise_for_status()
            except httpx.HTTPError as e:
                logger.error(f"Failed to fetch members page {page} for {film_slug}: {e}")
                break

            page_ratings, found_friends = self._parse_members_page(response.text, friends_set)

            # Add found friend ratings
            ratings.extend(page_ratings)

            # If we've found ratings from all friends, we can stop
            # (though a friend might appear on multiple pages if they rewatched)

            # Check if this page had any members
            if not self._page_has_members(response.text):
                break

            page += 1

        logger.info(f"Found {len(ratings)} friend ratings for {film_slug}")
        return ratings

    def _parse_members_page(
        self, html: str, friends: set[str]
    ) -> tuple[list[FriendRating], set[str]]:
        """Parse a /film/SLUG/members/ page to find friend ratings.

        Args:
            html: Page HTML
            friends: Set of friend usernames to look for

        Returns:
            Tuple of (friend ratings found, usernames of friends found)
        """
        ratings = []
        found_friends: set[str] = set()
        soup = BeautifulSoup(html, "html.parser")

        # Members page can use table.person-table OR a list structure
        # Try table structure first
        member_rows = soup.select("table.person-table tr")

        # If no table rows, try list/div structure
        if not member_rows:
            member_rows = soup.select("li.person-summary, div.person-summary")

        # Also check for any link that could be a username
        if not member_rows:
            # Fallback: find all user profile links
            for link in soup.select("a[href]"):
                href = link.get("href", "")
                match = re.match(r"^/([a-zA-Z0-9_]+)/$", href)
                if match:
                    username = match.group(1).lower()
                    if username in friends and username not in found_friends:
                        found_friends.add(username)
                        # Try to find rating nearby
                        parent = link.find_parent(["li", "div", "tr"])
                        rating = None
                        liked = False
                        review_exists = False

                        if parent:
                            # Look for star rating
                            rating_elem = parent.select_one("span.rating")
                            if rating_elem:
                                rating = self._parse_star_rating(rating_elem.get_text(strip=True))
                            # Also check text for stars
                            if not rating:
                                text = parent.get_text()
                                rating = self._parse_star_rating(text)

                            liked = parent.select_one(".icon-liked, .liked") is not None

                        ratings.append(
                            FriendRating(
                                username=username,
                                rating=rating,
                                liked=liked,
                                review_exists=review_exists,
                            )
                        )

            return ratings, found_friends

        for row in member_rows:
            try:
                # Get username from profile link
                name_link = row.select_one("a.name, h3 a, a[href^='/']")
                if not name_link:
                    continue

                href = name_link.get("href", "")
                match = re.match(r"^/([a-zA-Z0-9_]+)/$", href)
                if not match:
                    continue

                username = match.group(1).lower()

                # Check if this is a friend
                if username not in friends:
                    continue

                found_friends.add(username)

                # Get rating
                rating = None
                rating_span = row.select_one("span.rating, p.rating")
                if rating_span:
                    rating = self._parse_star_rating(rating_span.get_text(strip=True))

                # If no rating span, check text content for stars
                if not rating:
                    text = row.get_text()
                    rating = self._parse_star_rating(text)

                # Check for liked
                liked = row.select_one("span.icon-liked, .liked") is not None

                # Check for review
                review_exists = row.select_one("a.review-micro, a[href*='review']") is not None

                ratings.append(
                    FriendRating(
                        username=username,
                        rating=rating,
                        liked=liked,
                        review_exists=review_exists,
                    )
                )

            except Exception as e:
                logger.warning(f"Failed to parse member row: {e}")
                continue

        return ratings, found_friends

    def _page_has_members(self, html: str) -> bool:
        """Check if a members page has any entries."""
        soup = BeautifulSoup(html, "html.parser")
        has_table = len(soup.select("table.person-table tr")) > 0
        has_list = len(soup.select("li.person-summary, div.person-summary")) > 0
        # Check if there are any username-style links
        has_links = any(
            re.match(r"^/[a-zA-Z0-9_]+/$", a.get("href", ""))
            for a in soup.select("a[href]")
        )
        return has_table or has_list or has_links

    async def get_friend_rating_direct(
        self,
        friend_username: str,
        film_slug: str,
    ) -> FriendRating | None:
        """Get a specific friend's rating for a film by checking their activity page.

        Args:
            friend_username: The friend's username
            film_slug: The film slug

        Returns:
            FriendRating if found, None otherwise
        """
        # Check the friend's activity/diary entry for this film
        url = f"{LETTERBOXD_BASE}/{friend_username}/film/{film_slug}/"
        try:
            response = await self.client.get(url)
            if response.status_code != 200:
                return None

            soup = BeautifulSoup(response.text, "html.parser")

            # Look for rating on the page
            rating = None
            rating_elem = soup.select_one("span.rating, .rating-large, span.own-rating")
            if rating_elem:
                rating = self._parse_star_rating(rating_elem.get_text(strip=True))

            # If no rating element, look for stars in text
            if not rating:
                # Check sidebar for rating
                sidebar = soup.select_one(".sidebar-user-rating, .user-rating")
                if sidebar:
                    rating = self._parse_star_rating(sidebar.get_text(strip=True))

            # Check if liked
            liked = soup.select_one(".icon-liked.liked, .like-link.liked") is not None

            # Check if reviewed
            review_exists = soup.select_one(".review, .body-text") is not None

            # If we found anything, return it
            if rating is not None or liked or review_exists:
                return FriendRating(
                    username=friend_username,
                    rating=rating,
                    liked=liked,
                    review_exists=review_exists,
                )

            # Page exists but no activity - might be on watchlist only
            return None

        except httpx.HTTPError as e:
            logger.debug(f"Failed to check {friend_username}'s rating for {film_slug}: {e}")
            return None

    async def get_friends_ratings_direct(
        self,
        film_slug: str,
        username: str,
    ) -> list[FriendRating]:
        """Get ratings from friends by checking each friend's film page directly.

        This is slower but more reliable than scraping through members pages.

        Args:
            film_slug: The film slug
            username: User whose friends to check

        Returns:
            List of FriendRating for friends who have rated this film
        """
        friends = await self.get_following(username)
        if not friends:
            return []

        ratings: list[FriendRating] = []

        # Check each friend's page for this film
        for friend in friends:
            rating = await self.get_friend_rating_direct(friend, film_slug)
            if rating:
                ratings.append(rating)

        logger.info(f"Found {len(ratings)} friend ratings for {film_slug} (direct method)")
        return ratings

    async def get_friends_ratings_batch(
        self,
        film_slugs: list[str],
        username: str,
    ) -> dict[str, list[FriendRating]]:
        """Get friends' ratings for multiple films.

        Args:
            film_slugs: List of film slugs
            username: User whose friends to check

        Returns:
            Dict mapping film slug to list of friend ratings
        """
        # First get the following list once
        friends = await self.get_following(username)
        if not friends:
            return {}

        results: dict[str, list[FriendRating]] = {}
        for slug in film_slugs:
            ratings = await self.get_friends_ratings_for_film(slug, friends=friends)
            if ratings:
                results[slug] = ratings

        return results


# Global instance
letterboxd_sync = LetterboxdSyncService()
