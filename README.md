# Yaad یاد

> Ta mémoire média personnelle / Your personal media memory

Application self-hosted de tracking de médias (films, livres, YouTube) avec recommandations IA locales et intégration Jellyfin.

## Features (planned)

- 🎬 Track films, books, YouTube videos
- 🔍 Auto-fetch metadata (TMDb, Open Library, yt-dlp)
- 🤖 Local AI recommendations (embeddings)
- 📺 Jellyfin integration (bidirectional sync)
- 📚 Kobo e-reader sync
- 🔄 Import from Letterboxd, Notion, YouTube Watch Later
- 🌙 Dark mode only
- 🌍 i18n (FR/EN)

## Tech Stack

- **Backend**: FastAPI, SQLAlchemy 2.0, PostgreSQL, Redis
- **Frontend**: Jinja2, HTMX, Alpine.js, Tailwind CSS
- **AI**: ChromaDB, sentence-transformers
- **Deploy**: Docker Compose

## Status

🚧 In development - Phase 1

## License

MIT
