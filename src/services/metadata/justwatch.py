"""JustWatch integration for streaming deep links.

This service uses JustWatch's internal GraphQL API to fetch direct streaming URLs.
Note: This is an unofficial API that may change without notice.
"""

from datetime import datetime
from typing import Any

import httpx

from src.utils.cache import CACHE_TTL_LONG, cache, make_cache_key

JUSTWATCH_GRAPHQL_URL = "https://apis.justwatch.com/graphql"

# GraphQL query to search for titles
SEARCH_QUERY = """
query GetSearchTitles($searchTitlesFilter: TitleFilter!, $country: Country!, $language: Language!, $first: Int!) {
  popularTitles(country: $country, filter: $searchTitlesFilter, first: $first) {
    edges {
      node {
        id
        objectId
        objectType
        content(country: $country, language: $language) {
          title
          fullPath
          originalReleaseYear
          externalIds {
            tmdbId
          }
        }
      }
    }
  }
}
"""

# GraphQL query to get offers by URL path
OFFERS_BY_PATH_QUERY = """
query GetUrlTitleDetails($fullPath: String!, $country: Country!) {
  urlV2(fullPath: $fullPath) {
    node {
      ... on MovieOrShowOrSeason {
        objectType
        objectId
        offers(country: $country, platform: WEB) {
          monetizationType
          standardWebURL
          package {
            clearName
            packageId
          }
        }
      }
    }
  }
}
"""

# Mapping of JustWatch package IDs to TMDB provider IDs
# JustWatch uses different IDs than TMDB for some providers
# We normalize to TMDB IDs since that's what user settings use
PACKAGE_TO_TMDB = {
    8: 8,       # Netflix
    9: 119,     # Amazon Prime Video (JustWatch uses 9, TMDB uses 119)
    10: 10,     # Amazon Video (rent/buy)
    119: 119,   # Amazon Prime Video (TMDB ID)
    1024: 119,  # Amazon Prime Video (another alt)
    337: 337,   # Disney Plus
    390: 337,   # Disney+ (alt ID)
    381: 381,   # Canal+
    350: 350,   # Apple TV+
    2: 2,       # Apple iTunes
    56: 56,     # OCS
    531: 531,   # Paramount+
    582: 531,   # Paramount+ (alt)
    283: 283,   # Crunchyroll
    415: 415,   # ADN
    236: 236,   # France TV
    234: 234,   # Arte
    1899: 1899, # Max
    1825: 1899, # Max (alt)
    15: 15,     # Hulu
    386: 386,   # Peacock
    3: 3,       # Google Play Movies
    192: 192,   # YouTube
    188: 192,   # YouTube Premium
    1967: 1967, # Molotov TV
    685: 685,   # Cine+ OCS Amazon Channel
    11: 11,     # Mubi
    175: 175,   # Netflix Kids
    300: 300,   # Pluto TV
}

# Country code to JustWatch locale mapping
COUNTRY_LOCALES = {
    "FR": ("fr_FR", "fr"),
    "US": ("en_US", "en"),
    "GB": ("en_GB", "en"),
    "DE": ("de_DE", "de"),
    "ES": ("es_ES", "es"),
    "IT": ("it_IT", "it"),
    "CA": ("en_CA", "en"),
    "AU": ("en_AU", "en"),
    "JP": ("ja_JP", "ja"),
    "BR": ("pt_BR", "pt"),
    "BE": ("fr_BE", "fr"),
    "CH": ("fr_CH", "fr"),
    "NL": ("nl_NL", "nl"),
}


class JustWatchService:
    """Service for fetching streaming deep links from JustWatch."""

    def __init__(self) -> None:
        self.headers = {
            "Content-Type": "application/json",
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
        }

    async def get_streaming_links(
        self,
        tmdb_id: int,
        media_type: str,  # "movie" or "tv"
        country: str = "FR",
        title: str | None = None,
        year: int | None = None,
    ) -> dict[str, Any] | None:
        """Get streaming deep links for a title.

        Args:
            tmdb_id: TMDB ID of the movie/TV show
            media_type: "movie" or "tv"
            country: ISO 3166-1 alpha-2 country code
            title: Optional title for search (improves matching)
            year: Optional release year for search

        Returns:
            Dict with provider_id -> deep_link mapping, or None if not found
        """
        cache_key = make_cache_key("justwatch", tmdb_id, media_type, country=country)

        # Try Redis cache first
        cached = await cache.get(cache_key)
        if cached is not None:
            return cached

        try:
            result = await self._fetch_offers(tmdb_id, media_type, country, title, year)
            if result:
                await cache.set(cache_key, result, ttl=CACHE_TTL_LONG)
            return result
        except Exception as e:
            print(f"JustWatch API error: {e}")
            return None

    async def _search_title(
        self,
        tmdb_id: int,
        media_type: str,
        country: str,
        title: str | None,
        year: int | None,
    ) -> str | None:
        """Search for a title and return its JustWatch path."""
        locale_info = COUNTRY_LOCALES.get(country, ("en_US", "en"))
        language = locale_info[1]

        # Check Redis cache for path first
        path_cache_key = make_cache_key("justwatch:path", tmdb_id, media_type, country=country)
        cached_path = await cache.get(path_cache_key)
        if cached_path:
            return cached_path

        # Build search filter
        object_type = "MOVIE" if media_type == "movie" else "SHOW"
        search_filter: dict[str, Any] = {"objectTypes": [object_type]}

        if tmdb_id:
            search_filter["searchQuery"] = str(tmdb_id)

        async with httpx.AsyncClient(timeout=10.0) as client:
            response = await client.post(
                JUSTWATCH_GRAPHQL_URL,
                headers=self.headers,
                json={
                    "query": SEARCH_QUERY,
                    "variables": {
                        "searchTitlesFilter": search_filter,
                        "country": country,
                        "language": language,
                        "first": 10,
                    },
                },
            )

            if response.status_code != 200:
                return None

            data = response.json()
            edges = data.get("data", {}).get("popularTitles", {}).get("edges", [])

            # Find matching title by TMDB ID
            for edge in edges:
                node = edge.get("node", {})
                content = node.get("content", {})
                external_ids = content.get("externalIds", {})

                if external_ids and str(external_ids.get("tmdbId")) == str(tmdb_id):
                    full_path = content.get("fullPath")
                    if full_path:
                        await cache.set(path_cache_key, full_path, ttl=CACHE_TTL_LONG)
                        return full_path

            # Fallback: search by title if provided
            if title:
                search_filter["searchQuery"] = title
                response = await client.post(
                    JUSTWATCH_GRAPHQL_URL,
                    headers=self.headers,
                    json={
                        "query": SEARCH_QUERY,
                        "variables": {
                            "searchTitlesFilter": search_filter,
                            "country": country,
                            "language": language,
                            "first": 10,
                        },
                    },
                )

                if response.status_code == 200:
                    data = response.json()
                    edges = data.get("data", {}).get("popularTitles", {}).get("edges", [])

                    for edge in edges:
                        node = edge.get("node", {})
                        content = node.get("content", {})
                        external_ids = content.get("externalIds", {})

                        # Match by TMDB ID
                        if external_ids and str(external_ids.get("tmdbId")) == str(tmdb_id):
                            full_path = content.get("fullPath")
                            if full_path:
                                await cache.set(path_cache_key, full_path, ttl=CACHE_TTL_LONG)
                                return full_path

                        # Match by year if TMDB ID not available
                        if year and content.get("originalReleaseYear") == year:
                            full_path = content.get("fullPath")
                            if full_path:
                                await cache.set(path_cache_key, full_path, ttl=CACHE_TTL_LONG)
                                return full_path

            return None

    async def _fetch_offers(
        self,
        tmdb_id: int,
        media_type: str,
        country: str,
        title: str | None = None,
        year: int | None = None,
    ) -> dict[str, Any] | None:
        """Fetch offers for a title."""
        # First, find the JustWatch path for this title
        full_path = await self._search_title(tmdb_id, media_type, country, title, year)

        if not full_path:
            return None

        async with httpx.AsyncClient(timeout=10.0) as client:
            response = await client.post(
                JUSTWATCH_GRAPHQL_URL,
                headers=self.headers,
                json={
                    "query": OFFERS_BY_PATH_QUERY,
                    "variables": {
                        "fullPath": full_path,
                        "country": country,
                    },
                },
            )

            if response.status_code != 200:
                return None

            data = response.json()
            node = data.get("data", {}).get("urlV2", {}).get("node")

            if not node:
                return None

            return self._parse_offers(node.get("offers", []))

    def _parse_offers(self, offers: list[dict]) -> dict[str, Any]:
        """Parse JustWatch offers into our format."""
        result: dict[str, Any] = {
            "links": {},  # provider_id -> {url, type, provider_name}
            "fetched_at": datetime.now().isoformat(),
        }

        seen_providers: set[int] = set()

        for offer in offers:
            package = offer.get("package", {})
            package_id = package.get("packageId")
            url = offer.get("standardWebURL")
            monetization = offer.get("monetizationType", "").lower()

            if not url or not package_id:
                continue

            # Map to TMDB provider ID if possible, otherwise use package ID
            provider_id = PACKAGE_TO_TMDB.get(package_id, package_id)

            # Skip duplicates, prefer flatrate over others
            if provider_id in seen_providers:
                existing = result["links"].get(str(provider_id), {})
                if existing.get("type") == "flatrate":
                    continue  # Keep existing flatrate link
                if monetization != "flatrate":
                    continue  # Don't replace with non-flatrate

            seen_providers.add(provider_id)
            result["links"][str(provider_id)] = {
                "url": url,
                "type": monetization,  # flatrate, rent, buy
                "provider_name": package.get("clearName"),
            }

        return result

    async def health_check(self) -> dict[str, Any]:
        """Check if the JustWatch API is working as expected.

        Tests with a known movie path (Inception) to verify:
        1. API is reachable
        2. Response structure is as expected
        3. Deep links are being returned

        Returns:
            Dict with status, message, and details
        """
        test_path = "/fr/film/inception"
        test_country = "FR"

        try:
            async with httpx.AsyncClient(timeout=10.0) as client:
                response = await client.post(
                    JUSTWATCH_GRAPHQL_URL,
                    headers=self.headers,
                    json={
                        "query": OFFERS_BY_PATH_QUERY,
                        "variables": {
                            "fullPath": test_path,
                            "country": test_country,
                        },
                    },
                )

                if response.status_code != 200:
                    return {
                        "status": "error",
                        "message": f"API returned status {response.status_code}",
                        "details": {},
                    }

                data = response.json()

                if "errors" in data:
                    return {
                        "status": "error",
                        "message": "GraphQL errors in response",
                        "details": {"errors": data["errors"]},
                    }

                node = data.get("data", {}).get("urlV2", {}).get("node")

                if not node:
                    return {
                        "status": "error",
                        "message": "No data returned for test movie",
                        "details": {"path": test_path},
                    }

                offers = node.get("offers", [])
                if not offers:
                    return {
                        "status": "warning",
                        "message": "API responded but no streaming offers found",
                        "details": {"path": test_path},
                    }

                # Verify offer structure
                sample_offer = offers[0]
                if not sample_offer.get("standardWebURL"):
                    return {
                        "status": "error",
                        "message": "Offer structure changed - missing standardWebURL",
                        "details": {"sample_offer": sample_offer},
                    }

                # Count unique providers
                providers = set()
                for offer in offers:
                    pkg = offer.get("package", {})
                    if pkg.get("clearName"):
                        providers.add(pkg["clearName"])

                return {
                    "status": "ok",
                    "message": f"API working - found {len(offers)} offers from {len(providers)} providers",
                    "details": {
                        "providers_found": list(providers)[:10],
                        "sample_url": sample_offer.get("standardWebURL", "")[:80] + "...",
                    },
                }

        except httpx.TimeoutException:
            return {
                "status": "error",
                "message": "API request timed out",
                "details": {},
            }
        except Exception as e:
            return {
                "status": "error",
                "message": f"API error: {str(e)}",
                "details": {},
            }


# Singleton instance
justwatch_service = JustWatchService()
