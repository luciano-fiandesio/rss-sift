# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project Overview

RSS-Sift is a Python Flask web application that fetches content from configured URLs, filters entries using AI (Replicate API with Llama 3 70B), and generates RSS feeds. Currently configured to filter technical ebooks from avxhm.in based on relevance to software development, AI, DevOps, and related topics.

## Development Commands

```bash
# Install dependencies
pip install -r requirements.txt

# Run locally (requires REPLICATE_API_TOKEN environment variable)
export REPLICATE_API_TOKEN="your_token"
python rss-sift.py --host 0.0.0.0 --port 8088

# Run with Docker
docker-compose up
```

**No test infrastructure exists.** There are no test files, pytest configuration, or linting setup.

## Architecture

### Core Flow
1. **Fetch**: Background scheduler (APScheduler) runs cron jobs to fetch HTML from configured URLs
2. **Parse**: BeautifulSoup extracts titles, links, and metadata using CSS selectors (`div.row > div.article > h1 > a.title-link`)
3. **Filter**: Each title is sent to Replicate API for AI relevance filtering
4. **Store**: Filtered entries saved to SQLite with SHA256 hash for deduplication
5. **Serve**: Flask routes generate RSS XML from database entries

### Key Files
- `rss-sift.py` - Monolithic application: Flask app, SQLAlchemy models, scheduling, parsing, AI filtering
- `config.json` - Feed configuration (URLs, cron schedules, timezone)
- `templates/index.html` - Web UI for manual feed management

### Database Schema (SQLite)
- **FeedData**: id, feed_name, title, link, additional_info, hash (unique), skip_ai, created
- **FeedMeta**: id, feed_name (unique), last_fetched

### Flask Routes
- `GET /` - Feed management UI
- `POST /fetch_all_feeds` - Trigger all feeds
- `POST /fetch_feed` - Trigger single feed (form param: feed_name)
- `POST /clean` - Delete entries for feed (form param: feed_name)
- `GET /<feed_name>/rss.xml` - RSS feed output

## Configuration

`config.json` structure:
```json
{
  "timezone": "Europe/Berlin",
  "feeds": [{
    "name": "ebooks",
    "url_to_fetch": "https://avxhm.in/ebooks",
    "url_prefix": "https://avxhm.in",
    "cron": "*/10 * * * *"
  }]
}
```

## Code Patterns

- All datetimes are timezone-aware using pytz
- Deduplication via SHA256 hash of title+link
- AI filtering via `is_interesting_title()` returns boolean; exceptions default to False
- Uses waitress WSGI server in production (not Flask dev server)

## Known Limitations

- HTML parsing is brittle (hardcoded CSS selectors)
- Global SQLAlchemy session (not thread-safe)
- Synchronous AI calls block feed processing
- No input validation on form parameters
- `rep.py` contains hardcoded API token (test script only)
