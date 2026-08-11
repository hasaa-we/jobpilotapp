import os
import json
from openai import AsyncOpenAI
from dotenv import load_dotenv

load_dotenv()

client = AsyncOpenAI(api_key=os.getenv("OPENAI_API_KEY"))

# ─── Resume Scorer ───

async def score_resume(resume_text: str) -> dict:
    """Scores a resume 1-100 and gives specific fixes."""
    prompt = f"""
You are an expert resume reviewer who has hired 10,000+ people.

Score this resume from 1 to 100 and provide exactly 5 specific, actionable fixes.

Return ONLY this JSON format:
{{
  "score": 72,
  "verdict": "Good foundation but needs stronger impact metrics",
  "fixes": [
    "Add quantified achievements (e.g., 'Increased revenue by 30%')",
    "Remove the objective statement — use a professional summary instead",
    "Add specific technologies to each role",
    "Remove graduation date to avoid age bias",
    "Add a 'Key Achievements' section at the top"
  ]
}}

Resume:
{resume_text}
"""
    response = await client.chat.completions.create(
        model="gpt-4o",
        response_format={"type": "json_object"},
        messages=[{"role": "user", "content": prompt}],
        temperature=0.3
    )
    return json.loads(response.choices[0].message.content)


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

MAX_SCORED_JOBS = 60
SCORING_CONCURRENCY = 8

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
        f"Domains: {join('domains')}\n"
        f"Education: {edu}\n"
        f"Certifications: {join('certifications')}\n"
        f"Languages: {join('languages')}\n"
        f"Achievements: {join('notable_achievements', 6)}"
    )


def _requirements_excerpt(description: str, budget: int = 7000) -> str:
    """
    Trims a description to fit the prompt without losing the requirements.

    Requirements normally sit at the *end* of a posting, so blind truncation
    (the old behaviour, at 3000 chars) cut off exactly the section being scored.
    """
    text = (description or "").strip()
    if len(text) <= budget:
        return text

    markers = ("requirement", "qualification", "what you", "who you are", "you have",
               "skills", "we are looking for", "must have", "your profile", "about you")
    lowered = text.lower()
    cut = next((lowered.find(m) for m in markers if lowered.find(m) > 0), -1)

    if cut > 0:
        head = text[: budget // 3]
        tail = text[cut: cut + (budget - budget // 3)]
        return f"{head}\n\n[...]\n\n{tail}"
    return text[:budget]


def _score_from_requirements(requirements: list, seniority_fit: str) -> int:
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
    return max(0, min(100, round(score)))


async def score_job_against_profile(profile_brief: str, job: dict, search_query: str = "") -> dict:
    """
    Judges one job on its own. Scoring each job in isolation removes the batch
    effects that made scores depend on a job's neighbours, and means a malformed
    response costs one job instead of five.
    """
    description = (job.get("description") or "").strip()
    has_description = len(description) > 120

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
            "List the substantive requirements the posting actually states — skills, "
            "years of experience, education, certifications, languages, location and "
            "work authorisation. Do not invent requirements it does not mention.\n"
            "Give at most 12, merging duplicates and near-duplicates. Skip generic "
            "filler such as 'team player', 'good communication' or 'attention to "
            "detail' unless the role is genuinely built around it — padding the list "
            "with boilerplate distorts the percentage."
        )
    else:
        job_block = (
            f"Title: {job.get('title')}\nCompany: {job.get('company')}\n"
            f"Location: {job.get('location')}\n{criteria_text}\n\n"
            f"Full posting: NOT AVAILABLE."
        )
        instruction = (
            "The posting text is unavailable, so infer the 4-6 requirements this "
            "role type normally carries and mark every one of them inferred:true."
        )

    prompt = f"""You are a precise recruiting screener. Decide how well ONE candidate fits ONE job.

CANDIDATE PROFILE
{profile_brief}

JOB
{job_block}

TASK
{instruction}

For each requirement decide, using ONLY the candidate profile above:
- "met"     — the profile clearly satisfies it
- "partial" — related or adjacent experience, or slightly short on years
- "missing" — no evidence in the profile

Mark "importance": "must" for stated hard requirements, "nice" for preferred or bonus items.
Judge honestly. Do not award "met" for a skill the profile does not evidence, and do
not mark something "missing" when the profile plainly shows it.

Treat a closely equivalent skill as "met", not "missing" — Flask experience satisfies
"a Python web framework", and PostgreSQL satisfies "SQL". Reserve "missing" for a
genuine absence.
The candidate is already searching in this job's region, so never mark a location
requirement "missing" merely because the profile omits a location; use "met" when the
stated location fits and "partial" when it is simply unstated.

Also judge "seniority_fit": is the CANDIDATE "below", "match", or "above" this role's level?

Return JSON:
{{
  "requirements": [
    {{"text": "short requirement", "importance": "must"|"nice",
      "verdict": "met"|"partial"|"missing", "evidence": "brief why", "inferred": true|false}}
  ],
  "seniority_fit": "below"|"match"|"above",
  "verdict_summary": "one sentence on the overall fit"
}}"""

    for attempt in range(2):
        try:
            response = await client.chat.completions.create(
                model="gpt-4o",
                response_format={"type": "json_object"},
                messages=[
                    {"role": "system", "content": "You are a precise recruiting screener. Output raw JSON only."},
                    {"role": "user", "content": prompt},
                ],
                temperature=0,
            )
            data = json.loads(response.choices[0].message.content)
            requirements = data.get("requirements") or []
            if not requirements:
                raise ValueError("no requirements returned")

            score = _score_from_requirements(requirements, data.get("seniority_fit"))

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
                "no_description": not has_description,
                "scored": True,
            }
        except Exception as e:
            if attempt == 0:
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
            result = await score_job_against_profile(brief, job, search_query)
        # Only successful scores are cached; a failed one should be retried next time
        # rather than pinned as a permanent 0%.
        if result.get('scored'):
            _cache_score(key, result)
        job.update(result)
        return job

    scored_jobs = list(await asyncio.gather(*[score_one(j) for j in eval_jobs]))

    # Jobs with a real posting outrank ones scored from the title alone, since an
    # inferred score is a guess and shouldn't beat a verified match.
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
{email_body}

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


