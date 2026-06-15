"""Shared configuration, Supabase client, and tunables for the pipeline."""
from __future__ import annotations

import os
from functools import lru_cache
from pathlib import Path

from dotenv import load_dotenv
from supabase import Client, create_client

load_dotenv()

SUPABASE_URL = os.environ.get("SUPABASE_URL", "").strip()
SUPABASE_SERVICE_KEY = os.environ.get("SUPABASE_SERVICE_KEY", "").strip()

# 2Captcha API key for solving the document-download captcha.
TWOCAPTCHA_API_KEY = os.environ.get("TWOCAPTCHA_API_KEY", "").strip()

# Storage bucket for downloaded tender documents.
BUCKET = "tender-documents"

# Local scratch dir for downloads before upload.
TEMP_DIR = Path(__file__).resolve().parent / "temp"

# Pipeline tunables.
BATCH_SIZE = 10
DOWNLOAD_RETRIES = 3              # retries for navigation/session failures
CAPTCHA_ATTEMPTS = 6             # captcha solve attempts per tender
NAV_TIMEOUT_MS = 60_000          # page navigation timeout
DOWNLOAD_TIMEOUT_MS = 120_000    # wait for a download to start/finish

# NIC GePNIC portal endpoints.
PORTAL_HOME = "https://tendersodisha.gov.in/nicgep/app"
PORTAL_BASE = "https://tendersodisha.gov.in"
# Hitting this first establishes a fresh session so an expired $DirectLink in
# tenders.source_url resolves to the live tender detail page.
RESTART_URL = "https://tendersodisha.gov.in/nicgep/app?service=restart"


def _require_credentials() -> None:
    missing = [
        name
        for name, value in (
            ("SUPABASE_URL", SUPABASE_URL),
            ("SUPABASE_SERVICE_KEY", SUPABASE_SERVICE_KEY),
        )
        if not value
    ]
    if missing:
        raise RuntimeError(
            "Missing required environment variable(s): "
            + ", ".join(missing)
            + ". Copy .env.example to .env and fill them in."
        )


@lru_cache(maxsize=1)
def get_client() -> Client:
    """Return a singleton Supabase client built from the service key."""
    _require_credentials()
    return create_client(SUPABASE_URL, SUPABASE_SERVICE_KEY)
