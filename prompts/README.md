# Prompts

Gemini prompt templates live here as plain Markdown files so they can be edited without touching code.

Planned templates:

| File | Used by | Purpose |
|---|---|---|
| `company_research.md` | research_agent | Summarise a company, its product and fit for a PM intern |
| `fit_scoring.md` | research_agent | Score 0-100 for sector, remote-friendliness and stage |
| `outreach_email.md` | writer_agent | Draft a short personalised email referencing your projects (config/profile.toml) |
| `follow_up_email.md` | writer_agent | Polite follow-up after no reply |

Templates use `{placeholder}` fields filled from `config/profile.toml` and the database.
