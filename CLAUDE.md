# Instapaper to Remarkable Pipeline

## What it does
Fetches unread Instapaper bookmarks via OAuth/xAuth API, extracts article content with trafilatura, converts to A5 PDF with e-reader CSS via WeasyPrint, uploads to Remarkable tablet via the `rm_api` Python library (tectonic sync API).

## Files
- `instapaper_to_remarkable.py` — Main script
- `.env` — Credentials (gitignored)
- `config.example.env` — Template with all config vars
- `requirements.txt` — Python deps

## Config (.env)
- `INSTAPAPER_CONSUMER_KEY`, `INSTAPAPER_CONSUMER_SECRET`, `INSTAPAPER_USERNAME`, `INSTAPAPER_PASSWORD`
- `REMARKABLE_FOLDER=/Instapaper`
- `BATCH_SIZE=25` (default, Instapaper API max is 500)
- `PROCESSED_LOG=~/.instapaper_to_remarkable_processed.json`

## Scheduling
- Uses **launchd** (not cron): `~/Library/LaunchAgents/com.jonny.instapaper-to-remarkable.plist`
- Runs at 6 AM and 5 PM daily. Log at `/tmp/instapaper_to_remarkable.log`
- Plist uses `.venv/bin/python3.12` (Homebrew Python) and sets `PATH=/opt/homebrew/bin:/usr/local/bin:/usr/bin`
- **Network resilience**: `wait_for_network()` polls DNS every 30s for up to 5 min before running — handles launchd firing before WiFi is up after wake-from-sleep
- **Retry**: if `main()` fails for any reason (including `SystemExit`), sleeps 1 hour, runs network check again, then retries once

## Key implementation details
- Processed articles tracked in JSON log (`{bookmark_id: timestamp}`) to avoid duplicates — this is the sole dedup guard; rm_api has no built-in dedup
- Upload uses `rm_api` (PyPI: `rm_api`): reads device token from `~/Library/Application Support/rmapi/rmapi.conf`, refreshes to a user token, syncs metadata, then uploads via the tectonic blob API (`eu.tectonic.remarkable.com`)
- Sync cache persisted at `~/.rm_api_sync`; device token mirrored to `~/.rm_api_device_token` for rm_api's token file format
- Monkey-patches `certifi.where()` to use Zscaler CA bundle (`~/.zscaler/certs.pem`) so trafilatura/urllib3 trust SSL-intercepted connections
- Converts trafilatura `<graphic>` tags to `<img>` for WeasyPrint
- trafilatura HTML output already includes `<h1>` title — don't add another in template
- Passes `base_url` to WeasyPrint for relative image resolution
- `.env` loaded via absolute path (`Path(__file__).parent / ".env"`) for cron/launchd compatibility
- Paywalled sites (NYT, WSJ, Forbes, Reuters) fail gracefully — logged and skipped

## Dependencies
- Python 3.12 venv (`.venv/`): requests-oauthlib, trafilatura, weasyprint, python-dotenv, lxml_html_clean, rm_api
- System: pango (`brew install pango`), python@3.12 (brew)
- No longer requires the `rmapi` binary

## Gotchas
- `lxml` 6.x needs separate `lxml_html_clean` package
- WeasyPrint requires `brew install pango`
- Remarkable's old "tortoise" sync API (used by rmapi binary v0.0.32) returns 400 "Software must be updated" — `rm_api` Python library uses the new tectonic API and is not affected
- `sanitize_filename` strips periods (`.`) to avoid Remarkable API rejecting titles like "GPT-5.5"
