# AGENTS.md

- Python 3.11 venv via uv: `uv venv --python 3.11 .venv && uv pip install --python .venv -r requirements.txt`
- Run: `.venv/bin/uvicorn app.main:app --port 8799` (port 8765 is taken on this machine)
- Tests: `.venv/bin/python -m unittest discover -s tests -v`
- JS syntax check: `node --check app/static/app.js && node --check app/static/studio.js && node --check app/static/charts.js`
- Without Reddit creds in `.env` the app runs in demo mode (`app/demo.py`); delete `data/radar.db` to reseed.
- Frontend is plain JS served from `app/static` (no build). Deep links: `#/radar?post=<id>`, `#/queue?post=<id>&studio=1`.
- Product rules from the user: no auto-posting (user copies text/images manually), never fetch comment threads
  (hotness = comment count vs subreddit baseline), only fresh posts, filter junk/ads before tracking.
