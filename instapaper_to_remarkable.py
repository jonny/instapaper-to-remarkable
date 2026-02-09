#!/usr/bin/env python3
"""Fetch unread Instapaper bookmarks, convert to PDF, upload to Remarkable."""

import ctypes.util
import json
import logging
import os
import re
import shutil
import subprocess
import sys
import tempfile
from datetime import datetime, timezone
from pathlib import Path

# Ensure Homebrew libraries are discoverable on macOS (needed for WeasyPrint).
_brew_lib = "/opt/homebrew/lib"
if sys.platform == "darwin" and os.path.isdir(_brew_lib):
    os.environ.setdefault("DYLD_FALLBACK_LIBRARY_PATH", _brew_lib)
    # Monkey-patch find_library so cffi/WeasyPrint can locate Homebrew .dylib files.
    _orig_find_library = ctypes.util.find_library

    def _find_library_brew(name):
        result = _orig_find_library(name)
        if result:
            return result
        # Try Homebrew lib directory directly.
        for suffix in (".dylib",):
            candidate = os.path.join(_brew_lib, f"lib{name}{suffix}")
            if os.path.exists(candidate):
                return candidate
        return None

    ctypes.util.find_library = _find_library_brew

import trafilatura
from dotenv import load_dotenv
from requests_oauthlib import OAuth1Session
from weasyprint import HTML

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
)
log = logging.getLogger(__name__)

INSTAPAPER_API = "https://www.instapaper.com"

EREADER_CSS = """\
@page {
    size: A5;
    margin: 1.5cm;
}
body {
    font-family: Georgia, 'Times New Roman', serif;
    font-size: 11pt;
    line-height: 1.6;
    color: #000;
}
h1 { font-size: 16pt; margin-bottom: 0.5em; }
h2 { font-size: 14pt; }
h3 { font-size: 12pt; }
img { max-width: 100%; height: auto; }
a { color: #000; text-decoration: underline; }
pre, code { font-size: 9pt; overflow-wrap: break-word; }
blockquote { border-left: 2px solid #666; padding-left: 0.8em; margin-left: 0; }
"""

HTML_TEMPLATE = """\
<!DOCTYPE html>
<html>
<head>
<meta charset="utf-8">
<style>{css}</style>
</head>
<body>
{content}
</body>
</html>
"""


def load_config():
    load_dotenv()
    required = [
        "INSTAPAPER_CONSUMER_KEY",
        "INSTAPAPER_CONSUMER_SECRET",
        "INSTAPAPER_USERNAME",
        "INSTAPAPER_PASSWORD",
    ]
    missing = [k for k in required if not os.getenv(k)]
    if missing:
        log.error("Missing env vars: %s. See config.example.env", ", ".join(missing))
        sys.exit(1)
    return {
        "consumer_key": os.environ["INSTAPAPER_CONSUMER_KEY"],
        "consumer_secret": os.environ["INSTAPAPER_CONSUMER_SECRET"],
        "username": os.environ["INSTAPAPER_USERNAME"],
        "password": os.environ["INSTAPAPER_PASSWORD"],
        "remarkable_folder": os.getenv("REMARKABLE_FOLDER", "/Instapaper"),
        "batch_size": int(os.getenv("BATCH_SIZE", "25")),
        "processed_log": Path(
            os.path.expanduser(
                os.getenv(
                    "PROCESSED_LOG", "~/.instapaper_to_remarkable_processed.json"
                )
            )
        ),
    }


def check_rmapi():
    if not shutil.which("rmapi"):
        log.error(
            "rmapi not found. Install via: brew install ddvk/tap/rmapi "
            "or download from https://github.com/ddvk/rmapi/releases"
        )
        sys.exit(1)


def instapaper_auth(config):
    """Authenticate via Instapaper's xAuth flow and return an OAuth1Session."""
    session = OAuth1Session(
        config["consumer_key"],
        client_secret=config["consumer_secret"],
    )
    resp = session.post(
        f"{INSTAPAPER_API}/api/1/oauth/access_token",
        data={
            "x_auth_username": config["username"],
            "x_auth_password": config["password"],
            "x_auth_mode": "client_auth",
        },
    )
    if resp.status_code != 200:
        log.error("Instapaper auth failed (%s): %s", resp.status_code, resp.text)
        sys.exit(1)

    # Response is url-encoded: oauth_token=...&oauth_token_secret=...
    from urllib.parse import parse_qs

    creds = parse_qs(resp.text)
    token = creds["oauth_token"][0]
    token_secret = creds["oauth_token_secret"][0]

    return OAuth1Session(
        config["consumer_key"],
        client_secret=config["consumer_secret"],
        resource_owner_key=token,
        resource_owner_secret=token_secret,
    )


def fetch_bookmarks(session, limit=25):
    """Fetch unread bookmarks. Returns list of bookmark dicts."""
    resp = session.post(
        f"{INSTAPAPER_API}/api/1/bookmarks/list",
        data={"folder_id": "unread", "limit": limit},
    )
    if resp.status_code != 200:
        log.error("Failed to fetch bookmarks (%s): %s", resp.status_code, resp.text)
        return []
    data = resp.json()
    # The API returns a list of mixed objects; bookmarks have type "bookmark"
    return [item for item in data if item.get("type") == "bookmark"]


def load_processed(path):
    if path.exists():
        return json.loads(path.read_text())
    return {}


def save_processed(path, processed):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(processed, indent=2))


def sanitize_filename(title):
    name = re.sub(r'[<>:"/\\|?*]', "", title)
    name = re.sub(r"\s+", " ", name).strip()
    return name[:120] if name else "untitled"


def article_to_pdf(title, url, output_path):
    """Fetch, extract, and convert an article to PDF. Returns True on success."""
    html = trafilatura.fetch_url(url)
    if not html:
        log.warning("Could not fetch HTML for: %s", url)
        return False

    content = trafilatura.extract(
        html,
        output_format="html",
        include_images=True,
        include_formatting=True,
    )
    if not content:
        log.warning("Could not extract content for: %s", url)
        return False

    # Trafilatura outputs <graphic> tags instead of <img>; convert for WeasyPrint.
    content = re.sub(r"<graphic\b", "<img", content)
    content = re.sub(r"</graphic>", "", content)

    full_html = HTML_TEMPLATE.format(
        css=EREADER_CSS,
        content=content,
    )
    HTML(string=full_html, base_url=url).write_pdf(str(output_path))
    return True


def upload_to_remarkable(pdf_path, folder):
    """Upload a PDF to Remarkable via rmapi. Returns True on success."""
    # Ensure the target folder exists
    subprocess.run(["rmapi", "mkdir", folder], capture_output=True)

    result = subprocess.run(
        ["rmapi", "put", "--force", str(pdf_path), folder],
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        log.error("rmapi upload failed: %s %s", result.stdout, result.stderr)
        return False
    return True


def main():
    config = load_config()
    check_rmapi()

    log.info("Authenticating with Instapaper...")
    session = instapaper_auth(config)

    log.info("Fetching unread bookmarks...")
    bookmarks = fetch_bookmarks(session, limit=config["batch_size"])
    if not bookmarks:
        log.info("No bookmarks found.")
        return

    processed = load_processed(config["processed_log"])
    new_bookmarks = [b for b in bookmarks if str(b["bookmark_id"]) not in processed]
    if not new_bookmarks:
        log.info("No new bookmarks to process.")
        return

    log.info("Processing %d new bookmark(s)...", len(new_bookmarks))
    tmpdir = tempfile.mkdtemp(prefix="instapaper_")

    try:
        for bookmark in new_bookmarks:
            bid = str(bookmark["bookmark_id"])
            title = bookmark.get("title", "Untitled")
            url = bookmark.get("url", "")
            log.info("Processing: %s", title)

            filename = sanitize_filename(title) + ".pdf"
            pdf_path = os.path.join(tmpdir, filename)

            try:
                if not article_to_pdf(title, url, pdf_path):
                    continue

                if upload_to_remarkable(pdf_path, config["remarkable_folder"]):
                    processed[bid] = datetime.now(timezone.utc).isoformat()
                    save_processed(config["processed_log"], processed)
                    log.info("Uploaded: %s", title)
                else:
                    log.error("Upload failed, will retry next run: %s", title)
            except Exception:
                log.exception("Error processing bookmark %s (%s)", bid, title)
    finally:
        shutil.rmtree(tmpdir, ignore_errors=True)

    log.info("Done. Processed %d bookmark(s).", len(processed))


if __name__ == "__main__":
    main()
