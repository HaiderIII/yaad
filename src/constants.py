"""Application constants - centralized configuration values."""

# =============================================================================
# Pagination
# =============================================================================
DEFAULT_PAGE_SIZE = 20
CATALOGUE_PAGE_SIZE = 24
MAX_SEARCH_RESULTS = 12
SEARCH_MIN_LENGTH = 2
MAX_PAGE_SIZE = 100

# =============================================================================
# Limits
# =============================================================================
MAX_UNFINISHED_ITEMS = 100
MAX_RECENT_ITEMS = 20
MAX_TAGS_LIMIT = 1000
MAX_GENRES_DISPLAY = 30
MAX_IMPORT_ITEMS = 5000  # Max items per import operation

# =============================================================================
# Cache TTLs (in seconds)
# =============================================================================
CACHE_TTL_STATS = 300  # 5 minutes
CACHE_TTL_PROVIDERS = 2592000  # 30 days
CACHE_TTL_SEARCH = 60  # 1 minute
CACHE_TTL_GENRES = 300  # 5 minutes
SEARCH_CACHE_MAX_SIZE = 100

# =============================================================================
# API Timeouts (in seconds)
# =============================================================================
API_TIMEOUT_DEFAULT = 10.0
API_TIMEOUT_EXTERNAL = 15.0
API_TIMEOUT_LONG = 30.0  # For slow operations like imports
HTTPX_TIMEOUT = 10.0

# =============================================================================
# Background Task Intervals (in seconds)
# =============================================================================
SYNC_INTERVAL_KOBO = 6 * 60 * 60  # 6 hours
SYNC_INTERVAL_LETTERBOXD = 12 * 60 * 60  # 12 hours
SYNC_INTERVAL_STREAMING = 24 * 60 * 60  # 24 hours
SYNC_INTERVAL_YOUTUBE = 6 * 60 * 60  # 6 hours
SYNC_INTERVAL_RECOMMENDATIONS = 12 * 60 * 60  # 12 hours (2x/day)

# =============================================================================
# Streaming
# =============================================================================
STREAMING_LINKS_REFRESH_DAYS = 7
STREAMING_RATE_LIMIT_DELAY = 0.5  # seconds between API calls

# Known streaming provider IDs (TMDB)
PROVIDER_ID_NETFLIX = 8
PROVIDER_ID_AMAZON_PRIME = 9
PROVIDER_ID_DISNEY_PLUS = 337
PROVIDER_ID_HBO_MAX = 384
PROVIDER_ID_APPLE_TV = 350
PROVIDER_ID_CANAL_PLUS = 381
PROVIDER_ID_PARAMOUNT_PLUS = 531

# =============================================================================
# Sort Options
# =============================================================================
VALID_SORT_FIELDS = ["created_at", "updated_at", "title", "year", "rating"]
VALID_SORT_ORDERS = ["asc", "desc"]
DEFAULT_SORT_FIELD = "created_at"
DEFAULT_SORT_ORDER = "desc"

# =============================================================================
# Session & Security
# =============================================================================
SESSION_TIMEOUT_DAYS = 7
SESSION_COOKIE_NAME = "yaad_session"
MAX_CONSECUTIVE_FAILURES = 5  # For background tasks

# =============================================================================
# Import Settings
# =============================================================================
IMPORT_BATCH_SIZE = 10
IMPORT_DELAY_BETWEEN_BATCHES = 0.5  # seconds

# =============================================================================
# Rating
# =============================================================================
RATING_MIN = 0.5
RATING_MAX = 5.0
RATING_STEP = 0.5

# =============================================================================
# Media Types
# =============================================================================
TMDB_MEDIA_TYPE_MOVIE = "movie"
TMDB_MEDIA_TYPE_TV = "tv"

# =============================================================================
# External API URLs
# =============================================================================
TMDB_API_BASE_URL = "https://api.themoviedb.org/3"
TMDB_IMAGE_BASE_URL = "https://image.tmdb.org/t/p"
JUSTWATCH_GRAPHQL_URL = "https://apis.justwatch.com/graphql"
OPEN_LIBRARY_API_URL = "https://openlibrary.org"
LETTERBOXD_RSS_URL = "https://letterboxd.com"
