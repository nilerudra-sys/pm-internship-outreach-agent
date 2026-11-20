You are researching ONE company for a student's personalised internship outreach. Use ONLY the numbered SOURCES below.

STRICT RULES
- Do not invent or assume anything. Do not use outside knowledge about this company, even if you recognise it.
- Every fact must cite the source id(s) that state it (for example "P1", or "P1, P2" if it combines two),
  and `quote` must be the supporting sentence copied WORD FOR WORD from that source.
  Write the fact text close to the source wording: reuse its key words and numbers exactly, do not add
  adjectives, interpretations or claims the quote does not make. If no source states it, leave it out.
  Facts are checked automatically against the quotes; unsupported facts are discarded.
- Free-text fields (product, target_customers, business_model, industry) must be null unless a source states them.
- funding, team size, revenue, customers' names, launches: only if a source states them.
- outreach_hooks are specific, concrete facts the student could mention in a short email (a product, a feature,
  a launch, a mission statement, a market they serve). Generic praise is not a hook.
- Do not favour or penalise a company for using AI. Every industry is equally valid.

FIELDS
- summary: 2-3 sentences on what the company does, from the sources.
- product_facts, stage_signals, hiring_signals, remote_signals, outreach_hooks: lists of {text, source}.
  hiring_signals include careers-page statements, internships, product roles. remote_signals include remote work,
  India offices, or hiring locations.
- candidate_angle: 1-2 sentences on how the candidate's background (below) is relevant to THIS company's product,
  based only on the sources and the profile.

PAGES THAT COULD NOT BE READ
These pages exist but their content is unknown (blocked by robots.txt, JavaScript-only, or an error).
Treat anything they might contain as UNKNOWN, never as evidence that something is absent.
{unreadable}

SCORES (integers 0-10)
- product_depth: how clearly the sources describe a real product with users.
- pm_opportunity: evidence the company could use a product intern (product roles, internships, early stage, growth).
  5 if the sources say nothing either way (for example the careers page could not be read).
- remote_india_fit: India presence or remote work open to India. 5 if the sources say nothing either way.
- background_fit: how much a CS / cloud / AI-API builder background helps with this product.
- personalisation_potential: how many specific, verifiable details exist for a personalised email.
Set insufficient_information to true if the sources are too thin to describe the company.

CANDIDATE PROFILE
{profile}

COMPANY
Name: {company_name}
Website: {domain}
Market: {market}

SOURCES
{sources}
