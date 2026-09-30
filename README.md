# Recrute

Human-in-the-loop job discovery and application system. See [PLAN.md](PLAN.md) for the design.

## Setup (Linux or Windows)

1. Install [uv](https://docs.astral.sh/uv/getting-started/installation/).
2. Install and log into the LLM CLIs you want to use (subscriptions, no API keys):
   [Claude Code](https://docs.claude.com/en/docs/claude-code) (`claude`) and/or
   [Codex](https://github.com/openai/codex) (`codex`).
3. Install Google Chrome (recommended). Or use the bundled Chromium:
   `uv run patchright install chromium`.
4. Then:

```sh
uv sync
uv run recrute init            # creates recrute.toml, data/, resources/, database
uv run recrute doctor          # checks everything
uv run recrute llm test        # round-trips a prompt through claude and codex
uv run recrute browser login   # log into LinkedIn/Google/etc. once in the dedicated profile
uv run recrute browser probe   # shows what bot-detection scripts can see
uv run recrute serve           # web UI at http://127.0.0.1:8765
```

The volume knob can be set from the UI or with `uv run recrute set apps_per_day 25`.

## Your files

- `resources/`: your mega resume, cover-letter samples, and answer bank (see
  `resources/README.md`). This folder is gitignored.
- `data/`: database, browser profile (with your logged-in sessions), and receipts. This folder is
  gitignored. **Treat it as sensitive.**

## Development

```sh
uv run pytest
uv run ruff check .
uv run python tools/audit.py   # independent code audit by Codex (see tools/audit.py)
```
