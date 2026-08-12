import os
import re
import json
import asyncio
from openai import AsyncOpenAI
from dotenv import load_dotenv

load_dotenv()

client = AsyncOpenAI(api_key=os.getenv("OPENAI_API_KEY"))


# ─── Resume Tailor ───

async def tailor_resume(resume_text: str, job_description: str, target_role: str = None) -> dict:
    """Rewrites a resume to match a specific job listing."""
    role_context = f"\nThe user is targeting: {target_role}" if target_role else ""
    
    prompt = f"""
You are an expert resume writer and ATS optimization specialist.

Your job:
1. Analyze the job description to identify the key requirements, skills, and keywords
2. Rewrite the user's resume to precisely match this job
3. Preserve all truthful information — do NOT invent experience
4. Optimize for ATS (Applicant Tracking Systems) by including exact keywords from the job listing
5. Calculate a match score (0-100) between the tailored resume and the job

Return ONLY this JSON format:
{{
  "match_score": 87,
  "tailored_resume": "The full rewritten resume text here...",
  "keywords_added": ["Python", "AWS", "Agile"],
  "tips": "Consider adding a specific project that demonstrates your cloud experience"
}}
{role_context}

Original Resume:
{resume_text}

Job Description:
{job_description}
"""
    response = await client.chat.completions.create(
        model="gpt-4o",
        response_format={"type": "json_object"},
        messages=[{"role": "user", "content": prompt}],
        temperature=0.4
    )
    return json.loads(response.choices[0].message.content)


# ─── Cover Letter Generator ───

async def generate_cover_letter(resume_text: str, job_description: str, company: str, role: str) -> str:
    """Generates a highly targeted cover letter."""
    prompt = f"""
You are an expert career coach. Write a compelling, professional cover letter.

Rules:
- Keep it under 300 words
- Open with a hook, not "I am writing to apply for..."
- Reference specific details from the job description
- Connect the candidate's experience directly to the role's requirements
- End with confidence, not desperation
- Sound human, not robotic

Company: {company}
Role: {role}

Candidate's Resume:
{resume_text}

Job Description:
{job_description}
"""
    response = await client.chat.completions.create(
        model="gpt-4o",
        messages=[{"role": "user", "content": prompt}],
        temperature=0.6
    )
    return response.choices[0].message.content


# ─── Interview Prep ───

async def generate_interview_questions(company: str, role: str, job_description: str = None,
                                       resume_text: str = None, already_asked: list = None,
                                       count: int = 10) -> list:
    """
    Writes interview questions for one specific job, with answers.

    Grounded in the real posting wherever one was found, because questions written
    from a job title alone come out generic — every backend role gets "tell me
    about a time you debugged something". The posting names the stack, the scale
    and the responsibilities the panel will actually probe.

    Answers are drawn from the CV when the question is about the candidate. When a
    question is factual or technical the answer stands on its own, and when the CV
    simply doesn't cover what's being asked, the gap is named instead of invented.

    `already_asked` lets a second round produce genuinely different questions rather
    than rephrasings of the first.
    """
    grounding = (
        f"THE ACTUAL JOB POSTING:\n{job_description[:6000]}"
        if job_description and len(job_description) > 200
        else "No posting text was available. Base the questions on what this role at "
             "this company would realistically involve, and keep them concrete."
    )

    avoid = ""
    if already_asked:
        listed = "\n".join(f"- {q}" for q in already_asked[:40])
        avoid = (
            f"\n\nALREADY ASKED — do not repeat these or rephrase them. Go deeper or "
            f"into different territory:\n{listed}"
        )

    cv_block = (
        f"CANDIDATE'S CV:\n{resume_text[:8000]}"
        if resume_text and resume_text.strip()
        else "No CV available — answer generically and say what the candidate should prepare."
    )

    prompt = f"""You are preparing a candidate for a real interview at {company} for the role of {role}.

{grounding}

{cv_block}{avoid}

Write exactly {count} questions this panel would realistically ask.

RULES
- Tie questions to what the posting actually asks for — the named technologies,
  responsibilities and seniority. If it stresses a specific skill, probe it.
- Mix categories: role-specific technical, experience/behavioural, situational,
  and questions about the company or the candidate's motivation.
- No filler. "Tell me about yourself" only if you make it specific to this role.
- Every answer must be something the candidate can actually say out loud —
  concrete and in first person, not advice about how to answer.
- Use the CV for anything about their background, and name real things from it.
- If the CV lacks what the question needs, still give the best answer you can from
  what is there, and put the missing piece in "needs_from_you".
- If a question doesn't depend on the candidate at all (a technical definition,
  say), answer it properly and set "from_cv" to false.

Return JSON:
{{
  "questions": [
    {{
      "question": "the question as it would be asked",
      "category": "Technical" | "Experience" | "Situational" | "Company" | "Motivation",
      "why_asked": "one short line on why this job in particular prompts it",
      "answer": "a complete answer the candidate can say, in first person",
      "from_cv": true or false,
      "needs_from_you": "what the candidate must supply because the CV lacks it, or null"
    }}
  ]
}}"""

    try:
        response = await client.chat.completions.create(
            model="gpt-4o",
            response_format={"type": "json_object"},
            messages=[
                {"role": "system", "content": "You are an experienced interviewer and coach. Output raw JSON only."},
                {"role": "user", "content": prompt},
            ],
            # A little variance so a second round explores new ground rather than
            # re-deriving the same list.
            temperature=0.5 if already_asked else 0.3,
        )
        return json.loads(response.choices[0].message.content).get("questions", [])
    except Exception as e:
        print(f"Error generating interview questions for {company}: {e}")
        return []


# ─── Follow-Up Email ───

async def draft_follow_up(company: str, role: str, days_since: int) -> str:
    """Drafts a professional follow-up email."""
    prompt = f"""
Write a short, professional follow-up email for a job application.

Context:
- Applied to: {company} for {role}
- Days since application: {days_since}
- This is the first follow-up

Rules:
- Keep it under 100 words
- Sound confident, not desperate
- Reference the specific role
- Add value (mention something relevant about the company)
- End with a clear but polite ask

Write ONLY the email body (no subject line needed).
"""
    response = await client.chat.completions.create(
        model="gpt-4o",
        messages=[{"role": "user", "content": prompt}],
        temperature=0.5
    )
    return response.choices[0].message.content


# ─── Job Matching ───

# One scoring call per job, so this number is the bot's dominant cost. Users open
# Top 5, 10 or 25, so scoring 60 spent roughly half the budget ranking jobs nobody
# opened. Per-job quality is unaffected — only how deep the ranking goes.
MAX_SCORED_JOBS = 30
# Measured against a 30,000 tokens-per-minute account limit: at ~1,900 tokens a
# call, 8 in flight overruns it within seconds and jobs come back unscored. 4 keeps
# a search inside the limit, and the backoff in score_job_against_profile absorbs
# whatever still slips through.
SCORING_CONCURRENCY = 4

# Measured on 8 real postings plus 4 calibration cases, gpt-4o-mini is NOT a drop-in
# here: it scored a near-perfect match 74 instead of 87, compressed the whole scale
# (74-24 against 87-8), disagreed by 11 points on average, swung 11 points between
# identical runs, and picked a different top job and a different top 3. Ranking is
# the product, so the saving isn't worth it. Left configurable, but don't switch
# without re-running that comparison.
SCORING_MODEL = os.getenv("SCORING_MODEL", "gpt-4o")

GRADES = (
    (85, "Excellent Match"), (70, "Strong Match"),
    (50, "Good Match"), (35, "Weak Match"), (0, "Poor Match"),
)

# A must-have outweighs a nice-to-have, and partial credit is real credit —
# "some exposure to Kubernetes" is not the same as never having touched it.
_IMPORTANCE_WEIGHT = {"must": 2.2, "nice": 1.0}
_VERDICT_CREDIT = {"met": 1.0, "partial": 0.6, "missing": 0.0}

# Deliberately mild. Seniority is already priced in by the requirements themselves
# ("4+ years experience" shows up as a missed must-have), so a steep multiplier here
# charges the candidate twice for the same gap and squashes every underqualified
# match down into a single indistinguishable band.
_SENIORITY_FACTOR = {"below": 0.88, "match": 1.0, "above": 0.95}

_resume_profile_cache = {}

# Scores keyed by (job id, CV fingerprint). A job's score is a pure function of the
# posting and the CV, so re-deriving it wastes money and — because the model isn't
# perfectly deterministic even at temperature 0 — can hand back a slightly different
# number for the same job, which reads as guesswork. Cached, a re-run of the same
# search is both free and identical.
_score_cache = {}
_SCORE_CACHE_MAX = 5000


def _cache_score(key, result: dict):
    if len(_score_cache) >= _SCORE_CACHE_MAX:
        _score_cache.clear()
    _score_cache[key] = result


def grade_for(score: int) -> str:
    """Single source of truth, so the grade can never contradict the number."""
    for threshold, label in GRADES:
        if score >= threshold:
            return label
    return "Poor Match"


async def parse_resume(resume_text: str) -> dict:
    """
    Reads the CV once into a structured profile.

    Every job is then judged against this one interpretation. Previously each
    parallel batch re-read the raw CV itself, so a job's score depended on which
    batch it happened to land in — the same job could swing 15 points between runs.
    Results are cached by CV content, so repeat searches don't pay for this again.
    """
    if not resume_text or not resume_text.strip():
        return {}

    key = hash(resume_text)
    if key in _resume_profile_cache:
        return _resume_profile_cache[key]

    prompt = f"""Extract this candidate's profile as JSON. Record only what the CV
actually supports — never infer a skill that isn't evidenced, and never inflate
seniority. Internships and freelance work count as real experience but must be
labelled as such.

Return exactly:
{{
  "seniority": "intern" | "entry" | "junior" | "mid" | "senior" | "lead" | "executive",
  "total_years_experience": number,
  "current_or_last_title": "string",
  "past_titles": ["string"],
  "core_skills": ["skills used substantially, with real evidence in the CV"],
  "familiar_skills": ["skills touched briefly, coursework, or self-taught"],
  "tools": ["frameworks, platforms, databases, tooling"],
  "practices": ["working practices evidenced in the CV, named plainly: CI/CD, unit testing, code review, Agile, REST API design, containerisation"],
  "domains": ["industries or problem areas worked in"],
  "education": [{{"degree": "string", "field": "string", "institution": "string", "year": "string"}}],
  "certifications": ["string"],
  "languages": ["spoken languages"],
  "location": "string or null",
  "notable_achievements": ["quantified results if present"]
}}

CV:
{resume_text[:20000]}
"""
    try:
        response = await client.chat.completions.create(
            model="gpt-4o",
            response_format={"type": "json_object"},
            messages=[
                {"role": "system", "content": "You extract structured CV data. Output raw JSON only."},
                {"role": "user", "content": prompt},
            ],
            temperature=0,
        )
        profile = json.loads(response.choices[0].message.content)
        _resume_profile_cache[key] = profile
        return profile
    except Exception as e:
        print(f"Error parsing resume: {e}")
        return {}


def _profile_brief(profile: dict, fallback_resume: str = "") -> str:
    """Compact, stable rendering of the CV for the scoring prompt."""
    if not profile:
        return f"RAW CV (could not be parsed):\n{(fallback_resume or 'No CV uploaded')[:4000]}"

    def join(key, limit=25):
        vals = profile.get(key) or []
        return ", ".join(str(v) for v in vals[:limit]) if vals else "none stated"

    edu = "; ".join(
        f"{e.get('degree','')} {e.get('field','')} ({e.get('year','')})".strip()
        for e in (profile.get("education") or [])[:4]
    ) or "none stated"

    return (
        f"Location: {profile.get('location') or 'not stated'}\n"
        f"Seniority: {profile.get('seniority', 'unknown')}\n"
        f"Total experience: {profile.get('total_years_experience', 'unknown')} years\n"
        f"Current/last title: {profile.get('current_or_last_title', 'unknown')}\n"
        f"Past titles: {join('past_titles')}\n"
        f"Core skills (strong evidence): {join('core_skills')}\n"
        f"Familiar skills (light exposure): {join('familiar_skills')}\n"
        f"Tools: {join('tools')}\n"
        # Without this, "CI pipelines on GitLab" compressed to just "GitLab" and a
        # CI/CD requirement came back "missing" even though the CV evidenced it.
        f"Practices: {join('practices')}\n"
        f"Domains: {join('domains')}\n"
        f"Education: {edu}\n"
        f"Certifications: {join('certifications')}\n"
        f"Languages: {join('languages')}\n"
        f"Achievements: {join('notable_achievements', 6)}"
    )


# Paragraphs matching these are pure boilerplate. They're a large share of a typical
# posting and contain nothing that affects whether a candidate fits, so paying to
# send them on every one of the scoring calls is waste.
_BOILERPLATE = re.compile(
    r"equal opportunit|reasonable accommodation|e-?verify|drug[-\s]free|"
    r"what we offer|benefits (include|package)|perks|why join|about (us|the company|our)|"
    r"our (mission|values|culture|story)|diversity|inclusion belong|"
    r"protected veteran|without regard to race|applicants will receive|"
    r"click (here|apply)|follow us on|privacy (policy|notice)|cookie",
    re.I,
)

_REQUIREMENT_MARKERS = (
    "requirement", "qualification", "what you", "who you are", "you have",
    "skills", "we are looking for", "must have", "your profile", "about you",
    "responsibilit", "experience", "you will", "nice to have", "preferred",
)


def _paragraph_score(para: str) -> int:
    """How likely a paragraph is to contain something a candidate is judged on."""
    low = para.lower()
    score = 0
    if any(m in low for m in _REQUIREMENT_MARKERS):
        score += 3
    if re.search(r"\d+\+?\s*(years|yrs)", low):
        score += 3
    if re.search(r"(^|\n)\s*[-•*·●]|\n\s*\d+[.)]\s", para):
        score += 2                      # bullet lists are where requirements live
    if re.search(r"\b(must|required|proficien|familiar|degree|bachelor|fluent|certif)", low):
        score += 2
    return score


def _is_heading(line: str) -> bool:
    """A short line with no sentence punctuation — 'Requirements', 'The Role'."""
    stripped = line.strip()
    return 0 < len(stripped) <= 60 and (stripped.endswith(":") or not stripped.endswith("."))


def _requirements_excerpt(description: str, budget: int = 5000) -> str:
    """
    Trims a description down to the part that decides the score.

    Selection is per line, not per paragraph: LinkedIn renders a posting as dozens
    of single-newline lines inside one block, so anything splitting on blank lines
    sees a single giant paragraph, ranks nothing, and falls back to lopping off the
    end — which is exactly where requirements usually are.

    Each line is scored for requirement-density, and a requirements-style heading
    passes a bonus down to the lines beneath it so bare bullets ("- Kafka") are
    kept along with the header that gives them meaning. Boilerplate — benefits, EEO
    statements, company mission — is dropped outright. Surviving lines are
    reassembled in their original order.

    The budget is set from measurement, not taste: across 30 real postings, 5000
    loses 3 requirement lines where 3800 loses 9, for about 20% more tokens. Going
    further pays badly — 6500 recovers only one more line for another 10%. Most
    postings fit well under the cap and lose nothing at all.
    """
    text = (description or "").strip()
    if not text:
        return ""

    lines = [l.rstrip() for l in text.split("\n")]
    if sum(len(l) + 1 for l in lines) <= budget:
        return "\n".join(l for l in lines if l.strip())

    scores, section = [], 0
    for line in lines:
        if not line.strip():
            scores.append(-1)
            continue
        if _BOILERPLATE.search(line):
            section = -5                      # everything under this heading too
            scores.append(-5)
            continue
        if _is_heading(line):
            low = line.lower()
            section = 4 if any(m in low for m in _REQUIREMENT_MARKERS) else 0
        scores.append(_paragraph_score(line) + section)

    # Best lines first; shorter wins ties so the budget buys more of them.
    order = sorted(range(len(lines)), key=lambda i: (-scores[i], len(lines[i])))

    chosen, used = set(), 0
    for i in order:
        if scores[i] < 0:
            continue
        cost = len(lines[i]) + 1
        if used + cost > budget:
            continue
        chosen.add(i)
        used += cost

    if not chosen:
        return text[:budget]

    out, prev = [], -1
    for i in sorted(chosen):
        if not lines[i].strip():
            continue
        if prev >= 0 and i != prev + 1:
            out.append("[...]")
        out.append(lines[i])
        prev = i
    return "\n".join(out)


_DEGREE_RANK = {"phd": 4, "doctorate": 4, "dphil": 4,
                "master": 3, "msc": 3, "m.s": 3, "ma ": 3, "mba": 3, "meng": 3,
                "bachelor": 2, "bsc": 2, "b.s": 2, "bs ": 2, "ba ": 2, "beng": 2, "undergraduate": 2,
                "associate": 1, "diploma": 1}

_DEGREE_WORDS = re.compile(r"\b(phd|doctorate|dphil|master'?s?|msc|m\.s|mba|meng|"
                           r"bachelor'?s?|bsc|b\.s|beng|associate|diploma|degree)\b", re.I)


def _degree_rank(text: str, lowest: bool = False) -> int:
    """
    Highest degree level named in the text — or the lowest, for requirements.

    "Bachelor's or Master's in CS" lists alternatives, so the bachelor's is what
    actually has to be cleared. Taking the maximum there rejected candidates who
    met the requirement outright.
    """
    low = (text or "").lower()
    found = [rank for key, rank in _DEGREE_RANK.items() if key in low]
    if not found:
        return 0
    return min(found) if lowest else max(found)


def _reconcile_degrees(requirements: list, profile: dict) -> list:
    """
    Corrects degree verdicts from the parsed education, in code.

    Whether a BSc in Computer Science satisfies "bachelor's in CS or related" is a
    lookup, not a judgement — but the model kept answering "partial" on degrees the
    candidate plainly holds, which quietly cost real points. Structured data the
    parser already extracted decides it instead.
    """
    education = profile.get("education") or []
    if not education:
        return requirements

    held_rank = max((_degree_rank(f"{e.get('degree','')} ") for e in education), default=0)
    held_fields = " ".join(f"{e.get('degree','')} {e.get('field','')}" for e in education).lower()

    for req in requirements:
        text = str(req.get("text", ""))
        if not _DEGREE_WORDS.search(text):
            continue

        needed = _degree_rank(text, lowest=True)
        if not needed or not held_rank:
            continue

        low = text.lower()
        # "or related field" means the field barely constrains anything.
        related_ok = "related" in low or "equivalent" in low or any(
            w in held_fields for w in re.findall(r"[a-z]{4,}", low)
        )
        if held_rank >= needed and related_ok:
            req["verdict"] = "met"
            req["evidence"] = next(
                (f"{e.get('degree','')} {e.get('field','')}".strip() for e in education), "degree held"
            )
        elif held_rank < needed:
            req["verdict"] = "missing"
    return requirements


# Meeting a job's stated bar is not the same as being a candidate for it. An
# entry-level sales role that asks only for good English and a willingness to learn
# is "met" by almost any graduate — which is how a cybersecurity CV scored 95% on
# cold-calling. Field fit keeps those out of the top of a search without hiding them.
_FIELD_FACTOR = {"same": 1.0, "adjacent": 0.85, "different": 0.55}


def _score_from_requirements(requirements: list, seniority_fit: str, field_fit: str = None) -> int:
    """
    Turns per-requirement verdicts into the percentage.

    Deliberately arithmetic rather than another model call: asking a model to
    invent a number produced scores that drifted run to run and didn't follow
    from the requirements being displayed. Computing it here means the percentage
    and the matched/missing lists can never disagree.
    """
    total = credit = 0.0
    for req in requirements:
        weight = _IMPORTANCE_WEIGHT.get(str(req.get("importance", "nice")).lower(), 1.0)
        total += weight
        credit += weight * _VERDICT_CREDIT.get(str(req.get("verdict", "missing")).lower(), 0.0)

    if total <= 0:
        return 0

    score = (credit / total) * 100
    score *= _SENIORITY_FACTOR.get(str(seniority_fit or "match").lower(), 1.0)
    score *= _FIELD_FACTOR.get(str(field_fit or "same").lower(), 1.0)
    return max(0, min(100, round(score)))


async def score_job_against_profile(profile_brief: str, job: dict, search_query: str = "",
                                    profile: dict = None) -> dict:
    """
    Judges one job on its own. Scoring each job in isolation removes the batch
    effects that made scores depend on a job's neighbours, and means a malformed
    response costs one job instead of five.
    """
    description = (job.get("description") or "").strip()
    has_description = len(description) > 120

    # No posting, no score. Guessing requirements from the title produced confident
    # nonsense — a cybersecurity CV was told a cold-calling job was a 100% match
    # because the invented requirements happened to be things the CV satisfied.
    # Saying "I couldn't read this one" is both honest and free.
    if not has_description:
        return {
            "score": 0, "grade": "", "scored": False, "unreadable": True,
            "no_description": True, "requirements": [],
            "matched_count": 0, "partial_count": 0, "missing_count": 0,
            "main_gaps": [], "matched_qualifications": [],
            "partial_qualifications": [], "missing_qualifications": [],
            "verdict_summary": "", "seniority_fit": "", "field_fit": "",
        }

    criteria = []
    if job.get("employment_type"):
        criteria.append(f"Employment type: {job['employment_type']}")
    if job.get("seniority_level") and job["seniority_level"].lower() != "not applicable":
        criteria.append(f"Seniority level: {job['seniority_level']}")
    criteria_text = "\n".join(criteria)

    if has_description:
        job_block = (
            f"Title: {job.get('title')}\nCompany: {job.get('company')}\n"
            f"Location: {job.get('location')}\n{criteria_text}\n\n"
            f"Full posting:\n{_requirements_excerpt(description)}"
        )
        instruction = (
            "List what this job actually demands of the candidate. Read BOTH the "
            "skills/requirements section AND the responsibilities — a duty implies the "
            "ability to perform it, so 'make outbound cold calls' is a requirement.\n"
            "Include the capabilities the role is genuinely built on, even when they are "
            "soft skills: for a sales role, objection handling, resilience and CRM "
            "discipline ARE the requirements. Only drop wording so vague it would appear "
            "in any posting for any job ('hard worker', 'team player').\n"
            "Max 10, merge duplicates, invent nothing, and never list perks, benefits or "
            "what the company offers — a requirement is something the CANDIDATE must have."
        )

    # Deliberately terse: this prompt is sent once per job, so every word here is
    # paid for dozens of times per search. All the judging rules are kept — only
    # the wording is compressed.
    prompt = f"""Recruiting screener. Judge how well ONE candidate fits ONE job.

CANDIDATE
{profile_brief}

JOB
{job_block}

TASK
{instruction}

Verdict per requirement, using ONLY the candidate profile:
met = profile clearly satisfies it | partial = adjacent experience or slightly short on years | missing = no evidence.
importance: "must" = stated hard requirement, "nice" = preferred/bonus.

Rules: don't award "met" without evidence, or "missing" when the profile plainly shows it.
An equivalent skill is "met" (Flask satisfies "a Python web framework"; PostgreSQL satisfies "SQL").
Degrees: "met" if Education lists that level or higher in a related field (a BSc in Computer
Science meets "bachelor's in CS or related", and a PhD meets a master's requirement). Only use
"missing" when the required level is genuinely higher than anything held. Never "partial" for a
degree the candidate clearly holds.
A named tool implies its practice: GitLab/Jenkins/GitHub Actions = CI/CD, pytest/JUnit = testing.
The candidate is already searching this region — for a location requirement use "met" if their
stated location fits, "partial" if unstated. Never "missing".
seniority_fit: is the CANDIDATE "below", "match" or "above" this role's level?
field_fit: is this job in the candidate's own field? "same" (their profession),
"adjacent" (a realistic sideways move — backend dev to data engineer), or
"different" (a career change — a cybersecurity graduate applying to phone sales).
Judge the profession, not whether they could scrape through the requirements.

JSON:
{{"requirements":[{{"text":"short requirement","importance":"must|nice","verdict":"met|partial|missing","evidence":"max 8 words"}}],
"seniority_fit":"below|match|above","field_fit":"same|adjacent|different","verdict_summary":"one short sentence"}}"""

    # A whole search fires these within seconds, which is exactly the shape that
    # trips a tokens-per-minute limit. Without backoff the job is simply dropped
    # and shown as unscored, so the user silently loses results whenever the
    # account is busy.
    for attempt in range(4):
        try:
            response = await client.chat.completions.create(
                model=SCORING_MODEL,
                response_format={"type": "json_object"},
                messages=[{"role": "user", "content": prompt}],
                temperature=0,
                # Output is the expensive half. Ten requirements with short evidence
                # fit comfortably; the cap only stops a runaway response.
                max_tokens=700,
            )
            data = json.loads(response.choices[0].message.content)
            requirements = data.get("requirements") or []
            if not requirements:
                raise ValueError("no requirements returned")

            requirements = _reconcile_degrees(requirements, profile or {})

            score = _score_from_requirements(requirements, data.get("seniority_fit"),
                                             data.get("field_fit"))


            matched = [r["text"] for r in requirements if str(r.get("verdict")).lower() == "met"]
            partial = [r["text"] for r in requirements if str(r.get("verdict")).lower() == "partial"]
            missing = [r["text"] for r in requirements if str(r.get("verdict")).lower() == "missing"]
            gaps = [r["text"] for r in requirements
                    if str(r.get("verdict")).lower() == "missing"
                    and str(r.get("importance")).lower() == "must"] or missing

            return {
                "score": score,
                "grade": grade_for(score),
                "requirements": requirements,
                "matched_qualifications": matched,
                "partial_qualifications": partial,
                "missing_qualifications": missing,
                # Counts are derived from the lists so the card can never show
                # "4 matched" next to three bullet points.
                "matched_count": len(matched),
                "partial_count": len(partial),
                "missing_count": len(missing),
                "main_gaps": gaps[:3],
                "verdict_summary": data.get("verdict_summary", ""),
                "seniority_fit": data.get("seniority_fit", "match"),
                "field_fit": data.get("field_fit", "same"),
                "no_description": not has_description,
                "scored": True,
            }
        except Exception as e:
            rate_limited = "429" in str(e) or "rate_limit" in str(e).lower()
            if attempt < 3:
                # A per-minute limit only clears with time, so retrying immediately
                # just burns the remaining attempts. Back off, and harder when the
                # error says the account is over its quota.
                await asyncio.sleep((4 if rate_limited else 1) * (attempt + 1))
                continue
            print(f"Error scoring '{job.get('title')}': {e}")

    return {"score": 0, "grade": "Poor Match", "scored": False, "score_error": True,
            "matched_count": 0, "missing_count": 0, "main_gaps": [],
            "matched_qualifications": [], "missing_qualifications": [],
            "no_description": not has_description}


async def match_jobs_to_profile(user_profile: dict, jobs: list, search_query: str = "") -> list:
    """
    Scores jobs against the user's CV and returns them ranked by fit.

    The CV is parsed once into a structured profile, then each job is judged
    independently against it: the model enumerates and rules on the posting's
    requirements, and the percentage is computed from those rulings here in Python.
    """
    if not jobs:
        return []

    from linkedin_services import fetch_job_details
    import concurrent.futures
    import asyncio

    # With no CV there is nothing to match against; scoring anyway would label
    # every job 0%, which reads as "these jobs are bad" rather than "I don't know
    # anything about you yet". Hand them back in LinkedIn's own order instead.
    if not (user_profile.get('resume_text') or "").strip():
        for job in jobs:
            job['scored'] = False
            job['no_cv'] = True
            job.setdefault('score', 0)
        return jobs

    eval_jobs = jobs[:MAX_SCORED_JOBS]

    def fetch_desc(job):
        # Jobs already enriched during filtering keep their description and criteria.
        if not job.get('description'):
            job.update(fetch_job_details(job.get('job_id') or job.get('url', '')))
        return job

    loop = asyncio.get_running_loop()
    with concurrent.futures.ThreadPoolExecutor(max_workers=8) as executor:
        futures = [loop.run_in_executor(executor, fetch_desc, job) for job in eval_jobs]
        eval_jobs = list(await asyncio.gather(*futures))

    # A search fires ~100 LinkedIn requests against a ~3/second ceiling, so some
    # descriptions come back empty purely from throttling. Scoring those means
    # inventing requirements from the title, which is far worse than waiting — so
    # anything still missing gets one slower, serial retry.
    missing = [j for j in eval_jobs if not (j.get('description') or '').strip()]
    if missing:
        print(f"Retrying {len(missing)} descriptions LinkedIn didn't return first time")
        await asyncio.sleep(2)
        for job in missing:
            await loop.run_in_executor(None, fetch_desc, job)
        still = sum(1 for j in missing if not (j.get('description') or '').strip())
        if still:
            print(f"{still} job(s) unreadable — shown without a score")

    resume_text = user_profile.get('resume_text') or ""
    profile = await parse_resume(resume_text)
    brief = _profile_brief(profile, resume_text)

    resume_fingerprint = hash(resume_text)
    semaphore = asyncio.Semaphore(SCORING_CONCURRENCY)

    async def score_one(job):
        key = (job.get('job_id') or job.get('url', ''), resume_fingerprint)
        cached = _score_cache.get(key)
        if cached is not None:
            job.update(cached)
            return job

        async with semaphore:
            result = await score_job_against_profile(brief, job, search_query, profile=profile)
        # Only successful scores are cached; a failed one should be retried next time
        # rather than pinned as a permanent 0%.
        if result.get('scored'):
            _cache_score(key, result)
        job.update(result)
        return job

    scored_jobs = list(await asyncio.gather(*[score_one(j) for j in eval_jobs]))

    # Jobs with a real posting outrank ones scored from the title alone, since an
    # unreadable postings carry no score and must sort last.
    scored_jobs.sort(
        key=lambda j: (j.get('scored', False), not j.get('no_description', False), j.get('score', 0)),
        reverse=True,
    )

    # Anything past the scoring cap is carried through unscored and marked as such,
    # rather than being labelled a 0% match.
    for extra in jobs[MAX_SCORED_JOBS:]:
        extra.setdefault('score', 0)
        extra.setdefault('scored', False)
        scored_jobs.append(extra)

    return scored_jobs


# ─── Intent Classification & Search Parsing ───

async def parse_job_search_query(user_query: str, default_location: str = "Worldwide") -> dict:
    """
    Parses user natural language query into clean LinkedIn search parameters.
    Preserves role type (intern/senior/junior) in keywords for accurate LinkedIn matching.
    Corrects typos and understands bad English automatically.
    """
    prompt = f"""
    You are an expert search parser for job queries on LinkedIn.
    The user typed: "{user_query}"
    
    IMPORTANT RULES:
    - Correct ALL typos automatically (e.g. "soiftwaeengner" → "Software Engineer", "enginerr" → "Engineer", "cybersecuirty" → "Cybersecurity")
    - Understand broken/bad English and extract the intended job search
    - "keywords" MUST include the FULL role title including seniority/type (e.g. "Software Engineer Intern", "Senior Data Scientist", "Junior Backend Developer") — do NOT strip words like "intern", "senior", "junior" from keywords!
    - For location: be very specific to avoid LinkedIn defaulting to US cities (e.g. "Beirut, Lebanon" NOT just "Lebanon", "Dubai, UAE" NOT just "Dubai")
    - If no location is mentioned in the query, return null for location (the system will use the user's saved location)
    
    Extract these fields:
    1. "keywords": Full corrected job title including seniority/type (e.g. "Software Engineer Intern", "Senior Cybersecurity Engineer"). KEEP all role-describing words.
    2. "location": Specific "City, Country" format, or null if not mentioned in query.
    3. "date_posted": "r86400" (past 24h), "r604800" (past week), "r2592000" (past month), or null if not specified.
    4. "work_type": "1" (On-site), "2" (Remote), "3" (Hybrid), or null if not specified.
    5. "job_type": "F" (Full-time), "C" (Contract), "I" (Internship), "P" (Part-time), or null.
    6. "experience_level": "1" (Internship), "2" (Entry level), "3" (Associate), "4" (Mid-Senior level), "5" (Director), "6" (Executive), or null.
    
    Return ONLY raw JSON with these 6 keys, no extra text.
    """
    try:
        response = await client.chat.completions.create(
            model="gpt-4o-mini",
            messages=[
                {"role": "system", "content": "You are a precise JSON extractor. Output ONLY raw JSON, no markdown."},
                {"role": "user", "content": prompt}
            ],
            temperature=0.0
        )
        content = response.choices[0].message.content.strip()
        if content.startswith("```"):
            content = content.replace("```json", "").replace("```", "").strip()
        parsed = json.loads(content)
        
        # Apply location fallback: if null/empty, use default_location
        if not parsed.get("location"):
            parsed["location"] = default_location if default_location else "Worldwide"
            
        return parsed
    except Exception as e:
        print(f"Error parsing search query: {e}")
        return {"keywords": user_query, "location": default_location or "Worldwide"}

async def classify_user_intent(text: str) -> str:
    """
    Classifies raw user text into one of the bot's core actions.
    Returns one of: FIND_JOBS, TAILOR_RESUME, TRACK_APP, GENERAL_CHAT
    """
    prompt = f"""
    You are an intelligent routing assistant for a job-hunting Telegram bot.
    The user sent the following message:
    "{text}"
    
    Classify the intent into EXACTLY ONE of these strings:
    FIND_JOBS - if they are looking for jobs, e.g. "software engineer in london", "remote jobs", "find me work"
    TAILOR_RESUME - if they paste a job description or ask to tailor/write a resume/CV
    TRACK_APP - if they want to save or track a job application
    GENERAL_CHAT - if it's just a general question or greeting
    
    Return ONLY the exact string, nothing else.
    """
    
    try:
        response = await client.chat.completions.create(
            model="gpt-4o-mini",
            messages=[{"role": "user", "content": prompt}],
            temperature=0.3,
            max_tokens=20
        )
        return response.choices[0].message.content.strip()
    except Exception as e:
        print(f"Intent error: {e}")
        return "GENERAL_CHAT"

# ─── Email Parsing ───

def _extract_domain_company(sender: str) -> str:
    """Extracts a best-guess company name from an email sender string."""
    import re
    # Extract email address from sender like "Murex Careers <noreply@murex.com>"
    match = re.search(r'[\w.+-]+@([\w.-]+)', sender)
    if not match:
        return None
    domain = match.group(1).lower()
    # Strip common email service domains — these aren't the actual company
    generic = {
        'gmail.com', 'yahoo.com', 'hotmail.com', 'outlook.com',
        'workday.com', 'myworkday.com', 'greenhouse.io', 'lever.co',
        'smartrecruiters.com', 'jobvite.com', 'icims.com', 'breezy.hr',
        'bamboohr.com', 'recruitee.com', 'taleo.net', 'successfactors.com',
        'notifications.linkedin.com', 'linkedin.com', 'indeed.com',
        'mail.indeed.com', 'jobsforhumanity.com', 'noreply.com'
    }
    if domain in generic:
        return None
    # For subdomains like "careers.murex.com" → take the main part "murex"
    parts = domain.split('.')
    if len(parts) >= 2:
        company = parts[-2]  # e.g. "murex" from "murex.com"
        return company.capitalize()
    return None

async def parse_job_email(email_subject: str, email_sender: str, email_body: str) -> dict:
    """Parses a raw email to determine if it's a job update and extracts data."""
    # Pre-extract sender name and domain as fallback for company
    sender_name = email_sender.split('<')[0].strip().strip('"')
    domain_company = _extract_domain_company(email_sender)

    prompt = f"""
You are an AI assistant that tracks job applications from emails.

Your job is to determine if this email is related to a job application and extract structured data.

=== RULES ===

1. is_job_email: true if the email is ANY of: application confirmation, rejection, assessment/test invite, interview invite, job offer, or any update about a job application.
   Set to false ONLY for marketing newsletters, spam, or emails clearly unrelated to a specific job application.

2. company: Extract the hiring company's name.
   - Read the email body carefully for the company name.
   - If not found in the body, use the sender name: "{sender_name}"
   - If still unclear, use the domain hint: "{domain_company or 'Unknown Company'}"
   - NEVER return null for company if it's a job email — always provide a best guess.

3. role: The job title. If not mentioned, use "Unknown Role".

4. status: Must be exactly one of:
   - "applied" → any confirmation of receiving/submitting an application
   - "assessment" → invitation to complete a test, quiz, coding challenge, exam, or task. IMPORTANT: Even if the subject line says "Interview Opportunity", if the body asks the user to complete an assessment, test, or HackerRank, you MUST classify it as "assessment".
   - "interview" → ONLY if the email directly invites the candidate to speak/meet with a recruiter or team member (phone call, video call, in-person). Not when it just mentions interview as a future possibility.
   - "offer" → job offer extended
   - "rejected" → application declined or not moved forward
   - "responded" → a human replied asking for more info (not a standard update)

=== EMAIL DATA ===

Subject: {email_subject}
From: {email_sender}
Body:
{email_body[:1800]}

=== OUTPUT FORMAT ===
Return ONLY valid JSON:
{{
  "is_job_email": true,
  "company": "Murex",
  "role": "Junior QA Analyst",
  "status": "applied"
}}
"""
    response = await client.chat.completions.create(
        model="gpt-4o",
        response_format={"type": "json_object"},
        messages=[{"role": "user", "content": prompt}],
        temperature=0.1
    )
    result = json.loads(response.choices[0].message.content)
    # Final safety net: if AI returned null company but it IS a job email, use domain fallback
    if result.get("is_job_email") and not result.get("company"):
        result["company"] = domain_company or sender_name or "Unknown Company"
    return result


