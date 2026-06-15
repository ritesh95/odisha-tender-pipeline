# Odisha Tender Document Pipeline

Downloads, stores, and parses BOQ/DTCN documents for tenders scraped from
[tendersodisha.gov.in](https://tendersodisha.gov.in) into the Supabase project
`odisha-tender-portal`.

For each unprocessed tender it:

1. Establishes a fresh portal session (`?service=restart`), then opens the tender's
   stored `source_url` — which alone shows "session timed out", but resolves to the
   live detail page once a session exists (Playwright).
2. **Authorizes the session** by downloading the **"Download as zip file"** bundle
   (which yields the BOQ `.xls`). This is gated by a captcha (`DocDownCaptcha`) solved
   via **2Captcha** — one solve per tender.
3. **Reloads the detail page.** Once authorized, every document link is re-rendered as
   a pre-authorized `$DirectLink_N&sp=...` URL that serves the file directly. The
   NIT/Tendernotice (DTCN) PDFs — which are *not* in the zip — are fetched here by
   matching each anchor whose text is the document's filename.
4. Uploads every file to Supabase Storage at `tender-documents/{tender_id}/{filename}`.
5. Parses each BOQ `.xls` into `boq_items` rows (pandas + xlrd, auto header detection).
6. Concatenates all tender PDFs (Tendernotice/NIT + any bundled DTCN) into a single
   `dtcn_documents` row — labeled per file, page counts summed (pdfplumber).
7. Sets `tenders.storage_path` so the tender is skipped on future runs.

> **Why the restart + captcha + reload dance?** The stored `source_url` is a one-time
> `$DirectLink` bound to the scrape-time session, so it can't be hit directly. The
> portal gates document downloads behind a captcha, but solving **one** captcha
> authorizes the whole session — after which the detail page's links become direct
> `sp`-token downloads (this mirrors the manual flow: solve captcha once, then
> download each document). The BOQ comes from the zip; the DTCN/NIT PDFs come from the
> post-authorization direct links.

## Setup

```powershell
python -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -r requirements.txt
playwright install chromium

copy .env.example .env   # then fill in SUPABASE_SERVICE_KEY
```

`.env` needs:

- `SUPABASE_URL` — `https://yebgtohucsnrxmyijyts.supabase.co`
- `SUPABASE_SERVICE_KEY` — service role key (Settings → API). Secret; bypasses RLS.
- `TWOCAPTCHA_API_KEY` — [2Captcha](https://2captcha.com) key for the download captcha.

## Usage

```powershell
# Smoke test a single tender (watch the browser)
python orchestrator.py --tender-id 2026_CERWI_131627_1 --headful

# Process a batch
python orchestrator.py --limit 10
```

`--limit` caps how many tenders are processed; `--batch-size` controls the DB
fetch size (default 10). Already-processed tenders (`storage_path` set) are
skipped. Re-running a failed tender re-downloads and replaces its rows.

Logs go to stdout and `pipeline.log`. Downloads land in `temp/{tender_id}/`.

## Files

| File | Role |
|------|------|
| `config.py` | env loading, Supabase client, tunables |
| `supabase_io.py` | DB reads/writes + storage upload |
| `captcha_solver.py` | 2Captcha integration for the download captcha |
| `downloader.py` | Playwright: session restart → zip bundle → captcha → extract |
| `boq_parser.py` | `.xls` → line items |
| `dtcn_parser.py` | `.pdf` → text + page count |
| `orchestrator.py` | main batch pipeline |
| `migrations/001_*.sql` | schema (already applied to the project) |

## Schema

- `tenders.storage_path` (added) — processed gate / docs folder path.
- `boq_items.storage_path` (added) — source file for each parsed line.
- `dtcn_documents` (new) — `tender_id`, `raw_text`, `storage_path`, `page_count`.
- `tender-documents` storage bucket (public read).

## Notes & known risks

- **RLS is disabled** on all public tables — anyone with the anon key can
  read/write every row. Enabling it requires adding access policies; the
  service key used here bypasses RLS regardless. See `migrations/` security note.
- **Session/captcha:** handled automatically (restart + 2Captcha). If 2Captcha runs
  out of balance or returns a bad key, the tender is logged `FAIL ... captcha` and the
  batch continues. Each captcha-gated tender costs one 2Captcha solve (~$0.001–0.003);
  sessions that render a pre-authorized zip link cost nothing.
- **Coverage:** only tenders whose `source_url` still resolves on the live portal can
  be fetched. Tenders that have moved to the Archive may not resolve; those log a
  `stale session`/`no zip link` failure and are left unprocessed.
- A few BOQ sheets with unusual layouts may fail header detection; those tenders
  are logged and left unprocessed for follow-up.
