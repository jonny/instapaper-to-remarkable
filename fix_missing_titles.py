#!/usr/bin/env python3
"""
One-off script: scan /Instapaper on Remarkable and re-upload any articles
whose PDFs are missing a title heading.

Run with: .venv/bin/python3.12 fix_missing_titles.py
"""

import logging
import os
import re
import shutil
import tempfile
from pathlib import Path

# Module-level imports from main script bring in the certifi monkey-patch
# and shared helpers — intentional.
from instapaper_to_remarkable import (
    INSTAPAPER_API,
    _get_rm_device_token,
    article_to_pdf,
    fetch_html,
    instapaper_auth,
    load_config,
    load_processed,
    sanitize_filename,
)
import trafilatura

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
)
log = logging.getLogger(__name__)


def fetch_instapaper_bookmarks(session):
    """Fetch bookmarks from all folders. Returns {bookmark_id: {title, url}}."""
    result = {}
    for folder in ("unread", "starred", "archive"):
        resp = session.post(
            f"{INSTAPAPER_API}/api/1/bookmarks/list",
            data={"folder_id": folder, "limit": 500},
        )
        if resp.status_code != 200:
            log.warning("Could not fetch folder '%s' (%s)", folder, resp.status_code)
            continue
        items = [b for b in resp.json() if b.get("type") == "bookmark"]
        for item in items:
            bid = str(item["bookmark_id"])
            result[bid] = {"title": item.get("title", ""), "url": item.get("url", "")}
        log.info("  %s: %d bookmarks", folder, len(items))
    return result


def has_title_heading(url):
    """Return True if the article's extracted HTML contains an <h1>."""
    html = fetch_html(url)
    if not html:
        return None  # couldn't fetch — unknown
    content = trafilatura.extract(html, output_format="html", include_formatting=True)
    if not content:
        return None
    return bool(re.search(r"<h1[\s>]", content, re.IGNORECASE))


def find_doc(folder_docs, title):
    """Find a Remarkable doc matching the given Instapaper title."""
    # rm_api uploads use the raw title as visible_name
    for doc in folder_docs.values():
        if doc.metadata.visible_name == title:
            return doc
    # Older rmapi uploads used the sanitized filename (without .pdf)
    sanitized = sanitize_filename(title)
    for doc in folder_docs.values():
        if doc.metadata.visible_name == sanitized:
            return doc
    return None


def main():
    config = load_config()

    log.info("Authenticating with Instapaper...")
    session = instapaper_auth(config)

    log.info("Fetching Instapaper bookmarks (all folders)...")
    all_bookmarks = fetch_instapaper_bookmarks(session)
    log.info("Total bookmarks retrieved: %d", len(all_bookmarks))

    processed = load_processed(config["processed_log"])
    log.info("Total processed articles in log: %d", len(processed))

    # Only articles we have both a processed entry AND Instapaper metadata for
    to_check = {
        bid: all_bookmarks[bid]
        for bid in processed
        if bid in all_bookmarks and all_bookmarks[bid]["url"]
    }
    missing_metadata = len(processed) - len(to_check)
    log.info(
        "Articles to check: %d (%d skipped — not found in Instapaper fetch)",
        len(to_check), missing_metadata,
    )

    log.info("Connecting to Remarkable...")
    from rm_api import API
    from rm_api.models import Document

    logging.getLogger("rm_api").setLevel(logging.ERROR)
    token_file = Path.home() / ".rm_api_device_token"
    token_file.write_text(_get_rm_device_token())
    api = API(
        token_file_path=str(token_file),
        sync_file_path=str(Path.home() / ".rm_api_sync"),
        log_file=os.devnull,
    )
    api.get_documents()

    folder_name = config["remarkable_folder"].strip("/")
    target_folder = next(
        (c for c in api.document_collections.values()
         if c.metadata.visible_name == folder_name and not c.metadata.parent),
        None,
    )
    if target_folder is None:
        log.error("Remarkable folder /%s not found — aborting.", folder_name)
        return

    folder_docs = {
        uuid: doc for uuid, doc in api.documents.items()
        if doc.metadata.parent == target_folder.uuid
    }
    log.info("Found %d documents in /%s on Remarkable", len(folder_docs), folder_name)

    # --- Scan ---
    needs_update = []
    unfetchable = 0

    log.info("Scanning articles for missing title headings...")
    for bid, bookmark in to_check.items():
        title = bookmark["title"]
        url = bookmark["url"]

        result = has_title_heading(url)
        if result is None:
            log.debug("Could not fetch/extract — skipping: %s", title)
            unfetchable += 1
            continue
        if result:
            log.debug("Title present — OK: %s", title)
            continue

        doc = find_doc(folder_docs, title)
        if doc is None:
            log.warning("No matching Remarkable doc found for: %s", title)
            unfetchable += 1
            continue

        needs_update.append((bid, bookmark, doc))
        log.info("Needs update: %s", title)

    log.info(
        "Scan complete — needs update: %d, unfetchable/unmatched: %d, OK: %d",
        len(needs_update), unfetchable, len(to_check) - len(needs_update) - unfetchable,
    )

    if not needs_update:
        log.info("Nothing to do.")
        return

    # --- Update ---
    tmpdir = tempfile.mkdtemp(prefix="instapaper_fix_")
    updated = 0
    failed = 0
    try:
        for bid, bookmark, old_doc in needs_update:
            title = bookmark["title"]
            url = bookmark["url"]
            log.info("Updating: %s", title)

            pdf_path = Path(tmpdir) / (sanitize_filename(title) + ".pdf")
            try:
                if not article_to_pdf(title, url, str(pdf_path)):
                    log.warning("Could not regenerate PDF — skipping: %s", title)
                    failed += 1
                    continue

                # Upload new version first (safer — old doc untouched if this fails)
                pdf_bytes = pdf_path.read_bytes()
                new_doc = Document.new_pdf(
                    api=api, name=title, pdf_data=pdf_bytes,
                    parent=target_folder.uuid,
                )
                api.upload(new_doc)
                log.info("  Uploaded new version")

                # Only delete old doc once new one is confirmed uploaded
                api.delete(old_doc)
                log.info("  Deleted old version")

                updated += 1
            except Exception:
                log.exception("Error updating: %s", title)
                failed += 1
    finally:
        shutil.rmtree(tmpdir, ignore_errors=True)

    log.info("Done. Updated: %d, failed: %d", updated, failed)


if __name__ == "__main__":
    main()
