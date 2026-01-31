"""TMDB API integration for movie metadata."""

from typing import Any

from src.config import get_settings
from src.utils.cache import CACHE_TTL_LONG, CACHE_TTL_MEDIUM, cached
from src.utils.http_client import get_tmdb_client

settings = get_settings()

TMDB_BASE_URL = "https://api.themoviedb.org/3"
TMDB_IMAGE_BASE = "https://image.tmdb.org/t/p"


class TMDBService:
    """Service for fetching movie metadata from TMDB."""

    def __init__(self) -> None:
        self.api_key = settings.tmdb_api_key
        # Support both API key v3 and Bearer token
        if self.api_key and self.api_key.startswith("eyJ"):
            # Bearer token (API Read Access Token)
            self.headers = {
                "Authorization": f"Bearer {self.api_key}",
                "Accept": "application/json",
            }
            self.use_api_key_param = False
        else:
            # API key v3 - pass as query parameter
            self.headers = {"Accept": "application/json"}
            self.use_api_key_param = True

    def _add_api_key(self, params: dict) -> dict:
        """Add API key to params if using v3 key."""
        if self.use_api_key_param:
            params["api_key"] = self.api_key
        return params

    async def search_movies(
        self,
        query: str,
        year: int | None = None,
        language: str = "fr-FR",
    ) -> list[dict[str, Any]]:
        """Search for movies by title."""
        if not self.api_key:
            return []

        params = self._add_api_key({
            "query": query,
            "language": language,
            "include_adult": "false",
        })
        if year:
            params["year"] = str(year)

        client = get_tmdb_client()
        response = await client.get(
            f"{TMDB_BASE_URL}/search/movie",
            params=params,
            headers=self.headers,
        )

        if response.status_code != 200:
            return []

        data = response.json()
        results = []

        for movie in data.get("results", [])[:10]:
            original_title = movie.get("original_title", "")
            local_title = movie.get("title", "")

            # Build display title: Original (French) if different
            if original_title and local_title and original_title != local_title:
                display_title = f"{original_title} ({local_title})"
            else:
                display_title = original_title or local_title

            results.append(
                {
                    "id": movie["id"],
                    "title": original_title or local_title,  # Store original as main title
                    "local_title": local_title,
                    "original_title": original_title,
                    "display_title": display_title,
                    "year": movie.get("release_date", "")[:4] or None,
                    "overview": movie.get("overview"),
                    "poster_path": movie.get("poster_path"),
                    "poster_url": (
                        f"{TMDB_IMAGE_BASE}/w342{movie['poster_path']}"
                        if movie.get("poster_path")
                        else None
                    ),
                    "vote_average": movie.get("vote_average"),
                }
            )

        return results

    async def get_movie_details(
        self,
        tmdb_id: int,
        language: str = "fr-FR",
        country: str = "FR",
    ) -> dict[str, Any] | None:
        """Get detailed movie information including credits, keywords, and certifications."""
        if not self.api_key:
            return None

        return await self._fetch_movie_details(tmdb_id, language, country)

    @cached("tmdb:movie", ttl=CACHE_TTL_MEDIUM)
    async def _fetch_movie_details(
        self,
        tmdb_id: int,
        language: str = "fr-FR",
        country: str = "FR",
    ) -> dict[str, Any] | None:
        """Fetch movie details from TMDB API (cached)."""
        client = get_tmdb_client()
        # Get movie details with credits, keywords, and release dates (for certification)
        params = self._add_api_key({
            "language": language,
            "append_to_response": "credits,keywords,release_dates",
        })
        response = await client.get(
            f"{TMDB_BASE_URL}/movie/{tmdb_id}",
            params=params,
            headers=self.headers,
        )

        if response.status_code != 200:
            return None

        movie = response.json()

        # Extract directors from credits
        directors = []
        credits = movie.get("credits", {})
        for crew_member in credits.get("crew", []):
            if crew_member.get("job") == "Director":
                directors.append(
                    {
                        "id": crew_member["id"],
                        "name": crew_member["name"],
                    }
                )

        # Extract top cast (first 10 actors)
        cast = []
        for actor in credits.get("cast", [])[:10]:
            cast.append({
                "id": actor["id"],
                "name": actor["name"],
                "character": actor.get("character"),
                "profile_path": f"{TMDB_IMAGE_BASE}/w185{actor['profile_path']}" if actor.get("profile_path") else None,
            })

        # Extract genres
        genres = [genre["name"] for genre in movie.get("genres", [])]

        # Extract keywords
        keywords = [kw["name"] for kw in movie.get("keywords", {}).get("keywords", [])]

        # Extract certification for the specified country
        certification = None
        release_dates = movie.get("release_dates", {}).get("results", [])
        for release in release_dates:
            if release.get("iso_3166_1") == country:
                for date_info in release.get("release_dates", []):
                    if date_info.get("certification"):
                        certification = date_info["certification"]
                        break
                break
        # Fallback to US certification if not found
        if not certification:
            for release in release_dates:
                if release.get("iso_3166_1") == "US":
                    for date_info in release.get("release_dates", []):
                        if date_info.get("certification"):
                            certification = date_info["certification"]
                            break
                    break

        # Extract production countries
        production_countries = [c["iso_3166_1"] for c in movie.get("production_countries", [])]

        # Extract collection info
        collection = movie.get("belongs_to_collection")
        collection_id = collection["id"] if collection else None
        collection_name = collection["name"] if collection else None

        # Build titles
        original_title = movie.get("original_title", "")
        local_title = movie.get("title", "")

        return {
            "id": movie["id"],
            "title": original_title or local_title,
            "local_title": local_title,
            "original_title": original_title,
            "year": movie.get("release_date", "")[:4] or None,
            "description": movie.get("overview"),
            "duration_minutes": movie.get("runtime"),
            "cover_url": (
                f"{TMDB_IMAGE_BASE}/w500{movie['poster_path']}"
                if movie.get("poster_path")
                else None
            ),
            "external_url": f"https://www.themoviedb.org/movie/{movie['id']}",
            "genres": genres,
            "directors": directors,
            # Extended metadata
            "tmdb_rating": movie.get("vote_average"),
            "tmdb_vote_count": movie.get("vote_count"),
            "popularity": movie.get("popularity"),
            "budget": movie.get("budget") or None,
            "revenue": movie.get("revenue") or None,
            "original_language": movie.get("original_language"),
            "production_countries": production_countries,
            "cast": cast,
            "keywords": keywords,
            "collection_id": collection_id,
            "collection_name": collection_name,
            "certification": certification,
            "tagline": movie.get("tagline") or None,
        }

    async def search_tv(
        self,
        query: str,
        year: int | None = None,
        language: str = "fr-FR",
    ) -> list[dict[str, Any]]:
        """Search for TV series by title."""
        if not self.api_key:
            return []

        params = self._add_api_key({
            "query": query,
            "language": language,
            "include_adult": "false",
        })
        if year:
            params["first_air_date_year"] = str(year)

        client = get_tmdb_client()
        response = await client.get(
            f"{TMDB_BASE_URL}/search/tv",
            params=params,
            headers=self.headers,
        )

        if response.status_code != 200:
            return []

        data = response.json()
        results = []

        for show in data.get("results", [])[:10]:
            original_title = show.get("original_name", "")
            local_title = show.get("name", "")

            # Build display title: Original (French) if different
            if original_title and local_title and original_title != local_title:
                display_title = f"{original_title} ({local_title})"
            else:
                display_title = original_title or local_title

            results.append(
                {
                    "id": show["id"],
                    "title": original_title or local_title,  # Store original as main title
                    "local_title": local_title,
                    "original_title": original_title,
                    "display_title": display_title,
                    "year": show.get("first_air_date", "")[:4] or None,
                    "overview": show.get("overview"),
                    "poster_path": show.get("poster_path"),
                    "poster_url": (
                        f"{TMDB_IMAGE_BASE}/w342{show['poster_path']}"
                        if show.get("poster_path")
                        else None
                    ),
                    "vote_average": show.get("vote_average"),
                }
            )

        return results

    async def get_tv_details(
        self,
        tmdb_id: int,
        language: str = "fr-FR",
        country: str = "FR",
    ) -> dict[str, Any] | None:
        """Get detailed TV series information including credits, keywords, and content ratings."""
        if not self.api_key:
            return None

        return await self._fetch_tv_details(tmdb_id, language, country)

    @cached("tmdb:tv", ttl=CACHE_TTL_MEDIUM)
    async def _fetch_tv_details(
        self,
        tmdb_id: int,
        language: str = "fr-FR",
        country: str = "FR",
    ) -> dict[str, Any] | None:
        """Fetch TV details from TMDB API (cached)."""
        client = get_tmdb_client()
        # Get TV details with credits, keywords, and content ratings
        params = self._add_api_key({
            "language": language,
            "append_to_response": "credits,keywords,content_ratings",
        })
        response = await client.get(
            f"{TMDB_BASE_URL}/tv/{tmdb_id}",
            params=params,
            headers=self.headers,
        )

        if response.status_code != 200:
            return None

        show = response.json()

        # Extract creators
        creators = []
        for creator in show.get("created_by", []):
            creators.append(
                {
                    "id": creator["id"],
                    "name": creator["name"],
                }
            )

        # Extract top cast (first 10 actors)
        cast = []
        credits = show.get("credits", {})
        for actor in credits.get("cast", [])[:10]:
            cast.append({
                "id": actor["id"],
                "name": actor["name"],
                "character": actor.get("character"),
                "profile_path": f"{TMDB_IMAGE_BASE}/w185{actor['profile_path']}" if actor.get("profile_path") else None,
            })

        # Extract genres
        genres = [genre["name"] for genre in show.get("genres", [])]

        # Extract keywords
        keywords = [kw["name"] for kw in show.get("keywords", {}).get("results", [])]

        # Extract certification/content rating for the specified country
        certification = None
        content_ratings = show.get("content_ratings", {}).get("results", [])
        for rating in content_ratings:
            if rating.get("iso_3166_1") == country:
                certification = rating.get("rating")
                break
        # Fallback to US rating if not found
        if not certification:
            for rating in content_ratings:
                if rating.get("iso_3166_1") == "US":
                    certification = rating.get("rating")
                    break

        # Extract production countries
        production_countries = [c["iso_3166_1"] for c in show.get("production_countries", [])]

        # Extract networks
        networks = []
        for network in show.get("networks", []):
            networks.append({
                "id": network["id"],
                "name": network["name"],
                "logo_path": f"{TMDB_IMAGE_BASE}/w92{network['logo_path']}" if network.get("logo_path") else None,
            })

        # Calculate average episode runtime
        episode_runtimes = show.get("episode_run_time", [])
        avg_runtime = episode_runtimes[0] if episode_runtimes else None

        # Build titles
        original_title = show.get("original_name", "")
        local_title = show.get("name", "")

        return {
            "id": show["id"],
            "title": original_title or local_title,
            "local_title": local_title,
            "original_title": original_title,
            "year": show.get("first_air_date", "")[:4] or None,
            "description": show.get("overview"),
            "duration_minutes": avg_runtime,
            "cover_url": (
                f"{TMDB_IMAGE_BASE}/w500{show['poster_path']}"
                if show.get("poster_path")
                else None
            ),
            "external_url": f"https://www.themoviedb.org/tv/{show['id']}",
            "genres": genres,
            "directors": creators,  # Use creators as "directors" for series
            # Extended metadata
            "tmdb_rating": show.get("vote_average"),
            "tmdb_vote_count": show.get("vote_count"),
            "popularity": show.get("popularity"),
            "original_language": show.get("original_language"),
            "production_countries": production_countries,
            "cast": cast,
            "keywords": keywords,
            "certification": certification,
            "tagline": show.get("tagline") or None,
            # Series-specific
            "number_of_seasons": show.get("number_of_seasons"),
            "number_of_episodes": show.get("number_of_episodes"),
            "series_status": show.get("status"),  # Returning Series, Ended, Canceled
            "networks": networks,
        }


    async def get_watch_providers(
        self,
        tmdb_id: int,
        media_type: str = "movie",
        country: str = "FR",
    ) -> dict[str, Any] | None:
        """Get watch/streaming providers for a movie or TV show.

        Args:
            tmdb_id: TMDB ID of the movie/TV show
            media_type: "movie" or "tv"
            country: ISO 3166-1 alpha-2 country code (e.g., "FR", "US")

        Returns:
            Dict with flatrate (subscription), rent, buy providers for the country
        """
        if not self.api_key:
            return None

        client = get_tmdb_client()
        params = self._add_api_key({})
        response = await client.get(
            f"{TMDB_BASE_URL}/{media_type}/{tmdb_id}/watch/providers",
            params=params,
            headers=self.headers,
        )

        if response.status_code != 200:
            return None

        data = response.json()
        results = data.get("results", {})

        # Get providers for the specified country
        country_data = results.get(country, {})

        if not country_data:
            return None

        return {
            "link": country_data.get("link"),
            "flatrate": [
                {
                    "provider_id": p["provider_id"],
                    "provider_name": p["provider_name"],
                    "logo_path": f"{TMDB_IMAGE_BASE}/w92{p['logo_path']}" if p.get("logo_path") else None,
                }
                for p in country_data.get("flatrate", [])
            ],
            "rent": [
                {
                    "provider_id": p["provider_id"],
                    "provider_name": p["provider_name"],
                    "logo_path": f"{TMDB_IMAGE_BASE}/w92{p['logo_path']}" if p.get("logo_path") else None,
                }
                for p in country_data.get("rent", [])
            ],
            "buy": [
                {
                    "provider_id": p["provider_id"],
                    "provider_name": p["provider_name"],
                    "logo_path": f"{TMDB_IMAGE_BASE}/w92{p['logo_path']}" if p.get("logo_path") else None,
                }
                for p in country_data.get("buy", [])
            ],
        }

    async def get_available_providers(
        self,
        country: str = "FR",
    ) -> list[dict[str, Any]]:
        """Get all available streaming providers for a country.

        Args:
            country: ISO 3166-1 alpha-2 country code

        Returns:
            List of streaming providers available in the country
        """
        if not self.api_key:
            return []

        return await self._fetch_available_providers(country)

    @cached("tmdb:providers", ttl=CACHE_TTL_LONG)
    async def _fetch_available_providers(
        self,
        country: str = "FR",
    ) -> list[dict[str, Any]]:
        """Fetch available providers from TMDB API (cached)."""
        client = get_tmdb_client()
        params = self._add_api_key({
            "watch_region": country,
        })
        response = await client.get(
            f"{TMDB_BASE_URL}/watch/providers/movie",
            params=params,
            headers=self.headers,
        )

        if response.status_code != 200:
            return []

        data = response.json()

        # Return the most common streaming providers
        providers = []
        for provider in data.get("results", []):
            providers.append({
                "provider_id": provider["provider_id"],
                "provider_name": provider["provider_name"],
                "logo_path": f"{TMDB_IMAGE_BASE}/w92{provider['logo_path']}" if provider.get("logo_path") else None,
                "display_priority": provider.get("display_priority", 999),
            })

        # Sort by display priority (lower = more important)
        providers.sort(key=lambda x: x["display_priority"])

        return providers


    async def get_trending(
        self,
        media_type: str = "movie",
        time_window: str = "week",
        language: str = "fr-FR",
        page: int = 1,
    ) -> list[dict[str, Any]]:
        """Get trending movies or TV shows.

        Args:
            media_type: "movie" or "tv"
            time_window: "day" or "week"
            language: Language for results
            page: Page number (1-based)

        Returns:
            List of trending media
        """
        if not self.api_key:
            return []

        params = self._add_api_key({
            "language": language,
            "page": str(page),
        })

        client = get_tmdb_client()
        response = await client.get(
            f"{TMDB_BASE_URL}/trending/{media_type}/{time_window}",
            params=params,
            headers=self.headers,
        )

        if response.status_code != 200:
            return []

        data = response.json()
        results = []

        for item in data.get("results", []):
            if media_type == "movie":
                title = item.get("original_title") or item.get("title", "")
                year = item.get("release_date", "")[:4] or None
            else:
                title = item.get("original_name") or item.get("name", "")
                year = item.get("first_air_date", "")[:4] or None

            results.append({
                "id": item["id"],
                "title": title,
                "year": year,
                "overview": item.get("overview"),
                "poster_url": (
                    f"{TMDB_IMAGE_BASE}/w342{item['poster_path']}"
                    if item.get("poster_path")
                    else None
                ),
                "vote_average": item.get("vote_average"),
                "popularity": item.get("popularity"),
                "genre_ids": item.get("genre_ids", []),
            })

        return results

    async def discover(
        self,
        media_type: str = "movie",
        language: str = "fr-FR",
        sort_by: str = "popularity.desc",
        with_genres: list[int] | None = None,
        without_genres: list[int] | None = None,
        vote_average_gte: float | None = None,
        vote_count_gte: int | None = None,
        year: int | None = None,
        page: int = 1,
    ) -> list[dict[str, Any]]:
        """Discover movies or TV shows with filters.

        Args:
            media_type: "movie" or "tv"
            language: Language for results
            sort_by: Sort order (popularity.desc, vote_average.desc, etc.)
            with_genres: Genre IDs to include
            without_genres: Genre IDs to exclude
            vote_average_gte: Minimum vote average
            vote_count_gte: Minimum vote count
            year: Release year filter
            page: Page number (1-based)

        Returns:
            List of discovered media
        """
        if not self.api_key:
            return []

        params = self._add_api_key({
            "language": language,
            "sort_by": sort_by,
            "include_adult": "false",
            "page": str(page),
        })

        if with_genres:
            params["with_genres"] = ",".join(str(g) for g in with_genres)
        if without_genres:
            params["without_genres"] = ",".join(str(g) for g in without_genres)
        if vote_average_gte:
            params["vote_average.gte"] = str(vote_average_gte)
        if vote_count_gte:
            params["vote_count.gte"] = str(vote_count_gte)
        if year:
            if media_type == "movie":
                params["primary_release_year"] = str(year)
            else:
                params["first_air_date_year"] = str(year)

        client = get_tmdb_client()
        response = await client.get(
            f"{TMDB_BASE_URL}/discover/{media_type}",
            params=params,
            headers=self.headers,
        )

        if response.status_code != 200:
            return []

        data = response.json()
        results = []

        for item in data.get("results", []):
            if media_type == "movie":
                title = item.get("original_title") or item.get("title", "")
                year_val = item.get("release_date", "")[:4] or None
            else:
                title = item.get("original_name") or item.get("name", "")
                year_val = item.get("first_air_date", "")[:4] or None

            results.append({
                "id": item["id"],
                "title": title,
                "year": year_val,
                "overview": item.get("overview"),
                "poster_url": (
                    f"{TMDB_IMAGE_BASE}/w342{item['poster_path']}"
                    if item.get("poster_path")
                    else None
                ),
                "vote_average": item.get("vote_average"),
                "popularity": item.get("popularity"),
                "genre_ids": item.get("genre_ids", []),
            })

        return results

    async def get_recommendations(
        self,
        tmdb_id: int,
        media_type: str = "movie",
        language: str = "fr-FR",
        page: int = 1,
    ) -> list[dict[str, Any]]:
        """Get TMDB recommendations for a movie or TV show.

        Args:
            tmdb_id: TMDB ID of the source media
            media_type: "movie" or "tv"
            language: Language for results
            page: Page number (1-based)

        Returns:
            List of recommended media
        """
        if not self.api_key:
            return []

        params = self._add_api_key({
            "language": language,
            "page": str(page),
        })

        client = get_tmdb_client()
        response = await client.get(
            f"{TMDB_BASE_URL}/{media_type}/{tmdb_id}/recommendations",
            params=params,
            headers=self.headers,
        )

        if response.status_code != 200:
            return []

        data = response.json()
        results = []

        for item in data.get("results", []):
            if media_type == "movie":
                title = item.get("original_title") or item.get("title", "")
                year = item.get("release_date", "")[:4] or None
            else:
                title = item.get("original_name") or item.get("name", "")
                year = item.get("first_air_date", "")[:4] or None

            results.append({
                "id": item["id"],
                "title": title,
                "year": year,
                "overview": item.get("overview"),
                "poster_url": (
                    f"{TMDB_IMAGE_BASE}/w342{item['poster_path']}"
                    if item.get("poster_path")
                    else None
                ),
                "vote_average": item.get("vote_average"),
                "popularity": item.get("popularity"),
            })

        return results

    async def get_similar(
        self,
        tmdb_id: int,
        media_type: str = "movie",
        language: str = "fr-FR",
        page: int = 1,
    ) -> list[dict[str, Any]]:
        """Get similar movies or TV shows from TMDB.

        Args:
            tmdb_id: TMDB ID of the source media
            media_type: "movie" or "tv"
            language: Language for results
            page: Page number (1-based)

        Returns:
            List of similar media
        """
        if not self.api_key:
            return []

        params = self._add_api_key({
            "language": language,
            "page": str(page),
        })

        client = get_tmdb_client()
        response = await client.get(
            f"{TMDB_BASE_URL}/{media_type}/{tmdb_id}/similar",
            params=params,
            headers=self.headers,
        )

        if response.status_code != 200:
            return []

        data = response.json()
        results = []

        for item in data.get("results", []):
            if media_type == "movie":
                title = item.get("original_title") or item.get("title", "")
                year = item.get("release_date", "")[:4] or None
            else:
                title = item.get("original_name") or item.get("name", "")
                year = item.get("first_air_date", "")[:4] or None

            results.append({
                "id": item["id"],
                "title": title,
                "year": year,
                "overview": item.get("overview"),
                "poster_url": (
                    f"{TMDB_IMAGE_BASE}/w342{item['poster_path']}"
                    if item.get("poster_path")
                    else None
                ),
                "vote_average": item.get("vote_average"),
                "popularity": item.get("popularity"),
            })

        return results

    @cached("tmdb:genres", ttl=CACHE_TTL_LONG)
    async def get_genre_list(
        self,
        media_type: str = "movie",
        language: str = "fr-FR",
    ) -> list[dict[str, Any]]:
        """Get the list of official genres for movies or TV shows.

        Args:
            media_type: "movie" or "tv"
            language: Language for genre names

        Returns:
            List of genres with id and name
        """
        if not self.api_key:
            return []

        params = self._add_api_key({
            "language": language,
        })

        client = get_tmdb_client()
        response = await client.get(
            f"{TMDB_BASE_URL}/genre/{media_type}/list",
            params=params,
            headers=self.headers,
        )

        if response.status_code != 200:
            return []

        data = response.json()
        return data.get("genres", [])

    @cached("tmdb:trailer", ttl=CACHE_TTL_MEDIUM)
    async def get_trailer(
        self,
        tmdb_id: int,
        media_type: str = "movie",
        language: str = "fr-FR",
    ) -> dict[str, Any] | None:
        """Get the trailer for a movie or TV show.

        Args:
            tmdb_id: TMDB ID of the media
            media_type: "movie" or "tv"
            language: Language for results (will also try English as fallback)

        Returns:
            Trailer info with key (YouTube video ID) and site, or None if not found
        """
        if not self.api_key:
            return None

        client = get_tmdb_client()
        # Try requested language first
        params = self._add_api_key({
            "language": language,
        })
        response = await client.get(
            f"{TMDB_BASE_URL}/{media_type}/{tmdb_id}/videos",
            params=params,
            headers=self.headers,
        )

        trailers = []
        if response.status_code == 200:
            data = response.json()
            trailers = data.get("results", [])

        # If no results in requested language, try English
        if not trailers and language != "en-US":
            params = self._add_api_key({
                "language": "en-US",
            })
            response = await client.get(
                f"{TMDB_BASE_URL}/{media_type}/{tmdb_id}/videos",
                params=params,
                headers=self.headers,
            )
            if response.status_code == 200:
                data = response.json()
                trailers = data.get("results", [])

        if not trailers:
            return None

        # Prioritize: Official Trailer > Trailer > Teaser (YouTube only)
        youtube_trailers = [t for t in trailers if t.get("site") == "YouTube"]

        for video_type in ["Trailer", "Teaser", "Clip"]:
            for trailer in youtube_trailers:
                if trailer.get("type") == video_type:
                    if video_type == "Trailer" and trailer.get("official", False):
                        return {
                            "key": trailer["key"],
                            "site": trailer["site"],
                            "name": trailer.get("name"),
                            "type": trailer.get("type"),
                        }

        # If no official trailer, return first trailer/teaser
        for video_type in ["Trailer", "Teaser"]:
            for trailer in youtube_trailers:
                if trailer.get("type") == video_type:
                    return {
                        "key": trailer["key"],
                        "site": trailer["site"],
                        "name": trailer.get("name"),
                        "type": trailer.get("type"),
                    }

        # Return first YouTube video as fallback
        if youtube_trailers:
            return {
                "key": youtube_trailers[0]["key"],
                "site": youtube_trailers[0]["site"],
                "name": youtube_trailers[0].get("name"),
                "type": youtube_trailers[0].get("type"),
            }

        return None


# Common streaming providers with their TMDB IDs for quick reference
POPULAR_PROVIDERS = {
    "Netflix": 8,
    "Amazon Prime Video": 9,
    "Disney Plus": 337,
    "Canal+": 381,
    "Apple TV+": 350,
    "OCS": 56,
    "Paramount+": 531,
    "Crunchyroll": 283,
    "ADN": 415,
    "France TV": 236,
    "Arte": 234,
    "Max": 1899,  # HBO Max
    "Hulu": 15,
    "Peacock": 386,
}


# Singleton instance
tmdb_service = TMDBService()
