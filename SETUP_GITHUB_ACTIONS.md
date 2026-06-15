# Running the pipeline live on GitHub Actions

The workflow at [.github/workflows/pipeline.yml](.github/workflows/pipeline.yml)
runs the pipeline **every 4 hours**. Each run processes tenders that are not yet
done (`storage_path IS NULL`), so it clears the backlog and automatically picks
up any newly-scraped tenders. Failed tenders stay unprocessed and are retried on
the next run.

## One-time setup

### 1. Push this project to a GitHub repo
From the project folder:
```bash
git init
git add .
git commit -m "Odisha tender document pipeline"
gh repo create odisha-tender-pipeline --private --source=. --push
# or, without gh:
#   create an empty repo on github.com, then:
#   git remote add origin https://github.com/<you>/odisha-tender-pipeline.git
#   git branch -M main && git push -u origin main
```
`.env` is git-ignored, so your secrets are NOT pushed.

### 2. Add the secrets in GitHub
Repo → **Settings → Secrets and variables → Actions → New repository secret**.
Add three (same values as your local `.env`):

| Secret | Value |
|---|---|
| `SUPABASE_URL` | `https://yebgtohucsnrxmyijyts.supabase.co` |
| `SUPABASE_SERVICE_KEY` | your service-role key |
| `TWOCAPTCHA_API_KEY` | your 2Captcha key |

### 3. Done
Scheduled runs start automatically. To run on demand: **Actions → tender-document-pipeline → Run workflow** (you can set a `limit`, or tick `reprocess` to re-do already-done tenders).

## Tuning
- **Frequency / batch size:** edit the `cron` and the `limit` default in the
  workflow. `limit: 40` every 4 h ≈ 240 tenders/day — clears the ~690 backlog in
  ~3 days, then idles on new ones.
- **Logs:** each run uploads `pipeline.log` as a downloadable artifact (Actions →
  the run → Artifacts).

## ⚠️ Important risk: runner location
GitHub-hosted runners are in the US/EU. `tendersodisha.gov.in` is an Indian
government portal that **may be slow or may block datacenter / non-India IPs**.
If scheduled runs fail with timeouts or blocks where local runs succeed, switch
to a **self-hosted runner** on an India-based machine (or your own PC):

1. Repo → Settings → Actions → Runners → **New self-hosted runner**, follow the
   install steps on the target machine.
2. In the workflow, change `runs-on: ubuntu-latest` to
   `runs-on: self-hosted`.

A self-hosted runner on a machine that's normally on (or a small India-region
VM) avoids both the IP-block risk and 2Captcha geolocation quirks.

## Notes
- The runner is stateless: it installs Chromium fresh each run, so the browser
  recycling/restart logic in `orchestrator.py` keeps long runs stable.
- Concurrency is capped to one run at a time, so overlapping schedules can't
  double-process or waste captcha solves.
