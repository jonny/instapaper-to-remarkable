#!/usr/bin/env python3
"""Fetch unread Instapaper bookmarks, convert to PDF, upload to Remarkable."""

import json
import logging
import os
import re
import shutil
import socket
import sys
import tempfile
import time
from http.cookiejar import MozillaCookieJar
from urllib.parse import urlparse
from datetime import datetime, timezone
from pathlib import Path

# If a custom CA bundle is present (e.g. Zscaler SSL interception), use it
# instead of certifi's default so trafilatura/urllib3 trust the proxy CA.
_zscaler_certs = Path.home() / ".zscaler" / "certs.pem"
if _zscaler_certs.is_file():
    import certifi

    certifi.where = lambda: str(_zscaler_certs)

import requests
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


def wait_for_network(host="www.instapaper.com", timeout=300, interval=30):
    """Wait up to `timeout` seconds for DNS resolution of `host`.

    Checks every `interval` seconds. Returns True if network is available,
    False if the timeout was reached without connectivity.
    """
    deadline = time.monotonic() + timeout
    while True:
        try:
            socket.getaddrinfo(host, 443)
            log.info("Network is available.")
            return True
        except socket.gaierror:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                log.warning(
                    "Network not available after %ds — proceeding anyway.", timeout
                )
                return False
            log.info(
                "Network not available, retrying in %ds (%.0fs remaining)...",
                interval,
                remaining,
            )
            time.sleep(min(interval, remaining))


def load_config():
    load_dotenv(Path(__file__).parent / ".env")
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
    name = re.sub(r'[<>:"/\\|?*.]', "", title)
    name = re.sub(r"\s+", " ", name).strip()
    return name[:120] if name else "untitled"


_COOKIES_DIR = Path(__file__).parent / ".cookies"
_FETCH_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
        "AppleWebKit/605.1.15 (KHTML, like Gecko) "
        "Version/17.0 Safari/605.1.15"
    ),
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.9",
    "Accept-Encoding": "gzip, deflate, br",
}


def _cookie_jar_for_url(url):
    """Return a loaded MozillaCookieJar for the URL's domain, or None."""
    host = urlparse(url).hostname or ""
    parts = host.split(".")
    # Try www.nytimes.com → nytimes.com → com (stop at 2 parts)
    for i in range(len(parts) - 1):
        cookie_file = _COOKIES_DIR / f"{'.'.join(parts[i:])}.txt"
        if cookie_file.exists():
            jar = MozillaCookieJar(str(cookie_file))
            try:
                jar.load(ignore_discard=True, ignore_expires=True)
                return jar
            except Exception as e:
                log.warning("Could not load cookie file %s: %s", cookie_file.name, e)
    return None


def fetch_html(url):
    """Fetch a URL, using a site cookie file if available, else trafilatura."""
    jar = _cookie_jar_for_url(url)
    if jar is not None:
        try:
            resp = requests.get(
                url, cookies=jar, headers=_FETCH_HEADERS,
                timeout=30, allow_redirects=True,
            )
            if resp.ok:
                return resp.text
            log.debug("Cookie fetch got HTTP %d for %s", resp.status_code, url)
        except Exception as e:
            log.debug("Cookie fetch error for %s: %s", url, e)
    return trafilatura.fetch_url(url)


def article_to_pdf(title, url, output_path):
    """Fetch, extract, and convert an article to PDF. Returns True on success."""
    html = fetch_html(url)
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


def _get_rm_device_token():
    """Read the device token from rmapi's config file."""
    conf = Path.home() / "Library" / "Application Support" / "rmapi" / "rmapi.conf"
    if conf.exists():
        for line in conf.read_text().splitlines():
            if line.startswith("devicetoken:"):
                return line.split(":", 1)[1].strip()
    return None


def upload_to_remarkable(pdf_path, title, folder):
    """Upload a PDF to Remarkable via rm_api. Returns True on success."""
    from rm_api import API
    from rm_api.models import Document, DocumentCollection

    device_token = _get_rm_device_token()
    if not device_token:
        log.error("No rmapi device token found in ~/Library/Application Support/rmapi/rmapi.conf")
        return False

    token_file = Path.home() / ".rm_api_device_token"
    token_file.write_text(device_token)

    sync_dir = str(Path.home() / ".rm_api_sync")
    logging.getLogger("rm_api").setLevel(logging.ERROR)
    try:
        api = API(token_file_path=str(token_file), sync_file_path=sync_dir, log_file=os.devnull)
        if api.offline_mode:
            log.error("rm_api: offline — cannot upload")
            return False

        api.get_documents()

        folder_name = folder.strip("/")
        target = next(
            (c for c in api.document_collections.values()
             if c.metadata.visible_name == folder_name and not c.metadata.parent),
            None,
        )
        if target is None:
            target = DocumentCollection.create(api, folder_name, parent=None)
            api.upload(target)

        pdf_bytes = Path(pdf_path).read_bytes()
        doc = Document.new_pdf(api=api, name=title, pdf_data=pdf_bytes, parent=target.uuid)
        api.upload(doc)
        return True

    except Exception:
        log.exception("rm_api upload error")
        return False


def main():
    config = load_config()

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

                size_mb = Path(pdf_path).stat().st_size / 1024**2
                log.info("PDF size: %.1f MB — %s", size_mb, title)
                if upload_to_remarkable(pdf_path, title, config["remarkable_folder"]):
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
    wait_for_network()
    try:
        main()
    except (Exception, SystemExit) as exc:
        log.error("Run failed (%s). Will retry once in 1 hour.", exc)
        time.sleep(3600)
        log.info("Retrying after 1 hour delay...")
        wait_for_network()
        main()
