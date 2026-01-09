"""Application constants - centralized configuration values."""

# Pagination defaults
DEFAULT_PAGE_SIZE = 20
CATALOGUE_PAGE_SIZE = 24
MAX_SEARCH_RESULTS = 12
SEARCH_MIN_LENGTH = 2

# Limits
MAX_UNFINISHED_ITEMS = 100
MAX_RECENT_ITEMS = 20
MAX_TAGS_LIMIT = 1000
MAX_GENRES_DISPLAY = 30

# Cache TTLs (in seconds)
CACHE_TTL_STATS = 300  # 5 minutes
CACHE_TTL_PROVIDERS = 2592000  # 30 days
CACHE_TTL_SEARCH = 60  # 1 minute
SEARCH_CACHE_MAX_SIZE = 100

# API timeouts (in seconds)
API_TIMEOUT_DEFAULT = 10.0
API_TIMEOUT_EXTERNAL = 15.0
HTTPX_TIMEOUT = 10.0

# Streaming links refresh interval (in days)
STREAMING_LINKS_REFRESH_DAYS = 7

# Sort options
VALID_SORT_FIELDS = ["created_at", "updated_at", "title", "year", "rating"]
VALID_SORT_ORDERS = ["asc", "desc"]
DEFAULT_SORT_FIELD = "created_at"
DEFAULT_SORT_ORDER = "desc"

# Session settings
SESSION_TIMEOUT_DAYS = 7

# Import batch sizes
IMPORT_BATCH_SIZE = 10
