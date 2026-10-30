"""Main pipeline: download -> upload -> parse -> insert, in batches.

Usage:
    python orchestrator.py --tender-id 2026_CERWI_131627_1 --headful
    python orchestrator.py --limit 10
"""
from __future__ import annotations

import argparse
import logging
import sys
import time
from pathlib import Path
from typing import Any

from boq_parser import parse_boq
from config import BATCH_SIZE
from downloader import CaptchaError, DocumentDownloader
from dtcn_parser import parse_dtcn
from supabase_io import (
    get_processed_tenders,
    get_tender,
    get_unprocessed_tenders,
    insert_boq_items,
    mark_processed,
    upload_file,
    upsert_dtcn_document,
)

LOG_FILE = Path(__file__).resolve().parent / "pipeline.log"

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s",
    handlers=[logging.StreamHandler(sys.stdout), logging.FileHandler(LOG_FILE)],
)
log = logging.getLogger("pipeline")


def _is_xls(filename: str) -> bool:
    return filename.lower().endswith((".xls", ".xlsx"))


def _is_pdf(filename: str) -> bool:
    return filename.lower().endswith(".pdf")


def process_tender(
    downloader: DocumentDownloader, tender: dict[str, Any]
) -> tuple[bool, str]:
    """Run the full pipeline for one tender. Returns (success, message)."""
    tender_id = tender["tender_id"]
    files = downloader.download_tender_documents(tender)
    if not files:
        return False, "no documents extracted"

    # Upload every file to storage; collect BOQ rows and DTCN/NIT text.
    boq_rows: list[dict[str, Any]] = []
    dtcn_texts: list[str] = []
    dtcn_pages = 0
    for f in files:
        object_path = upload_file(tender_id, f["filename"], f["local_path"])
        if _is_xls(f["filename"]):
            rows = parse_boq(f["local_path"])
            for r in rows:
                r["storage_path"] = object_path
            boq_rows.extend(rows)
        elif _is_pdf(f["filename"]):
            parsed = parse_dtcn(f["local_path"])
            # Label each PDF's text so the concatenated DTCN stays navigable.
            dtcn_texts.append(f"===== {f['filename']} =====\n{parsed['raw_text']}")
            dtcn_pages += parsed["page_count"]

    boq_count = insert_boq_items(tender_id, boq_rows)

    if dtcn_texts:
        upsert_dtcn_document(
            tender_id,
            "\n\n".join(dtcn_texts),
            f"{tender_id}/",
            dtcn_pages,
        )

    mark_processed(tender_id, f"{tender_id}/")
    summary = f"{len(files)} files, {boq_count} boq items"
    if dtcn_texts:
        summary += f", dtcn {len(dtcn_texts)} pdfs/{dtcn_pages} pages"
    return True, summary


# Substrings that signal the Playwright browser/driver has died and the
# downloader must be relaunched before it can process anything else.
_BROWSER_DEAD = (
    "connection closed",
    "reading from the driver",
    "target page, context or browser has been closed",
    "browser has been closed",
    "target closed",
)

# Relaunch the browser every N tenders to avoid memory buildup over long runs.
_RECYCLE_EVERY = 8


def _browser_dead(exc: Exception) -> bool:
    msg = str(exc).lower()
    return any(token in msg for token in _BROWSER_DEAD)


def run(tenders: list[dict[str, Any]], headful: bool) -> tuple[int, int]:
    ok = failed = 0
    with DocumentDownloader(headful=headful) as downloader:
        for i, tender in enumerate(tenders):
            tender_id = tender["tender_id"]

            # Proactively recycle the browser to keep long runs stable.
            if i and i % _RECYCLE_EVERY == 0:
                try:
                    downloader.restart()
                except Exception:
                    log.warning("browser recycle failed; continuing")

            for attempt in (1, 2):
                try:
                    success, message = process_tender(downloader, tender)
                    if success:
                        ok += 1
                        log.info("OK %s (%s)", tender_id, message)
                    else:
                        failed += 1
                        log.warning("FAIL %s: %s", tender_id, message)
                    break
                except CaptchaError as exc:
                    failed += 1
                    log.error("FAIL %s: captcha — %s", tender_id, exc)
                    break
                except Exception as exc:
                    # A dead browser would cascade to every later tender; relaunch
                    # it and retry this one once before giving up on it.
                    if _browser_dead(exc) and attempt == 1:
                        log.warning("browser crashed on %s; restarting", tender_id)
                        try:
                            downloader.restart()
                        except Exception:
                            log.exception("restart failed")
                        continue
                    failed += 1
                    log.exception("FAIL %s: %s", tender_id, exc)
                    break
    return ok, failed


def main() -> None:
    parser = argparse.ArgumentParser(description="Odisha tender document pipeline")
    parser.add_argument("--tender-id", help="Process a single tender id and exit")
    parser.add_argument(
        "--limit", type=int, default=10, help="Max tenders to process (default 10)"
    )
    parser.add_argument(
        "--max-minutes",
        type=float,
        default=0,
        help="Stop starting new batches after this many minutes (0 = no limit)",
    )
    parser.add_argument(
        "--batch-size",
        type=int,
        default=BATCH_SIZE,
        help="Tenders fetched per DB batch (default 10)",
    )
    parser.add_argument(
        "--headful", action="store_true", help="Run the browser non-headless"
    )
    parser.add_argument(
        "--reprocess",
        action="store_true",
        help="Re-process already-done tenders (storage_path set) instead of new ones",
    )
    args = parser.parse_args()

    # Single-tender mode (smoke test).
    if args.tender_id:
        tender = get_tender(args.tender_id)
        if not tender:
            log.error("Tender %s not found", args.tender_id)
            sys.exit(1)
        ok, failed = run([tender], args.headful)
        log.info("Done. ok=%d failed=%d", ok, failed)
        sys.exit(0 if failed == 0 else 1)

    # Batch mode.
    total_ok = total_failed = total_seen = 0
    deadline = time.monotonic() + args.max_minutes * 60 if args.max_minutes else None

    def out_of_time() -> bool:
        if deadline and time.monotonic() >= deadline:
            log.info("Time budget reached; stopping after this run")
            return True
        return False

    if args.reprocess:
        # Fixed list of already-done tenders (re-querying would loop forever,
        # since each stays "processed"). Chunk it so the session refreshes.
        todo = get_processed_tenders(args.limit)
        for start in range(0, len(todo), args.batch_size):
            if out_of_time():
                break
            batch = todo[start : start + args.batch_size]
            total_seen += len(batch)
            ok, failed = run(batch, args.headful)
            total_ok += ok
            total_failed += failed
            log.info("Batch summary: ok=%d failed=%d", ok, failed)
    else:
        attempted: set[str] = set()
        while total_seen < args.limit:
            if out_of_time():
                break
            remaining = args.limit - total_seen
            batch = get_unprocessed_tenders(min(args.batch_size, remaining), attempted)
            if not batch:
                break
            attempted.update(t["tender_id"] for t in batch)
            total_seen += len(batch)
            ok, failed = run(batch, args.headful)
            total_ok += ok
            total_failed += failed
            log.info("Batch summary: ok=%d failed=%d", ok, failed)

    log.info(
        "Pipeline finished. processed=%d ok=%d failed=%d",
        total_seen,
        total_ok,
        total_failed,
    )

    # Surface a systemic problem (e.g. the portal blocking the runner's IP) as a
    # failed job, while tolerating the odd transient per-tender failure.
    if total_seen > 0 and total_ok == 0:
        log.error("All %d tenders failed — likely a systemic issue", total_seen)
        sys.exit(1)


if __name__ == "__main__":
    main()
