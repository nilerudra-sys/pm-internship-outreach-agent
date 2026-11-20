"""Thin clients for external APIs. Each client only talks to its API; no business logic.

    gemini_client.py  - google-genai SDK (genai.Client), structured JSON output
    ats.py            - official public Greenhouse / Lever / Ashby job-board APIs
    http.py           - polite HTTP session (rate limiting, Retry-After)

Planned:
    hunter.py         - Hunter.io domain search / email finder / verifier
    gmail.py          - Gmail API OAuth (installed app flow) + send
"""
