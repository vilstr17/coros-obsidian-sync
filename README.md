# 🏃 Training Sync

Automatic sync pipeline from **COROS** watch data → **Obsidian** vault.

## What it does

Every evening (cron at 22:00) it pulls your last 7 days from COROS and writes:

```
Training Log/
├── Dashboard.md                    ← overview with HR zones, totals
├── 00-Daily/YYYY-MM/              ← daily metrics + sleep + activities
├── 01-Activities/YYYY-MM/         ← per-activity detail notes
├── 02-Weekly/                     ← weekly summaries (W18, W19...)
└── 03-Monthly/                    ← monthly MOCs (index pages)
```

Monthly folders keep things tidy — ~30 files max per folder, nothing gets deleted.

## Setup

1. **Install Obsidian Local REST API plugin**  
   Obsidian → Settings → Community Plugins → `Local REST API`  
   Copy the API key and set it in `.env` as `OBSIDIAN_API_KEY`.

2. **COROS credentials**  
   Put your COROS email & password in `.env`:
   ```
   COROS_EMAIL=you@example.com
   COROS_PASSWORD=your_password
   COROS_REGION=eu
   OBSIDIAN_API_KEY=...
   OBSIDIAN_VAULT=Life
   ```

3. **Run**
   ```bash
   cd training-sync
   python3 -m venv .venv
   source .venv/bin/activate
   pip install -r requirements.txt
   ./run.sh --days 7
   ```

4. **Cron** (optional)
   ```bash
   crontab -e
   # 0 22 * * * cd ~/training-sync && ./run.sh --days 7
   ```

## Files

| File | Purpose |
|------|---------|
| `coros_client.py` | Login, token refresh, fetch activities / daily records / sleep |
| `obsidian_writer.py` | Markdown builders + Obsidian Local REST API client |
| `sync.py` | Orchestration: daily → activities → weekly → monthly → dashboard |
| `run.sh` | Launcher (activates venv, runs sync) |
| `requirements.txt` | `httpx`, `pycryptodome`, `python-dotenv` |

## License

MIT
