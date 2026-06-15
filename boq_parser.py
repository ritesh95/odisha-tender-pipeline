"""Parse BOQ spreadsheets (.xls / .xlsx) into structured line items.

BOQ sheets from tendersodisha.gov.in vary in layout, so the header row is
auto-detected by matching expected column labels rather than assumed to be
the first row.
"""
from __future__ import annotations

import re
from pathlib import Path
from typing import Any

import pandas as pd

# Field -> substrings that identify its header cell (lowercased, no spaces).
_FIELD_LABELS: dict[str, tuple[str, ...]] = {
    "item_no": ("itemno", "slno", "sno", "srno", "item", "sl"),
    "description": ("description", "itemdescription", "particular", "nameofwork"),
    "unit": ("unit", "uom"),
    "quantity": ("quantity", "qty", "qnty"),
    "rate": ("rate", "rateinrs", "rateinfigures"),
    "amount": ("amount", "amountinrs", "totalamount"),
}

_NUMERIC_FIELDS = ("quantity", "rate", "amount")

_SKIP_DESCRIPTION = re.compile(
    r"\b(sub\s*-?\s*total|grand\s*total|total|carried\s*over|carried\s*forward|"
    r"page\s*total|b/?f|c/?f)\b",
    re.IGNORECASE,
)


def _norm(value: Any) -> str:
    return re.sub(r"[^a-z0-9]", "", str(value).lower()) if value is not None else ""


def _to_number(value: Any) -> float | None:
    if value is None:
        return None
    text = str(value).strip()
    if not text:
        return None
    # strip currency symbols, commas, spaces; keep digits, dot, minus
    cleaned = re.sub(r"[^0-9.\-]", "", text.replace(",", ""))
    if cleaned in ("", "-", ".", "-."):
        return None
    try:
        return float(cleaned)
    except ValueError:
        return None


def _detect_header(df: pd.DataFrame, scan_rows: int = 25) -> tuple[int, dict[str, int]] | None:
    """Find the header row and map each field to a column index.

    Returns (header_row_index, {field: col_index}) for the best-scoring row,
    or None if no row matches at least description + one numeric column.
    """
    best: tuple[int, dict[str, int], int] | None = None  # (row, mapping, score)

    for row_idx in range(min(scan_rows, len(df))):
        cells = [_norm(v) for v in df.iloc[row_idx].tolist()]
        mapping: dict[str, int] = {}
        for field, labels in _FIELD_LABELS.items():
            for col_idx, cell in enumerate(cells):
                if col_idx in mapping.values() or not cell:
                    continue
                if any(label in cell for label in labels):
                    mapping[field] = col_idx
                    break
        score = len(mapping)
        # Require a description column plus at least one numeric column.
        has_core = "description" in mapping and any(
            f in mapping for f in _NUMERIC_FIELDS
        )
        if has_core and (best is None or score > best[2]):
            best = (row_idx, mapping, score)

    if best is None:
        return None
    return best[0], best[1]


def _is_skippable(row: dict[str, Any]) -> bool:
    desc = (row.get("description") or "").strip()
    nums = [row.get(f) for f in _NUMERIC_FIELDS]
    has_number = any(n is not None for n in nums)

    # fully blank
    if not desc and not has_number and not (row.get("item_no") or "").strip():
        return True
    # subtotal / total / carried-over lines
    if desc and _SKIP_DESCRIPTION.search(desc):
        return True
    # column-number row (e.g. "1 2 3 4 5 6") or any stray numeric row: a real
    # BOQ description always contains letters.
    if desc and not re.search(r"[A-Za-z]", desc):
        return True
    # section header: a description with no unit and no numeric values
    if desc and not has_number and not (row.get("unit") or "").strip():
        return True
    return False


def parse_boq(local_path: str | Path) -> list[dict[str, Any]]:
    """Parse a BOQ workbook into a list of line-item dicts.

    Each dict has: item_no, description, unit, quantity, rate, amount.
    Header/subtotal/blank rows are skipped.
    """
    path = Path(local_path)
    engine = "xlrd" if path.suffix.lower() == ".xls" else "openpyxl"

    raw = pd.read_excel(path, header=None, dtype=str, engine=engine)
    raw = raw.where(pd.notna(raw), None)

    detected = _detect_header(raw)
    if detected is None:
        raise ValueError(f"Could not detect a BOQ header row in {path.name}")
    header_row, mapping = detected

    items: list[dict[str, Any]] = []
    for _, series in raw.iloc[header_row + 1 :].iterrows():
        values = series.tolist()

        def cell(field: str) -> Any:
            idx = mapping.get(field)
            if idx is None or idx >= len(values):
                return None
            return values[idx]

        row = {
            "item_no": (str(cell("item_no")).strip() if cell("item_no") else None),
            "description": (
                str(cell("description")).strip() if cell("description") else None
            ),
            "unit": (str(cell("unit")).strip() if cell("unit") else None),
            "quantity": _to_number(cell("quantity")),
            "rate": _to_number(cell("rate")),
            "amount": _to_number(cell("amount")),
        }
        if _is_skippable(row):
            continue
        items.append(row)

    return items
