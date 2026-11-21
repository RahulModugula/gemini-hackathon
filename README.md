# Drawing claim checker (Gemini 4 hackathon)

Public data only: the Outagamie County YMCA childcare bid set. Plan: [PLAN.md](PLAN.md).

## Setup
```bash
python3.12 -m venv .venv && .venv/bin/pip install -r requirements.lock
./fetch_data.sh                      # downloads 5 public PDFs to data/, verifies SHA256SUMS
.venv/bin/python ingest.py           # build/: words + boxes, tiles, sheets.json (76 sheets, ~1 min)
.venv/bin/python analyze_sheets.py   # build/sheet_index.json: sheet numbers + supersession preview
.venv/bin/python -m pytest -q        # ingest tests
GEMINI_API_KEY=... .venv/bin/python models_check.py   # which Gemini 4 models answer (key never printed)
```
`GOOGLE_API_KEY` overrides `GEMINI_API_KEY` inside the SDK; our code passes the key explicitly, but unset
`GOOGLE_API_KEY` to avoid surprises.

## Files
| File | What |
|---|---|
| `ingest.py` | Drawing-size pages from base set + addenda → words (rotation-corrected pt boxes), 1024 px tiles, `sheets.json` |
| `analyze_sheets.py` | Title-block sheet numbers, supersession by addendum order (ticket 5 seed) |
| `models_check.py` | Pings the four Gemini 4 models |
| `CONTRACT.md` | JSON shapes every later piece plugs into |
| `TICKETS.md` | Tickets with acceptance criteria, dependency graph, long-horizon agent ladder |
| `RESEARCH.md` | Sourced findings: model facts, SDK gotchas, prior art, eval method, our measurements |
