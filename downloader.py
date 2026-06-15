"""Playwright-based downloader for tender BOQ + DTCN/NIT documents.

The tender's stored `source_url` is a `$DirectLink` whose session expires; hit
`?service=restart` first and the same URL then resolves to the live tender
detail page.

Document access on this portal works in two stages, mirroring the manual flow:

  1. **Authorize the session** by downloading the "Download as zip file" bundle
     (which also yields the BOQ `.xls`). This is gated by a captcha solved via
     2Captcha.
  2. **Reload the detail page.** Once authorized, every document link is
     re-rendered as a pre-authorized `$DirectLink_N&sp=...` URL that serves the
     file directly (no further captcha). We fetch each by matching the anchor
     whose text is the document's filename, then GET it with the context's
     cookies.

So a tender costs at most one captcha solve, and the NIT/Tendernotice PDFs (which
are not in the zip) are picked up in stage 2.
"""
from __future__ import annotations

import zipfile
from pathlib import Path
from typing import Any

from playwright.sync_api import (
    Download,
    Page,
    TimeoutError as PlaywrightTimeoutError,
    sync_playwright,
)
from tenacity import (
    retry,
    retry_if_exception_type,
    stop_after_attempt,
    wait_exponential,
)

from captcha_solver import CaptchaSolveError, solve_captcha
from config import (
    CAPTCHA_ATTEMPTS,
    DOWNLOAD_RETRIES,
    DOWNLOAD_TIMEOUT_MS,
    NAV_TIMEOUT_MS,
    PORTAL_BASE,
    RESTART_URL,
    TEMP_DIR,
)


class CaptchaError(Exception):
    """Raised when the captcha could not be solved within the attempt budget."""


class SessionExpiredError(Exception):
    """Raised when the portal session is stale (retryable after a restart)."""


def _doc_names(tender: dict[str, Any]) -> list[str]:
    """Filenames the tender advertises across nit_documents + boq_documents."""
    names: list[str] = []
    for key in ("nit_documents", "boq_documents"):
        for d in tender.get(key) or []:
            name = d.get("name")
            if name and name not in names:
                names.append(name)
    return names


class DocumentDownloader:
    """Reusable Playwright browser wrapper. Use as a context manager."""

    def __init__(self, headful: bool = False) -> None:
        self._headful = headful
        self._pw = None
        self._browser = None
        self._context = None

    def __enter__(self) -> "DocumentDownloader":
        self._pw = sync_playwright().start()
        self._browser = self._pw.chromium.launch(headless=not self._headful)
        self._context = self._browser.new_context(accept_downloads=True)
        self._context.set_default_navigation_timeout(NAV_TIMEOUT_MS)
        self._restart_session()
        return self

    def __exit__(self, *exc: object) -> None:
        for closer in (self._context, self._browser):
            try:
                if closer is not None:
                    closer.close()
            except Exception:
                pass
        if self._pw is not None:
            self._pw.stop()

    def _restart_session(self) -> None:
        """Establish a fresh portal session so expired DirectLinks resolve."""
        page = self._context.new_page()
        try:
            page.goto(RESTART_URL, wait_until="domcontentloaded")
        finally:
            page.close()

    def restart(self) -> None:
        """Tear down and relaunch the browser (recover from a crash / recycle)."""
        for closer in (self._context, self._browser):
            try:
                if closer is not None:
                    closer.close()
            except Exception:
                pass
        self._browser = self._pw.chromium.launch(headless=not self._headful)
        self._context = self._browser.new_context(accept_downloads=True)
        self._context.set_default_navigation_timeout(NAV_TIMEOUT_MS)
        self._restart_session()

    # -- public API --------------------------------------------------------

    def download_tender_documents(
        self, tender: dict[str, Any]
    ) -> list[dict[str, str]]:
        """Download + extract a tender's documents (BOQ zip + all PDFs).

        Returns a list of {filename, local_path} for every file obtained.
        Raises CaptchaError if the authorizing captcha could not be solved.
        """
        source_url = tender.get("source_url")
        if not source_url:
            return []

        dest_dir = TEMP_DIR / tender["tender_id"]
        dest_dir.mkdir(parents=True, exist_ok=True)
        return self._collect(
            tender["tender_id"], source_url, _doc_names(tender), dest_dir
        )

    # -- navigation + download --------------------------------------------

    @retry(
        retry=retry_if_exception_type(
            (SessionExpiredError, PlaywrightTimeoutError)
        ),
        stop=stop_after_attempt(DOWNLOAD_RETRIES),
        wait=wait_exponential(multiplier=2, min=2, max=20),
        reraise=True,
    )
    def _collect(
        self,
        tender_id: str,
        source_url: str,
        doc_names: list[str],
        dest_dir: Path,
    ) -> list[dict[str, str]]:
        page = self._context.new_page()
        try:
            page.goto(source_url, wait_until="domcontentloaded")
            if "stale session" in (page.title() or "").lower():
                self._restart_session()
                raise SessionExpiredError(f"stale session for {tender_id}")

            zip_link = page.locator("a:has-text('Download as zip')")
            if not zip_link.count():
                raise SessionExpiredError(
                    f"no zip link on detail page for {tender_id}"
                )
            zip_url = PORTAL_BASE + zip_link.first.get_attribute("href")

            files: list[dict[str, str]] = []
            got: set[str] = set()

            # Stage 1: authorize the session + obtain the BOQ (and any bundled
            # DTCN) from the zip.
            zip_path = self._get_document(page, zip_url, dest_dir / "bundle.zip")
            for extracted in self._extract(zip_path, dest_dir):
                files.append(extracted)
                got.add(extracted["filename"])

            # Stage 2: reload the (now authorized) detail page; each document is
            # a direct $DirectLink+sp URL we can fetch by cookie.
            page.goto(source_url, wait_until="domcontentloaded")
            for name in doc_names:
                if name in got:
                    continue
                anchor = page.locator(f'a:has-text("{name}")')
                if not anchor.count():
                    continue
                href = anchor.first.get_attribute("href")
                if not href:
                    continue
                dest = self._fetch_direct(PORTAL_BASE + href, dest_dir / name)
                if dest is not None:
                    files.append({"filename": name, "local_path": str(dest)})
                    got.add(name)

            return files
        finally:
            page.close()

    def _fetch_direct(self, url: str, dest: Path) -> Path | None:
        """GET a pre-authorized document URL; save if it is a real file."""
        body = self._context.request.get(url).body()
        if body[:4] == b"%PDF" or body[:2] == b"PK":
            dest.write_bytes(body)
            return dest
        return None

    def _get_document(self, page: Page, url: str, dest: Path) -> Path:
        """Fetch the zip, solving the captcha if the session is not yet authorized.

        Tries a cookie-shared request first (free if already authorized); a zip
        starts with ``PK``. Otherwise solve the DocDownCaptcha and capture the
        resulting download.
        """
        body = self._context.request.get(url).body()
        if body[:2] == b"PK":
            dest.write_bytes(body)
            return dest

        page.goto(url, wait_until="domcontentloaded")
        if not page.locator("#captchaImage").count():
            raise SessionExpiredError(
                f"zip route returned neither a zip nor a captcha: {dest.name}"
            )
        return self._solve_and_download(page, dest)

    def _solve_and_download(self, page: Page, dest: Path) -> Path:
        """On the DocDownCaptcha page: solve, submit, capture the file."""
        for _ in range(CAPTCHA_ATTEMPTS):
            if not page.locator("#captchaImage").count():
                break
            try:
                answer = solve_captcha(
                    page.locator("#captchaImage").get_attribute("src")
                )
            except CaptchaSolveError:
                # Solver couldn't read it; request a fresh captcha and retry.
                if page.locator("#captcha").count():
                    page.click("#captcha")
                    page.wait_for_timeout(1000)
                continue
            page.fill("#captchaText", answer)
            try:
                with page.expect_download(timeout=DOWNLOAD_TIMEOUT_MS) as dl_info:
                    page.click("#Submit")
                download: Download = dl_info.value
            except PlaywrightTimeoutError:
                # Wrong captcha -> page reloads with a fresh challenge; retry.
                if page.locator("#captchaImage").count():
                    continue
                raise
            download.save_as(str(dest))
            return dest

        raise CaptchaError(
            f"failed to solve captcha after {CAPTCHA_ATTEMPTS} attempts"
        )

    # -- zip handling ------------------------------------------------------

    @staticmethod
    def _extract(zip_path: Path, dest_dir: Path) -> list[dict[str, str]]:
        extract_dir = dest_dir / "extracted"
        extract_dir.mkdir(parents=True, exist_ok=True)

        try:
            with zipfile.ZipFile(zip_path) as zf:
                zf.extractall(extract_dir)
        except zipfile.BadZipFile as exc:
            raise ValueError(f"downloaded bundle is not a valid zip: {exc}") from exc

        results: list[dict[str, str]] = []
        for path in sorted(extract_dir.rglob("*")):
            if path.is_file():
                results.append({"filename": path.name, "local_path": str(path)})
        return results
