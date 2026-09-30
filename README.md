# Recrute

Human-in-the-loop job discovery and application system. It finds jobs that match your criteria
across ATS job boards, aggregators, and LinkedIn, then ranks them. For the ones you approve, it
builds a tailored resume and answers, and submits them after you say "go ahead". See
[PLAN.md](PLAN.md) for the full design.

```
discover → dedup → rules → LLM triage → 🧑 CP1 review → packet (resume + answers) → 🧑 CP2 go-ahead
→ drip-scheduled submission (🧑 CP3 only if something unexpected) → inbox tracking → analytics
```

## Setup (Linux or Windows)

1. Install [uv](https://docs.astral.sh/uv/getting-started/installation/).
2. Install the LLM CLIs and log in with your **subscriptions**. No API keys are used; API-key
   environment variables are stripped from their environment.
   - [Claude Code](https://docs.claude.com/en/docs/claude-code): `claude auth login`
   - [Codex](https://github.com/openai/codex): `codex login`
3. Install Google Chrome (recommended). Or use the bundled Chromium:
   `uv run patchright install chromium`.
4. Then:

```sh
uv sync
uv run recrute init            # creates recrute.toml, data/, resources/, database
uv run recrute doctor          # checks CLIs (and subscription auth), browser, resources
uv run recrute llm test        # round-trips a prompt through claude and codex
uv run recrute browser login   # log into LinkedIn/Google/etc. once in the dedicated profile
uv run recrute browser probe   # shows what bot-detection scripts can see
uv run recrute serve --worker  # web UI at http://127.0.0.1:8765 + background worker
```

## Your inputs (`resources/`, gitignored)

| File | Purpose |
|---|---|
| `resources/resume/*` | Your **mega resume**: everything you've done (md/docx/pdf/txt). `recrute profile ingest` structures it into `data/profile.yaml` for you to review. |
| `resources/answers.yaml` | Answer bank for application forms (copy `answers.example.yaml`). |
| `resources/criteria.yaml` | Tracks, filters, and thresholds (copy `criteria.example.yaml`). |
| `resources/cover_letters/`, `resources/writing/` | Voice/style samples. |

The system never invents facts. Every claim it writes comes from these files, and a verifier
flags anything it can't trace back to them.

## Daily use

- **Review** (`/queue`): approve or reject jobs with `j/k/a/r/s/m`.
- **Packets** (`/packets`): check the focused resume, cover letter, and every form answer, then
  press **Go ahead**, edit first, or regenerate with a note.
- **Applications** (`/applications`): the drip scheduler submits approved packets spread across
  your active hours. Anything unexpected (a new required field, a CAPTCHA, account creation) ends
  up here as *needs you*.
- **Volume knob**: set it in the UI, or with `uv run recrute set apps_per_day 25`. Per-site caps
  (e.g. LinkedIn Easy Apply) are never raised by the global knob.

## Command reference

| Command | What it does |
|---|---|
| `recrute serve [--worker]` / `recrute worker` | Web UI and/or the background worker |
| `recrute run <task>` | Run one worker task now (`discover_boards`, `discover_search`, `filter`, `score`, `packets`, `apply`, `inbox`, …) |
| `recrute profile ingest` / `accept [--force]` | Mega resume → proposed profile → your approval |
| `recrute company add <url>` | Add a company board (Greenhouse/Lever/Ashby/Workable/SmartRecruiters URL) |
| `recrute badges import-h1b <csv…>` / `import-everify <csv>` | Visa badge data. Informational only; never used to filter or rank |
| `recrute inbox set-password <user>` | IMAP app password → OS keyring (then enable IMAP in Settings) |
| `recrute notify set-secret telegram\|smtp\|ntfy` / `notify test` | Notification secrets (OS keyring) and a test message |
| `recrute set <key> <value>` / `settings` | Runtime settings (e.g. `apps_per_day`, `site_caps`, `company_cap`) |
| `recrute token` | Access token for LAN login and the browser extension |

## Browser extension ("Save to Recrute")

Load `extension/` unpacked in Chrome (`chrome://extensions`, then Developer mode). In its
options, set the server URL and your `recrute token`. Then the toolbar button saves any job page
you're viewing into Recrute (read-only; no automation on the site).

## Safety model (short version)

- **Nothing is submitted without your CP2 "go ahead" on that exact packet revision.** Blocking
  truthfulness flags need an explicit acknowledgement. Approved files are fingerprinted and
  verified before upload.
- **Live forms are re-read before submitting.** Any question that isn't exactly what you
  approved (changed wording, numbers, description), any unapproved pre-filled value, a CAPTCHA,
  or a login wall hands the form to you (CP3), with a notification.
- **The first 5 submissions per site are fill-and-pause** (trial period). Daily, per-site and
  per-company caps apply, and submissions only happen during your active hours.
- **Any security check on LinkedIn pauses both browsing and Easy Apply** until you resume them.

## Running on a LAN server (e.g. an old Windows laptop)

1. Install uv, Chrome, and the two CLIs, and log into both. Clone the repo, then run
   `uv sync` and `uv run recrute init`.
2. Copy over `resources/` and your `recrute.toml`. Set `[server] host = "0.0.0.0"`.
3. Log into sites again in the browser profile (`recrute browser login`). Browser cookies are
   encrypted per machine, so they can't be copied.
4. Run `uv run recrute serve --worker`. From other devices, open `http://<laptop>:8765` and log
   in with the token printed by `uv run recrute token`.

Access control: requests from the machine itself need no login. LAN clients need the token.
Every state-changing request is CSRF-protected.

## Development

```sh
uv run pytest                  # unit + browser tests (live network tests: RECRUTE_LIVE=1)
uv run ruff check .
uv run python tools/audit.py --changed   # independent audit by Codex gpt-6.1-sol
```
