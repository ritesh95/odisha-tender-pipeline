"""Solve the NIC document-download captcha via the 2Captcha service.

The captcha is delivered inline on the DocDownCaptcha page as a
`data:image/png;base64,...` image, so we forward that base64 straight to
2Captcha's base64 endpoint and poll for the answer.
"""
from __future__ import annotations

import time

import httpx

from config import TWOCAPTCHA_API_KEY

_IN_URL = "https://2captcha.com/in.php"
_RES_URL = "https://2captcha.com/res.php"

_POLL_INTERVAL_S = 5
_POLL_ATTEMPTS = 24  # ~2 minutes


class CaptchaSolveError(Exception):
    """Raised when 2Captcha cannot return an answer (bad key, no balance, ...)."""


def _strip_data_uri(image_src: str) -> str:
    """Return the raw base64 payload from a data: URI (or pass through)."""
    if "," in image_src and image_src.strip().lower().startswith("data:"):
        return image_src.split(",", 1)[1].strip()
    return image_src.strip()


def solve_captcha(image_src: str) -> str:
    """Submit a base64 captcha image to 2Captcha and return the solved text."""
    if not TWOCAPTCHA_API_KEY:
        raise CaptchaSolveError(
            "TWOCAPTCHA_API_KEY is not set; add it to .env to solve captchas."
        )

    body = _strip_data_uri(image_src)

    with httpx.Client(timeout=30) as client:
        submit = client.post(
            _IN_URL,
            data={
                "key": TWOCAPTCHA_API_KEY,
                "method": "base64",
                "body": body,
                "json": 1,
            },
        ).json()
        if submit.get("status") != 1:
            raise CaptchaSolveError(f"2Captcha submit failed: {submit.get('request')}")

        captcha_id = submit["request"]

        for _ in range(_POLL_ATTEMPTS):
            time.sleep(_POLL_INTERVAL_S)
            res = client.get(
                _RES_URL,
                params={
                    "key": TWOCAPTCHA_API_KEY,
                    "action": "get",
                    "id": captcha_id,
                    "json": 1,
                },
            ).json()
            if res.get("status") == 1:
                return str(res["request"]).strip()
            if res.get("request") != "CAPCHA_NOT_READY":
                raise CaptchaSolveError(f"2Captcha error: {res.get('request')}")

    raise CaptchaSolveError("2Captcha timed out without returning an answer")
