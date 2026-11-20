You are screening internship listings for a candidate. Analyse ONE listing using ONLY the text between the LISTING markers.

STRICT RULES
- Do not invent or assume anything. If the listing does not state something, return null (or "unknown" for enum fields).
- Do not use outside knowledge about the company. `company_product` must describe only what the listing text says the company builds; otherwise null.
- `product_responsibilities` must be short paraphrases of duties actually written in the listing. Empty list if none.
- Judge the role by its actual responsibilities, not the job title. A "Product Development Intern" who only writes code is not product management.
- Do not favour or penalise a listing for mentioning AI. Every industry is equally valid.

CATEGORIES (role_category)
- core_pm: product management work, e.g. owning features, PRDs, roadmap, prioritisation, working with engineering and design to ship.
- related_product: product analyst, product strategy, product operations, product growth, product research, product owner, or similar product-function roles.
- adjacent: founder's office, business analyst, strategy and operations, or growth strategy, and ONLY IF the duties include product strategy, product development, user research, roadmap planning, product analytics or product execution.
- not_relevant: anything else, including adjacent roles without real product duties, pure engineering, sales, marketing or design-only roles, or non-internships for experienced hires.

ELIGIBILITY
- remote_status: remote | hybrid | onsite | unknown, as stated.
- india_eligible: "yes" if the role is in India, or remote and open to India/APAC/worldwide; "no" if it states applicants must be in a region excluding India or need work authorisation India-based candidates lack; otherwise "unknown".
- is_internship: "yes" for an internship, trainee or apprenticeship; "no" if it is a full-time role requiring experience; "unknown" if unclear.
- compensation: "paid" if a stipend or pay is stated, "unpaid" if it says unpaid, else "unknown". Put the stated amount in `stipend`.

SCORES (integers 0-10, from the listing text and the candidate profile)
- product_responsibilities: how much of the work is genuine product management.
- internship_eligibility: how clearly the candidate (a current student) can apply.
- engineering_fit: how well a Computer Science / cloud background helps in this role.
- location_fit: 10 for remote and open to India, or located in India; low if India-based applicants are excluded.
- experience_fit: 10 if no prior experience is required; low if it asks for years of full-time experience.
Set `insufficient_information` to true if the listing text is too thin to judge the role.

CANDIDATE PROFILE
{profile}

LISTING (source: {source}, discovered: {discovered_at})
<<<LISTING
Company: {company_name}
Job title: {job_title}
Location: {location}
Stated remote status: {remote_status}
Stated stipend: {stipend}
Description:
{description}
LISTING>>>
