#!/usr/bin/env python3
"""Enrich Series with creators from Wikipedia.

This script finds series missing creators and tries to fetch them from Wikipedia.
TMDB often lacks creator information for series, but Wikipedia usually has it.

Usage:
    python scripts/enrich_series_creators.py [--dry-run] [--user-id=ID] [--all]

Options:
    --dry-run   Show what would be done without making changes
    --user-id   Only process media for a specific user
    --all       Process all series (not just those missing creators)
"""

import argparse
import asyncio
import re
import sys
from pathlib import Path

import httpx

# Add project root to path
sys.path.insert(0, str(Path(__file__).parent.parent))

from sqlalchemy import select
from sqlalchemy.orm import selectinload

from src.db.crud import get_or_create_author
from src.db.database import async_session_maker
from src.models.media import Media, MediaType


async def get_creators_from_wikipedia(title: str, year: int | None = None) -> list[str]:
    """Fetch series creators from Wikipedia.

    Searches Wikipedia for the series and extracts creator names from the infobox.

    Returns: List of creator names
    """
    creators = []

    try:
        async with httpx.AsyncClient(timeout=15.0, follow_redirects=True) as client:
            headers = {
                "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"
            }

            # Search Wikipedia for the series
            search_query = f"{title} série télévisée" if year else f"{title} TV series"
            search_url = "https://fr.wikipedia.org/w/api.php"
            search_params = {
                "action": "query",
                "list": "search",
                "srsearch": search_query,
                "format": "json",
                "srlimit": 5,
            }

            response = await client.get(search_url, params=search_params, headers=headers)
            if response.status_code != 200:
                return creators

            data = response.json()
            search_results = data.get("query", {}).get("search", [])

            if not search_results:
                # Try English Wikipedia
                search_url = "https://en.wikipedia.org/w/api.php"
                search_params["srsearch"] = f"{title} TV series"
                response = await client.get(search_url, params=search_params, headers=headers)
                if response.status_code != 200:
                    return creators
                data = response.json()
                search_results = data.get("query", {}).get("search", [])

            if not search_results:
                return creators

            # Get the first result's page
            page_title = search_results[0]["title"]
            wiki_lang = "fr" if "fr.wikipedia" in search_url else "en"

            # Fetch the page content
            page_url = f"https://{wiki_lang}.wikipedia.org/wiki/{page_title.replace(' ', '_')}"
            response = await client.get(page_url, headers=headers)

            if response.status_code != 200:
                return creators

            html = response.text

            # Extract creators from infobox
            # French Wikipedia: "Créateur" or "Créé par"
            # English Wikipedia: "Created by"

            # Pattern for French Wikipedia infobox
            fr_patterns = [
                r'(?:Créateur|Créé par|Créateurs)[^<]*?</th>\s*<td[^>]*>(.*?)</td>',
                r'(?:Créateur|Créé par|Créateurs)\s*</th>\s*<td[^>]*>(.*?)</td>',
                r'data-wikidata-property-id="P170"[^>]*>.*?<td[^>]*>(.*?)</td>',
            ]

            # Pattern for English Wikipedia infobox
            en_patterns = [
                r'(?:Created by|Creator)[^<]*?</th>\s*<td[^>]*>(.*?)</td>',
                r'(?:Created by|Creator)\s*</th>\s*<td[^>]*>(.*?)</td>',
            ]

            patterns = fr_patterns if wiki_lang == "fr" else en_patterns

            for pattern in patterns:
                match = re.search(pattern, html, re.IGNORECASE | re.DOTALL)
                if match:
                    creator_html = match.group(1)

                    # Extract names from links or plain text
                    # First try to get names from <a> tags
                    link_names = re.findall(r'<a[^>]*title="([^"]+)"[^>]*>', creator_html)
                    if link_names:
                        for name in link_names:
                            # Filter out non-person pages
                            if not any(x in name.lower() for x in ['(page', 'wiki', 'catégorie', 'category', 'modifier']):
                                # Clean up name
                                clean_name = re.sub(r'\s*\([^)]*\)\s*$', '', name)
                                if clean_name and len(clean_name) > 2:
                                    creators.append(clean_name)

                    # If no links, try plain text
                    if not creators:
                        # Remove HTML tags
                        plain_text = re.sub(r'<[^>]+>', '', creator_html)
                        # Split by common separators
                        names = re.split(r'[,;]|\bet\b|\band\b', plain_text)
                        for name in names:
                            clean_name = name.strip()
                            if clean_name and len(clean_name) > 2 and not clean_name.startswith('['):
                                creators.append(clean_name)

                    if creators:
                        break

            # Also try to find in the first paragraph
            if not creators:
                # Look for "créée par X" or "created by X" in intro
                intro_patterns = [
                    r'(?:créée? par|réalisée? par)\s+([A-ZÀ-Ü][a-zà-ü]+(?:\s+[A-ZÀ-Ü][a-zà-ü]+)+)',
                    r'(?:created by|developed by)\s+([A-Z][a-z]+(?:\s+[A-Z][a-z]+)+)',
                ]
                for pattern in intro_patterns:
                    matches = re.findall(pattern, html, re.IGNORECASE)
                    if matches:
                        creators.extend(matches[:3])
                        break

    except Exception:
        pass

    # Remove duplicates while preserving order
    seen = set()
    unique_creators = []
    for c in creators:
        if c.lower() not in seen:
            seen.add(c.lower())
            unique_creators.append(c)

    return unique_creators[:3]  # Limit to 3 creators


async def get_creators_from_imdb(title: str) -> list[str]:
    """Fetch series creators from IMDB as fallback.

    Returns: List of creator names
    """
    creators = []

    try:
        async with httpx.AsyncClient(timeout=15.0, follow_redirects=True) as client:
            headers = {
                "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"
            }

            # Search IMDB
            search_url = f"https://www.imdb.com/find/?q={title}&s=tt&ttype=tv"
            response = await client.get(search_url, headers=headers)

            if response.status_code != 200:
                return creators

            html = response.text

            # Find first TV series result
            match = re.search(r'/title/(tt\d+)/', html)
            if not match:
                return creators

            imdb_id = match.group(1)

            # Fetch the title page
            title_url = f"https://www.imdb.com/title/{imdb_id}/"
            response = await client.get(title_url, headers=headers)

            if response.status_code != 200:
                return creators

            html = response.text

            # Look for creators in the page
            # IMDB shows "Creators:" or "Creator:" section
            creator_patterns = [
                r'(?:Creators?|Created by)[^<]*</span>\s*<div[^>]*>(.*?)</div>',
                r'"creator":\s*\[(.*?)\]',
                r'Creators?:</span>.*?<a[^>]*>([^<]+)</a>',
            ]

            for pattern in creator_patterns:
                match = re.search(pattern, html, re.IGNORECASE | re.DOTALL)
                if match:
                    content = match.group(1)

                    # Extract names from links
                    names = re.findall(r'>([^<]+)</a>', content)
                    if names:
                        creators.extend(n.strip() for n in names if len(n.strip()) > 2)
                        break

                    # Try JSON format
                    json_names = re.findall(r'"name":\s*"([^"]+)"', content)
                    if json_names:
                        creators.extend(json_names)
                        break

    except Exception:
        pass

    return creators[:3]


async def enrich_series_creators(
    dry_run: bool = False,
    user_id: int | None = None,
    process_all: bool = False,
) -> None:
    """Enrich series with creators from Wikipedia/IMDB."""

    async with async_session_maker() as db:
        # Get all series
        query = (
            select(Media)
            .where(Media.type == MediaType.SERIES)
            .options(selectinload(Media.authors))
        )

        if user_id:
            query = query.where(Media.user_id == user_id)

        result = await db.execute(query)
        all_series = result.scalars().all()

        # Filter to those missing creators
        if not process_all:
            series_list = [s for s in all_series if not s.authors or len(s.authors) == 0]
        else:
            series_list = list(all_series)

        print(f"Found {len(series_list)} series to process (out of {len(all_series)} total)\n")

        if not series_list:
            print("Nothing to process!")
            return

        enriched = 0
        not_found = []
        errors = []

        for i, series in enumerate(series_list, 1):
            print(f"[{i}/{len(series_list)}] {series.title[:45]:45}", end=" ")

            try:
                # First try Wikipedia
                creators = await get_creators_from_wikipedia(series.title, series.year)

                source = "Wikipedia"

                # If not found, try IMDB
                if not creators:
                    creators = await get_creators_from_imdb(series.title)
                    source = "IMDB"

                if creators:
                    creator_names = ", ".join(creators[:2])
                    print(f"- [{source}] {creator_names}")

                    if not dry_run:
                        try:
                            author_objects = []
                            for creator_name in creators[:3]:
                                author = await get_or_create_author(db, creator_name, MediaType.SERIES)
                                author_objects.append(author)
                            series.authors = author_objects
                        except Exception as e:
                            errors.append(f"{series.title}: Failed to save creators - {e}")
                            continue

                    enriched += 1
                else:
                    print("- NOT FOUND")
                    not_found.append(series.title)

                # Rate limiting
                await asyncio.sleep(0.5)

            except Exception as e:
                print(f"- ERROR: {e}")
                errors.append(f"{series.title}: {str(e)}")

        if not dry_run:
            await db.commit()

        # Summary
        print("\n" + "=" * 60)
        print("SUMMARY")
        print("=" * 60)

        print(f"\n{'[DRY RUN] ' if dry_run else ''}Results:\n")
        print(f"  Enriched with creators: {enriched}")
        print(f"  Not found: {len(not_found)}")
        print(f"  Errors: {len(errors)}")

        if not_found:
            print(f"\n--- NOT FOUND ({len(not_found)}) ---")
            for title in not_found[:20]:
                print(f"  - {title}")
            if len(not_found) > 20:
                print(f"  ... and {len(not_found) - 20} more")

        if errors:
            print(f"\n--- ERRORS ({len(errors)}) ---")
            for error in errors[:10]:
                print(f"  - {error}")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Enrich series with creators from Wikipedia")
    parser.add_argument("--dry-run", action="store_true", help="Show what would be done without changes")
    parser.add_argument("--user-id", type=int, help="Only process media for a specific user")
    parser.add_argument("--all", action="store_true", help="Process all series, not just those missing creators")
    args = parser.parse_args()

    asyncio.run(enrich_series_creators(
        dry_run=args.dry_run,
        user_id=args.user_id,
        process_all=args.all,
    ))
