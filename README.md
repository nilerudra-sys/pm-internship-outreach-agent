# Product Internship Outreach Agent

An AI job-search assistant for Product Management internships. It finds openings, researches each company from its own website, finds the right person to contact, and drafts a short personalised email that **I review and approve before anything is sent**.

**Why I built it:** good cold outreach means reading every company's site, finding a founder or product lead, and writing something specific, which takes about 30 minutes per company. Fully automated tools send generic or wrong emails. This keeps the human judgement and automates the legwork.

**What makes it different: it is built not to make things up.**
- Every company fact is checked sentence-by-sentence against a quote from the company's own page; distorted numbers, flipped negations and invented claims are dropped.
- Drafts may only use verified facts and my own profile; any unknown name, number or claim about my projects is rejected and rewritten.
- Email addresses are never guessed; every contact keeps the source that proves their name and role.
- Human approval, a hard cap of 15 emails/day, send-only Gmail permission, and follow-ups only after review.

> Built with Python, Streamlit, SQLite, Google Gemini (`google-genai`) and the Gmail API, with AI pair-programming (Claude Code). 337 automated tests.

## Screenshots

_Add 2-3 images to `docs/` (no real names or emails visible): the Review queue with "Verified facts used", Company Research, and Contact Discovery._

## Privacy and safety

- Secrets (`.env`, `credentials/`) and data (`data/`, which holds contacts) are git-ignored; never commit them.
- Website access respects robots.txt, waits between requests and caches pages; LinkedIn and Wellfound are never scraped (search links only).
- Gmail uses the send-only scope; optional reply tracking reads headers only.

- **Role:** Product Management Intern
- **Markets:** 80% India, 20% international, remote-first
- **Roles:** 22 product role titles (9 core PM, 9 related product, 4 adjacent), judged by actual responsibilities
- **Industries:** all of them (SaaS, fintech, e-commerce, consumer, AI/ML, EdTech, HealthTech, gaming, enterprise, dev tools, logistics, mobility, media, telecom, BFSI); AI is not required
- **Contacts:** founders, co-founders, Heads of Product, recruiters
- **Sending:** at most 15 emails per day, hybrid approval
- **AI:** Google Gemini via the official `google-genai` SDK. The Anthropic API is not used.

## Architecture

```
                 ┌──────────────────────── Streamlit dashboard (app.py) ───────────────────────┐
                 │  Overview · Review queue (approve / edit / reject) · Companies · Settings  │
                 └──────────────────────────────────┬──────────────────────────────────────────┘
                                                    │
   agents/            discovery ──► research ──► contacts ──► writer ──► [YOU APPROVE] ──► sender
                         │             │            │           │                           │
   integrations/   links + ATS APIs Gemini       Hunter.io    Gemini                      Gmail API
                                                    │
   services/        quota (15/day hard cap, 80/20 market mix) · pipeline orchestration
   database/        SQLite: companies · contacts · emails · activity_log · api_usage
   prompts/         editable Gemini prompt templates (Markdown)
   config/          .env settings + profile.toml (your background and targeting)
```

**Email lifecycle** (enforced in `database/repository.py`):

```
pending_review ──► approved ──► sent ──► replied
      │   ▲            │
      ▼   │            ├──► failed ──► approved (retry)
   rejected            └──► pending_review (any edit sends it back for re-approval)
```

A draft can never jump straight to `sent`, and editing an approved email requires re-approval.

## Discovery

| Source | How | Why |
|---|---|---|
| Google | Generated search links (each role × India / remote India / remote worldwide, plus 15 industries), you import results | Custom Search JSON API is closed to new customers (shuts down 2027-01-01); Gemini Search grounding is billed |
| LinkedIn Jobs | Generated searches filtered to Internship level, India / remote / worldwide, recency | No permitted job-search API; the app never logs in to, fetches or automates LinkedIn |
| Wellfound | Generated role-page and company links | No public API; automated access is blocked |
| Career pages | **Automatic** via official public Greenhouse, Lever and Ashby job-board APIs | Free, no key; rate-limited, max one check per board every 6 hours |
| CSV | Upload with validation, duplicate detection and merge | Bring in anything you collected |

Search plans and listing order keep **80% India / 20% international**.

**Deduplication** (`services/discovery/leads.py`): same normalised job URL, or same company (domain or normalised name) with the same normalised title. Duplicates are merged: missing fields are filled, nothing is overwritten, and every source URL is kept in `opportunity_sources`.

**Gemini analysis** (`services/discovery/analysis.py`, prompt in `prompts/opportunity_analysis.md`) classifies each listing as Core PM / Related Product / Adjacent / Not Relevant, extracts details, and gives five 0-10 sub-scores. The 0-100 relevance score is computed in code (`services/discovery/scoring.py`) with caps for non-product roles, non-internships, and roles that exclude India-based applicants. Gemini sees only the imported listing text; listings without a description are skipped, and extracted stipend/duration/location are kept only when they appear in the listing.

## Project structure

```
agents/          pipeline agents (base class now; discovery/research/contact/writer/sender later)
integrations/    API clients: Gemini, career-page ATS APIs, HTTP; Hunter and Gmail later
database/        schema.sql, db.py (connections), repository.py (queries), models.py, seed.py
services/        quota.py (daily cap, market mix)
dashboard/       components.py + views/ (overview, review_queue, companies, settings)
prompts/         Gemini prompt templates
config/          settings.py (.env loader) + profile.toml (applicant profile)
credentials/     Gmail OAuth files (git-ignored)
tests/           pytest suite
app.py           Streamlit entry point
```

## Setup (Windows / PowerShell)

```powershell
python -m venv .venv
.venv\Scripts\Activate.ps1
pip install -r requirements.txt
copy .env.example .env          # then fill in your API keys
python -m database              # create data/outreach.db
python -m database.seed         # optional: fictional demo rows
streamlit run app.py
```

Run tests:

```powershell
pytest
```

### API keys

| Service | Where | `.env` variable |
|---|---|---|
| Gemini | https://aistudio.google.com/apikey | `GEMINI_API_KEY` |
| Hunter.io | https://hunter.io/api-keys | `HUNTER_API_KEY` |
| Gmail | Google Cloud Console → enable Gmail API → OAuth client (Desktop) | save JSON to `credentials/gmail_client_secret.json` |

## Roadmap

- [x] **Phase 1 – Foundation:** structure, config, SQLite schema, approval workflow, quota rules, dashboard, tests
- [x] **Phase 2 – Discovery:** Google / LinkedIn / Wellfound links, career-page ATS APIs, CSV import, dedup, Gemini classification and scoring
- [x] **Phase 3 – Research:** company website pages (robots.txt respected, ≤5 pages, 7-day cache) + linked listings → Gemini research with cited facts, outreach hooks and a 0-100 fit score
- [x] **Phase 4 – Contacts:** people from official website team/about/leadership pages and job listings (with evidence quotes), Google/LinkedIn search links + manual/CSV entry, optional Hunter.io (found emails only), Python scoring 40/25/20/15, vacancy-aware priority, manual-verification protection
- [x] **Phase 5 – Writing:** Gemini drafts from verified facts only, automatic checks (numbers, names, facts used, no invented vacancy) with one retry, drafts queued in the Review queue with their evidence
- [x] **Phase 6 – Sending:** Gmail OAuth (send-only scope), approved-only sends with a 15/day hard cap, double-send lock, failure/retry and unknown-state resolution, batch sending 80/20 with spacing, test-to-self
- [x] **Phase 7 – Follow-ups & reply tracking:** one template follow-up after 5 days (reviewed, same thread), manual or header-only Gmail reply/bounce detection
