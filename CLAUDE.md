# Instapaper to Remarkable Pipeline

## What it does
Fetches unread Instapaper bookmarks via OAuth/xAuth API, extracts article content with trafilatura, converts to A5 PDF with e-reader CSS via WeasyPrint, uploads to Remarkable tablet via rmapi.

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
- Plist uses `.venv/bin/python3.12` (Homebrew Python) and sets `PATH=/opt/homebrew/bin:/usr/local/bin:/usr/bin` so rmapi is found
- **Network resilience**: `wait_for_network()` polls DNS every 30s for up to 5 min before running — handles launchd firing before WiFi is up after wake-from-sleep
- **Retry**: if `main()` fails for any reason (including `SystemExit`), sleeps 1 hour, runs network check again, then retries once

## Key implementation details
- Processed articles tracked in JSON log (`{bookmark_id: timestamp}`) to avoid duplicates
- Uses `--content-only` flag on rmapi to replace (not duplicate) existing files
- Monkey-patches `certifi.where()` to use Zscaler CA bundle (`~/.zscaler/certs.pem`) so trafilatura/urllib3 trust SSL-intercepted connections
- Converts trafilatura `<graphic>` tags to `<img>` for WeasyPrint
- trafilatura HTML output already includes `<h1>` title — don't add another in template
- Passes `base_url` to WeasyPrint for relative image resolution
- `.env` loaded via absolute path (`Path(__file__).parent / ".env"`) for cron/launchd compatibility
- Paywalled sites (NYT, WSJ, Forbes, Reuters) fail gracefully — logged and skipped

## Dependencies
- Python 3.12 venv (`.venv/`): requests-oauthlib, trafilatura, weasyprint, python-dotenv, lxml_html_clean
- System: pango (`brew install pango`), rmapi (`/opt/homebrew/bin/rmapi`), python@3.12 (brew)

## Gotchas
- `rmapi put --force` creates DUPLICATES, not replacements — always use `--content-only`
- `rmapi rm` with duplicate names deletes all copies at once
- `lxml` 6.x needs separate `lxml_html_clean` package
- WeasyPrint requires `brew install pango`
