#!/usr/bin/env python3
"""Rebuild incomplete media entries from scratch.

This script finds all incomplete media and rebuilds them properly:
- Films/Series: Search TMDB by title and replace ALL data with clean metadata
- Podcasts/Videos: Use YouTube URL to rebuild the entry completely

For TMDB searches, uses fuzzy matching to handle typos in titles.
Also handles special cases like AlloCiné URLs as titles.

Usage:
    python scripts/rebuild_incomplete.py [--dry-run] [--user-id=ID]

Options:
    --dry-run   Show what would be done without making changes
    --user-id   Only process media for a specific user
"""

import argparse
import asyncio
import re
import sys
from difflib import SequenceMatcher
from pathlib import Path

import httpx

# Add project root to path
sys.path.insert(0, str(Path(__file__).parent.parent))

from sqlalchemy import select

from src.db.crud import get_or_create_author, get_or_create_genre
from src.db.database import async_session_maker
from src.models.media import Media, MediaType
from src.services.metadata.tmdb import tmdb_service
from src.services.metadata.youtube import youtube_service


def is_incomplete(media: Media) -> bool:
    """Check if media is missing important metadata."""
    if not media.cover_url:
        return True
    if not media.description:
        return True
    if media.type in [MediaType.FILM, MediaType.SERIES]:
        if not media.year:
            return True
        if not media.authors or len(media.authors) == 0:
            return True
    if media.type in [MediaType.PODCAST, MediaType.YOUTUBE]:
        if not media.external_url:
            return True
    # Title is a URL (AlloCiné, IMDB, etc.)
    if media.title.startswith("http://") or media.title.startswith("https://"):
        return True
    return False


def is_allocine_url(title: str) -> bool:
    """Check if the title is an AlloCiné URL."""
    return "allocine.fr" in title.lower()


def is_imdb_url(title: str) -> bool:
    """Check if the title is an IMDB URL."""
    return "imdb.com" in title.lower()


def clean_allocine_url(url: str) -> str:
    """Clean AlloCiné URL - remove any text appended after the URL."""
    # Extract just the AlloCiné URL part
    # Pattern: https://www.allocine.fr/film/fichefilm_gen_cfilm=XXXXX.html
    match = re.search(r'(https?://[^\s]*allocine\.fr/(?:film|series)/[^\s]+\.html)', url)
    if match:
        return match.group(1)
    # Try without .html
    match = re.search(r'(https?://[^\s]*allocine\.fr/(?:film|series)/fichefilm[^\s]*cfilm=\d+)', url)
    if match:
        return match.group(1) + ".html"
    return url


async def get_title_from_allocine(url: str) -> str | None:
    """Extract movie/series title from AlloCiné URL by scraping the page."""
    try:
        # Clean the URL first (remove any appended text)
        clean_url = clean_allocine_url(url)

        async with httpx.AsyncClient(timeout=15.0, follow_redirects=True) as client:
            headers = {
                "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
                "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
                "Accept-Language": "fr-FR,fr;q=0.9",
            }
            response = await client.get(clean_url, headers=headers)

            if response.status_code != 200:
                return None

            html = response.text

            # Method 1: Try to extract from <title> tag
            # Format: "Film Name - Film 2024 - AlloCiné" or "Series Name - Série TV 2020 - AlloCiné"
            title_match = re.search(r"<title>([^<]+)</title>", html, re.IGNORECASE)
            if title_match:
                full_title = title_match.group(1)
                # Split by " - " and take first part (the actual title)
                parts = full_title.split(" - ")
                if parts:
                    title = parts[0].strip()
                    if title and len(title) > 1:
                        return title

            # Method 2: Try og:title meta tag
            og_match = re.search(r'<meta\s+property="og:title"\s+content="([^"]+)"', html, re.IGNORECASE)
            if og_match:
                title = og_match.group(1).split(" - ")[0].strip()
                if title and len(title) > 1:
                    return title

            # Method 3: Try the main title heading
            h1_match = re.search(r'<h1[^>]*class="[^"]*titlebar-title[^"]*"[^>]*>([^<]+)</h1>', html, re.IGNORECASE)
            if h1_match:
                return h1_match.group(1).strip()

            # Method 4: Try data-entity-title attribute
            entity_match = re.search(r'data-entity-title="([^"]+)"', html)
            if entity_match:
                return entity_match.group(1).strip()

    except Exception:
        pass

    return None


async def get_title_from_imdb(url: str) -> str | None:
    """Extract movie/series title from IMDB URL."""
    try:
        # Extract IMDB ID
        imdb_match = re.search(r"imdb\.com/title/(tt\d+)", url)
        if not imdb_match:
            return None

        imdb_id = imdb_match.group(1)

        async with httpx.AsyncClient(timeout=15.0, follow_redirects=True) as client:
            headers = {
                "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"
            }
            response = await client.get(url, headers=headers)

            if response.status_code != 200:
                return None

            html = response.text

            # Try to extract title from <title> tag
            # Format: "Movie Name (2024) - IMDb"
            title_match = re.search(r"<title>([^<]+)</title>", html, re.IGNORECASE)
            if title_match:
                full_title = title_match.group(1)
                # Remove " - IMDb" suffix
                title = full_title.replace(" - IMDb", "")
                # Remove year in parentheses at the end
                title = re.sub(r"\s*\(\d{4}\)\s*$", "", title)
                return title.strip()

    except Exception:
        pass

    return None


def is_wikipedia_url(title: str) -> bool:
    """Check if the title is a Wikipedia URL."""
    return "wikipedia.org" in title.lower()


async def get_title_from_wikipedia(url: str) -> str | None:
    """Extract movie/series title from Wikipedia URL."""
    try:
        async with httpx.AsyncClient(timeout=15.0, follow_redirects=True) as client:
            headers = {
                "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"
            }
            response = await client.get(url, headers=headers)

            if response.status_code != 200:
                return None

            html = response.text

            # Method 1: Extract from <title> tag
            # Format: "Le Labyrinthe : La Terre brûlée — Wikipédia"
            title_match = re.search(r"<title>([^<]+)</title>", html, re.IGNORECASE)
            if title_match:
                full_title = title_match.group(1)
                # Remove Wikipedia suffix
                title = re.sub(r"\s*[—–-]\s*Wikip[ée]dia.*$", "", full_title, flags=re.IGNORECASE)
                # Remove "(film)" or similar suffixes
                title = re.sub(r"\s*\((?:film|série|série télévisée|movie|TV series)\).*$", "", title, flags=re.IGNORECASE)
                if title and len(title) > 1:
                    return title.strip()

            # Method 2: Extract from h1 title
            h1_match = re.search(r'<h1[^>]*id="firstHeading"[^>]*>([^<]+)</h1>', html, re.IGNORECASE)
            if h1_match:
                title = h1_match.group(1).strip()
                # Remove "(film)" suffix
                title = re.sub(r"\s*\((?:film|série).*?\)$", "", title, flags=re.IGNORECASE)
                if title and len(title) > 1:
                    return title

    except Exception:
        pass

    return None


async def resolve_url_title(title: str) -> tuple[str | None, str]:
    """If title is a URL, try to extract the real title.

    Returns: (real_title or None, source_description)
    """
    if is_allocine_url(title):
        real_title = await get_title_from_allocine(title)
        if real_title:
            return real_title, f"AlloCiné: '{real_title}'"
        return None, "AlloCiné (failed to extract)"

    if is_imdb_url(title):
        real_title = await get_title_from_imdb(title)
        if real_title:
            return real_title, f"IMDB: '{real_title}'"
        return None, "IMDB (failed to extract)"

    if is_wikipedia_url(title):
        real_title = await get_title_from_wikipedia(title)
        if real_title:
            return real_title, f"Wikipedia: '{real_title}'"
        return None, "Wikipedia (failed to extract)"

    # Generic URL - can't extract title
    if title.startswith("http://") or title.startswith("https://"):
        return None, "Unknown URL"

    return title, "direct"


def normalize_title(title: str) -> str:
    """Normalize title for better search matching."""
    # Remove common suffixes
    title = re.sub(r'\s*\(\d{4}\)\s*$', '', title)
    title = re.sub(r'\s*-\s*Season\s*\d+\s*$', '', title, flags=re.IGNORECASE)
    title = re.sub(r'\s*-\s*Saison\s*\d+\s*$', '', title, flags=re.IGNORECASE)
    title = re.sub(r'\s*S\d+\s*$', '', title)
    title = re.sub(r'\s*Vol\.?\s*\d+\s*$', '', title, flags=re.IGNORECASE)
    # Remove special characters but keep accents
    title = re.sub(r'[:\-–—]', ' ', title)
    title = re.sub(r'\s+', ' ', title)
    return title.strip()


async def search_correct_title_google(title: str, media_type: str = "film") -> str | None:
    """Search Google for the correct movie/series title.

    Returns the corrected title or None if not found.
    """
    try:
        async with httpx.AsyncClient(timeout=15.0, follow_redirects=True) as client:
            type_word = "film" if media_type == "film" else "série TV"
            query = f"{title} {type_word}"

            headers = {
                "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
                "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
                "Accept-Language": "fr-FR,fr;q=0.9,en;q=0.8",
            }

            # Google search
            response = await client.get(
                "https://www.google.com/search",
                params={"q": query, "hl": "fr"},
                headers=headers,
            )

            if response.status_code != 200:
                return None

            html = response.text

            # Google often shows the correct title in various places:

            # 1. Knowledge panel title (most reliable)
            kp_match = re.search(r'data-attrid="title"[^>]*>([^<]+)<', html)
            if kp_match:
                found = kp_match.group(1).strip()
                if found and len(found) > 2:
                    return found

            # 2. Look for TMDB/IMDB/AlloCiné in results
            # TMDB
            tmdb_match = re.search(r'themoviedb\.org[^>]*>([^<]+)<', html)
            if tmdb_match:
                found = tmdb_match.group(1).strip()
                found = re.sub(r'\s*[-—–]\s*(?:The Movie|TMDB).*', '', found, flags=re.IGNORECASE)
                if found and len(found) > 2 and "themovie" not in found.lower():
                    return found

            # AlloCiné
            allocine_match = re.search(r'allocine\.fr[^>]*>([^<]+)<', html)
            if allocine_match:
                found = allocine_match.group(1).strip()
                found = re.sub(r'\s*[-—–]\s*(?:AlloCiné|Film \d{4}).*', '', found, flags=re.IGNORECASE)
                if found and len(found) > 2 and "allocine" not in found.lower():
                    return found

            # IMDB
            imdb_match = re.search(r'imdb\.com[^>]*>([^<]+)<', html)
            if imdb_match:
                found = imdb_match.group(1).strip()
                found = re.sub(r'\s*\(\d{4}\)\s*', '', found)
                found = re.sub(r'\s*[-—–]\s*IMDb.*', '', found, flags=re.IGNORECASE)
                if found and len(found) > 2 and "imdb" not in found.lower():
                    return found

            # 3. Wikipedia
            wiki_match = re.search(r'wikipedia\.org[^>]*>([^<]+)<', html)
            if wiki_match:
                found = wiki_match.group(1).strip()
                found = re.sub(r'\s*[-—–]\s*Wikip[ée]dia.*', '', found, flags=re.IGNORECASE)
                found = re.sub(r'\s*\((?:film|série).*?\)', '', found, flags=re.IGNORECASE)
                if found and len(found) > 2 and "wikipedia" not in found.lower():
                    return found

    except Exception:
        pass

    return None


async def search_correct_title(title: str, media_type: str = "film") -> str | None:
    """Search for the correct spelling of a movie/series title.

    Tries multiple sources: Google, then DuckDuckGo.

    This helps when titles have typos like:
    - "Bienvenu a Gattaca" -> "Bienvenue à Gattaca"
    - "Princesse Monoké" -> "Princesse Mononoké"
    - "Killers of the follow moon" -> "Killers of the Flower Moon"
    - "Zero dark day" -> "Zero Dark Thirty"
    - "Le chand du loup" -> "Le Chant du Loup"

    Returns the corrected title or None if not found.
    """
    # Try Google first (usually better results)
    result = await search_correct_title_google(title, media_type)
    if result:
        return result

    # Fallback to DuckDuckGo
    try:
        async with httpx.AsyncClient(timeout=10.0, follow_redirects=True) as client:
            type_word = "film" if media_type == "film" else "série"
            query = f"{title} {type_word}"

            headers = {
                "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"
            }
            params = {"q": query}
            response = await client.get(
                "https://html.duckduckgo.com/html/",
                params=params,
                headers=headers,
            )

            if response.status_code != 200:
                return None

            html = response.text

            # Extract from result snippets - DuckDuckGo format
            # Look for result titles
            result_titles = re.findall(r'class="result__a"[^>]*>([^<]+)<', html)
            for rt in result_titles:
                # Clean and check if it looks like a movie title
                clean = rt.strip()
                clean = re.sub(r'\s*[-—–]\s*(?:AlloCiné|IMDB|Wikipedia|TMDB|Film|Série).*', '', clean, flags=re.IGNORECASE)
                clean = re.sub(r'\s*\(\d{4}\)\s*', '', clean)
                if clean and len(clean) > 2 and len(clean) < 100:
                    # Check similarity - should be somewhat related to original
                    if any(word.lower() in clean.lower() for word in title.split()[:2]):
                        return clean

    except Exception:
        pass

    return None


def similarity_score(s1: str, s2: str) -> float:
    """Calculate similarity between two strings (0-1)."""
    s1 = s1.lower().strip()
    s2 = s2.lower().strip()
    return SequenceMatcher(None, s1, s2).ratio()


def find_best_match(title: str, results: list[dict], year: int | None = None) -> dict | None:
    """Find the best matching result using fuzzy matching.

    Considers:
    - Title similarity (original and local titles)
    - Year match (bonus points)
    - Popularity (tiebreaker)
    """
    if not results:
        return None

    normalized_title = normalize_title(title).lower()

    scored_results = []
    for result in results:
        # Get all title variants
        titles_to_check = [
            result.get("title", ""),
            result.get("local_title", ""),
            result.get("original_title", ""),
        ]

        # Calculate best title similarity
        best_similarity = 0
        for t in titles_to_check:
            if t:
                sim = similarity_score(normalized_title, normalize_title(t))
                best_similarity = max(best_similarity, sim)

        # Year bonus (if year matches exactly, big bonus)
        year_bonus = 0
        if year and result.get("year"):
            try:
                result_year = int(result["year"])
                if result_year == year:
                    year_bonus = 0.2
                elif abs(result_year - year) <= 1:
                    year_bonus = 0.1
            except (ValueError, TypeError):
                pass

        # Final score
        score = best_similarity + year_bonus
        scored_results.append((score, result))

    # Sort by score descending
    scored_results.sort(key=lambda x: x[0], reverse=True)

    # Return best match if score is acceptable (> 0.5)
    if scored_results and scored_results[0][0] > 0.5:
        return scored_results[0][1]

    # If no good match, return first result anyway (TMDB search is usually good)
    return results[0] if results else None


async def rebuild_film_or_series(
    db,
    media: Media,
    existing_external_ids: dict,
    dry_run: bool,
) -> tuple[str, str]:
    """Rebuild a film or series entry from TMDB.

    Returns: (status, message) where status is 'success', 'failed', or 'delete'
    - 'success': Entry was rebuilt successfully
    - 'failed': Could not rebuild (not found, etc.)
    - 'delete': Entry is a duplicate and should be deleted
    """
    is_film = media.type == MediaType.FILM
    type_name = "Film" if is_film else "Series"

    # Check if title is a URL (AlloCiné, IMDB, etc.)
    original_title = media.title
    search_title = media.title

    if original_title.startswith("http://") or original_title.startswith("https://"):
        resolved_title, source = await resolve_url_title(original_title)
        if resolved_title:
            search_title = resolved_title
            print(f"[{source}]", end=" ")
        else:
            return "failed", f"Could not extract title from URL ({source})"

    search_title = normalize_title(search_title)

    # First attempt: direct TMDB search
    if is_film:
        results = await tmdb_service.search_movies(search_title, year=media.year)
        if not results:
            results = await tmdb_service.search_movies(search_title)
    else:
        results = await tmdb_service.search_tv(search_title, year=media.year)
        if not results:
            results = await tmdb_service.search_tv(search_title)

    # Second attempt: if no results, try to correct spelling via web search
    if not results:
        media_type_str = "film" if is_film else "series"
        corrected_title = await search_correct_title(search_title, media_type_str)

        if corrected_title and corrected_title.lower() != search_title.lower():
            print(f"[Typo: '{search_title[:20]}' -> '{corrected_title[:20]}']", end=" ")
            search_title = corrected_title

            # Retry TMDB search with corrected title
            if is_film:
                results = await tmdb_service.search_movies(search_title, year=media.year)
                if not results:
                    results = await tmdb_service.search_movies(search_title)
            else:
                results = await tmdb_service.search_tv(search_title, year=media.year)
                if not results:
                    results = await tmdb_service.search_tv(search_title)

    if not results:
        return "failed", "NOT FOUND on TMDB (even after spell check)"

    # Find best match
    best_match = find_best_match(media.title, results, media.year)
    if not best_match:
        return "failed", "No good match found"

    # Get full details
    if is_film:
        info = await tmdb_service.get_movie_details(best_match["id"])
    else:
        info = await tmdb_service.get_tv_details(best_match["id"])

    if not info:
        return "failed", "Could not fetch details"

    # Check for duplicate external_id - if duplicate exists, DELETE this incomplete entry
    new_external_id = str(info["id"])
    new_key = (media.user_id, media.type, new_external_id)
    existing_media_id = existing_external_ids.get(new_key)
    if existing_media_id is not None and existing_media_id != media.id:
        # This is a duplicate - mark for deletion
        return "delete", f"Duplicate of existing entry (TMDB ID {new_external_id})"

    # Build update message
    new_title = info.get("title") or info.get("original_title") or original_title

    updates = []

    if not dry_run:
        # Replace ALL data
        media.title = new_title
        media.description = (info.get("description") or "")[:2000]
        media.cover_url = info.get("cover_url")
        media.year = int(info["year"]) if info.get("year") else None
        media.duration_minutes = info.get("duration_minutes")
        media.external_id = new_external_id
        media.external_url = info.get("external_url")
        existing_external_ids[new_key] = media.id

        # Directors/Creators
        if info.get("directors"):
            try:
                author_objects = []
                for director in info["directors"][:3]:
                    author = await get_or_create_author(db, director["name"], media.type)
                    author_objects.append(author)
                media.authors = author_objects
            except Exception:
                pass

        # Genres
        if info.get("genres"):
            try:
                genre_objects = []
                for genre_name in info["genres"][:5]:
                    genre = await get_or_create_genre(db, genre_name, media.type)
                    genre_objects.append(genre)
                media.genres = genre_objects
            except Exception:
                pass

    # Build message
    if original_title != new_title:
        if original_title.startswith("http"):
            updates.append(f"title: URL -> '{new_title[:30]}'")
        else:
            updates.append(f"title: '{original_title[:20]}' -> '{new_title[:20]}'")
    if info.get("year"):
        updates.append(f"year={info['year']}")
    if info.get("directors"):
        director_names = ", ".join(d["name"] for d in info["directors"][:2])
        updates.append(f"{'directors' if is_film else 'creators'}={director_names[:20]}")
    if info.get("cover_url"):
        updates.append("cover")

    return "success", f"Rebuilt: {', '.join(updates)}"


async def rebuild_podcast_or_video(
    db,
    media: Media,
    existing_external_ids: dict,
    dry_run: bool,
) -> tuple[bool, str]:
    """Rebuild a podcast or video entry from YouTube URL.

    Returns: (success, message)
    """
    if not media.external_url:
        return False, "No URL to fetch"

    # Extract video ID
    video_id = None
    patterns = [
        r"(?:youtube\.com/watch\?v=|youtu\.be/|youtube\.com/embed/)([a-zA-Z0-9_-]{11})",
        r"youtube\.com/shorts/([a-zA-Z0-9_-]{11})",
    ]
    for pattern in patterns:
        match = re.search(pattern, media.external_url)
        if match:
            video_id = match.group(1)
            break

    if not video_id:
        return False, "Invalid YouTube URL"

    # Fetch from YouTube
    info = await youtube_service.get_video_info(video_id)
    if not info:
        return False, "YouTube API returned no data (private/deleted?)"

    # Check for duplicate external_id
    new_key = (media.user_id, media.type, video_id)
    existing_media_id = existing_external_ids.get(new_key)
    if existing_media_id is not None and existing_media_id != media.id:
        return False, f"Duplicate (video ID {video_id} already used)"

    old_title = media.title
    new_title = info.get("title") or old_title

    updates = []

    if not dry_run:
        # Replace ALL data
        media.title = new_title
        media.description = (info.get("description") or "")[:2000]
        media.cover_url = info.get("cover_url")
        media.year = info.get("year")
        media.duration_minutes = info.get("duration_minutes")
        media.external_id = video_id
        existing_external_ids[new_key] = media.id

        # Author (channel name)
        if info.get("channel_name"):
            try:
                author = await get_or_create_author(db, info["channel_name"], media.type)
                media.authors = [author]
            except Exception:
                pass

    # Build message
    if old_title != new_title:
        updates.append(f"title: '{old_title[:20]}' -> '{new_title[:20]}'")
    if info.get("channel_name"):
        updates.append(f"channel={info['channel_name'][:15]}")
    if info.get("duration_minutes"):
        updates.append(f"duration={info['duration_minutes']}min")
    if info.get("cover_url"):
        updates.append("cover")

    return True, f"Rebuilt: {', '.join(updates)}"


async def rebuild_incomplete(
    dry_run: bool = False,
    user_id: int | None = None,
) -> None:
    """Rebuild all incomplete media entries."""

    async with async_session_maker() as db:
        # Get all media
        query = select(Media)
        if user_id:
            query = query.where(Media.user_id == user_id)

        result = await db.execute(query)
        all_media = result.scalars().all()

        # Filter to incomplete
        incomplete_media = [m for m in all_media if is_incomplete(m)]

        print(f"Found {len(incomplete_media)} incomplete media entries out of {len(all_media)} total\n")

        if not incomplete_media:
            print("Nothing to rebuild!")
            return

        # Build external_ids index
        existing_external_ids: dict[tuple[int, MediaType, str], int] = {}
        for m in all_media:
            if m.external_id:
                key = (m.user_id, m.type, m.external_id)
                existing_external_ids[key] = m.id

        # Group by type
        films = [m for m in incomplete_media if m.type == MediaType.FILM]
        series = [m for m in incomplete_media if m.type == MediaType.SERIES]
        podcasts = [m for m in incomplete_media if m.type == MediaType.PODCAST]
        videos = [m for m in incomplete_media if m.type == MediaType.YOUTUBE]

        # Results tracking
        results = {
            "films": {"success": 0, "failed": [], "to_delete": []},
            "series": {"success": 0, "failed": [], "to_delete": []},
            "podcasts": {"success": 0, "failed": [], "to_delete": []},
            "videos": {"success": 0, "failed": [], "to_delete": []},
        }

        # ========================================
        # FILMS
        # ========================================
        if films:
            print("=" * 60)
            print(f"FILMS ({len(films)} incomplete)")
            print("=" * 60)

            for i, media in enumerate(films, 1):
                print(f"[{i}/{len(films)}] {media.title[:50]:50}", end=" ")

                status, msg = await rebuild_film_or_series(
                    db, media, existing_external_ids, dry_run
                )

                if status == "success":
                    results["films"]["success"] += 1
                    print(f"- {msg}")
                elif status == "delete":
                    results["films"]["to_delete"].append(media.title)
                    print(f"- DELETED: {msg}")
                    if not dry_run:
                        await db.delete(media)
                else:
                    results["films"]["failed"].append(f"{media.title}: {msg}")
                    print(f"- FAILED: {msg}")

                await asyncio.sleep(0.25)

            if not dry_run:
                await db.commit()

        # ========================================
        # SERIES
        # ========================================
        if series:
            print("\n" + "=" * 60)
            print(f"SERIES ({len(series)} incomplete)")
            print("=" * 60)

            for i, media in enumerate(series, 1):
                print(f"[{i}/{len(series)}] {media.title[:50]:50}", end=" ")

                status, msg = await rebuild_film_or_series(
                    db, media, existing_external_ids, dry_run
                )

                if status == "success":
                    results["series"]["success"] += 1
                    print(f"- {msg}")
                elif status == "delete":
                    results["series"]["to_delete"].append(media.title)
                    print(f"- DELETED: {msg}")
                    if not dry_run:
                        await db.delete(media)
                else:
                    results["series"]["failed"].append(f"{media.title}: {msg}")
                    print(f"- FAILED: {msg}")

                await asyncio.sleep(0.25)

            if not dry_run:
                await db.commit()

        # ========================================
        # PODCASTS
        # ========================================
        if podcasts:
            print("\n" + "=" * 60)
            print(f"PODCASTS ({len(podcasts)} incomplete)")
            print("=" * 60)

            for i, media in enumerate(podcasts, 1):
                print(f"[{i}/{len(podcasts)}] {media.title[:50]:50}", end=" ")

                if not media.external_url:
                    results["podcasts"]["to_delete"].append(media.title)
                    print("- TO DELETE (no URL)")
                    if not dry_run:
                        await db.delete(media)
                    continue

                success, msg = await rebuild_podcast_or_video(
                    db, media, existing_external_ids, dry_run
                )

                if success:
                    results["podcasts"]["success"] += 1
                    print(f"- {msg}")
                else:
                    results["podcasts"]["failed"].append(f"{media.title}: {msg}")
                    print(f"- FAILED: {msg}")

                await asyncio.sleep(0.5)  # Rate limit for YouTube

            if not dry_run:
                await db.commit()

        # ========================================
        # VIDEOS
        # ========================================
        if videos:
            print("\n" + "=" * 60)
            print(f"VIDEOS ({len(videos)} incomplete)")
            print("=" * 60)

            for i, media in enumerate(videos, 1):
                print(f"[{i}/{len(videos)}] {media.title[:50]:50}", end=" ")

                if not media.external_url:
                    results["videos"]["to_delete"].append(media.title)
                    print("- TO DELETE (no URL)")
                    if not dry_run:
                        await db.delete(media)
                    continue

                success, msg = await rebuild_podcast_or_video(
                    db, media, existing_external_ids, dry_run
                )

                if success:
                    results["videos"]["success"] += 1
                    print(f"- {msg}")
                else:
                    results["videos"]["failed"].append(f"{media.title}: {msg}")
                    print(f"- FAILED: {msg}")

                await asyncio.sleep(0.5)  # Rate limit for YouTube

            if not dry_run:
                await db.commit()

        # ========================================
        # SUMMARY
        # ========================================
        print("\n" + "=" * 60)
        print("SUMMARY")
        print("=" * 60)

        print(f"\n{'[DRY RUN] ' if dry_run else ''}Results:\n")

        total_success = 0
        total_failed = 0
        total_deleted = 0

        for category, data in results.items():
            if data["success"] or data["failed"] or data["to_delete"]:
                print(f"  {category.upper()}:")
                print(f"    Rebuilt: {data['success']}")
                if data["to_delete"]:
                    print(f"    Deleted (duplicates/no URL): {len(data['to_delete'])}")
                if data["failed"]:
                    print(f"    Failed: {len(data['failed'])}")
                total_success += data["success"]
                total_failed += len(data["failed"])
                total_deleted += len(data["to_delete"])

        print(f"\n  TOTAL: {total_success} rebuilt, {total_deleted} deleted, {total_failed} failed")

        # Show failures
        all_failures = []
        for category, data in results.items():
            for failure in data["failed"]:
                all_failures.append(f"[{category}] {failure}")

        if all_failures:
            print(f"\n--- FAILURES ({len(all_failures)}) ---")
            for f in all_failures:
                print(f"  - {f}")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Rebuild incomplete media entries")
    parser.add_argument("--dry-run", action="store_true", help="Show what would be done without changes")
    parser.add_argument("--user-id", type=int, help="Only process media for a specific user")
    args = parser.parse_args()

    asyncio.run(rebuild_incomplete(
        dry_run=args.dry_run,
        user_id=args.user_id,
    ))
