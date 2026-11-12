"""AI research shown next to a job: a plain-language brief of the posting, and public-web
research on the employer.

* briefs.py   one posting -> JobBrief (posting text only: no profile data, nothing written to
              Job columns)
* gather.py   public snippets about a company, fetched without any AI (careers-page blurb,
              Wikipedia)
* company.py  company -> CompanyResearch: web research from PUBLIC data only, then a separate
              tool-less "fit for you" note (the only step that sees the profile)
* validation.py  checks on the AI's answers (quotes really in the posting, sources really cited)
* claims.py   the atomic claim the worker and the web app share, so nothing runs twice

Failures are stored as a stable `error_kind` (never raw error text); the UI words them.
"""
