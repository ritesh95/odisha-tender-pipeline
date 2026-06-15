"""Supabase database reads/writes and storage upload helpers."""
from __future__ import annotations

import mimetypes
from pathlib import Path
from typing import Any

from config import BUCKET, get_client


def get_unprocessed_tenders(limit: int) -> list[dict[str, Any]]:
    """Tenders with documents that have not been processed yet."""
    client = get_client()
    resp = (
        client.table("tenders")
        .select("tender_id, source_url, boq_documents, nit_documents")
        .is_("storage_path", "null")
        .not_.is_("boq_documents", "null")
        .order("published_date", desc=True)
        .limit(limit)
        .execute()
    )
    # jsonb_array_length can't be expressed via the query builder; filter here.
    return [t for t in (resp.data or []) if t.get("boq_documents")]


def get_processed_tenders(limit: int) -> list[dict[str, Any]]:
    """Already-processed tenders (storage_path set), for re-processing/backfill."""
    client = get_client()
    resp = (
        client.table("tenders")
        .select("tender_id, source_url, boq_documents, nit_documents")
        .not_.is_("storage_path", "null")
        .order("published_date", desc=True)
        .limit(limit)
        .execute()
    )
    return resp.data or []


def get_tender(tender_id: str) -> dict[str, Any] | None:
    """Fetch a single tender by id (used for the --tender-id smoke test)."""
    client = get_client()
    resp = (
        client.table("tenders")
        .select("tender_id, source_url, boq_documents, nit_documents")
        .eq("tender_id", tender_id)
        .limit(1)
        .execute()
    )
    rows = resp.data or []
    return rows[0] if rows else None


def upload_file(tender_id: str, filename: str, local_path: str | Path) -> str:
    """Upload a local file to tender-documents/{tender_id}/{filename}.

    Returns the object path within the bucket. Overwrites on re-run.
    """
    local_path = Path(local_path)
    object_path = f"{tender_id}/{filename}"
    content_type = mimetypes.guess_type(filename)[0] or "application/octet-stream"

    with local_path.open("rb") as fh:
        data = fh.read()

    client = get_client()
    client.storage.from_(BUCKET).upload(
        path=object_path,
        file=data,
        file_options={"content-type": content_type, "upsert": "true"},
    )
    return object_path


def insert_boq_items(tender_id: str, rows: list[dict[str, Any]]) -> int:
    """Replace this tender's BOQ items with a fresh parsed set (idempotent).

    Each row should carry its own ``storage_path`` (the source file it came
    from), since a tender's zip may contain more than one BOQ sheet.
    """
    client = get_client()
    client.table("boq_items").delete().eq("tender_id", tender_id).execute()
    if not rows:
        return 0

    payload = [
        {
            "tender_id": tender_id,
            "item_no": r.get("item_no"),
            "description": r.get("description"),
            "unit": r.get("unit"),
            "quantity": r.get("quantity"),
            "rate": r.get("rate"),
            "amount": r.get("amount"),
            "storage_path": r.get("storage_path"),
        }
        for r in rows
    ]
    client.table("boq_items").insert(payload).execute()
    return len(payload)


def upsert_dtcn_document(
    tender_id: str, raw_text: str, storage_path: str, page_count: int
) -> None:
    """Insert/update the DTCN document row for a tender (unique on tender_id)."""
    client = get_client()
    client.table("dtcn_documents").upsert(
        {
            "tender_id": tender_id,
            "raw_text": raw_text,
            "storage_path": storage_path,
            "page_count": page_count,
        },
        on_conflict="tender_id",
    ).execute()


def mark_processed(tender_id: str, storage_path: str) -> None:
    """Set the tender's storage_path so it is skipped on subsequent runs."""
    client = get_client()
    client.table("tenders").update({"storage_path": storage_path}).eq(
        "tender_id", tender_id
    ).execute()
