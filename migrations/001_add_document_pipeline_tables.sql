-- 001_add_document_pipeline_tables.sql
-- Adds document-pipeline schema to the odisha-tender-portal project.
-- Applied via Supabase MCP migration "add_document_pipeline_tables".

-- tenders: processed gate (orchestrator skips rows where storage_path is not null)
alter table public.tenders
  add column if not exists storage_path text;

-- boq_items: link each parsed row to the source file in storage
alter table public.boq_items
  add column if not exists storage_path text;

-- new DTCN documents table (full text + provenance)
create table if not exists public.dtcn_documents (
  id           uuid primary key default gen_random_uuid(),
  tender_id    varchar not null references public.tenders(tender_id),
  raw_text     text,
  storage_path text,
  page_count   integer,
  created_at   timestamptz default now()
);

-- unique tender_id makes the DTCN insert an idempotent upsert target
create unique index if not exists dtcn_documents_tender_id_key
  on public.dtcn_documents(tender_id);

create index if not exists boq_items_tender_id_idx
  on public.boq_items(tender_id);

-- public storage bucket for downloaded tender documents
insert into storage.buckets (id, name, public)
values ('tender-documents', 'tender-documents', true)
on conflict (id) do nothing;

-- allow public read of objects in this bucket (uploads use the service key,
-- which bypasses RLS; this policy only grants public read access)
do $$
begin
  if not exists (
    select 1 from pg_policies
    where schemaname = 'storage' and tablename = 'objects'
      and policyname = 'public read tender-documents'
  ) then
    create policy "public read tender-documents"
      on storage.objects for select
      using (bucket_id = 'tender-documents');
  end if;
end $$;
