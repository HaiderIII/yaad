"""Book metadata service combining Google Books and Open Library APIs.

Strategy for ISBN/EAN lookup:
1. Search by provided ISBN/EAN first
2. If result is incomplete (no cover/description), extract title and search by title
3. Find the best edition (original language preferred, with most complete data)
4. Return original edition + link to French edition if different
"""

import re
from typing import Any

import httpx

# API endpoints (both free, no API key required for basic usage)
GOOGLE_BOOKS_API = "https://www.googleapis.com/books/v1"
OPEN_LIBRARY_API = "https://openlibrary.org"
OPEN_LIBRARY_COVERS = "https://covers.openlibrary.org"


def normalize_isbn(isbn: str) -> str | None:
    """Normalize ISBN by removing hyphens and spaces, validate format."""
    cleaned = re.sub(r"[^0-9Xx]", "", isbn)
    if len(cleaned) == 10 or len(cleaned) == 13:
        return cleaned.upper()
    return None


def extract_year(date_str: str | None) -> int | None:
    """Extract year from various date formats."""
    if not date_str:
        return None
    match = re.search(r"\b(19|20)\d{2}\b", date_str)
    return int(match.group()) if match else None


def is_complete_result(result: dict[str, Any] | None) -> bool:
    """Check if a result has all essential fields."""
    if not result:
        return False
    return bool(
        result.get("title")
        and result.get("authors")
        and result.get("cover_url")  # Cover is essential
    )


def has_cover(result: dict[str, Any] | None) -> bool:
    """Check if result has a cover URL."""
    return bool(result and result.get("cover_url"))


def completeness_score(result: dict[str, Any] | None) -> int:
    """
    Calculate completeness score for a result.
    Higher score = more complete.
    Priority: cover (10) > authors (5) > description (3) > year (2) > pages (1) > publisher (1)
    """
    if not result:
        return 0

    score = 0
    if result.get("cover_url"):
        score += 10  # Cover is most important
    if result.get("authors"):
        score += 5
    if result.get("description"):
        score += 3
    if result.get("year"):
        score += 2
    if result.get("page_count"):
        score += 1
    if result.get("publisher"):
        score += 1
    return score


def is_french_edition(result: dict[str, Any]) -> bool:
    """Check if this is a French edition."""
    lang = result.get("language", "")
    if isinstance(lang, str):
        return lang.lower() in ("fr", "fre", "fra", "french")
    return False


class BookService:
    """Service for fetching book metadata from Google Books and Open Library."""

    def __init__(self) -> None:
        self.google_api = GOOGLE_BOOKS_API
        self.ol_api = OPEN_LIBRARY_API
        self.ol_covers = OPEN_LIBRARY_COVERS

    async def search_by_isbn(self, isbn: str) -> dict[str, Any] | None:
        """
        Get book information by ISBN/EAN with smart edition finding.

        Strategy:
        1. Search by provided ISBN/EAN
        2. If incomplete, search by title to find better edition
        3. Return original edition with link to French edition if different
        """
        normalized = normalize_isbn(isbn)
        if not normalized:
            return None

        async with httpx.AsyncClient(timeout=15.0) as client:
            # Step 1: Try direct ISBN lookup
            direct_result = await self._direct_isbn_lookup(client, normalized)

            # Step 2: If incomplete (especially missing cover), search by title
            direct_score = completeness_score(direct_result)
            if direct_result and direct_result.get("title"):
                # Search if missing cover or low completeness score
                if not has_cover(direct_result) or direct_score < 15:
                    # Search by title to find edition with better data
                    title = direct_result.get("title", "")
                    authors = direct_result.get("authors", [])

                    better_result = await self._find_best_edition(
                        client, title, authors, normalized
                    )

                    # Use better result if it has higher completeness score
                    if better_result and completeness_score(better_result) > direct_score:
                        # Keep the user's ISBN as reference but use better data
                        better_result["user_isbn"] = normalized
                        better_result["user_edition"] = direct_result
                        return better_result

            # Step 3: If still no cover, try title search as last resort
            if direct_result and not has_cover(direct_result):
                if direct_result.get("title"):
                    search_results = await self._search_google(
                        client, direct_result["title"], 10
                    )
                    # Sort by completeness and pick best with cover
                    search_results.sort(key=lambda x: -completeness_score(x))
                    for result in search_results:
                        if has_cover(result):
                            result["user_isbn"] = normalized
                            return result

            return direct_result

    async def _direct_isbn_lookup(
        self, client: httpx.AsyncClient, isbn: str
    ) -> dict[str, Any] | None:
        """Direct lookup by ISBN from both APIs."""
        google_result = await self._search_google_by_isbn(client, isbn)
        ol_result = await self._search_open_library_by_isbn(client, isbn)
        return self._merge_results(google_result, ol_result, isbn)

    async def _find_best_edition(
        self,
        client: httpx.AsyncClient,
        title: str,
        authors: list[str],
        original_isbn: str,
    ) -> dict[str, Any] | None:
        """Find the best edition by searching with title and author."""
        # Build search query
        query = title
        if authors:
            query += f" {authors[0]}"

        # Search both APIs
        google_results = await self._search_google(client, query, 10)
        ol_results = await self._search_open_library(client, query, 10)

        all_results = google_results + ol_results

        # Filter to find matching books (similar title)
        title_lower = title.lower()
        matching = []
        for result in all_results:
            result_title = result.get("title", "").lower()
            # Check title similarity (simple containment check)
            if (title_lower in result_title or result_title in title_lower or
                self._titles_match(title_lower, result_title)):
                matching.append(result)

        if not matching:
            return None

        # Sort by completeness score (cover is most important, then other fields)
        # Secondary: prefer original language editions over French
        matching.sort(
            key=lambda x: (
                -completeness_score(x),  # Higher score = better (cover weighted 10)
                -int(not is_french_edition(x)),  # Non-French (original) first
            )
        )

        best = matching[0] if matching else None

        # If best is not French, find French edition for linking
        if best and not is_french_edition(best):
            french_editions = [r for r in matching if is_french_edition(r)]
            if french_editions:
                # Sort French editions by completeness to get the best one
                french_editions.sort(key=lambda x: -completeness_score(x))
                best_french = french_editions[0]
                best["french_edition"] = {
                    "title": best_french.get("title"),
                    "isbn": best_french.get("isbn"),
                    "year": best_french.get("year"),
                    "publisher": best_french.get("publisher"),
                    "cover_url": best_french.get("cover_url"),
                }

        return best

    def _titles_match(self, title1: str, title2: str) -> bool:
        """Check if two titles are similar enough to be the same book."""
        # Normalize titles
        def normalize(t: str) -> str:
            # Remove common articles and punctuation
            t = re.sub(r"^(the|le|la|les|l'|un|une|a|an)\s+", "", t.lower())
            t = re.sub(r"[^\w\s]", "", t)
            return t.strip()

        n1, n2 = normalize(title1), normalize(title2)

        # Check if one contains the other or they start the same way
        if n1 in n2 or n2 in n1:
            return True

        # Check if first N words match
        words1 = n1.split()[:3]
        words2 = n2.split()[:3]
        if words1 and words2 and words1 == words2:
            return True

        return False

    async def search_books(
        self, query: str, limit: int = 10
    ) -> list[dict[str, Any]]:
        """
        Search books by title/author.
        Combines results from both APIs, deduplicates, and sorts by most recent.
        """
        async with httpx.AsyncClient(timeout=10.0) as client:
            # Search both APIs
            google_results = await self._search_google(client, query, limit)
            ol_results = await self._search_open_library(client, query, limit)

            # Combine and deduplicate
            all_results = self._deduplicate_results(google_results + ol_results)

            # Sort by completeness score (prioritizes cover, then authors, etc.)
            all_results.sort(
                key=lambda x: (
                    -completeness_score(x),  # Higher score = better
                    -(x.get("year") or 0),  # Then most recent first
                )
            )

            return all_results[:limit]

    # -------------------------------------------------------------------------
    # Google Books API
    # -------------------------------------------------------------------------

    async def _search_google_by_isbn(
        self, client: httpx.AsyncClient, isbn: str
    ) -> dict[str, Any] | None:
        """Search Google Books by ISBN."""
        try:
            response = await client.get(
                f"{self.google_api}/volumes",
                params={"q": f"isbn:{isbn}", "maxResults": 1},
            )
            if response.status_code == 200:
                data = response.json()
                items = data.get("items", [])
                if items:
                    return self._parse_google_book(items[0], isbn)
        except Exception:
            pass
        return None

    async def _search_google(
        self, client: httpx.AsyncClient, query: str, limit: int
    ) -> list[dict[str, Any]]:
        """Search Google Books by query."""
        results = []
        try:
            response = await client.get(
                f"{self.google_api}/volumes",
                params={
                    "q": query,
                    "maxResults": min(limit, 40),
                    "orderBy": "relevance",
                },
            )
            if response.status_code == 200:
                data = response.json()
                for item in data.get("items", []):
                    parsed = self._parse_google_book(item, None)
                    if parsed:
                        results.append(parsed)
        except Exception:
            pass
        return results

    def _parse_google_book(
        self, item: dict[str, Any], isbn: str | None
    ) -> dict[str, Any] | None:
        """Parse Google Books API response."""
        info = item.get("volumeInfo", {})
        if not info:
            return None

        # Extract ISBN from identifiers if not provided
        if not isbn:
            for identifier in info.get("industryIdentifiers", []):
                if identifier.get("type") == "ISBN_13":
                    isbn = identifier.get("identifier")
                    break
            if not isbn:
                for identifier in info.get("industryIdentifiers", []):
                    if identifier.get("type") == "ISBN_10":
                        isbn = identifier.get("identifier")
                        break

        # Get cover URL (prefer large thumbnail)
        cover_url = None
        images = info.get("imageLinks", {})
        for size in ["extraLarge", "large", "medium", "thumbnail", "smallThumbnail"]:
            if size in images:
                cover_url = images[size].replace("http://", "https://")
                cover_url = re.sub(r"&edge=curl", "", cover_url)
                cover_url = re.sub(r"zoom=\d", "zoom=3", cover_url)
                break

        authors = info.get("authors", [])

        return {
            "title": info.get("title", ""),
            "authors": authors,
            "publisher": info.get("publisher", ""),
            "year": extract_year(info.get("publishedDate")),
            "page_count": info.get("pageCount"),
            "cover_url": cover_url,
            "isbn": isbn,
            "description": info.get("description"),
            "external_id": isbn,
            "language": info.get("language"),
            "source": "google",
        }

    # -------------------------------------------------------------------------
    # Open Library API
    # -------------------------------------------------------------------------

    async def _search_open_library_by_isbn(
        self, client: httpx.AsyncClient, isbn: str
    ) -> dict[str, Any] | None:
        """Search Open Library by ISBN."""
        # Try direct ISBN endpoint first
        try:
            response = await client.get(f"{self.ol_api}/isbn/{isbn}.json")
            if response.status_code == 200:
                data = response.json()
                return await self._parse_ol_edition(client, data, isbn)
        except Exception:
            pass

        # Fallback to search
        try:
            response = await client.get(
                f"{self.ol_api}/search.json",
                params={"isbn": isbn, "limit": 1},
            )
            if response.status_code == 200:
                data = response.json()
                if data.get("docs"):
                    return self._parse_ol_search_result(data["docs"][0], isbn)
        except Exception:
            pass

        return None

    async def _search_open_library(
        self, client: httpx.AsyncClient, query: str, limit: int
    ) -> list[dict[str, Any]]:
        """Search Open Library by query."""
        results = []
        try:
            response = await client.get(
                f"{self.ol_api}/search.json",
                params={"q": query, "limit": limit},
            )
            if response.status_code == 200:
                data = response.json()
                for doc in data.get("docs", []):
                    parsed = self._parse_ol_search_result(doc, None)
                    if parsed:
                        results.append(parsed)
        except Exception:
            pass
        return results

    async def _parse_ol_edition(
        self, client: httpx.AsyncClient, data: dict[str, Any], isbn: str
    ) -> dict[str, Any]:
        """Parse Open Library edition data with author fetching."""
        authors = []

        # Fetch authors from author references
        for author_ref in data.get("authors", []):
            author_key = author_ref.get("key")
            if author_key:
                try:
                    author_resp = await client.get(f"{self.ol_api}{author_key}.json")
                    if author_resp.status_code == 200:
                        author_data = author_resp.json()
                        name = author_data.get("name")
                        if name:
                            authors.append(name)
                except Exception:
                    pass

        # Fallback to by_statement
        if not authors:
            by_statement = data.get("by_statement", "")
            if by_statement:
                cleaned = re.sub(r"^(by|par|de)\s+", "", by_statement, flags=re.IGNORECASE)
                authors = [a.strip() for a in cleaned.split(",") if a.strip()]

        # Try to get authors from work if still empty
        if not authors:
            for work_ref in data.get("works", []):
                work_key = work_ref.get("key")
                if work_key:
                    try:
                        work_resp = await client.get(f"{self.ol_api}{work_key}.json")
                        if work_resp.status_code == 200:
                            work_data = work_resp.json()
                            for author_ref in work_data.get("authors", []):
                                author_key = author_ref.get("author", {}).get("key")
                                if author_key:
                                    author_resp = await client.get(f"{self.ol_api}{author_key}.json")
                                    if author_resp.status_code == 200:
                                        author_data = author_resp.json()
                                        name = author_data.get("name")
                                        if name:
                                            authors.append(name)
                    except Exception:
                        pass
                    break

        # Get description from work
        description = None
        for work_ref in data.get("works", []):
            work_key = work_ref.get("key")
            if work_key:
                try:
                    work_resp = await client.get(f"{self.ol_api}{work_key}.json")
                    if work_resp.status_code == 200:
                        work_data = work_resp.json()
                        desc = work_data.get("description")
                        if isinstance(desc, dict):
                            description = desc.get("value")
                        elif isinstance(desc, str):
                            description = desc
                except Exception:
                    pass
                break

        # Build cover URL
        cover_url = None
        if data.get("covers"):
            cover_id = data["covers"][0]
            cover_url = f"{self.ol_covers}/b/id/{cover_id}-L.jpg"
        elif isbn:
            cover_url = f"{self.ol_covers}/b/isbn/{isbn}-L.jpg"

        # Detect language
        language = None
        if data.get("languages"):
            lang_key = data["languages"][0].get("key", "")
            language = lang_key.replace("/languages/", "")

        return {
            "title": data.get("title", ""),
            "authors": authors,
            "publisher": ", ".join(data.get("publishers", [])),
            "year": extract_year(data.get("publish_date", "")),
            "page_count": data.get("number_of_pages"),
            "cover_url": cover_url,
            "isbn": isbn,
            "description": description,
            "external_id": isbn,
            "open_library_key": data.get("key"),
            "language": language,
            "source": "openlibrary",
        }

    def _parse_ol_search_result(
        self, doc: dict[str, Any], isbn: str | None
    ) -> dict[str, Any]:
        """Parse Open Library search result."""
        if not isbn:
            isbns = doc.get("isbn", [])
            isbn = isbns[0] if isbns else None

        cover_url = None
        cover_id = doc.get("cover_i")
        if cover_id:
            cover_url = f"{self.ol_covers}/b/id/{cover_id}-L.jpg"
        elif isbn:
            cover_url = f"{self.ol_covers}/b/isbn/{isbn}-L.jpg"

        # Get language
        languages = doc.get("language", [])
        language = languages[0] if languages else None

        return {
            "title": doc.get("title", ""),
            "authors": doc.get("author_name", []),
            "publisher": ", ".join(doc.get("publisher", [])[:1]),
            "year": doc.get("first_publish_year"),
            "page_count": doc.get("number_of_pages_median"),
            "cover_url": cover_url,
            "isbn": isbn,
            "description": None,
            "external_id": isbn,
            "open_library_key": doc.get("key"),
            "language": language,
            "source": "openlibrary",
        }

    # -------------------------------------------------------------------------
    # Result merging and deduplication
    # -------------------------------------------------------------------------

    def _merge_results(
        self,
        google_result: dict[str, Any] | None,
        ol_result: dict[str, Any] | None,
        isbn: str,
    ) -> dict[str, Any] | None:
        """Merge results from Google Books and Open Library."""
        if not google_result and not ol_result:
            return None

        if not google_result:
            return ol_result
        if not ol_result:
            return google_result

        # Use Google as base (usually more complete/recent)
        merged = google_result.copy()

        # Fill missing fields from Open Library
        if not merged.get("authors") and ol_result.get("authors"):
            merged["authors"] = ol_result["authors"]
        if not merged.get("description") and ol_result.get("description"):
            merged["description"] = ol_result["description"]
        if not merged.get("page_count") and ol_result.get("page_count"):
            merged["page_count"] = ol_result["page_count"]
        if not merged.get("cover_url") and ol_result.get("cover_url"):
            merged["cover_url"] = ol_result["cover_url"]
        if not merged.get("year") and ol_result.get("year"):
            merged["year"] = ol_result["year"]

        merged["source"] = "merged"
        return merged

    def _deduplicate_results(
        self, results: list[dict[str, Any]]
    ) -> list[dict[str, Any]]:
        """Deduplicate results based on ISBN or title similarity."""
        seen_isbns: set[str] = set()
        seen_titles: set[str] = set()
        deduplicated = []

        for result in results:
            isbn = result.get("isbn")
            title = result.get("title", "").lower().strip()

            if isbn and isbn in seen_isbns:
                continue

            title_key = re.sub(r"[^a-z0-9]", "", title)[:30]
            if title_key and title_key in seen_titles:
                continue

            if isbn:
                seen_isbns.add(isbn)
            if title_key:
                seen_titles.add(title_key)

            deduplicated.append(result)

        return deduplicated


# Singleton instance
book_service = BookService()
