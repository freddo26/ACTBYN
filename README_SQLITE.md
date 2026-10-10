# ActBayan — SQLite version (no MySQL server, no Ollama needed)

## Quick start
```
pip install -r requirements.txt
python init_db.py        # builds actbayan.db from "actbayan (1).sql" + seeds LGU/Admin
python app.py            # open http://127.0.0.1:5000
```

Default logins (passwords set by init_db.py):
- LGU: `lgu@actbayan.gov.ph` / `lgu12345`
- Admin: `admin@actbayan.gov.ph` / `admin12345`

## What changed
- **Database:** `app.py` now uses `db.py` (sqlite3 shim) + `actbayan.db`.
  All existing `%s` / `cursor(dictionary=True)` code keeps working; the shim
  rewrites `CONCAT`, `DATE_FORMAT`, `NOW()`, `DATE_ADD`, `FOREIGN_KEY_CHECKS`.
  Rebuild anytime with `python init_db.py`.
- **Analytics AI (`api_analytics_summary`):** no Ollama. Offline website-model
  `analytics_templates.py` holds **5 AI dictionary versions** per summary type
  (executive/monthly/sample/status/location/category). Slots are filled from
  live counts; Regenerate cycles v1..v5. Same JSON shape, so
  `templates/lgu_analytics.html` is untouched.
- **Duplicate-block AI:** copied from `old_app.py` — `openrouter_chat()` +
  `llm_judge_duplicates()` using:
  `OPENROUTER_MODEL=apodex/apodex-1.1-mini:free`.
  `/api/check_duplicate` pre-filters to top-12, scores with ONE OpenRouter call,
  falls back to offline word-overlap when offline. Set your key via
  `$env:OPENROUTER_API_KEY="sk-or-v1-..."` (default baked-in key still works).
- `requirements.txt`: dropped `mysql-connector-python` + `ollama`.
- Backups: `app_mysql_backup.py` (original), `old_app.py` (OpenRouter source).
