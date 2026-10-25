"""Vacancy analyzer workflow: the deterministic parts of it-job-analyzer.

    python analyze.py prepare --input <a.json> [<b.json> …] --criteria <criteria.json> [--fresh]
    python analyze.py status  --work <dir>
    python analyze.py next    --work <dir> [--n 8] [--shard 0/2]   # next unassessed vacancies (full text)
    python analyze.py save    --work <dir> --input <assessments.json>   # new or partial updates
    python analyze.py research-queue --work <dir> [--min-score 50]  # companies worth researching
    python analyze.py company --name "<company>"                    # cached company research, if any
    python analyze.py company-save --input <company.json>
    python analyze.py render  --work <dir> [--partial]              # → <ts>_analysis.md / .json

`prepare` loads the collectors' `<ts>_<platform>.json` reports, applies the
hard filters that need no judgement (from the criteria file), merges
duplicates (same role at the same company across platforms or reposts) and
reuses assessments of vacancies already judged in earlier sessions. The
daemon judges the queue in batches (`next` → judge → `save`); `render`
scores, ranks and writes the report next to the inputs.

All personal requirements come from the criteria file (see
`criteria.example.json`); nothing about the candidate is hardcoded here.
Standard library only: Windows / macOS / Linux with plain `python`.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
from datetime import datetime, timedelta
from pathlib import Path

for _stream in (sys.stdout, sys.stderr):
    if hasattr(_stream, "reconfigure"):
        _stream.reconfigure(encoding="utf-8", errors="replace")

REPO = Path(__file__).resolve().parents[3]
SEARCHES = Path(os.environ.get("JOB_SEARCH_DIR") or REPO / "domains" / "career" / "searches")
STATE = Path(os.environ.get("JOB_SEARCH_STATE_DIR") or SEARCHES / ".state")
COMPANIES = STATE / "companies"
SEEN = STATE / "assessments"          # assessments reused across sessions
COMPANY_TTL = timedelta(days=60)
SEEN_TTL = timedelta(days=30)

ENGLISH_ORDER = ["none", "a2", "b1", "b2", "c1", "c2"]
TIER_KEYS = ["first", "second", "maybe", "skip"]
# Neutral defaults; every key can be overridden by the criteria file.
DEFAULT_CRITERIA = {
    "report_language": "en",
    "currency_to_usd": {"USD": 1.0, "EUR": 1.08, "GBP": 1.27, "PLN": 0.25, "UAH": 0.024, "CZK": 0.043},
    # salary_floor_usd: a stated upper bound below it → reject;
    # salary_min_floor_usd: a stated lower bound below it → reject;
    # max_experience_years: required experience above it → reject.
    "hard": {"remote_only": False, "salary_floor_usd": None, "salary_min_floor_usd": None,
             "max_experience_years": None},
    "hard_conditions": [],
    "salary_target_usd": None,
    "my_english": "b2",
    "my_level": None,                    # junior / middle / senior / lead; roles only above it drop a tier
    "preferred_team_languages": [],
    "preferred_company_countries": [],   # ISO codes; these companies go first within a tier
    "company_type_points": {"product": 1.0, "outstaff": 0.6, "unknown": 0.4, "outsource": 0.2},
    "weights": {"company": 30, "cv": 40, "salary": 15, "team_language": 8, "english": 7},
    "tiers": {"first": 70, "second": 55, "maybe": 40},
    "ranking_notes": [],
    # Not shown in the report at all (only counted in the .json): roles above
    # max_experience_years, roles that require a language outside allowed_languages,
    # roles whose title / stack contains one of the keywords.
    # require_any: regexes; a vacancy whose title + description match none of them is
    # dropped in `prepare` without any LLM work (e.g. AI-category roles with no JS/TS at all).
    "exclude": {"over_max_experience": False, "allowed_languages": None, "keywords": [], "levels": [],
                "require_any": []},
    # Sort keys, most important first: recency (posted within recency_days buckets),
    # tier, country (preferred_company_countries first), score.
    "order": ["tier", "country", "score"],
    "recency_days": [7, 14],
}

ASSESSMENT_FIELDS = {
    "key": str, "summary": str, "pros": list, "cons": list, "matches": list, "gaps": list,
    "cv_match": (int, float), "company_type": str, "team_language": str, "english": str,
    "remote": str, "salary_note": str, "experience_required": str, "stack": list,
    "company": dict, "hard_fail": (str, type(None)), "tier_override": (dict, type(None)),
    "other": list,
}
LEVEL_ORDER = ["junior", "middle", "senior", "lead"]
# Optional fields: derived from the text when missing (older assessments).
OPTIONAL_FIELDS = {"experience_min_years": (int, float, type(None)), "level": str, "level_allows_lower": bool,
                   "company_country": str, "required_languages": list}
# Programming languages recognised in `stack` when `required_languages` is not set.
LANGUAGES = {
    "javascript": r"\bjavascript\b|\bjs\b|node\.?js|\bnode\b|react(?! native)|angular|vue|next\.?js|nest\.?js|express",
    "typescript": r"\btypescript\b|\bts\b",
    "python": r"\bpython\b|django|fastapi|flask", "php": r"\bphp\b|laravel|symfony|wordpress",
    "java": r"\bjava\b(?!script)|spring|kotlin", "go": r"\bgo\b|golang", "ruby": r"\bruby\b|rails",
    "c#": r"c#|\.net\b|dotnet|asp\.net", "c++": r"c\+\+", "rust": r"\brust\b", "scala": r"\bscala\b",
    "elixir": r"\belixir\b", "swift": r"\bswift\b", "dart": r"\bdart\b|flutter",
}
ENUMS = {
    "level": set(LEVEL_ORDER) | {"unknown"},
    "company_type": {"product", "outstaff", "outsource", "unknown"},
    "team_language": {"preferred", "other", "unknown"},
    "english": set(ENGLISH_ORDER) | {"unknown"},
    "remote": {"yes", "no", "unclear"},
}
HIDDEN_COMPANY = {"", "?", "confidential", "конфіденційно", "конфиденциально", "anonymous", "анонімно",
                  "анонимно", "hidden", "company", "компанія", "компания", "unknown", "n/a", "na"}


# ------------------------------------------------------------- helpers ---

def load(path) -> object:
    """JSON from a file written by any tool: UTF-8 (with or without BOM), or
    the console code page a Windows shell redirect produces."""
    raw = Path(path).read_bytes()
    for enc in ("utf-8-sig", "cp1251", "latin-1"):
        try:
            return json.loads(raw.decode(enc))
        except (UnicodeDecodeError, json.JSONDecodeError):
            continue
    raise SystemExit(f"cannot read JSON from {path}")


def dump(path: Path, data) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2), "utf-8")
    tmp.replace(path)


def load_criteria(path: str | None) -> dict:
    crit = json.loads(json.dumps(DEFAULT_CRITERIA))
    if path:
        user = load(path)
        for k, v in user.items():
            if isinstance(v, dict) and isinstance(crit.get(k), dict):
                crit[k].update(v)
            else:
                crit[k] = v
    if crit["my_english"] not in ENGLISH_ORDER:
        raise SystemExit(f"criteria.my_english must be one of {ENGLISH_ORDER}")
    return crit


LEGAL = r"\b(llc|ltd|inc|gmbh|sp\.? ?z ?o\.? ?o\.?|tov|тов|fop|фоп|group|ukraine|україна|corp|co|s\.?r\.?o\.?|plc)\b"


def company_slug(name: str | None) -> str:
    n = (name or "").strip().lower()
    if n.strip('"\'«» ') in HIDDEN_COMPANY:
        return "unknown"
    n = re.sub(LEGAL, " ", n.replace("&", " and "))
    n = re.sub(r"[^a-z0-9а-яіїєґ]+", "", n)  # "Yozma Tech" == "YozmaTech"
    return n or "unknown"


SENIORITY = {"junior", "middle", "senior", "strong", "lead", "trainee", "intern", "mid", "sr", "jr",
             "principal", "staff", "head", "the", "a", "an", "of", "and", "for", "to", "in", "with", "developer",
             "engineer", "розробник", "remote", "віддалено"}
LEVEL_WORDS = {"junior", "jr", "middle", "mid", "senior", "sr", "lead", "principal", "staff", "head",
               "trainee", "intern", "strong"}


def title_tokens(title: str | None) -> set[str]:
    words = [w.strip(".") for w in re.findall(r"[a-z0-9а-яіїєґ+#.]+", (title or "").lower())]
    return {w for w in words if w and w not in SENIORITY}


def levels(title: str | None) -> frozenset[str]:
    norm = {"jr": "junior", "mid": "middle", "sr": "senior"}
    return frozenset(norm.get(w, w) for w in set(re.findall(r"[a-z]+", (title or "").lower())) & LEVEL_WORDS)


def similar(a: set[str], b: set[str]) -> float:
    return len(a & b) / len(a | b) if a and b else 0.0


def same_role(a: dict, b: dict) -> float:
    """0 = different roles; otherwise title similarity. Different seniority =
    different role; on one platform only identical titles count (reposts)."""
    la, lb = levels(a.get("title")), levels(b.get("title"))
    if la and lb and la != lb:
        return 0.0
    if a.get("platform") == b.get("platform"):
        return 1.0 if title_tokens(a.get("title")) == title_tokens(b.get("title")) else 0.0
    return similar(title_tokens(a.get("title")), title_tokens(b.get("title")))


REMOTE_YES = ("remote", "віддален", "удален", "дистанц", "work from home", "telecommute")
ONSITE_ONLY = ("тільки офіс", "только офис", "office only", "on-site only", "onsite only", "лише офіс")
HYBRID = ("гібрид", "гибрид", "hybrid")


def remote_hint(rec: dict) -> str:
    loc = f"{rec.get('format') or ''} {rec.get('location') or ''}".lower()
    if rec.get("format") == "remote" or any(w in loc for w in REMOTE_YES):
        return "yes"
    if rec.get("format") in ("office", "hybrid") or any(w in loc for w in ONSITE_ONLY + HYBRID):
        return "no"
    return "unclear"


def to_usd(value, currency: str | None, crit: dict) -> float | None:
    if value is None:
        return None
    rate = crit["currency_to_usd"].get(currency or "USD")
    return float(value) * rate if rate else None


def seen_key(link: dict) -> str:
    return f"{link['platform']}-{re.sub(r'[^A-Za-z0-9_-]', '_', str(link['source_id']))}"


# ------------------------------------------------------------- prepare ---

def cmd_prepare(args) -> int:
    crit = load_criteria(args.criteria)
    inputs = [Path(p).resolve() for p in args.input]
    missing = [str(p) for p in inputs if not p.exists()]
    if missing:
        sys.exit(f"input not found: {missing}")
    started = datetime.now()
    session = inputs[0].parent
    work = session / f"{started.strftime('%Y-%m-%d_%H-%M-%S')}_analysis.work"

    by_source: dict[tuple, dict] = {}   # newest report wins per (platform, source_id)
    sources = []
    for path in sorted(inputs, key=lambda p: load(p).get("searched_at", "")):
        rep = load(path)
        sources.append({"file": str(path), "platform": rep.get("platform"), "searched_at": rep.get("searched_at"),
                        "count": rep.get("count"), "stopped_early": rep.get("stopped_early"),
                        "warnings": rep.get("warnings") or [], "query": rep.get("query")})
        for rec in rep.get("records", []):
            by_source[(rec.get("platform"), str(rec.get("source_id")))] = rec

    hard, ex = crit["hard"], crit["exclude"]
    L = LABELS.get(crit.get("report_language"), LABELS["en"])
    rejected, kept, excluded = [], [], []
    for rec in by_source.values():
        reason, hint = None, remote_hint(rec)
        title = (rec.get("title") or "").lower()
        if any(k.lower() in title for k in ex.get("keywords") or []):
            excluded.append(_brief(rec, "keyword"))
            continue
        text = f"{rec.get('title') or ''} {rec.get('description') or ''}"
        if ex.get("require_any") and text.strip() and not re.search(
                "|".join(ex["require_any"]), text, re.I):
            excluded.append(_brief(rec, "no_required_keyword"))  # e.g. not a JS/TS role at all
            continue
        if (ex.get("over_max_experience") and hard.get("max_experience_years") is not None
                and rec.get("experience_years") is not None and float(rec["experience_years"]) > hard["max_experience_years"]):
            excluded.append(_brief(rec, "experience"))
            continue
        top = to_usd(rec.get("salary_max"), rec.get("currency"), crit)  # only a known upper bound can fail
        if hard.get("remote_only") and hint == "no":
            reason = f"{L['r_remote']}: {rec.get('location') or rec.get('format')}"
        elif hard.get("salary_floor_usd") and top is not None and top < hard["salary_floor_usd"]:
            reason = f"{L['r_salary']} ${hard['salary_floor_usd']}: {rec.get('salary_text') or round(top)}"
        elif (hard.get("salary_min_floor_usd") and (low := to_usd(rec.get("salary_min"), rec.get("currency"), crit)) is not None
              and low < hard["salary_min_floor_usd"]):
            reason = f"{L['r_salary_min']} ${hard['salary_min_floor_usd']}: {rec.get('salary_text') or round(low)}"
        elif (hard.get("max_experience_years") is not None and rec.get("experience_years") is not None
              and float(rec["experience_years"]) > hard["max_experience_years"]):
            reason = f"{L['r_exp']} {hard['max_experience_years']}: {rec.get('experience_text') or rec['experience_years']}"
        if reason:
            rejected.append({**_brief(rec, None), "reason": reason})
        else:
            rec["_remote_hint"] = hint
            kept.append(rec)
    dump(work / "excluded.json", excluded)

    groups: list[dict] = []
    for rec in sorted(kept, key=lambda r: r.get("posted_at") or "", reverse=True):
        slug = company_slug(rec.get("company"))
        match = None
        if slug != "unknown":  # hidden / missing company names are never merged
            for g in groups:
                if g["company_slug"] == slug:
                    score = min(same_role(rec, other) for other in g["records"])
                    if score >= 0.6:
                        match = g
                        if score < 0.85:
                            g["duplicate_note"] = "merged by similar title — check"
                        break
        if match:
            match["records"].append(rec)
        else:
            groups.append({"company_slug": slug, "records": [rec]})

    candidates, reused = [], 0
    (work / "assessments").mkdir(parents=True, exist_ok=True)
    for i, g in enumerate(groups, 1):
        main = max(g["records"], key=lambda r: len(r.get("description") or ""))
        links = [{"platform": r.get("platform"), "source_id": str(r.get("source_id")), "url": r.get("url"),
                  "posted_at": r.get("posted_at")} for r in g["records"]]
        cand = {"key": f"v{i:04d}", "company_slug": g["company_slug"], "main": main, "links": links,
                "duplicate_note": g.get("duplicate_note"),
                "description_complete": any(_complete(r) for r in g["records"])}
        if not args.fresh:
            prev = _seen(links)
            if prev:
                dump(work / "assessments" / f"{cand['key']}.json", {**prev["assessment"], "key": cand["key"]})
                cand["seen_before"] = prev["assessed_at"][:10]
                reused += 1
        candidates.append(cand)
    dump(work / "meta.json", {"started_at": started.isoformat(timespec="seconds"), "session": str(session),
                              "criteria_file": str(Path(args.criteria).resolve()) if args.criteria else None,
                              "criteria": crit, "sources": sources, "loaded": len(by_source),
                              "rejected_count": len(rejected), "candidates_count": len(candidates)})
    dump(work / "candidates.json", candidates)
    dump(work / "rejected.json", rejected)
    print(json.dumps({"work": str(work), "loaded": len(by_source), "rejected": len(rejected),
                      "candidates": len(candidates), "reused_from_earlier": reused,
                      "to_assess": len(candidates) - reused,
                      "companies": len({c["company_slug"] for c in candidates})}, ensure_ascii=False))
    return 0


def _brief(rec: dict, why: str | None) -> dict:
    out = {"title": rec.get("title"), "company": rec.get("company"), "url": rec.get("url"),
           "platform": rec.get("platform")}
    return {**out, "excluded_by": why} if why else out


def required_languages(a: dict) -> list[str]:
    """Languages the role requires: the assessment field, else recognised in `stack`."""
    if isinstance(a.get("required_languages"), list):
        return [str(x).lower() for x in a["required_languages"]]
    stack = " ".join(a.get("stack") or []).lower()
    return [lang for lang, rx in LANGUAGES.items() if re.search(rx, stack)]


def excluded_by(c: dict, a: dict, crit: dict) -> str | None:
    """Why a vacancy is left out of the report entirely (criteria.exclude), or None."""
    ex, rec = crit["exclude"], c["main"]
    text = f"{rec.get('title') or ''} {' '.join(a.get('stack') or [])}".lower()
    if any(k.lower() in text for k in ex.get("keywords") or []):
        return "keyword"
    max_exp = crit["hard"].get("max_experience_years")
    years = exp_min(a, rec)
    if ex.get("over_max_experience") and max_exp is not None and years is not None and years > max_exp:
        return "experience"
    if level_of(a, rec)[0] in (ex.get("levels") or []):
        return "level"
    allowed = ex.get("allowed_languages")
    if allowed:
        allowed = {x.lower() for x in allowed}
        if any(lang not in allowed for lang in required_languages(a)):
            return "languages"
    return None


def _complete(rec: dict) -> bool:
    """Full description? Older reports have no flag: judge by length."""
    flag = rec.get("description_complete")
    return bool(flag) if flag is not None else len(rec.get("description") or "") > 500


def _seen(links: list[dict]) -> dict | None:
    for link in links:
        path = SEEN / f"{seen_key(link)}.json"
        if path.exists():
            item = load(path)
            if datetime.now() - datetime.fromisoformat(item["assessed_at"]) <= SEEN_TTL:
                return item
    return None


# ---------------------------------------------------------- the queue ---

def _assessed(work: Path) -> set[str]:
    return {p.stem for p in (work / "assessments").glob("*.json")}


def cmd_status(args) -> int:
    work = Path(args.work)
    cands = load(work / "candidates.json")
    done = _assessed(work)
    print(json.dumps({"candidates": len(cands), "assessed": len(done & {c["key"] for c in cands}),
                      "left": len([c for c in cands if c["key"] not in done])}))
    return 0


def cmd_next(args) -> int:
    work = Path(args.work)
    shard, shards = (int(x) for x in args.shard.split("/")) if args.shard else (0, 1)
    done = _assessed(work)
    todo = [c for i, c in enumerate(load(work / "candidates.json")) if i % shards == shard and c["key"] not in done]
    batch = []
    for c in todo[:args.n]:
        m = c["main"]
        batch.append({
            "key": c["key"], "title": m.get("title"), "company": m.get("company"),
            "company_url": m.get("company_url"), "links": [l["url"] for l in c["links"]],
            "location": m.get("location"), "remote_hint": m.get("_remote_hint"), "salary": m.get("salary_text"),
            "experience": m.get("experience_text") or m.get("experience_years"), "english": m.get("english"),
            "posted_at": m.get("posted_at"), "notes": m.get("notes"),
            "description_complete": c.get("description_complete"),
            "company_research": _company_cached(m.get("company")),
            "description": m.get("description"),
        })
    print(json.dumps({"left_after_this_batch": len(todo) - len(batch), "batch": batch}, ensure_ascii=False, indent=1))
    return 0


def _validate(a: dict) -> list[str]:
    errs = []
    for k, t in ASSESSMENT_FIELDS.items():
        if k not in a:
            errs.append(f"missing {k}")
        elif not isinstance(a[k], t):
            errs.append(f"{k}: wrong type {type(a[k]).__name__}")
    for k, t in OPTIONAL_FIELDS.items():
        if k in a and not isinstance(a[k], t):
            errs.append(f"{k}: wrong type {type(a[k]).__name__}")
    if "company_country" in a and not re.fullmatch(r"[A-Z]{2}|unknown", a["company_country"]):
        errs.append("company_country must be an ISO-3166 alpha-2 code (UA, PL, US…) or 'unknown'")
    for k, allowed in ENUMS.items():
        if k in a and a[k] not in allowed:
            errs.append(f"{k}: {a[k]!r} not in {sorted(allowed)}")
    if isinstance(a.get("cv_match"), (int, float)) and not 0 <= a["cv_match"] <= 100:
        errs.append("cv_match must be 0..100")
    if a.get("tier_override") and a["tier_override"].get("tier") not in TIER_KEYS:
        errs.append(f"tier_override.tier must be one of {TIER_KEYS}")
    return errs


def cmd_save(args) -> int:
    """New assessments, or partial updates of saved ones (only the given
    fields change — e.g. company_type after company research)."""
    work = Path(args.work)
    cands = {c["key"]: c for c in load(work / "candidates.json")}
    items = load(args.input)
    items = items if isinstance(items, list) else [items]
    bad, saved = {}, 0
    for a in items:
        key = a.get("key")
        if key not in cands:
            bad[str(key)] = ["unknown key"]
            continue
        path = work / "assessments" / f"{key}.json"
        merged = {**(load(path) if path.exists() else {}), **a}
        errs = _validate(merged)
        if errs:
            bad[key] = errs
            continue
        dump(path, merged)
        now = datetime.now().isoformat(timespec="seconds")
        for link in cands[key]["links"]:  # remember for later sessions
            dump(SEEN / f"{seen_key(link)}.json", {"assessed_at": now, "assessment": merged})
        saved += 1
    print(json.dumps({"saved": saved, "rejected": bad}, ensure_ascii=False))
    return 1 if bad else 0


# ------------------------------------------------------------ companies ---

def _company_cached(name: str | None) -> dict | None:
    slug = company_slug(name)
    path = COMPANIES / f"{slug}.json"
    if slug == "unknown" or not path.exists():
        return None
    data = load(path)
    if datetime.now() - datetime.fromisoformat(data.get("researched_at", "2000-01-01")) > COMPANY_TTL:
        return None
    return data


def cmd_company(args) -> int:
    data = _company_cached(args.name)
    print(json.dumps(data or {"cached": False, "slug": company_slug(args.name)}, ensure_ascii=False, indent=1))
    return 0


def cmd_company_save(args) -> int:
    data = load(args.input)
    items = data if isinstance(data, list) else [data]
    for item in items:
        if not item.get("name"):
            sys.exit("company research needs at least 'name'")
        if company_slug(item["name"]) == "unknown":
            continue
        item.setdefault("researched_at", datetime.now().isoformat(timespec="seconds"))
        dump(COMPANIES / f"{company_slug(item['name'])}.json", item)
    print(json.dumps({"saved": len(items)}))
    return 0


def cmd_research_queue(args) -> int:
    """Companies worth researching: not cached, and at least one of their
    vacancies scores >= min-score with what is known now."""
    work = Path(args.work)
    meta = load(work / "meta.json")
    crit = meta["criteria"]
    done = _assessed(work)
    queue: dict[str, dict] = {}
    for c in load(work / "candidates.json"):
        if c["key"] not in done or c["company_slug"] == "unknown" or _company_cached(c["main"].get("company")):
            continue
        a = load(work / "assessments" / f"{c['key']}.json")
        if a.get("hard_fail"):
            continue
        total, _ = score(c, a, crit)
        if total >= args.min_score:
            q = queue.setdefault(c["company_slug"], {"company": c["main"].get("company"),
                                                     "company_url": c["main"].get("company_url"), "keys": []})
            q["keys"].append(c["key"])
    print(json.dumps({"companies": list(queue.values())}, ensure_ascii=False, indent=1))
    return 0


# --------------------------------------------------------------- render ---

def salary_points(rec: dict, crit: dict) -> float:
    target, floor = crit.get("salary_target_usd"), crit["hard"].get("salary_floor_usd") or 0
    lo = to_usd(rec.get("salary_min"), rec.get("currency"), crit)
    hi = to_usd(rec.get("salary_max"), rec.get("currency"), crit)
    if not target or (lo is None and hi is None):
        return 0.5
    best = hi if hi is not None else lo  # "from X": X is the least we know
    if best >= target[0]:
        return 1.0
    if best <= floor:
        return 0.0
    return 0.4 + 0.6 * (best - floor) / max(1, target[0] - floor)


def english_points(required: str, mine: str) -> float:
    if required == "unknown":
        return 0.6
    gap = ENGLISH_ORDER.index(required) - ENGLISH_ORDER.index(mine)
    return 1.0 if gap <= 0 else 0.6 if gap == 1 else 0.1


def score(c: dict, a: dict, crit: dict) -> tuple[int, dict]:
    w = crit["weights"]
    team = {"preferred": 1.0, "unknown": 0.4, "other": 0.0}[a["team_language"]]
    if not crit.get("preferred_team_languages"):
        team = 1.0
    parts = {
        "company": w["company"] * crit["company_type_points"][a["company_type"]],
        "cv": w["cv"] * a["cv_match"] / 100,
        "salary": w["salary"] * salary_points(c["main"], crit),
        "team_language": w["team_language"] * team,
        "english": w["english"] * english_points(a["english"], crit["my_english"]),
    }
    return round(sum(parts.values())), {k: round(v, 1) for k, v in parts.items()}


def exp_min(a: dict, rec: dict) -> float | None:
    """Minimum years required: the assessment field, else the first number in
    its `experience_required` text ("3+ года", "2–5 years"), else the site's own."""
    if a.get("experience_min_years") is not None:
        return float(a["experience_min_years"])
    m = re.search(r"(\d+(?:[.,]\d+)?)\s*(?:\+|–|-|—|до|to)?\s*\d*\s*(?:years?|yrs?|рок|рік|лет|год|г\.)",
                  (a.get("experience_required") or "").lower())
    if m:
        return float(m.group(1).replace(",", "."))
    return float(rec["experience_years"]) if rec.get("experience_years") is not None else None


LEAD_WORDS = ("lead", "staff", "principal", "head of", "architect", "cto", "тимлид", "лид")


def level_of(a: dict, rec: dict) -> tuple[str, bool]:
    """(lowest level the role is open to, open to lower levels too?) — from
    the assessment, else from the title + experience text."""
    if a.get("level"):
        return a["level"], bool(a.get("level_allows_lower"))
    text = f"{rec.get('title') or ''} {a.get('experience_required') or ''}".lower()
    if re.search(r"\bjunior\b|\bjr\b|trainee|intern", text):
        return "junior", True
    if re.search(r"\bmiddle\b|\bmid\b|mid-level|\bmidl", text):
        return "middle", True
    if any(w in text for w in LEAD_WORDS):
        return "lead", False
    if re.search(r"\bsenior\b|\bsr\b|сеньор|синьор", text):
        return "senior", False
    return "unknown", True


def tier_for(total: int, a: dict, rec: dict, crit: dict, L: dict) -> tuple[str, str | None]:
    if a.get("hard_fail"):
        return "skip", f"{L['hard_fail']}: {a['hard_fail']}"
    max_exp = crit["hard"].get("max_experience_years")
    years = exp_min(a, rec)
    if max_exp is not None and years is not None and years > max_exp:
        return "skip", f"{L['r_exp']} {max_exp}: {a.get('experience_required') or years}"
    if a.get("tier_override"):
        return a["tier_override"]["tier"], a["tier_override"].get("reason")
    t = crit["tiers"]
    tier = ("first" if total >= t["first"] else "second" if total >= t["second"]
            else "maybe" if total >= t["maybe"] else "skip")
    mine = crit.get("my_level")
    level, lower_ok = level_of(a, rec)
    if mine in LEVEL_ORDER and level in LEVEL_ORDER and not lower_ok \
            and LEVEL_ORDER.index(level) > LEVEL_ORDER.index(mine) and tier != "skip":
        return TIER_KEYS[TIER_KEYS.index(tier) + 1], L["demoted"].format(level=level, mine=mine)
    return tier, None


def cmd_render(args) -> int:
    work = Path(args.work)
    meta, cands, rejected = load(work / "meta.json"), load(work / "candidates.json"), load(work / "rejected.json")
    crit = meta["criteria"]
    L = LABELS.get(args.lang or crit.get("report_language"), LABELS["en"])
    done = _assessed(work)
    pending = [c["key"] for c in cands if c["key"] not in done]
    if pending and not args.partial:
        sys.exit(f"{len(pending)} vacancies not assessed yet (e.g. {pending[:5]}); finish them or pass --partial")
    excluded = load(work / "excluded.json") if (work / "excluded.json").exists() else []
    rows = []
    for c in cands:
        if c["key"] not in done:
            continue
        if args.new_only and c.get("seen_before"):
            continue  # already in an earlier analysis
        a = load(work / "assessments" / f"{c['key']}.json")
        why_out = excluded_by(c, a, crit)
        if why_out:
            excluded.append({**_brief(c["main"], why_out), "key": c["key"]})
            continue
        total, parts = score(c, a, crit)
        tier, why_tier = tier_for(total, a, c["main"], crit, L)
        company = {**(_company_cached(c["main"].get("company")) or {}), **(a.get("company") or {})}
        posted = max((l.get("posted_at") or "" for l in c["links"]), default="") or c["main"].get("posted_at") or ""
        rows.append({"c": c, "a": a, "score": total, "parts": parts, "tier": tier, "why_tier": why_tier,
                     "company": company, "posted": posted, "recency": recency_bucket(posted, crit)})
    rows.sort(key=lambda r: r["posted"], reverse=True)          # newest first among equals
    rows.sort(key=lambda r: sort_key(r, crit))
    stem = meta["started_at"].replace(":", "-").replace("T", "_") + "_analysis"
    out_md, out_json = Path(meta["session"]) / f"{stem}.md", Path(meta["session"]) / f"{stem}.json"
    out_md.write_text(_markdown(meta, crit, rows, rejected, pending, L), "utf-8")
    dump(out_json, {"meta": meta, "pending": pending, "rejected": rejected, "excluded": excluded,
                    "ranked": [{"n": i, "key": r["c"]["key"], "tier": r["tier"], "score": r["score"],
                                "parts": r["parts"], "links": r["c"]["links"], "record": r["c"]["main"],
                                "assessment": r["a"], "company": r["company"],
                                "seen_before": r["c"].get("seen_before")} for i, r in enumerate(rows, 1)]})
    counts = {t: sum(1 for r in rows if r["tier"] == t) for t in TIER_KEYS}
    top = [{"n": i, "title": r["c"]["main"].get("title"), "company": r["c"]["main"].get("company"),
            "score": r["score"], "why": r["a"]["summary"]} for i, r in enumerate(rows[:5], 1)]
    print(json.dumps({"report": str(out_md), "json": str(out_json), "ranked": len(rows), "tiers": counts,
                      "rejected": len(rejected), "excluded": len(excluded), "pending": len(pending),
                      "top": top}, ensure_ascii=False))
    return 0


def recency_bucket(posted: str, crit: dict) -> int:
    """0 = within the first recency_days bound, 1 = the second, … last = older / unknown."""
    bounds = crit.get("recency_days") or []
    try:
        age = (datetime.now() - datetime.fromisoformat(posted[:10])).days
    except ValueError:
        return len(bounds)
    return next((i for i, b in enumerate(bounds) if age <= b), len(bounds))


def sort_key(r: dict, crit: dict) -> tuple:
    preferred = set(crit.get("preferred_company_countries") or [])
    parts = {"recency": r["recency"], "tier": TIER_KEYS.index(r["tier"]),
             "country": 0 if r["a"].get("company_country") in preferred else 1, "score": -r["score"]}
    return tuple(parts[k] for k in crit.get("order") or ["tier", "country", "score"] if k in parts)


# --------------------------------------------------------------- labels ---

LABELS = {
    "ru": dict(
        title="Анализ вакансий", sources="Источники", search="поиск", vacancies="вакансий",
        stopped="остановлен досрочно", warning="предупреждение",
        totals="Загружено {loaded}, отсеяно жесткими фильтрами {rejected}, после склейки дублей {cands}, оценено {rows}",
        pending_note="не оценено {n}", criteria="Критерии",
        weights="Балл из 100: компания {company}, совпадение с резюме {cv}, зарплата {salary}, язык команды {team_language}, английский {english}",
        tiers={"first": "В первую очередь", "second": "Во вторую очередь", "maybe": "Может быть", "skip": "Пропустить"},
        summary_h="Сводка", n="#", tier="Ярус", score="Балл", position="Должность", company="Компания",
        type="Тип", salary="ЗП", exp="Опыт", eng="Англ.", platforms="Площадки",
        score_line="**Ярус:** {tier} · **балл {score}** (компания {company}, резюме {cv}, ЗП {salary}, язык команды {team_language}, англ. {english})",
        short="Коротко", why_tier="Почему такой ярус", links="Ссылки", seen="Уже было в анализе {d} — оценка взята оттуда",
        partial="Оценено по краткому описанию — полного текста нет", not_stated="не указано",
        f_salary="Зарплата", f_exp="Опыт", f_format="Формат", f_eng="Английский", f_team="Язык команды",
        f_stack="Стек", f_posted="Опубликовано", company_h="Компания", c_summary="О компании",
        c_type="Почему такой тип", c_size="Размер", c_rating="Рейтинг / отзывы", c_risks="Риски", c_sources="Источники",
        matches="Совпадения с резюме", gaps="Не совпадает / пробелы", pros="Плюсы", cons="Минусы", other="Еще важно",
        full="Полное описание вакансии", no_desc="_описание не получено_", rejected_h="Отсеяно жесткими фильтрами",
        reason="Причина", link="Ссылка", r_remote="не удаленка", r_salary="зарплата ниже", rec_within="Опубликовано {a}–{d} дн. назад", rec_older="Опубликовано раньше / дата неизвестна", r_salary_min="нижняя граница вилки ниже", r_exp="требуемый опыт больше", demoted="только {level}, ниже не рассматривают (я {mine}) — ярус понижен", f_level="Уровень", f_country="Страна компании", country="Страна", pending_h="Не оценено", hard_fail="не проходит жесткие условия",
        ctype={"product": "продуктовая", "outstaff": "аутстаф", "outsource": "аутсорс", "unknown": "не ясно"},
        team={"preferred": "предпочтительный", "other": "другой", "unknown": "не ясно"},
        remote={"yes": "удаленно", "no": "не удаленно", "unclear": "не ясно"},
        eng_unknown="не указан", eng_none="не нужен"),
    "uk": dict(
        title="Аналіз вакансій", sources="Джерела", search="пошук", vacancies="вакансій",
        stopped="зупинено достроково", warning="попередження",
        totals="Завантажено {loaded}, відсіяно жорсткими фільтрами {rejected}, після склеювання дублів {cands}, оцінено {rows}",
        pending_note="не оцінено {n}", criteria="Критерії",
        weights="Бал зі 100: компанія {company}, збіг із резюме {cv}, зарплата {salary}, мова команди {team_language}, англійська {english}",
        tiers={"first": "У першу чергу", "second": "У другу чергу", "maybe": "Можливо", "skip": "Пропустити"},
        summary_h="Зведення", n="#", tier="Ярус", score="Бал", position="Посада", company="Компанія",
        type="Тип", salary="ЗП", exp="Досвід", eng="Англ.", platforms="Майданчики",
        score_line="**Ярус:** {tier} · **бал {score}** (компанія {company}, резюме {cv}, ЗП {salary}, мова команди {team_language}, англ. {english})",
        short="Коротко", why_tier="Чому такий ярус", links="Посилання", seen="Вже було в аналізі {d} — оцінку взято звідти",
        partial="Оцінено за коротким описом — повного тексту немає", not_stated="не вказано",
        f_salary="Зарплата", f_exp="Досвід", f_format="Формат", f_eng="Англійська", f_team="Мова команди",
        f_stack="Стек", f_posted="Опубліковано", company_h="Компанія", c_summary="Про компанію",
        c_type="Чому такий тип", c_size="Розмір", c_rating="Рейтинг / відгуки", c_risks="Ризики", c_sources="Джерела",
        matches="Збіги з резюме", gaps="Не збігається / прогалини", pros="Плюси", cons="Мінуси", other="Ще важливо",
        full="Повний опис вакансії", no_desc="_опис не отримано_", rejected_h="Відсіяно жорсткими фільтрами",
        reason="Причина", link="Посилання", r_remote="не віддалено", r_salary="зарплата нижче", rec_within="Опубліковано {a}–{d} дн. тому", rec_older="Опубліковано раніше / дата невідома", r_salary_min="нижня межа вилки нижче", r_exp="потрібний досвід більше", demoted="лише {level}, нижче не розглядають (я {mine}) — ярус знижено", f_level="Рівень", f_country="Країна компанії", country="Країна", pending_h="Не оцінено", hard_fail="не проходить жорсткі умови",
        ctype={"product": "продуктова", "outstaff": "аутстаф", "outsource": "аутсорс", "unknown": "не ясно"},
        team={"preferred": "бажана", "other": "інша", "unknown": "не ясно"},
        remote={"yes": "віддалено", "no": "не віддалено", "unclear": "не ясно"},
        eng_unknown="не вказано", eng_none="не потрібна"),
    "en": dict(
        title="Vacancy analysis", sources="Sources", search="searched", vacancies="vacancies",
        stopped="stopped early", warning="warning",
        totals="Loaded {loaded}, dropped by hard filters {rejected}, after merging duplicates {cands}, assessed {rows}",
        pending_note="not assessed {n}", criteria="Criteria",
        weights="Score out of 100: company {company}, CV match {cv}, salary {salary}, team language {team_language}, English {english}",
        tiers={"first": "Apply first", "second": "Apply second", "maybe": "Maybe", "skip": "Skip"},
        summary_h="Summary", n="#", tier="Tier", score="Score", position="Position", company="Company",
        type="Type", salary="Salary", exp="Experience", eng="English", platforms="Platforms",
        score_line="**Tier:** {tier} · **score {score}** (company {company}, CV {cv}, salary {salary}, team language {team_language}, English {english})",
        short="In short", why_tier="Why this tier", links="Links", seen="Already in the analysis of {d} — assessment reused",
        partial="Assessed from a short description — no full text", not_stated="not stated",
        f_salary="Salary", f_exp="Experience", f_format="Format", f_eng="English", f_team="Team language",
        f_stack="Stack", f_posted="Posted", company_h="Company", c_summary="About",
        c_type="Why this type", c_size="Size", c_rating="Rating / reviews", c_risks="Risks", c_sources="Sources",
        matches="Matches with the CV", gaps="Gaps", pros="Pros", cons="Cons", other="Also important",
        full="Full vacancy description", no_desc="_no description_", rejected_h="Dropped by hard filters",
        reason="Reason", link="Link", r_remote="not remote", r_salary="salary below", rec_within="Posted {a}–{d} days ago", rec_older="Posted earlier / date unknown", r_salary_min="salary range starts below", r_exp="required experience above", demoted="{level} only, no lower levels (I am {mine}) — tier lowered", f_level="Level", f_country="Company country", country="Country", pending_h="Not assessed", hard_fail="fails a hard condition",
        ctype={"product": "product", "outstaff": "outstaff", "outsource": "outsource", "unknown": "unclear"},
        team={"preferred": "preferred", "other": "other", "unknown": "unclear"},
        remote={"yes": "remote", "no": "not remote", "unclear": "unclear"},
        eng_unknown="not stated", eng_none="not needed"),
}


def _cell(v) -> str:
    return str(v if v is not None else "").replace("|", "/").replace("\n", " ")


def _bullets(items) -> list[str]:
    return [f"  - {x}" for x in items] if items else ["  - —"]


def _eng(code: str, L: dict) -> str:
    return {"unknown": L["eng_unknown"], "none": L["eng_none"]}.get(code, code.upper())


def _markdown(meta, crit, rows, rejected, pending, L) -> str:
    out = [f"# {L['title']} — {meta['started_at'].replace('T', ' ')}", "", f"{L['sources']}:"]
    for s in meta["sources"]:
        line = (f"- **{s['platform']}** — {L['search']} {(s.get('searched_at') or '').replace('T', ' ')}, "
                f"{s.get('count')} {L['vacancies']} (`{Path(s['file']).name}`)")
        if s.get("stopped_early"):
            line += f" ⚠ {L['stopped']}: {s['stopped_early']}"
        out.append(line)
        out += [f"  - ⚠ {L['warning']}: {w}" for w in s.get("warnings") or []]
    totals = L["totals"].format(loaded=meta["loaded"], rejected=len(rejected), cands=meta["candidates_count"], rows=len(rows))
    if pending:
        totals += ", **" + L["pending_note"].format(n=len(pending)) + "**"
    out += ["", totals + ".", f"{L['criteria']}: `{meta.get('criteria_file') or '—'}`. " + L["weights"].format(**crit["weights"]), ""]
    out += [f"- {L['tiers'][t]}: **{sum(1 for r in rows if r['tier'] == t)}**" for t in TIER_KEYS]
    out += ["", f"## {L['summary_h']}", "",
            f"| {L['n']} | {L['f_posted']} | {L['tier']} | {L['score']} | {L['position']} | {L['company']} | {L['country']} | {L['type']} | {L['salary']} | {L['exp']} | {L['eng']} | {L['platforms']} |",
            "|---|---|---|---|---|---|---|---|---|---|---|---|"]
    for i, r in enumerate(rows, 1):
        m, a = r["c"]["main"], r["a"]
        plats = ", ".join(f"[{l['platform']}]({l['url']})" for l in r["c"]["links"])
        out.append(f"| {i} | {r['posted'] or '—'} | {L['tiers'][r['tier']]} | {r['score']} | {_cell(m.get('title'))} | {_cell(m.get('company'))} | "
                   f"{_cell(a.get('company_country') or '?')} | {L['ctype'][a['company_type']]} | {_cell(m.get('salary_text') or '—')} | "
                   f"{_cell(a.get('experience_required') or m.get('experience_text'))} | {_eng(a['english'], L)} | {plats} |")
    by_recency = (crit.get("order") or [None])[0] == "recency"
    bounds = crit.get("recency_days") or []
    rec_names = [L["rec_within"].format(a=(bounds[i - 1] + 1 if i else 0), d=b) for i, b in enumerate(bounds)] + [L["rec_older"]]
    cur_rec = cur_tier = None
    for i, r in enumerate(rows, 1):
        if by_recency and r["recency"] != cur_rec:
            cur_rec, cur_tier = r["recency"], None
            out += ["", f"## {rec_names[cur_rec]}"]
        if r["tier"] != cur_tier:
            cur_tier = r["tier"]
            out += ["", f"{'###' if by_recency else '##'} {L['tiers'][cur_tier]}"]
        out += _entry(i, r, L, "####" if by_recency else "###")
    if rejected:
        out += ["", f"## {L['rejected_h']}", "", f"| {L['position']} | {L['company']} | {L['reason']} | {L['link']} |", "|---|---|---|---|"]
        out += [f"| {_cell(x['title'])} | {_cell(x['company'])} | {_cell(x['reason'])} | {x['url']} |" for x in rejected]
    if pending:
        out += ["", f"## {L['pending_h']} ({len(pending)})", "", ", ".join(pending)]
    return "\n".join(out) + "\n"


def _entry(i: int, r: dict, L: dict, h: str = "###") -> list[str]:
    m, a, comp, c = r["c"]["main"], r["a"], r["company"], r["c"]
    out = ["", f"{h} {i}. {m.get('title')} — {m.get('company') or '?'}", "",
           L["score_line"].format(tier=L["tiers"][r["tier"]], score=r["score"], **r["parts"]),
           "", f"**{L['short']}:** {a['summary']}"]
    if r["why_tier"]:
        out.append(f"**{L['why_tier']}:** {r['why_tier']}")
    if c.get("seen_before"):
        out.append(f"_{L['seen'].format(d=c['seen_before'])}_")
    if not c.get("description_complete"):
        out.append(f"⚠ {L['partial']}")
    out += ["", f"- **{L['links']}:** " + " · ".join(
        f"[{l['platform']}]({l['url']})" + (f" ({l['posted_at']})" if l.get("posted_at") else "") for l in c["links"])]
    if c.get("duplicate_note"):
        out.append(f"- ⚠ {c['duplicate_note']}")
    ns = L["not_stated"]
    out += [f"- **{L['f_salary']}:** {m.get('salary_text') or ns}" + (f" — {a['salary_note']}" if a.get("salary_note") else ""),
            f"- **{L['f_exp']}:** {a.get('experience_required') or m.get('experience_text') or ns}",
            f"- **{L['f_format']}:** {L['remote'][a['remote']]}" + (f" ({m.get('location')})" if m.get("location") else ""),
            f"- **{L['f_eng']}:** {_eng(a['english'], L)}" + (f" ({m.get('english')})" if m.get("english") else ""),
            f"- **{L['f_team']}:** {L['team'][a['team_language']]}",
            f"- **{L['f_level']}:** {level_of(a, m)[0]}" + (" +" if level_of(a, m)[1] else ""),
            f"- **{L['f_country']}:** {a.get('company_country') or '?'}",
            f"- **{L['f_stack']}:** {', '.join(a['stack']) or '—'}",
            f"- **{L['f_posted']}:** {m.get('posted_at') or '—'}",
            "", f"**{L['company_h']} — {L['ctype'][a['company_type']]}**"]
    for key, field in (("c_summary", "summary"), ("c_type", "type_evidence"), ("c_size", "size"),
                       ("c_rating", "rating"), ("c_risks", "risks")):
        if comp.get(field):
            out.append(f"- {L[key]}: {comp[field]}")
    if comp.get("sources"):
        out.append(f"- {L['c_sources']}: " + ", ".join(comp["sources"]))
    out += ["", f"**{L['matches']}:**", *_bullets(a["matches"]), "", f"**{L['gaps']}:**", *_bullets(a["gaps"]),
            "", f"**{L['pros']}:**", *_bullets(a["pros"]), "", f"**{L['cons']}:**", *_bullets(a["cons"])]
    if a.get("other"):
        out += ["", f"**{L['other']}:**", *_bullets(a["other"])]
    out += ["", f"<details><summary>{L['full']}</summary>", "", m.get("description") or L["no_desc"], "", "</details>"]
    return out


# ----------------------------------------------------------------- main ---

def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    p = sub.add_parser("prepare")
    p.add_argument("--input", nargs="+", required=True, help="collector reports <ts>_<platform>.json")
    p.add_argument("--criteria", required=True, help="criteria JSON (see criteria.example.json)")
    p.add_argument("--fresh", action="store_true", help="don't reuse assessments from earlier sessions")
    s = sub.add_parser("status")
    s.add_argument("--work", required=True)
    n = sub.add_parser("next")
    n.add_argument("--work", required=True)
    n.add_argument("--n", type=int, default=8)
    n.add_argument("--shard", help="i/n — take every n-th vacancy starting at i (parallel analyzers)")
    v = sub.add_parser("save")
    v.add_argument("--work", required=True)
    v.add_argument("--input", required=True)
    rq = sub.add_parser("research-queue")
    rq.add_argument("--work", required=True)
    rq.add_argument("--min-score", type=int, default=50)
    c = sub.add_parser("company")
    c.add_argument("--name", required=True)
    cs = sub.add_parser("company-save")
    cs.add_argument("--input", required=True)
    r = sub.add_parser("render")
    r.add_argument("--work", required=True)
    r.add_argument("--partial", action="store_true", help="render even if some vacancies are not assessed")
    r.add_argument("--lang", choices=tuple(LABELS), help="report language (default: criteria.report_language)")
    r.add_argument("--new-only", action="store_true",
                   help="only vacancies not seen in earlier analyses (their old assessments are still reused)")
    args = ap.parse_args()
    return {"prepare": cmd_prepare, "status": cmd_status, "next": cmd_next, "save": cmd_save,
            "research-queue": cmd_research_queue, "company": cmd_company, "company-save": cmd_company_save,
            "render": cmd_render}[args.cmd](args)


if __name__ == "__main__":
    sys.exit(main())
