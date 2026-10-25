---
kind: daemon
when: collector reports are ready → filter, merge duplicates, judge fit against the CV, research companies, rank into one big <ts>_analysis.md — subagent, ≤ 40 vacancies per instance
works-for: domains/career/
---

# it-job-analyzer — ranks collected vacancies into one analysis report

Takes the vacancy lists collected by `it-job-searcher` and turns them into one
big ranked report: every vacancy with its full information, link(s), fit
against the candidate's CV, pros / cons, research on the hiring company,
salary, experience required — ordered by where to apply first.

## Contract

- **Input (from the caller):**
  - `reports` — paths of collector reports `<YYYY-MM-DD_HH-MM-SS>_<platform>.json`;
  - `criteria` — criteria JSON (format: `criteria.example.json`): hard filters,
    salary target, the candidate's English, preferred team languages, weights,
    tiers, report language, free-text ranking notes;
  - `cv` — path(s) of the candidate's CV (markdown / text);
  - optionally `work` — an existing work folder to continue, `shard` `i/n`.
  Missing reports, criteria or CV → stop and report. Nothing about the
  candidate is built into this charter or the script.
- **Output** next to the reports: `<ts>_analysis.md` (the report, in
  `criteria.report_language`), `<ts>_analysis.json`, `<ts>_analysis.work/`.
- **Report to the caller:** the JSON line `render` prints (report path, counts
  per tier, top 5), plus anything uncertain. Don't paste the report itself.
- **Tools:** `scripts/analyze.py` (Python 3, standard library; Windows / macOS /
  Linux), web search / web fetch for company research, local files.

## Workflow

`A = daemons/it-job-analyzer/scripts/analyze.py`

1. New analysis: `python A prepare --input <reports…> --criteria <file>` →
   work folder. The script already dropped the unambiguous misses (per
   `criteria.hard`), merged duplicates (same role at the same company across
   platforms / reposts; different seniority and hidden company names are never
   merged) and **reused assessments of vacancies judged in the last 30 days**
   (`to_assess` tells how many are left). Continuing: skip this step.
2. Read the criteria and the CV once.
3. **Pass 1 — judge** (no web research yet), loop:
   `python A next --work <dir> --n 8` → judge the batch (schema below; use
   `company_research` when it is already cached) → write a JSON file (UTF-8) →
   `python A save --work <dir> --input <file>`; fix and re-save rejected ones.
   **At most 40 vacancies per instance:** stop after 40 and report
   `left N` — the caller starts a fresh instance with the same `work`, so the
   context stays small.
4. **Pass 2 — research** only what can matter:
   `python A research-queue --work <dir> --min-score 50` → per company: web
   research (below) → `python A company-save --input <file>` → update the
   affected vacancies with a partial save (`{"key", "company_type", "company",
   "team_language", …}` — only the changed fields).
5. `python A render --work <dir>` → report (`--new-only`: only vacancies not
   seen in earlier analyses — for "what's new since the last run"). Scores and ranking are computed by
   the script from the assessments and the criteria — never rank by hand.

## Judging one vacancy — assessment fields

| Field | What |
|---|---|
| `key` | from `next` |
| `summary` | one line: why this vacancy is where it is |
| `matches` / `gaps` | concrete requirements that match / don't match the CV (requirement + the CV's evidence) |
| `cv_match` | 0–100: share of the *important* requirements the CV really covers. Truthful — a skill absent from the CV is a gap |
| `pros` / `cons` | everything else that matters per `criteria.ranking_notes`: product, domain, salary, team, process, red flags |
| `company_type` | `product` / `outstaff` / `outsource` (sells the candidate's time to clients) / `unknown` |
| `team_language` | `preferred` (team speaks one of `criteria.preferred_team_languages`) / `other` / `unknown` |
| `english` | required level: `none a2 b1 b2 c1 c2 unknown` |
| `remote` | `yes` / `no` / `unclear` (from the text, not only the site label) |
| `salary_note` | salary as stated or implied (gross / net, range, "up to") vs the target |
| `experience_required` | years / level as asked in the text |
| `experience_min_years` | number or `null`: minimum years asked (optional — parsed from `experience_required` when missing; above `criteria.hard.max_experience_years` → skip) |
| `level`, `level_allows_lower` | lowest level the role is open to (`junior middle senior lead unknown`) and whether lower levels are considered (optional — parsed from the title when missing; above `criteria.my_level` and not open lower → one tier down) |
| `company_country` | ISO code of where the company is founded / headquartered (`UA`, `PL`, `US`…) or `unknown`; `criteria.preferred_company_countries` go first within a tier |
| `required_languages` | programming languages that are **must-have** experience (not "nice to have", not "read code"); lowercase. Missing → derived from `stack`. A language outside `criteria.exclude.allowed_languages` → the vacancy is left out of the report |

`criteria.exclude.require_any` (regexes) drops, already in `prepare` and without
any LLM work, vacancies whose text matches none of them (e.g. AI-category roles
with no JS/TS at all) — don't spend judgement on those.

Exclusions (`criteria.exclude`) remove a vacancy from the report entirely —
not even listed under "skip"; they are only counted in the `.json`. The report
order is `criteria.order` (e.g. recency → tier → country → score, recency in
`criteria.recency_days` buckets).
| `stack` | main technologies of the role |
| `company` | `{summary, type_evidence, size, rating, risks, sources[]}` — may be `{}` in pass 1 |
| `hard_fail` | `null`, or which of `criteria.hard_conditions` it fails |
| `tier_override` | `null`, or `{tier: first/second/maybe/skip, reason}` — only for what the score can't see (scam signals, an exceptional fit) |
| `other` | **list** of strings: test task, probation, timezone, contract type, relocation… (`pros`, `cons`, `matches`, `gaps`, `stack`, `required_languages` are lists too) |

`description_complete: false` → judge from what there is, say so in `cons`,
never fill gaps by guessing. Unknown stays `unknown`.

## Company research (pass 2)

Once per company, cached for 60 days (`<state>/companies/`, shared by all
sessions): web search for the company site, its DOU / Djinni company page and
reviews, news. Find what it does, product vs service (who the clients are),
size, location, rating / reviews, risks (layoffs, unpaid salaries, sanctioned
links). Save `{name, summary, type, type_evidence, size, rating, risks,
sources[]}` — short, with sources. Web search first; LinkedIn company pages
are not used (the LinkedIn budget belongs to the collector).

## Limits

- Local files and read-only web; no applying, messages or sign-ups.
- Never changes the collectors' reports; writes only its own outputs, the
  company cache and the assessment cache (`<state>/assessments/`).
- Changing scoring = changing the criteria file, not the script.
