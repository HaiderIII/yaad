# Yaad یاد

> Ta mémoire média personnelle / Your personal media memory

Application self-hosted de tracking de médias (films, livres, YouTube) avec recommandations IA locales et intégration Jellyfin.

## Features

- 🎬 Track films, books, YouTube videos, podcasts, shows
- 🔍 Auto-fetch metadata (TMDb, Open Library, yt-dlp)
- 🤖 Local AI recommendations (embeddings + user taste profile)
- 📺 Jellyfin integration (bidirectional sync)
- 📚 Kobo e-reader sync
- 🔄 Import from Letterboxd (with ratings), Notion, YouTube Watch Later
- ⭐ Unrated media filter (helps improve AI recommendations)
- 📊 Catalogue with advanced filters (streamable, incomplete, unrated)
- 🌙 Dark mode only
- 🌍 i18n (FR/EN)

## Tech Stack

- **Backend**: FastAPI, SQLAlchemy 2.0, PostgreSQL, Redis
- **Frontend**: Jinja2, HTMX, Alpine.js, Tailwind CSS
- **AI**: ChromaDB, sentence-transformers
- **Deploy**: Docker Compose

## Quick Start

### Prerequisites

- Python 3.12+
- PostgreSQL 15+
- Redis 7+
- Node.js 18+ (for Tailwind CSS build)

### Development Setup

1. **Clone and setup environment**:
```bash
git clone https://github.com/yourusername/yaad.git
cd yaad
python -m venv venv
source venv/bin/activate  # On Windows: .\venv\Scripts\activate
pip install -r requirements.txt
```

2. **Configure environment variables**:
```bash
cp .env.example .env
# Edit .env with your settings:
# - DATABASE_URL=postgresql+asyncpg://user:pass@localhost/yaad
# - REDIS_URL=redis://localhost:6379/0
# - APP_SECRET_KEY=your-secret-key
# - GITHUB_CLIENT_ID / GITHUB_CLIENT_SECRET (for OAuth)
# - GOOGLE_CLIENT_ID / GOOGLE_CLIENT_SECRET (for OAuth)
# - TMDB_API_KEY (for movie/TV metadata)
```

3. **Initialize database**:
```bash
alembic upgrade head
```

4. **Build CSS** (in separate terminal):
```bash
npm install
npm run watch  # or: npx tailwindcss -i ./static/css/input.css -o ./static/css/style.css --watch
```

5. **Run development server**:
```bash
uvicorn src.main:app --reload --host 0.0.0.0 --port 8080
```

6. **Access the app**:
- Web UI: http://localhost:8080
- API Docs: http://localhost:8080/docs
- ReDoc: http://localhost:8080/redoc

### Docker Deployment

```bash
docker-compose up -d
```

## API Documentation

Full interactive API documentation is available at `/docs` (Swagger UI) or `/redoc` (ReDoc) when the server is running.

### API Overview

| Endpoint | Description |
|----------|-------------|
| `GET /api/media` | List all media items |
| `POST /api/media` | Create new media item |
| `GET /api/media/{id}` | Get media by ID |
| `PATCH /api/media/{id}` | Update media item |
| `DELETE /api/media/{id}` | Delete media item |
| `GET /api/search` | Search media items |
| `GET /api/stats` | Get user statistics |
| `GET /api/user/settings` | Get user settings |
| `PATCH /api/user/settings` | Update user settings |

### Authentication

Yaad uses OAuth2 (GitHub/Google) for authentication. Session tokens are stored in cookies.

### Integrations

#### Jellyfin
| Endpoint | Description |
|----------|-------------|
| `GET /api/jellyfin/status` | Check connection status |
| `POST /api/jellyfin/connect` | Connect to Jellyfin server |
| `DELETE /api/jellyfin/disconnect` | Disconnect from Jellyfin |
| `POST /api/jellyfin/sync` | Trigger bidirectional sync |
| `POST /api/jellyfin/sync/import` | Import from Jellyfin |
| `POST /api/jellyfin/sync/export` | Export to Jellyfin |

#### Kobo
| Endpoint | Description |
|----------|-------------|
| `POST /api/kobo/start-auth` | Start Kobo device linking |
| `GET /api/kobo/auth-status` | Check auth progress |
| `POST /api/kobo/sync` | Sync reading progress |

#### Letterboxd
| Endpoint | Description |
|----------|-------------|
| `POST /api/import/letterboxd/rss` | Quick sync via RSS |
| `POST /api/import/letterboxd/scrape` | Full library import (with ratings) |
| `POST /api/import/letterboxd/watchlist` | Import watchlist |
| `POST /api/import/letterboxd/csv` | Import from CSV file |
| `GET /api/import/letterboxd/sync-stream` | SSE stream with force_update option |

#### Notion
| Endpoint | Description |
|----------|-------------|
| `POST /api/import/notion/csv` | Import from Notion CSV |

## Configuration

### Environment Variables

| Variable | Description | Default |
|----------|-------------|---------|
| `DATABASE_URL` | PostgreSQL connection URL | required |
| `REDIS_URL` | Redis connection URL | `redis://localhost:6379/0` |
| `APP_SECRET_KEY` | Session encryption key | required |
| `APP_URL` | Public URL of the app | `http://localhost:8080` |
| `APP_ENV` | Environment (`development`/`production`) | `development` |
| `TMDB_API_KEY` | TMDb API key for metadata | optional |
| `GITHUB_CLIENT_ID` | GitHub OAuth client ID | optional |
| `GITHUB_CLIENT_SECRET` | GitHub OAuth client secret | optional |
| `GOOGLE_CLIENT_ID` | Google OAuth client ID | optional |
| `GOOGLE_CLIENT_SECRET` | Google OAuth client secret | optional |

### Background Tasks

Yaad runs automatic background sync tasks:
- **Kobo sync**: Every 6 hours
- **Letterboxd sync**: Every 12 hours
- **Streaming links refresh**: Every 24 hours

## Internationalization (i18n)

Yaad supports English and French. Language preference is stored per user in settings.

Translation files are in `src/i18n/`:
- `en.json` - English translations
- `fr.json` - French translations

## Monitoring

### Health Check
```bash
curl http://localhost:8080/health
```

Returns:
```json
{
  "status": "healthy",
  "timestamp": "2024-01-10T12:00:00Z",
  "uptime_seconds": 3600,
  "version": "0.1.0",
  "checks": {
    "database": {"status": "healthy"},
    "redis": {"status": "healthy"}
  }
}
```

### Prometheus Metrics
```bash
curl http://localhost:8080/metrics
```

Available metrics:
- `http_requests_total` - Total HTTP requests by method, path, status
- `http_request_duration_seconds` - Request latency histogram
- `active_connections` - Current active connections

## Testing

```bash
# Run all tests
pytest

# Run with coverage
pytest --cov=src --cov-report=html

# Run specific test file
pytest tests/test_api_media.py -v
```

## License

MIT
