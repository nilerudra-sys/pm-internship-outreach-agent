"""AI agents that drive the outreach pipeline.

Planned agents (built in later phases):
    discovery        - implemented in services/discovery: search links, career-page APIs, CSV import (80/20)
    research_agent   - summarises a company and scores its fit with Gemini
    contact_agent    - finds founders / Heads of Product / recruiters via Hunter.io
    writer_agent     - drafts a personalised email with Gemini, queued for review
    sender_agent     - sends *approved* emails through Gmail within the daily cap
"""
