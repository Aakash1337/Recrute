# Recrute — Human-in-the-Loop Job Discovery & Application System

> Status: **v1 built** (M0–M7). See §8 for what's verified and what still needs real-world
> supervised runs. Everything here is still open to change.

## 1. Goal

Find jobs that match your criteria across as many sources as possible, rank them, generate tailored
materials, and fill in applications, with **you approving each important step**. The system does
the repetitive work (searching, deduplicating, form filling). You make the decisions (which jobs,
what gets said about you, when to submit).

### Design principles
1. **Quality over volume.** Sending a lot of generic applications gets ignored, and some ATS
   systems flag it. The system should improve the hit rate, not just the count.
2. **Never fabricate.** Every claim in a tailored resume, cover letter, or form answer must trace
   back to your master profile. If the system can't back a claim up, it asks you.
3. **Human checkpoints are the default.** Automation is something you turn on per source or ATS
   once it has earned trust.
4. **Prefer ToS-safe sources.** Use official APIs and public ATS endpoints first. Scraping sites
   that forbid it (LinkedIn, Indeed) risks bans, so treat it as optional and isolated.
5. **Local-first.** Your resume, answers, credentials, and browser sessions stay on your machine.

---

## 2. End-to-End Flow

```
 ┌────────────┐   ┌─────────────┐   ┌──────────────┐   ┌───────────────┐
 │ 1. Profile │──▶│ 2. Discover │──▶│ 3. Normalize │──▶│ 4. Filter &   │
 │ & Criteria │   │  (sources)  │   │   & Dedup    │   │    Score      │
 └────────────┘   └─────────────┘   └──────────────┘   └──────┬────────┘
                                                              ▼
 ┌────────────┐   ┌─────────────┐   ┌──────────────┐   ┌───────────────┐
 │ 8. Track & │◀──│ 7. Submit   │◀──│ 6. Fill Form │◀──│ 5. Tailor     │
 │   Follow-up│   │  🧑 CP3     │   │  (adapters)  │   │  Materials 🧑 │
 └────────────┘   └─────────────┘   └──────────────┘   │  CP2          │
                                                       └──────▲────────┘
                                          🧑 CP1: Review queue ┘
```

**Human checkpoints (CP):**
| CP  | What you do                                          | Can be automated later?                    |
|-----|------------------------------------------------------|--------------------------------------------|
| CP1 | Approve, reject, or snooze jobs from the ranked list | Partly: auto-approve above a score threshold |
| CP2 | Review the **application packet** (tailored resume, cover letter, and every form answer) and say "go ahead" | Partly: auto-approve if there are no new claims |
| CP3 | Only happens if submission hits something not in the approved packet (new field, CAPTCHA, account creation) | No, these always come back to you |

CP2 is the main approval. When you say "go ahead", the system submits on its own. It can do this
safely because the form's questions are fetched and answered *before* you review, so you are
approving exactly what gets sent.

---

## 3. Components

### 3.1 Profile & Criteria (source of truth)
- **Mega resume / master profile** (source files in `resources/resume/`, structured into `data/profile.yaml`): everything you've ever done, with no length
  limit. That covers every role, project, bullet, metric, skill, cert, talk, and side project. Each
  item can carry extra material that never appears on a resume as-is:
  - `tags` (e.g. `backend`, `leadership`, `ml`) and the tech used
  - `metrics` and `context`: the backstory behind a bullet, which the tailoring step can draw on to
    reword it truthfully
  - `strength`: how proud of or confident about this item you are, which acts as a tiebreaker when
    choosing
  - Import: if you give it an existing resume, LinkedIn export, or doc, an LLM turns it into this
    structure, and you check and fill in the gaps.
  Every tailored resume, cover letter, and answer is built from this file only.
- **Answer bank**: reusable answers to common screening questions: work authorization, sponsorship,
  salary expectations, notice period, relocation, years of experience with X, "why this company"
  templates. EEO/demographic answers are set explicitly by you. The default is "decline to answer".
- **Search criteria** (`criteria.yaml`; you can have several named profiles, e.g. "backend-remote",
  "ml-onsite-berlin"):
  - Titles and keywords (include/exclude), seniority, locations and remote policy, time zones
  - Minimum salary, company size and stage, industries
  - Hard excludes: specific companies, clearance or citizenship required, etc.
  - Weights for soft preferences, which feed the score
- **Target (v1): USA; roles in three priority tiers**
  | Priority | Track | Example titles |
  |---|---|---|
  | **P0 (bonus)** | Where security and AI overlap | AI security engineer, AI red teamer, adversarial ML, LLM security, ML for threat detection |
  | **P1** | Cybersecurity | SOC analyst, security analyst/engineer, incident response, detection engineering, threat intel, pentester/offensive security, AppSec, cloud security, vulnerability management, GRC, IAM |
  | **P2** | AI / ML | ML engineer, AI engineer, LLM/GenAI engineer, applied scientist, MLOps, data scientist (ML-heavy) |
  | **P3** | Related fields | Data analyst, BI analyst, data engineer, IT/sysadmin, network engineer, cloud engineer, software engineer |
  - Priority changes both the **ranking** and the **bar a job has to clear**. Minimum fit score
    by tier: P0/P1 ≥ 55, P2 ≥ 65, P3 ≥ 75. This means related-field jobs appear only when they fit
    you well, so they don't crowd out security roles.
  - **Hard filters.** Your personal values live in the gitignored `resources/criteria.yaml`;
    `resources/criteria.example.yaml` is the template. The defaults are:
    - Full-time only. No internships, contract-only, or part-time.
    - Seniority: entry-level through mid-level. Drop senior/staff/principal/lead/manager titles,
      and postings that require more than about 5 years.
    - Location: anywhere in the US, including remote.
    - Salary: no floor.
    - **Eligibility (not visa-related)**: toggles that drop jobs that are legally closed to you
      (based on your settings):
      - Clearance required or "must be able to obtain" clearance
      - US citizenship required (this rules out most USAJobs postings)
      - "US person" / ITAR / export control
      These can be turned off. Dropped jobs stay visible in a "filtered out" view.
  - **Visa notes (information only; never filtered or ranked on)**: you judge sponsorship
    yourself during review. Each job gets a small set of badges so these jobs are easy to pick
    out:
    - 🛂 **Sponsorship language**: "will sponsor", "no sponsorship", or not mentioned, quoted
      from the posting
    - 📊 **H-1B history**: the company's recent H-1B approval count, from the USCIS H-1B
      Employer Data Hub
    - ✅ **E-Verify**: enrolled / not found / unknown (relevant for STEM OPT)
    - 🎓 **Cap-exempt**: shown for universities and nonprofit research organizations
    You can filter or sort by these badges in the UI if you choose to. The system never does
    it on its own.
  - Answer-bank rule: work-authorization and sponsorship questions are always answered
    truthfully, using the answers you set.
  - Other fields specific to the US:
    - Veteran status and disability forms (EEO): answers are set once by you.
    - Salary ranges: postings in CA, NY, CO, WA and other states have to list them, so the
      salary floor filter will often have data to work with.
    - Certs that act as gates: Security+, CySA+, CISSP, OSCP, etc. (DoD 8140 roles require
      certain certs). These get matched against the certs in your profile.
- **Resume templates**: Typst or HTML-to-PDF templates, so a tailored resume is a re-render rather
  than an edit to a Word file.

### 3.2 Discovery: source connectors
A plugin interface is `fetch(criteria) -> [RawJob]`, and each source is one module. Sources are
grouped by reliability and ToS risk:

**Tier 1: public ATS job-board APIs** (structured, stable, ToS-friendly, and applyable)
| ATS             | Endpoint style                                          |
|-----------------|---------------------------------------------------------|
| Greenhouse      | `boards-api.greenhouse.io/v1/boards/{company}/jobs`     |
| Lever           | `api.lever.co/v0/postings/{company}?mode=json`          |
| Ashby           | `api.ashbyhq.com/posting-api/job-board/{company}`       |
| Workable        | public widget/JSON endpoints                            |
| SmartRecruiters | public postings API                                     |
| Recruitee, Personio, Teamtailor | public career-site feeds                |
| Workday         | per-tenant `/wday/cxs/...` JSON (discovery only; applying is hard) |

These APIs are per company, so we keep a **company registry**: a seed list plus automatic
expansion. New companies get discovered from aggregator results, HN threads, and search queries
like `site:boards.greenhouse.io "backend engineer"`.

**Tier 2: aggregators and free APIs**
- Adzuna API, USAJobs API, Remotive, RemoteOK, Himalayas, We Work Remotely (RSS), Arbeitnow
- HN "Who's Hiring" monthly thread (via the Algolia API, with an LLM extracting the postings)
- Optional paid option: JSearch (RapidAPI), which wraps Google for Jobs and has broad coverage
- **US and field-specific sources (v1)**:
  - ~~USAJobs, ClearanceJobs~~: dropped, since almost all of these need citizenship or clearance
  - Dice: US tech jobs
  - infosec-jobs.com, CyberSecJobs, isecjobs: security job boards
  - ai-jobs.net: AI/ML job boards
  - Built In, Wellfound (startups), Handshake (if you're a student or new grad)
  - Seed the company registry with security vendors (e.g. CrowdStrike, Palo Alto Networks,
    SentinelOne, Wiz), AI labs and AI-first companies, and big tech security teams

**Tier 3: walled gardens (LinkedIn, Indeed, Glassdoor, Wellfound)**

These sites forbid automated access, even when you're logged in. They look for automation and can
restrict or ban an account. Since the account is your real one, a ban costs you your network and
Easy Apply history, so the modes below are listed from lowest to highest risk:

1. **Passive capture (no risk):** the browser extension reads job pages as *you* browse them and
   saves them to Recrute. Alert emails are parsed through Gmail.
2. **Logged-out search (low account risk):** LinkedIn's public job search works without logging in.
   The worst case here is IP rate-limiting, not your account getting flagged. The data is less
   complete (no applicant counts, no "how you match").
3. **Session browsing (chosen; account risk):** The browser runtime connects to *your own* running
   Chrome/Firefox (CDP or a persistent profile). It uses your real session and fingerprint, runs
   headed, and never stores your password. It is read-only: search results and job details only.
   Guardrails:
   - Daily budget (e.g. ≤ 10 searches and ≤ 80 job views per day), with jittered delays of
     8–30s and scrolling that looks like a person reading
   - Runs only during your normal waking hours, as a few short sessions rather than one long run
   - Keeps a cache of seen job IDs, so no job page is opened twice
   - **Kill switch:** on any CAPTCHA, "unusual activity" notice, verification checkpoint, or
     unexpected logout, it stops at once, notifies you, and backs off for days
   - Never messages anyone, sends connection requests, or clicks Easy Apply (applying is a separate
     decision; see 3.7)
   - Where possible, extracts data from the page's own JSON/DOM, and sends found jobs to their
     Tier 1 ATS page, applying there instead of on LinkedIn

Modes 1–2 always run. Mode 3 is a per-site toggle.

Scheduling: each source runs on its own cadence (e.g. ATS boards every 6h, aggregators hourly) and
stores `last_seen` so the system can spot new, reposted, and closed jobs.

### 3.3 Normalize & Dedup
- Map every source to a common `Job` schema: title, company, location(s), remote type, salary
  range, description (both markdown and raw), apply URL, ATS type, posted date, and source(s).
- Resolve the canonical apply URL. For example, an Adzuna listing may point to a Greenhouse posting,
  and we want the Greenhouse one because we have an adapter for it.
- Dedup on the canonical URL first, then fall back to a fuzzy hash of company, normalized title,
  and location. When the same job appears in several places, merge the sources.
- Enrich company info: size, funding, Glassdoor-style signals if available, and whether you've
  applied there before.

### 3.4 Filter & Score
1. **Hard filters** (rule-based, no cost): location, excluded companies and keywords, seniority,
   salary floor when a salary is listed, and "already applied to this company in the last N days".
2. **LLM triage**, batched, through whichever subscription CLI is routed to triage. It returns a 0–100 fit score, a one-line reason,
   requirements you meet and don't meet, red flags (e.g. "unpaid trial", "10+ yrs required"), and
   the extracted salary and visa info.
3. **Optional deeper pass** with a stronger model, only for the top N jobs.
4. **Learning loop**: when you reject jobs at CP1, you pick a reason (e.g. "too senior", "hate the
   industry"). The system suggests updates to your criteria and weights. It shows you the change;
   it never silently edits criteria.

### 3.5 Review Queue (🧑 CP1)
- A web UI with a ranked list, filters, and one-key triage (approve / reject / snooze / "apply
  manually myself").
- Each job gets a detail pane with the score rationale, which requirements match, the full JD, and
  company info.
- Batch view: "Here are today's 15 new jobs above 70".

### 3.6 Application Packet (🧑 CP2, the main approval)
For each approved job, the system builds a packet in the background and puts it in the approval
queue. The packet contains:
- **Focused resume**: the relevant subset of the mega resume, usually 1 page (2 for senior roles).
  - Choosing content: rank items against what the JD asks for, using tags, keyword overlap, and
    LLM judgment. Keep the best-matching roles and bullets, cut the rest, and order skills by
    relevance.
  - Rewording: bullets can be rephrased to use the JD's language (e.g. "stream processing" →
    "real-time data pipelines"), but only when that's accurate given the item's `context`.
  - ATS-friendly output: one column, real text (no images), standard section headings. It checks
    that the PDF text extracts cleanly.
- **Cover letter** (if needed)
- **Every form answer**: the form's questions are fetched in advance (Greenhouse's API returns
  them; for other ATSs the adapter reads the form without filling it). Each question is answered
  from the answer bank, the profile, or an LLM draft that's marked as new.
- **Approval screen**:
  - The job summary and fit score next to the focused resume, shown as a PDF preview and as a
    "what was kept/cut/reworded compared with the mega resume" view
  - All answers, with new ones highlighted
  - Actions: **Go ahead** / edit then go ahead / regenerate with a note (e.g. "emphasize the Kafka
    work") / skip job

Details:
- **Truthfulness guard**: a second LLM pass checks every generated sentence against the profile and
  flags anything it can't support. Flagged items get highlighted in the UI.
- **Cover letter** (optional, per job or per ATS): short, specific, and in your voice, based on
  writing samples you provide.
- **Custom questions**: pre-draft answers from the answer bank, with an LLM filling the gaps. Every
  new answer is saved back to the bank once you approve it.
- **UI**: shows a diff against the master resume and a PDF preview, and lets you edit inline.

### 3.7 Application Execution
- **Browser runtime (shared by discovery and applying)**: patchright drives a **dedicated Chrome
  profile** that you log into once (LinkedIn, Google, any ATS accounts). It uses your real
  sessions, cookies, and fingerprint, in a visible window, without taking over the browser you're
  working in. Its actions are paced like a person's: typing character by character with varied
  delays, scrolling before clicking, uploading files through the file picker, and short pauses
  between steps. This helps with bot-detection scores (e.g. reCAPTCHA v3 on some Greenhouse
  forms).
- **Login needs by ATS**: Greenhouse, Lever, and Ashby forms need no account. Workday, iCIMS,
  Taleo, and SuccessFactors need an account per company. Account creation always goes to you at
  first. Later the system can create the account with credentials stored in your keyring or
  password manager, but only if you choose that.
- **Two ways of driving the form**:
  - *Scripted adapters* (known ATSs): fixed steps and selectors per ATS. They are fast, cheap, and
    predictable, and they're the default.
  - *LLM browser agent* (unknown forms): reads the page's accessibility tree, decides which field
    is which, then types, selects, and uploads. It's flexible but slower and more error-prone. It
    may only use values from the approved packet, and it always ends in fill-and-pause.
- **ATS adapters**, one per ATS (Greenhouse, Lever, Ashby, Workable,
  SmartRecruiters…). Each adapter knows that ATS's standard fields, uploads, and custom-question
  widgets.
- **Generic LLM form filler** for unknown forms. It reads the accessibility tree, maps fields to the
  profile and answer bank, and always gets marked low-confidence.
- **Auto-submit after "go ahead" (the default)**: the adapter fills the form using *only* the
  approved packet and submits. Before submitting, it checks that every required field on the live
  form was covered by the packet. If a field was not covered, it doesn't guess: it goes to CP3.
- **CP3 fallback (fill-and-pause)**: a visible browser fills in everything it can and stops. You
  get a notification, finish the form, and submit. Unknown forms (the generic LLM filler) always
  go this way until that ATS has a proper adapter.
- **Submission drip scheduler**: approving a batch doesn't submit the batch at once. Approved
  packets go into a queue and are sent one at a time over the next hours:
  - Random gaps between submissions, only during normal waking hours
  - A daily cap per site (e.g. LinkedIn Easy Apply ≤ 15 per day)
  - High-fit jobs and freshly posted jobs go to the front, since applying early matters
- **Trial period for new adapters**: each new adapter's first N submissions use fill-and-pause even
  after "go ahead", so you can watch it work before trusting it.
- **Hard stops**: CAPTCHA, account creation, assessments, and unknown required fields all hand
  control back to you. The system never tries to solve CAPTCHAs.
- **Receipts**: for each submission it saves a screenshot, the form HTML, the resume version, and
  the answers used.
- **Guardrails**: daily cap, per-company cap, a cooldown, and never applying twice to the same job
  ID.
- Workday and other sites that require an account per company stay on "manual with assist": the
  system opens the page and puts your answers on the clipboard, and you do the rest.

### 3.8 Tracking & Follow-up
- Status pipeline: `discovered → shortlisted → materials_ready → applied → acknowledged →
  interviewing → offer | rejected | ghosted`
- **Gmail ingestion**: classifies incoming mail (application confirmation, rejection, interview
  request, assessment) and updates the status automatically. You confirm the ambiguous cases.
- Follow-up reminders (e.g. no response after 14 days), with optional drafted follow-up emails for
  jobs where you have a contact.
- Calendar hookup for interviews (optional).
- **Analytics**: response rate by source, score band, resume variant, and ATS. This is how we tune
  scoring and tailoring.

### 3.9 Notifications
- A daily digest ("23 new matches, 6 above 80, 3 awaiting submit"), sent by email, Telegram, or
  ntfy.
- Instant alerts for very-high-fit jobs, since applying early matters.

---

## 4. Tech Stack & Deployment

### Deployment
- **Phase 1 (testing and the first few days)**: runs on your main Linux machine.
- **Phase 2**: moves to an **old Windows laptop on your home network**, which acts as an always-on
  server. You reach the UI from any device on the LAN at `http://<laptop>:<port>`.
- **Must run on both Linux and Windows**:
  - `uv` manages Python and dependencies identically on both
  - All paths go through `pathlib`
  - CLIs are resolved with `shutil.which` (this handles Windows `.exe`/`.cmd` shims)
  - Prompts go over stdin (Windows has a command-line length limit)
  - Moving to the laptop: install uv, Chrome, and the CLIs; clone the repo; run `uv sync`; then
    copy `resources/` and `recrute.toml` across. Log into sites again in the browser profile there,
    rather than copying `data/browser-profile`, since cookies are encrypted per machine/OS.
- **Remote browser view** (the **Live browser** page): in Phase 2 the automated browser runs on
  the laptop. When a form goes to CP3, or you open it to log into a site, the UI streams that
  browser's page (about one frame per second) and replays your clicks, typing and keys, so you
  can finish from your main machine without sitting at the laptop. RDP is a fallback. What you
  type there (passwords, verification codes) and the screenshots (which can show it) are kept in
  memory only and never written to disk, so the live view needs the worker in the web server's
  process (`recrute serve --worker`). Input is bound to the exact tab shown: a popup or a
  navigation since the picture you acted on drops it.

### Stack
Decided on **Python**. It was switched from Go after you made "fewest problems, and best at
browsing, data collection, and avoiding detection" the deciding criteria. On those, Python has the
strongest ecosystem.

| Layer          | Choice                                              | Why                                    |
|----------------|-----------------------------------------------------|----------------------------------------|
| Language       | **Python 3.13**, managed by **uv**                  | Best browser-automation and anti-detection ecosystem; uv makes Windows setup a single command |
| Browser        | **patchright**: drop-in Playwright fork that patches automation leaks (CDP `Runtime.enable`, `navigator.webdriver`, etc.) | Official Playwright API plus stealth. Probe on this machine: 0 failed detection checks |
| HTTP fetching  | httpx; later **curl_cffi** for sources that fingerprint TLS (impersonates Chrome's TLS/HTTP2) | Avoids bot flags on plain API/HTML fetches |
| DB             | SQLite (SQLModel/SQLAlchemy), WAL mode              | Zero-ops; the UI and workers share it   |
| Scheduler      | In-process, with a jobs table in the DB (M1)        | Survives restarts; nothing extra to install |
| LLM            | **Subscription CLIs, not APIs**: Claude Code (`claude -p`) and Codex (`codex exec`), called as subprocesses | Uses your existing subscriptions; no per-token billing |
| Frontend       | FastAPI + Jinja + HTMX (vendored, no CDN)           | No Node build step                     |
| Docs/PDF       | Typst → PDF (M3)                                    | Deterministic, versionable resumes; cross-platform |
| Extension      | Chrome MV3 "Save to Recrute" (M6)                   | Passive capture                        |
| Secrets        | OS keyring (`keyring`: Windows Credential Manager / Secret Service) | Credentials never go in the DB or files |

### Independent auditor
- `tools/audit.py` runs **Codex on `gpt-6.1-sol`** (on your subscription, in a read-only sandbox)
  as a second pair of eyes on everything Claude builds.
- It reviews against this plan for:
  - Correctness
  - Account and data safety
  - Plan conformance (checkpoints, no fabrication, visa rules, caps)
  - Windows compatibility
  - Robustness and test gaps
- Findings are written to `data/audits/` as JSON and markdown. Claude fixes them and re-audits.
  The audit runs after every milestone, or on `--changed` files during work.
- The auditor is told not to read `resources/` or `data/` (your personal data). The prompt is in
  `tools/audit_prompt.md`.

### LLM layer (subscriptions)
- **Interface**: `Provider.Complete(task, prompt, schema) -> JSON`. There are two implementations,
  `ClaudeCLI` and `CodexCLI`, which run headless with JSON output and a schema.
- **Routing per task, set in config**: e.g. triage → Codex, tailoring and truthfulness check →
  Claude. Swap them freely. An API provider can be added later if ever needed.
- **Usage limits are the constraint here, not cost.** Subscriptions have rolling usage windows,
  and you also use them for other work. The system handles this by:
  - Doing cheap work first: rule-based filters and keyword pre-scoring cut the volume before any
    LLM call
  - Scoring 10–20 jobs per call instead of one at a time
  - Caching results by JD hash
  - Detecting the rate-limit message, then pausing and resuming that queue later, or failing over
    to the other provider
  - Keeping a usage meter in the UI (LLM calls per provider). A "leave me X% of my window"
    reserve isn't possible: the subscription CLIs don't report remaining usage. Rate-limit
    responses trigger failover to the other provider and a cooldown instead.
- **Browser agent for unknown forms**: Claude Code or Codex runs headless with a browser MCP
  (e.g. chrome-devtools MCP) attached to the same Chrome profile, and is limited to the approved
  packet's values.
- **Both machines** need both CLIs installed and logged in, on Linux now and on the Windows laptop
  later.

### Volume knob
- A single **applications/day** setting (1–200), which you can change at any time from the UI. It
  takes effect immediately on the drip scheduler.
- **Per-site caps sit on top of it** and are not raised by the global knob (e.g. LinkedIn Easy
  Apply ≤ 15/day unless you raise that one separately, with a warning).
- At high settings, the choke point becomes your review time, not the system. The optional
  auto-approval rules in M7 (e.g. auto-approve packets with no new claims for P1 jobs with a
  score of 85 or more) are what make 50–100/day realistic.

### `resources/` folder (you provide; gitignored)
```
resources/
  resume/        mega resume in any format (md, docx, pdf); becomes structured data/profile.yaml
  cover_letters/ past cover letters, used as voice/style samples
  writing/       optional: other samples of your writing
  answers.yaml   answer bank (created with a template for you to fill in)
```
Anything dropped here gets re-ingested. The structured profile is regenerated and shown to you
as a diff to approve.

### Data model (initial)
`Profile`, `CriteriaSet`, `Company` (with ATS type and board token), `Source`, `Job`,
`JobSource` (M:N), `JobScore`, `Decision` (CP1 plus reason), `Document` (resume or cover letter
version), `AnswerBankEntry`, `Application` (with status and receipts), `StatusEvent`, `EmailEvent`,
`LLMCall` (provider, task, tokens/latency, cache key), `Setting` (volume knob, caps).

### Repo layout
```
src/recrute/
  cli.py            recrute init | doctor | serve | settings | set | browser … | llm …
  paths.py config.py db.py models.py settings.py doctor.py
  llm/              base, claude_cli, codex_cli, router (fallback, cache, cooldown)
  browser/          runtime (patchright persistent profile), probe (detection check)
  web/              FastAPI app, templates, static (vendored htmx)
  sources/          (M1) greenhouse, lever, ashby, adzuna, hn, linkedin, email_alerts, ...
  pipeline/         (M1–M2) normalize, dedup, filter, score
  tailor/           (M3) resume, coverletter, answers, verify
  apply/            (M4) drip scheduler, adapters/{greenhouse,lever,ashby,linkedin}, agent
  track/            (M5) gmail, reminders, analytics
tools/              audit.py + audit_prompt.md (independent auditor)
tests/
templates/          (M3) Typst resume/cover-letter templates
extension/          (M6) browser extension
resources/          your inputs (gitignored)
data/               db, profile.yaml, receipts, browser profile, audits (gitignored)
```

---

## 5. Milestones

| #  | Milestone                     | Deliverable                                                              |
|----|-------------------------------|--------------------------------------------------------------------------|
| M0 | Foundation ✅             | uv project, SQLite + additive auto-migration, config, runtime settings (volume knob), CLI, isolated subscription-CLI LLM router (Claude + Codex), patchright runtime + detection probe, auditor |
| M1 | Discovery ✅               | 12 sources (Greenhouse/Lever/Ashby/Workable/SmartRecruiters boards, Remotive, RemoteOK, Himalayas, Adzuna, HN, LinkedIn guest + budgeted logged-in session), 120 verified seed companies, dedup/ingest |
| M2 | Scoring + Review (CP1) ✅  | Rules (tracks, seniority, years, location, eligibility), batched LLM triage, keyboard review queue, filtered view, visa badges |
| M3 | Packets (CP2) ✅           | Mega-resume ingest → reviewed proposal, focused resume (Typst PDF), truthfulness verifier, answer bank, cover letters, versioned packets, approval screen |
| M4 | Apply ✅                   | Greenhouse/Lever/Ashby/LinkedIn Easy Apply adapters, human-like input, drip scheduler, caps, trial period, receipts, CP3 hand-off |
| M5 | Tracking ✅                | Read-only IMAP sync, email classification + matching, reminders, ntfy/Telegram/email notifications, digest |
| M6 | Breadth ✅                 | Generic LLM form filler (always CP3), alert-email parsing, "Save to Recrute" extension, company registry expansion |
| M7 | Trust & automation ✅      | Opt-in auto-approval (verified packets only), analytics, criteria learning-loop suggestions |

M1 and M2 already make the system useful on their own, as a smart job feed, before any applying is
automated.

---

## 6. Risks & Mitigations
| Risk                                             | Mitigation                                                   |
|--------------------------------------------------|--------------------------------------------------------------|
| Account bans (LinkedIn/Indeed)                   | Session browsing is read-only, uses your real browser, has daily caps and a kill switch on any checkpoint; logged-out search and passive capture as fallback |
| ATS form changes break adapters                  | Adapter smoke tests against live sample postings; fall back to the generic filler plus manual |
| LLM invents experience                           | Truthfulness verifier plus CP2; profile-grounded generation only |
| Low-quality mass applying hurts your reputation  | Score threshold, per-company caps, daily caps                |
| PII leakage                                      | Local-first, secrets in the keyring, receipts dir gitignored, minimal data sent to the LLM |
| Hitting subscription usage limits                | Rules before LLM calls, batched scoring, caching by JD hash, pause/resume queues, failover between Claude and Codex, reserve setting |
| Double-applying / applying to closed jobs        | Dedup by canonical URL and ATS job ID; re-check the posting is live before filling |

---

## 7. Open Questions (for you)
1. ~~**Target market**~~: USA. Priority order is cyber, then AI, then related fields (see 3.1).
   Filters are decided (entry to mid-level, full-time, anywhere in the US), with personal values
   in `resources/criteria.yaml`. Visa info is shown as badges only, never used to filter or
   rank. Volume stays moderate, favoring quality.
2. ~~**LinkedIn/Indeed**~~: decided. Session browsing with guardrails (see 3.2 Tier 3). **Easy
   Apply is automated** after packet approval, spread out by the drip scheduler and capped per day.
3. ~~**Automation ceiling**~~: decided. You approve the full packet, then it submits on its own.
   Anything unexpected comes back to you.
4. ~~**Deployment**~~: decided. Local Linux first, then an old Windows laptop on the LAN. Must run
   on both.
5. ~~**Stack**~~: decided. Python + uv + patchright (see section 4).
6. ~~**Volume**~~: decided. An adjustable daily knob, with per-site caps.
7. ~~**LLM**~~: decided. Claude Code and Codex CLIs on your subscriptions, no APIs.
8. **Cover letters**: default is "only when the form asks for one". Your samples go in
   `resources/cover_letters/`.
9. **Needed from you before M3/M4** (not blocking M0–M2):
   - Your mega resume and cover-letter samples in `resources/`
   - Answer-bank basics: contact info, current city, links (LinkedIn/GitHub/portfolio), earliest
     start date, notice period, EEO/veteran/disability answers (or "decline"), and a policy for
     "desired salary" fields
   - Logging into LinkedIn, Google, etc. once in the dedicated browser profile
   - Notification channel (UI only / email / Telegram / ntfy)
   - ~~Git~~: github.com/Aakash1337/Recrute (**public**, so personal data stays in gitignored
     `resources/` and `data/`)

---

## 8. Verification status (v1)
**Verified:**
- ~800 automated tests, run in CI on Linux and Windows.
- An independent Codex `gpt-6.1-sol` audit of every PR, with every finding fixed or explicitly
  resolved.
- A live smoke run: 7 real boards gave 1,085 postings, the rules kept 66, and real LLM triage
  queued 4. The UI was checked on that data.
- Both subscription CLIs run isolated. A canary file check confirmed the model can't read files.
- The browser detection probe was clean.

**Needs your first supervised runs** (the trial period exists for exactly this):
- **Live application forms.** Adapters were built against the real Greenhouse/Lever/Ashby API
  shapes and against local copies of their DOM. No real application has been submitted yet.
  Each adapter's first 5 submissions are fill-and-pause, so you watch them.
- **LinkedIn Easy Apply and logged-in LinkedIn browsing.** The markup was modeled; it has never
  been exercised on your account.
- **The alert-email and inbox classifiers.** They were tested on synthetic mail only.
- **The H-1B CSV importer.** It has been tested against the column layouts I know of; check it
  against a real USCIS download.
