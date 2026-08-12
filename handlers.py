import os
import re
import tempfile
from datetime import datetime, timedelta
from telegram import (
    Update, InlineKeyboardButton, InlineKeyboardMarkup, ReplyKeyboardMarkup,
    KeyboardButton, LabeledPrice
)
from telegram.ext import ContextTypes, ConversationHandler
from PyPDF2 import PdfReader

from database import (
    get_or_create_user, update_user_profile, get_user,
    add_application, update_application_status, get_applications, get_application,
    delete_application,
    get_follow_up_candidates, mark_follow_up_sent,
    save_generated_doc, get_user_stats,
    get_gmail_accounts, update_gmail_account_sync, remove_gmail_account,
    delete_user_cv, get_or_create_inbox_token,
    get_credits, has_tokens, consume_tokens, add_credits,
    FREE_SEARCHES, SEARCHES_PER_PACK, TOKENS_PER_SEARCH, TOKENS_PER_PACK
)
from ai_services import (
    tailor_resume, generate_cover_letter,
    draft_follow_up, generate_interview_questions,
    classify_user_intent, parse_job_email, parse_job_search_query
)
import asyncio
from linkedin_services import (
    search_linkedin_jobs, apply_linkedin_filters, build_linkedin_url, find_job_posting,
    resolve_location, WORK_TYPES, JOB_TYPES, EXPERIENCE_LEVELS, DATE_POSTED
)
from gmail_services import decrypt_token, fetch_recent_job_emails

WEBHOOK_URL = os.getenv("WEBHOOK_URL")

# ─── Conversation States ───
(ONBOARDING_RESUME,
 TRACK_COMPANY, TRACK_ROLE,
 RESUME_JOB_DESC,
 UPDATE_APP_STATUS,
 FIND_JOBS_KEYWORD, FIND_JOBS_LOCATION, FIND_JOBS_WORK_TYPE,
 FIND_JOBS_JOB_TYPE, FIND_JOBS_DATE_POSTED, FIND_JOBS_EXPERIENCE,
 FIND_JOBS_REVIEW) = range(12)

# ─── Markdown Safety ───

MAX_RESUME_CHARS = 20000

def extract_pdf_text(path: str):
    """
    Pulls text out of a CV PDF and reports whether the result looks trustworthy.

    Returns (text, quality) where quality is "ok" or "poor". Extraction quality is
    the ceiling on match quality — a scrambled CV produces confident, wrong
    matching — so a poor result is surfaced to the user rather than used silently.
    """
    text = ""
    try:
        from pypdf import PdfReader as ModernReader
        reader = ModernReader(path)
        text = "\n".join((page.extract_text() or "") for page in reader.pages)
    except Exception as e:
        print(f"pypdf failed ({e}); falling back to PyPDF2")

    if len(text.strip()) < 200:
        try:
            legacy = PdfReader(path)
            legacy_text = "\n".join((page.extract_text() or "") for page in legacy.pages)
            if len(legacy_text.strip()) > len(text.strip()):
                text = legacy_text
        except Exception as e:
            print(f"PyPDF2 fallback failed: {e}")

    text = _clean_resume_text(text)

    quality = "ok"
    if text:
        words = text.split()
        # Layout-mangled PDFs come out as long runs of glued-together fragments.
        avg_word = sum(len(w) for w in words) / len(words) if words else 0
        if len(text) < 400 or avg_word > 18 or len(words) < 60:
            quality = "poor"
    return text[:MAX_RESUME_CHARS], quality


def _clean_resume_text(text: str) -> str:
    """Normalises PDF artefacts that otherwise confuse the parser."""
    if not text:
        return ""
    replacements = {"ﬀ": "ff", "ﬁ": "fi", "ﬂ": "fl", "•": "\n• ",
                    "\xa0": " ", "–": "-", "—": "-", "’": "'"}
    for bad, good in replacements.items():
        text = text.replace(bad, good)
    text = re.sub(r"[ \t]+", " ", text)
    text = re.sub(r"\n\s*\n\s*\n+", "\n\n", text)
    return "\n".join(line.strip() for line in text.splitlines()).strip()


# Legacy Markdown has no escape syntax — a backslash before '*' shows up as a
# literal backslash rather than escaping anything (only MarkdownV2 supports that).
# So control characters are swapped for harmless lookalikes instead. '∗' is the
# asterisk operator, visually identical to '*' but not a formatting token.
_MD_SAFE = {"*": "∗", "_": " ", "`": "'", "[": "(", "]": ")"}

def md(text) -> str:
    """Neutralises Telegram legacy-Markdown control characters in dynamic text.

    Job titles routinely contain * _ [ ` — left alone they make send_message raise
    BadRequest ("can't parse entities"), which silently drops the card from results.
    """
    out = str(text or "")
    for char, replacement in _MD_SAFE.items():
        out = out.replace(char, replacement)
    return out

# ─── LinkedIn Filter Keyboards ───
# Multi-select toggles mirroring LinkedIn's own filter panel: several values
# within one filter are OR'd, exactly like ticking several boxes on the site.

def toggle_filter(context, key: str, code: str) -> list:
    selected = set(context.user_data.get(key) or [])
    selected.discard(code) if code in selected else selected.add(code)
    context.user_data[key] = sorted(selected)
    return context.user_data[key]

def filter_keyboard(options: dict, selected, prefix: str, per_row: int = 2, skip_label: str = "Any"):
    chosen = set(selected or [])
    rows, row = [], []
    for code, label in options.items():
        row.append(InlineKeyboardButton(
            f"{'✅' if code in chosen else '▫️'} {label}", callback_data=f"{prefix}{code}"
        ))
        if len(row) == per_row:
            rows.append(row)
            row = []
    if row:
        rows.append(row)
    rows.append([InlineKeyboardButton(
        f"➡️ Continue ({len(chosen)} selected)" if chosen else f"➡️ Skip ({skip_label})",
        callback_data=f"{prefix}done"
    )])
    return InlineKeyboardMarkup(rows)

def filter_summary(context) -> str:
    """Renders the active filters using LinkedIn's own labels."""
    lines = []
    for key, options, title in (
        ('fj_work_type', WORK_TYPES, "Work type"),
        ('fj_job_type', JOB_TYPES, "Job type"),
        ('fj_experience_level', EXPERIENCE_LEVELS, "Experience level"),
    ):
        codes = context.user_data.get(key) or []
        if codes:
            lines.append(f"• **{title}**: {', '.join(options[c] for c in codes if c in options)}")
    dp = context.user_data.get('fj_date_posted')
    if dp:
        lines.append(f"• **Date posted**: {DATE_POSTED.get(dp, dp)}")
    if context.user_data.get('fj_easy_apply'):
        lines.append("• **Easy Apply** only")
    return "\n".join(lines) if lines else "_No filters — showing all results_"

# ─── Shared Job Card Renderer ───

RANK_MEDALS = {0: "🥇", 1: "🥈", 2: "🥉"}

_AGE_UNIT_DAYS = {"minute": 0, "hour": 0, "day": 1, "week": 7, "month": 30, "year": 365}
STALE_AFTER_DAYS = 30

def posting_age_days(posted_date: str):
    """Turns LinkedIn's '3 weeks ago' into a day count, or None if unparseable."""
    m = re.search(r"(\d+)\s*(minute|hour|day|week|month|year)", str(posted_date or ""), re.I)
    if not m:
        return None
    return int(m.group(1)) * _AGE_UNIT_DAYS[m.group(2).lower()]

def rank_badge(global_idx: int, total_ranked: int) -> str:
    """
    Position of this job among the ranked results.

    Against real postings most honest scores land in the 20s-40s, and a column of
    "23%… 19%…" reads as a broken bot rather than a candid one. The percentage
    answers "is this job worth my time"; the rank answers "what do I read first",
    which is the question the search is actually for.
    """
    if not total_ranked or global_idx >= total_ranked:
        return ""
    if global_idx == 0:
        return f"🥇 **Best match** of {total_ranked}"
    return f"{RANK_MEDALS.get(global_idx, '📍')} **#{global_idx + 1}** of {total_ranked}"


async def send_job_cards(bot, chat_id: int, jobs: list, keywords: str, location: str,
                         start_idx: int = 0, total_ranked: int = 0):
    """Renders and sends job cards to chat. start_idx tracks position for pagination."""
    for idx, job in enumerate(jobs):
        score = job.get('score', 50)
        if score >= 85:
            badge = "🟢 Excellent Match"
        elif score >= 70:
            badge = "🔵 Strong Match"
        elif score >= 50:
            badge = "🟡 Good Match"
        elif score >= 35:
            badge = "🟠 Weak Match"
        else:
            badge = "🔴 Poor Match"

        filled = max(1, min(10, round(score / 10)))
        bar = "█" * filled + "░" * (10 - filled)
        matched_cnt = job.get('matched_count', len(job.get('matched_qualifications', [])))
        missing_cnt = job.get('missing_count', len(job.get('missing_qualifications', [])))

        text = f"🎯 **{md(job['title'])}**\n"
        text += f"🏢 **{md(job['company'])}** | 📍 **{md(job['location'])}**\n"
        if job.get('posted_date'):
            age = posting_age_days(job['posted_date'])
            # One "no longer accepting applications" after the bot called something a
            # top match costs more trust than several mediocre matches.
            stale = " ⚠️ _may no longer be open_" if age is not None and age > STALE_AFTER_DAYS else ""
            text += f"🕐 Posted {md(job['posted_date'])}{stale}\n"

        # LinkedIn's own criteria for this posting — the fields its filters run on
        criteria = [job.get('employment_type'), job.get('seniority_level')]
        criteria = [c for c in criteria if c and c.lower() != "not applicable"]
        if criteria:
            text += f"🏷️ {md(' · '.join(criteria))}\n"

        global_idx = start_idx + idx

        if not job.get('scored', True):
            if job.get('no_cv'):
                text += "\n⚪ **Not ranked** — upload a CV to get match scores\n"
            elif job.get('unreadable'):
                # Better to say nothing than to invent requirements and a number.
                text += ("\n⚠️ **Couldn't read this posting**\n"
                         "LinkedIn didn't return the description, so I won't guess at a match. "
                         "Open it below to check the requirements yourself.\n")
            else:
                text += "\n⚪ **Not scored** — beyond the analysis limit for this search\n"
        else:
            rank = rank_badge(global_idx, total_ranked)
            if rank:
                text += f"\n{rank}\n"
            text += f"⭐ **Profile Match**: `{score}%` `[{bar}]` ({badge})\n"
            if job.get('no_description'):
                # Say plainly this is a guess: the requirements were invented from the
                # title because LinkedIn wouldn't return the posting.
                text += (f"_(⚠️ Guessed from the job title only — LinkedIn didn't return this "
                         f"posting, so open it and judge for yourself)_\n")
            if job.get('filter_uncertain'):
                text += f"_(ℹ️ LinkedIn didn't publish all filter details for this post)_\n"
            if job.get('field_fit') == "different":
                # A low score on a job you could technically do reads as a bug unless
                # the reason is on the card.
                text += "_(🔀 Different field to your background — a career change)_\n"

            total_reqs = len(job.get('requirements') or [])
            partial_cnt = job.get('partial_count', 0)
            text += f"\n✅ **{matched_cnt}** met"
            if partial_cnt:
                text += f"  ·  🟨 **{partial_cnt}** partial"
            text += f"  ·  ❌ **{missing_cnt}** missing"
            text += f"  (of {total_reqs} requirements)\n" if total_reqs else "\n"

            if job.get('verdict_summary'):
                text += f"\n💬 _{md(job['verdict_summary'])}_\n"

            gaps = job.get('main_gaps', [])
            if gaps:
                text += "\n⚠️ **Main Gaps:**\n"
                for g in gaps:
                    text += f" • {md(g)}\n"

        keyboard = [
            [InlineKeyboardButton("🔗 View on LinkedIn", url=job['url']),
             InlineKeyboardButton("📊 Full Analysis", callback_data=f"job_analysis:{global_idx}")],
            [InlineKeyboardButton("📌 Save to Tracker", callback_data=f"track_job:{global_idx}")]
        ]
        try:
            await bot.send_message(chat_id=chat_id, text=text, reply_markup=InlineKeyboardMarkup(keyboard), parse_mode="Markdown")
        except Exception as e:
            # Never let one unrenderable card swallow the rest of the results
            print(f"Job card send failed ({job.get('title')}): {e}")
            try:
                await bot.send_message(chat_id=chat_id, text=re.sub(r"[*_`]", "", text),
                                       reply_markup=InlineKeyboardMarkup(keyboard))
            except Exception:
                pass
        await asyncio.sleep(0.1)

# ─── Keyboards ───

def get_main_keyboard(user_dict=None):
    keyboard = [
        [KeyboardButton("📄 My CV"), KeyboardButton("📊 My Apps")],
        [KeyboardButton("📈 Stats"), KeyboardButton("🔍 Find Jobs")],
        [KeyboardButton("📧 Email Tracking"), KeyboardButton("⭐ Searches")]
    ]
    return ReplyKeyboardMarkup(keyboard, resize_keyboard=True, is_persistent=True)

def get_status_keyboard(app_id: str):
    keyboard = [
        [InlineKeyboardButton("📤 Applied", callback_data=f"status:{app_id}:applied"),
         InlineKeyboardButton("📩 Responded", callback_data=f"status:{app_id}:responded")],
        [InlineKeyboardButton("📝 Assessment", callback_data=f"status:{app_id}:assessment"),
         InlineKeyboardButton("🎤 Interview", callback_data=f"status:{app_id}:interview")],
        [InlineKeyboardButton("🎉 Offer", callback_data=f"status:{app_id}:offer"),
         InlineKeyboardButton("❌ Rejected", callback_data=f"status:{app_id}:rejected")],
        [InlineKeyboardButton("👻 Ghosted", callback_data=f"status:{app_id}:ghosted")]
    ]
    return InlineKeyboardMarkup(keyboard)

def get_app_actions_keyboard(app_id: str):
    keyboard = [
        [InlineKeyboardButton("📝 Update Status", callback_data=f"update:{app_id}")]
    ]
    return InlineKeyboardMarkup(keyboard)

def get_pipeline_keyboard():
    keyboard = [
        [InlineKeyboardButton("📋 All Apps", callback_data="filter:all")],
        [InlineKeyboardButton("📤 Applied", callback_data="filter:applied"),
         InlineKeyboardButton("📩 Responded", callback_data="filter:responded")],
        [InlineKeyboardButton("📝 Assessment", callback_data="filter:assessment"),
         InlineKeyboardButton("🎤 Interview", callback_data="filter:interview")],
        [InlineKeyboardButton("Offers 🎉", callback_data="filter:offer"),
         InlineKeyboardButton("Rejected", callback_data="filter:rejected")]
    ]
    return InlineKeyboardMarkup(keyboard)

STATUS_EMOJI = {
    "applied": "📤",
    "responded": "📩",
    "assessment": "📝",
    "interview": "🎤",
    "offer": "🎉",
    "rejected": "❌",
    "ghosted": "👻"
}

# ─── Search Credits ───

# Telegram Stars. Payment happens inside Telegram, so there is no merchant account,
# no card processor and no country restriction — which is what makes this work at
# all from Lebanon, where Stripe cannot operate.
PACK_PRICE_STARS = int(os.getenv("PACK_PRICE_STARS", "150"))


def buy_credits_keyboard(db_user: dict = None) -> InlineKeyboardMarkup:
    """Button that opens the in-chat Stars invoice."""
    return InlineKeyboardMarkup([[
        InlineKeyboardButton(f"⭐ Buy ~{SEARCHES_PER_PACK} searches — {PACK_PRICE_STARS} Stars",
                             callback_data="buy_tokens")
    ]])


async def buy_tokens_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Sends the Stars invoice."""
    query = update.callback_query
    await query.answer()
    await context.bot.send_invoice(
        chat_id=update.effective_chat.id,
        title=f"{SEARCHES_PER_PACK} job searches",
        description=(
            f"{SEARCHES_PER_PACK} LinkedIn searches, each scored against your CV "
            f"requirement by requirement. Never expires."
        ),
        payload=f"tokens:{TOKENS_PER_PACK}",
        # Stars use the XTR currency and, unlike card payments, need no provider token.
        provider_token="",
        currency="XTR",
        prices=[LabeledPrice(label=f"{SEARCHES_PER_PACK} searches", amount=PACK_PRICE_STARS)],
    )


async def precheckout_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """
    Telegram asks for approval moments before charging.

    It must be answered within ~10 seconds or the payment fails, so this stays
    trivial — no database work, no validation that could hang.
    """
    await update.pre_checkout_query.answer(ok=True)


async def successful_payment_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Credits the tokens once Telegram confirms the Stars payment."""
    payment = update.message.successful_payment
    user = update.effective_user
    db_user, _ = get_or_create_user(user.id, user.username)

    tokens = TOKENS_PER_PACK
    if (payment.invoice_payload or "").startswith("tokens:"):
        try:
            tokens = int(payment.invoice_payload.split(":", 1)[1])
        except ValueError:
            pass

    # charge_id is unique per payment, so a duplicate update can't credit twice.
    credited = add_credits(
        db_user['id'], tokens,
        payment.telegram_payment_charge_id,
        payment.total_amount, payment.currency,
    )

    balance = get_credits(db_user['id'])
    if credited:
        await update.message.reply_text(
            f"✅ **Thank you!**\n\n"
            f"**{tokens // TOKENS_PER_SEARCH} searches** added — they never expire.\n\n"
            f"Balance: **{balance['searches']} searches**",
            parse_mode="Markdown",
        )
    else:
        await update.message.reply_text(
            f"This payment was already credited.\n\nBalance: {balance['searches']} searches"
        )


async def require_credit(update: Update, context: ContextTypes.DEFAULT_TYPE, db_user: dict) -> bool:
    """Checks there's enough balance to start, or explains why the search didn't run."""
    if has_tokens(db_user['id'], TOKENS_PER_SEARCH):
        return True

    chat_id = update.effective_chat.id
    keyboard = buy_credits_keyboard(db_user)
    text = (
        "🔒 **You're out of searches**\n\n"
        f"You've used all {FREE_SEARCHES} of your free searches.\n\n"
        f"**~{SEARCHES_PER_PACK} more searches — $3**\n"
        "One-off payment, no subscription. Never expires.\n\n"
        "_Email tracking stays free and unlimited — keep forwarding your job emails._"
    )
    if not keyboard:
        text += "\n\n⚠️ Payments aren't set up on this server yet."
    await context.bot.send_message(chat_id, text, reply_markup=keyboard, parse_mode="Markdown")
    return False


async def credits_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Shows the remaining balance and how to top up."""
    user = update.effective_user
    db_user, _ = get_or_create_user(user.id, user.username)
    balance = get_credits(db_user['id'])
    left = balance["searches"]

    # Shown in searches, never tokens. Tokens are an internal accounting unit —
    # they mean nothing to a job seeker and expose the cost structure.
    free_left = balance["free_left"] // TOKENS_PER_SEARCH
    paid_left = balance["paid"] // TOKENS_PER_SEARCH

    text = f"💳 **Your balance**\n\n**{left} searches** remaining\n"
    if paid_left:
        text += f"\n🆓 Free: {free_left}\n⭐ Purchased: {paid_left} _(never expire)_\n"
    text += ("\nSearching LinkedIn uses your balance. Email tracking, your CV, your "
             "pipeline and interview prep are all free.\n\n"
             f"**{SEARCHES_PER_PACK} more searches — {PACK_PRICE_STARS} ⭐ Stars.** "
             "One-off, never expires. Paid inside Telegram — no card details, "
             "nothing leaves the app.")
    # The buy button is always here, not only when the balance runs low: a user who
    # never sees a price never learns the bot is buyable.
    await update.message.reply_text(
        text, reply_markup=buy_credits_keyboard(db_user), parse_mode="Markdown",
    )


# ─── Interview Prep ───

async def send_long(bot, chat_id: int, text: str, reply_markup=None):
    """
    Sends text that may exceed Telegram's 4096-character limit.

    Splits on question boundaries so an answer is never cut in half, and only the
    final chunk carries the keyboard.
    """
    limit = 3500
    chunks, current = [], ""
    for block in text.split("\n\n"):
        if len(current) + len(block) + 2 > limit and current:
            chunks.append(current)
            current = block
        else:
            current = f"{current}\n\n{block}" if current else block
    if current:
        chunks.append(current)

    for i, chunk in enumerate(chunks):
        markup = reply_markup if i == len(chunks) - 1 else None
        try:
            await bot.send_message(chat_id=chat_id, text=chunk, reply_markup=markup, parse_mode="Markdown")
        except Exception as e:
            print(f"Chunk send failed ({e}); retrying without markdown")
            await bot.send_message(chat_id=chat_id, text=re.sub(r"[*_`]", "", chunk), reply_markup=markup)
        await asyncio.sleep(0.1)


def format_interview_questions(questions: list, start_number: int = 1) -> str:
    """Renders questions with their answers, flagging anything the CV can't cover."""
    out = ""
    for i, q in enumerate(questions, start_number):
        out += f"\n\n**{i}. {md(q.get('question', ''))}**\n"
        if q.get('category'):
            out += f"_{md(q['category'])}_"
            if q.get('why_asked'):
                out += f" — _{md(q['why_asked'])}_"
            out += "\n"
        if q.get('answer'):
            out += f"\n💬 {md(q['answer'])}\n"
        if q.get('needs_from_you'):
            out += f"\n⚠️ **You need to add:** {md(q['needs_from_you'])}\n"
    return out.strip()


async def interview_prep_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Generates interview questions for one tracked application."""
    query = update.callback_query
    await query.answer()

    app_id = query.data.split(":", 1)[1]
    is_more = query.data.startswith("prep_more:")
    chat_id = update.effective_chat.id

    app = get_application(app_id)
    if not app:
        await query.edit_message_text("⚠️ That application no longer exists.")
        return

    user = update.effective_user
    db_user, _ = get_or_create_user(user.id, user.username)
    company, role = app.get('company', ''), app.get('role', '')

    asked_key = f"prep_asked:{app_id}"
    already = context.user_data.get(asked_key, [])

    status = await context.bot.send_message(
        chat_id,
        f"🎤 Preparing for **{md(company)}** — {md(role)}\n\n"
        + ("🔍 Writing another 10 questions..." if is_more
           else "🔍 Finding the real job posting on LinkedIn..."),
        parse_mode="Markdown",
    )

    # The posting is fetched once and cached for this application, so "10 more"
    # doesn't repeat a slow LinkedIn search.
    posting_key = f"prep_posting:{app_id}"
    posting = context.user_data.get(posting_key)
    if posting is None:
        posting = app.get('job_description') or ""
        if not posting:
            found = await asyncio.to_thread(find_job_posting, company, role)
            posting = (found or {}).get('description', "")
        context.user_data[posting_key] = posting

    if not is_more:
        await status.edit_text(
            f"🎤 Preparing for **{md(company)}** — {md(role)}\n\n"
            + ("✅ Found the posting — writing questions from it..." if posting
               else "⚠️ Couldn't find the posting — writing from the role instead..."),
            parse_mode="Markdown",
        )

    questions = await generate_interview_questions(
        company=company, role=role, job_description=posting,
        resume_text=db_user.get('resume_text'), already_asked=already, count=10,
    )

    if not questions:
        await status.edit_text("⚠️ Couldn't generate questions right now. Please try again.")
        return

    context.user_data[asked_key] = already + [q.get('question', '') for q in questions]

    header = (
        f"🎤 **Interview Prep — {md(company)}**\n{md(role)}\n"
        f"{'📋 Based on the real LinkedIn posting' if posting else '📋 Based on the role (posting not found)'}\n"
        f"──────────────────"
    )
    body = format_interview_questions(questions, start_number=len(already) + 1)
    keyboard = InlineKeyboardMarkup([[
        InlineKeyboardButton("➕ 10 more questions", callback_data=f"prep_more:{app_id}")
    ]])

    await status.delete()
    await send_long(context.bot, chat_id, f"{header}\n{body}", reply_markup=keyboard)


# ─── Email → Applications ───

async def ingest_job_emails(db_user: dict, emails: list) -> dict:
    """
    Turns classified emails into tracked applications.

    Shared by both sources — the Gmail API sync and the forwarding inbox — because
    an email is an email once parsed; only where it came from differs.

    Returns {'found', 'added', 'updated', 'details'}.
    """
    outcome = {'found': 0, 'added': 0, 'updated': 0, 'details': ""}
    if not emails:
        return outcome

    # Loaded once, up front. This used to be re-queried inside the per-email loop
    # using the default limit of 20, so anything outside the 20 most recent
    # applications looked new and got added again as a duplicate.
    tracked = {a['company'].strip().lower(): a for a in get_applications(db_user['id'], limit=1000)}

    # Classify in parallel — one sequential AI call per email made a large mailbox
    # take minutes.
    gate = asyncio.Semaphore(8)

    async def classify(email):
        async with gate:
            try:
                return email, await parse_job_email(email['subject'], email['sender'], email['body'])
            except Exception as e:
                print(f"Failed to parse email '{email.get('subject')}': {e}")
                return email, {}

    for email, parsed in await asyncio.gather(*[classify(e) for e in emails]):
        if not (parsed.get("is_job_email") and parsed.get("company")):
            continue

        outcome['found'] += 1
        company = parsed["company"]
        role = parsed.get("role") or ""
        if not role or role == "Unknown Role":
            role = email['subject'][:80]   # use subject as role hint
        status = parsed.get("status", "applied")

        already_tracked = tracked.get(company.strip().lower())
        if already_tracked:
            current_rank = STATUS_RANK.get(already_tracked['status'], 0)
            new_rank = STATUS_RANK.get(status, 0)
            # Only update if the new status is a genuine promotion, so an old
            # "application received" email can't drag an interview back down.
            if new_rank > current_rank:
                update_application_status(already_tracked['id'], status)
                already_tracked['status'] = status
                outcome['updated'] += 1
                outcome['details'] += f"🔄 **{md(company)}**: → `{status.title()}`\n"
        else:
            created = add_application(db_user['id'], company, role)
            if created:
                if status != "applied":
                    update_application_status(created['id'], status)
                created['status'] = status
                # Index immediately so later emails in the same batch update this
                # application instead of creating another copy.
                tracked[company.strip().lower()] = created
            outcome['added'] += 1
            outcome['details'] += f"➕ **{md(company)}** — {md(role)} `({status.title()})`\n"

    return outcome

async def forwarding_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Same instructions, reached from the Email Tracking button."""
    query = update.callback_query
    await query.answer()
    await forwarding_command(update, context)


async def forwarding_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Shows the user their private forwarding address and how to wire it up."""
    user = update.effective_user
    db_user, _ = get_or_create_user(user.id, user.username)
    chat_id = update.effective_chat.id

    inbox_domain = os.getenv("INBOX_DOMAIN")
    if not inbox_domain:
        await context.bot.send_message(
            chat_id,
            "📮 Email forwarding isn't set up on this server yet.",
            parse_mode="Markdown",
        )
        return

    token = get_or_create_inbox_token(db_user['id'])
    address = f"u{token}@{inbox_domain}"

    await context.bot.send_message(
        chat_id,
        forwarding_setup_text(address),
        parse_mode="Markdown",
    )


def forwarding_setup_text(address: str) -> str:
    """The whole forwarding guide, in one message."""
    return (
        f"⚡ **Automatic forwarding setup**\n\n"
        f"**You need a computer for this.** Gmail hides forwarding and filter settings "
        f"on phones — even with *Desktop site* switched on, it usually still serves the "
        f"mobile version. Any laptop works, and it's only done once.\n\n"
        f"━━━━━━━━━━━━━━━\n"
        f"📱 **Only have a phone? Try desktop view**\n\n"
        f"**Android — Chrome:**\n"
        f"Tap **⋮** (top right) → tick **Desktop site** → reload *mail.google.com*\n\n"
        f"**iPhone — Safari:**\n"
        f"Tap **aA** on the left of the address bar → **Request Desktop Website**\n\n"
        f"**iPhone — Chrome:**\n"
        f"Tap **⋯** (bottom right) → **Request Desktop Site**\n\n"
        f"_If the page still looks like the phone version, Gmail has refused — use a "
        f"computer, or just keep forwarding emails manually. Both give the same result._\n\n"
        f"━━━━━━━━━━━━━━━\n"
        f"💻 **On the Gmail website**\n\n"
        f"1. **⚙️** (top right) → **See all settings**\n"
        f"2. **Forwarding and POP/IMAP** tab → **Add a forwarding address**\n"
        f"3. Paste:\n`{address}`\n"
        f"4. **Next** → **Proceed** → **OK**\n"
        f"5. Gmail emails a confirmation — **I'll send you the link here.** Tap it.\n"
        f"6. **Filters and blocked addresses** tab → **Create a new filter**\n"
        f"7. In the **Includes the words** box paste:\n\n"
        f"`{md(FORWARDING_FILTER_QUERY)}`\n\n"
        f"8. Leave every other box empty → **Create filter** _(bottom right — not Search)_\n"
        f"9. Tick **Forward it to** → choose your address → **Create filter**\n\n"
        f"Done — every job email is tracked from then on.\n\n"
        f"━━━━━━━━━━━━━━━\n"
        f"📚 **Add your past applications**\n\n"
        f"Filters only affect new mail. To bring in old emails: on the Gmail website "
        f"search your job emails, select them, then **⋮ → Forward as attachment** to "
        f"your address. Up to 50 at a time."
    )


# A deliberately broad filter. Over-forwarding is cheap because parse_job_email
# discards anything that isn't a job email, whereas an email the filter misses is
# never seen at all — Gmail cannot forward retroactively.
FORWARDING_FILTER_QUERY = (
    'subject:application OR subject:interview OR subject:offer OR subject:candidate '
    'OR subject:recruitment OR subject:position OR "your application" '
    'OR "thank you for applying" OR "we regret to inform" OR "next steps" '
    'OR from:greenhouse.io OR from:lever.co OR from:myworkday.com '
    'OR from:smartrecruiters.com OR from:workable.com OR from:linkedin.com'
)

# ─── /start Command ───

async def start_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    db_user, created = get_or_create_user(user.id, user.username, user.first_name)
    
    if created or not db_user.get("resume_text"):
        welcome = f"""🎯 Welcome to **JobPilot**, {user.first_name}!

I do three things:
• Find jobs on LinkedIn and score them against your CV
• Track your applications automatically from your email
• Tailor your CV for any job you apply to

First I need your CV, so I know what to match jobs against.

📎 **Send it as a PDF**
or
✍️ **Paste the text into this chat**

_You can add it later — everything except job matching works without it._"""
        await update.message.reply_text(
            welcome,
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton("⏭️ Upload later", callback_data="skip_cv")]
            ]),
        )
        return ONBOARDING_RESUME
    else:
        desc = f"""🎯 **JobPilot** — welcome back, {user.first_name}!

**🔍 Find Jobs**
Search LinkedIn by role, location and filters. I check every posting against your CV requirement by requirement and tell you what you match and what you're missing — so you only read the ones worth your time.

**📧 Email Tracking**
Forward your job emails to your private address and I log every application, interview invite and rejection by itself. No inbox access needed.

**📊 My Apps**
Your pipeline — where each application stands, and who needs a follow-up.

**📄 My CV**
The CV I score jobs against. Keep it current and the matches stay accurate.

**📈 Stats**
Response rate and how your search is actually going.

Also: send me a job description and I'll tailor your CV for it.

Tap a button below to start."""
        await update.message.reply_text(
            desc,
            reply_markup=get_main_keyboard(db_user)
        )
        return ConversationHandler.END

# ─── Onboarding Flow ───

async def onboarding_resume_pdf(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handles PDF document uploads during onboarding."""
    msg = await update.message.reply_text("📄 Reading your PDF...")

    try:
        doc = update.message.document
        file = await context.bot.get_file(doc.file_id)

        with tempfile.NamedTemporaryFile(suffix=".pdf", delete=False) as tmp:
            tmp_path = tmp.name
        await file.download_to_drive(tmp_path)

        try:
            resume_text, quality = extract_pdf_text(tmp_path)
        finally:
            os.remove(tmp_path)

        if not resume_text.strip():
            await msg.edit_text(
                "⚠️ I couldn't read any text from this PDF — it's most likely a scan or an image.\n\n"
                "Please paste your CV as text instead so the matching works properly."
            )
            return ONBOARDING_RESUME

        context.user_data['resume_text'] = resume_text

        if quality == "poor":
            # Bad extraction silently wrecks every match afterwards, so say so now.
            await msg.edit_text(
                f"⚠️ I extracted only {len(resume_text)} characters, and it looks garbled — "
                "PDFs with columns or heavy design often come out scrambled.\n\n"
                "I'll use it, but pasting your CV as text will give far more accurate matching.\n\n"
                "Analyzing..."
            )
        else:
            await msg.edit_text("✅ PDF extracted successfully!\n\nAnalyzing your resume...")
    except Exception as e:
        print(f"PDF extraction failed: {e}")
        await msg.edit_text("⚠️ Error reading PDF. Please paste your resume as text instead.")
        return ONBOARDING_RESUME
    
    return await finish_onboarding(update, context)

async def onboarding_resume(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handles pasted text resume during onboarding."""
    resume_text = update.message.text
    context.user_data['resume_text'] = resume_text
    
    await update.message.reply_text("📝 Saving your CV...")
    
    return await finish_onboarding(update, context)

async def finish_onboarding(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """
    Saves the CV and drops the user straight onto the main menu.

    There used to be a "what's your target job title?" step here. It's gone: the
    job search asks for a role every time anyway, so answering it up front bought
    nothing and stood between the user and the working product.
    """
    user = update.effective_user
    db_user, _ = get_or_create_user(user.id, user.username)
    resume_text = context.user_data.get('resume_text', '')

    if resume_text:
        update_user_profile(db_user['id'], resume_text=resume_text, onboarding_complete=True)

    await update.effective_message.reply_text(
        "✅ **You're set up.**\n\n"
        "📄 CV saved — I'll score every job against it.\n\n"
        "Tap **🔍 Find Jobs** to search LinkedIn, or **📧 Email Tracking** to have your "
        "applications tracked from your inbox automatically.",
        reply_markup=get_main_keyboard(db_user),
        parse_mode="Markdown",
    )
    return ConversationHandler.END


async def skip_cv_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """'Upload later' — go straight to the menu without a CV."""
    query = update.callback_query
    await query.answer()

    user = update.effective_user
    db_user, _ = get_or_create_user(user.id, user.username)

    await query.edit_message_text(
        "👍 No problem — you can add your CV any time from **📄 My CV**.\n\n"
        "_Without it I can still find and track jobs, but I can't score how well "
        "they match you._",
        parse_mode="Markdown",
    )
    await context.bot.send_message(
        update.effective_chat.id,
        "Tap **🔍 Find Jobs** to search LinkedIn, or **📧 Email Tracking** to track "
        "applications from your inbox.",
        reply_markup=get_main_keyboard(db_user),
        parse_mode="Markdown",
    )
    return ConversationHandler.END


# ─── Track Application ───

async def track_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    db_user, _ = get_or_create_user(user.id, user.username)
    
    
    await update.message.reply_text("🏢 What **company** did you apply to?")
    return TRACK_COMPANY

async def track_company(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data['track_company'] = update.message.text
    await update.message.reply_text("💼 What **role** did you apply for?")
    return TRACK_ROLE

async def track_role(update: Update, context: ContextTypes.DEFAULT_TYPE):
    company = context.user_data.get('track_company')
    role = update.message.text
    
    user = update.effective_user
    db_user, _ = get_or_create_user(user.id, user.username)
    
    app = add_application(db_user['id'], company, role)

    await update.message.reply_text(
        f"✅ **Application Tracked!**\n\n"
        f"🏢 {md(company)}\n"
        f"💼 {md(role)}\n"
        f"📤 Status: Applied\n"
        f"📅 {datetime.now().strftime('%b %d, %Y')}",
        reply_markup=get_app_actions_keyboard(app['id']) if app else None
    )
    return ConversationHandler.END

# ─── View Applications Pipeline ───

async def view_apps(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    db_user, _ = get_or_create_user(user.id, user.username)
    apps = get_applications(db_user['id'])
    
    if not apps:
        await update.message.reply_text(
            "📭 No applications yet.\n\nTap **🔍 Find Jobs** to search LinkedIn or **➕ Track** to log manually!"
        )
        return
    
    # Send summary header
    text = f"📊 **Your Application Pipeline** ({len(apps)} total)\n──────────────────\n\n"
    for app in apps[:10]:
        emoji = STATUS_EMOJI.get(app['status'], "📤")
        text += f"{emoji} **{app['company']}** — {app['role']}\n   Status: {app['status'].title()}\n\n"
    
    if len(apps) > 10:
        text += f"_...and {len(apps) - 10} more_\n"
    
    await update.message.reply_text(text, reply_markup=get_pipeline_keyboard(), parse_mode="Markdown")
    
    # Send individual cards with action buttons for each app
    for app in apps[:10]:
        emoji = STATUS_EMOJI.get(app['status'], "📤")
        card = f"{emoji} **{app['company']}** — {app['role']}\nStatus: `{app['status'].title()}`"
        keyboard = [
            [InlineKeyboardButton("🎤 Interview Prep", callback_data=f"prep:{app['id']}")],
            [InlineKeyboardButton("📝 Update Status", callback_data=f"update:{app['id']}"),
             InlineKeyboardButton("🗑️ Delete", callback_data=f"delete_app:{app['id']}")]
        ]
        await update.message.reply_text(card, reply_markup=InlineKeyboardMarkup(keyboard), parse_mode="Markdown")
        await asyncio.sleep(0.1)

async def filter_apps_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    status_filter = query.data.split(":")[1]
    
    user = update.effective_user
    db_user, _ = get_or_create_user(user.id, user.username)
    
    if status_filter == "all":
        apps = get_applications(db_user['id'])
    else:
        apps = get_applications(db_user['id'], status=status_filter)
    
    if not apps:
        try:
            await query.edit_message_text(f"No applications with status: {status_filter.title()}", reply_markup=get_pipeline_keyboard())
        except Exception:
            pass
        return

    text = f"📊 **Pipeline — {status_filter.title()}**\n──────────────────\n\n"
    for app in apps[:10]:
        emoji = STATUS_EMOJI.get(app['status'], "📤")
        text += f"{emoji} **{app['company']}** — {app['role']}\n"

    try:
        await query.edit_message_text(text, reply_markup=get_pipeline_keyboard(), parse_mode="Markdown")
    except Exception:
        pass

# ─── Resume Tailoring ───

async def tailor_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    db_user, _ = get_or_create_user(user.id, user.username)
    
    
    if not db_user.get("resume_text"):
        await update.message.reply_text("⚠️ You haven't uploaded your resume yet. Use /start to set it up.")
        return ConversationHandler.END
    
    await update.message.reply_text(
        "✍️ **Resume Tailor**\n\n"
        "Paste the **full job description** below.\n"
        "I'll rewrite your resume to match it perfectly."
    )
    return RESUME_JOB_DESC

async def tailor_process(update: Update, context: ContextTypes.DEFAULT_TYPE):
    job_desc = update.message.text
    user = update.effective_user
    db_user, _ = get_or_create_user(user.id, user.username)
    
    msg = await update.message.reply_text("🤖 Tailoring your resume + writing a cover letter...\nThis takes about 30 seconds.")
    
    try:
        resume_text = db_user.get("resume_text", "")
        target_role = db_user.get("target_role", "")
        
        # Run resume tailoring
        result = await tailor_resume(resume_text, job_desc, target_role)
        match_score = result.get("match_score", 0)
        tailored = result.get("tailored_resume", "")
        keywords = result.get("keywords_added", [])
        tips = result.get("tips", "")
        
        # Generate cover letter
        cover_letter = await generate_cover_letter(resume_text, job_desc, "the company", target_role)
        
        # Save to database
        save_generated_doc(db_user['id'], "resume", resume_text, tailored, match_score)
        save_generated_doc(db_user['id'], "cover_letter", job_desc, cover_letter)
        
        # Mark free resume as used
        if db_user.get("plan") == "free":
            update_user_profile(db_user['id'], free_resume_used=True)
        
        bar = "█" * (match_score // 10) + "░" * (10 - match_score // 10)
        keywords_text = ", ".join(keywords) if keywords else "N/A"
        
        await msg.edit_text(
            f"✅ **Resume Tailored!**\n\n"
            f"🎯 Match Score: **{match_score}/100**\n"
            f"{bar}\n\n"
            f"🔑 Keywords added: {keywords_text}\n"
            f"💡 {tips}"
        )
        
        # Send tailored resume as separate message
        await update.message.reply_text(f"📄 **TAILORED RESUME**\n──────────────────\n\n{tailored}")
        await update.message.reply_text(f"💌 **COVER LETTER**\n──────────────────\n\n{cover_letter}")
        
    except Exception as e:
        await msg.edit_text(f"Sorry, there was an error tailoring your resume. Please try again.")
    
    return ConversationHandler.END

# ─── Follow-Up Engine ───

async def follow_ups(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    db_user, _ = get_or_create_user(user.id, user.username)
    
    candidates = get_follow_up_candidates(db_user['id'])
    
    if not candidates:
        await update.message.reply_text("✅ No follow-ups needed right now!\n\nAll your applications are either responded to or already followed up on.")
        return
    
    now = datetime.utcnow()
    text = "⏰ **Follow-Up Suggestions**\n──────────────────\n\n"
    buttons = []
    
    for app in candidates:
        applied = datetime.fromisoformat(app['applied_date'].replace('Z', '+00:00'))
        days = (now - applied.replace(tzinfo=None)).days
        if days >= 5:
            text += f"📤 **{app['company']}** — {app['role']}\n   Applied {days} days ago, no response\n\n"
            buttons.append([InlineKeyboardButton(
                f"📧 Draft follow-up for {app['company']}", 
                callback_data=f"draft_followup:{app['id']}"
            )])
    
    if not buttons:
        await update.message.reply_text("✅ It's too early to follow up. Wait at least 5 days after applying.")
        return
    
    await update.message.reply_text(text, reply_markup=InlineKeyboardMarkup(buttons))

# ─── Stats Dashboard ───

async def stats_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    db_user, _ = get_or_create_user(user.id, user.username)
    stats = get_user_stats(db_user['id'])

    bar_applied = "█" * min(stats['applied'], 20)
    bar_interview = "🟢" * stats['interview']
    bar_offer = "🎉" * stats['offer']
    bar_rejected = "🔴" * stats['rejected']
    
    text = f"""📈 **Your Job Hunt Dashboard**
──────────────────

📊 **Total Applications**: {stats['total']}

📤 Applied: {stats['applied']} {bar_applied}
📩 Responded: {stats['responded']}
🎤 Interviews: {stats['interview']} {bar_interview}
🎉 Offers: {stats['offer']} {bar_offer}
❌ Rejected: {stats['rejected']} {bar_rejected}
👻 Ghosted: {stats['ghosted']}

📊 **Response Rate**: {stats['response_rate']}%"""

    await update.message.reply_text(text)

# ─── Help Command ───

async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = """🎯 **What JobPilot can do**
──────────────────

**Use the buttons below the chat:**
🔍 **Find Jobs** — search LinkedIn, scored against your CV
📧 **Email Tracking** — auto-track applications from your email
📊 **My Apps** — your application pipeline
📄 **My CV** — the CV I match jobs against
📈 **Stats** — response rate and progress
⭐ **Searches** — your balance, and how to buy more

**Commands:**
/forwarding — Your private email address for auto-tracking
/resume — Tailor your CV for a job
/credits — Searches remaining
/stats — Your dashboard
/cancel — Stop what you're doing
/help — This menu

**Shortcuts:**
• Forward a job email → I track it automatically
• Paste a job description → I tailor your CV for it
• Just tell me what job you want → I'll search for it"""
    await update.message.reply_text(text)

# ─── Cancel ───

async def cancel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("Cancelled.", reply_markup=get_main_keyboard())
    return ConversationHandler.END

# ─── Inline Button Callbacks ───

async def button_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    data = query.data
    
    # Filter pipeline
    if data.startswith("filter:"):
        await filter_apps_callback(update, context)
        return
    
    # Update status
    if data.startswith("status:"):
        parts = data.split(":")
        app_id = parts[1]
        new_status = parts[2]
        update_application_status(app_id, new_status)
        emoji = STATUS_EMOJI.get(new_status, "📤")
        await query.edit_message_text(f"{emoji} Status updated to **{new_status.title()}**!")
        return
    
    # Show status update buttons
    if data.startswith("update:"):
        app_id = data.split(":")[1]
        app = get_application(app_id)
        if app:
            await query.edit_message_text(
                f"📝 Update status for **{app['company']}** — {app['role']}",
                reply_markup=get_status_keyboard(app_id)
            )
        return
    
    # Delete application
    if data.startswith("delete_app:"):
        app_id = data.split(":")[1]
        app = get_application(app_id)
        if app:
            delete_application(app_id)
            await query.edit_message_text(
                f"🗑️ Deleted **{app['company']}** — {app['role']}",
                parse_mode="Markdown"
            )
        else:
            await query.edit_message_text("⚠️ Application not found.")
        return

    # Draft follow-up
    if data.startswith("draft_followup:"):
        app_id = data.split(":")[1]
        app = get_application(app_id)
        if app:
            await query.edit_message_text(f"✍️ Drafting follow-up for {app['company']}...")
            try:
                applied = datetime.fromisoformat(app['applied_date'].replace('Z', '+00:00'))
                days = (datetime.utcnow() - applied.replace(tzinfo=None)).days
                
                email = await draft_follow_up(app['company'], app['role'], days)
                mark_follow_up_sent(app_id)
                
                await context.bot.send_message(
                    chat_id=query.message.chat_id,
                    text=f"📧 **Follow-Up Email for {app['company']}**\n──────────────────\n\n{email}\n\n✅ Copy, paste, and send!"
                )
            except Exception as e:
                await context.bot.send_message(
                    chat_id=query.message.chat_id,
                    text="Error drafting follow-up."
                )
        return
    
    # Tailor resume for a specific application
    if data.startswith("tailor_for:"):
        app_id = data.split(":")[1]
        app = get_application(app_id)
        if app and app.get("job_description"):
            await query.edit_message_text(f"✍️ Tailoring resume for {app['company']}...")
            user = update.effective_user
            db_user, _ = get_or_create_user(user.id, user.username)
            try:
                result = await tailor_resume(db_user.get("resume_text", ""), app['job_description'], db_user.get("target_role"))
                await context.bot.send_message(
                    chat_id=query.message.chat_id,
                    text=f"📄 **Tailored Resume for {app['company']}**\n──────────────────\n\n{result.get('tailored_resume', '')}"
                )
            except:
                await context.bot.send_message(chat_id=query.message.chat_id, text="Error tailoring resume.")
        else:
            await query.edit_message_text("⚠️ No job description saved for this application. Use ✍️ Tailor Resume from the main menu.")
        return
    # Follow-up from actions
    if data.startswith("followup:"):
        app_id = data.split(":")[1]
        app = get_application(app_id)
        if app:
            await query.edit_message_text(f"✍️ Drafting follow-up for {app['company']}...")
            try:
                applied = datetime.fromisoformat(app['applied_date'].replace('Z', '+00:00'))
                days = max((datetime.utcnow() - applied.replace(tzinfo=None)).days, 1)
                email = await draft_follow_up(app['company'], app['role'], days)
                mark_follow_up_sent(app_id)
                await context.bot.send_message(
                    chat_id=query.message.chat_id,
                    text=f"📧 **Follow-Up for {app['company']}**\n──────────────────\n\n{email}"
                )
            except:
                await context.bot.send_message(chat_id=query.message.chat_id, text="Error drafting follow-up.")
        return

# ─── Gmail Integration ───

async def handle_gmail(update: Update, context: ContextTypes.DEFAULT_TYPE):
    # Show typing immediately so it feels instant
    await context.bot.send_chat_action(chat_id=update.effective_chat.id, action="typing")
    
    user = update.effective_user
    db_user, _ = get_or_create_user(user.id, user.username)
    accounts = get_gmail_accounts(db_user['id'])
    
    keyboard = []
    text = "🔗 **Email Tracking**\n──────────────────\n\n"

    if not accounts:
        text += (
            "Track your applications automatically — I'll read the job emails you send me "
            "and update your pipeline.\n\n"
            "**📮 Forward job emails** _(recommended)_\n"
            "You get a private address and forward job mail to it. I never touch your inbox, "
            "and it takes one setup in Gmail.\n"
        )
    else:
        text += "Select ❌ to remove an account.\n\n"
        for acc in accounts:
            keyboard.append([
                InlineKeyboardButton(f"✅ {acc['email_address']}", callback_data="ignore"),
                InlineKeyboardButton("❌ Remove", callback_data=f"remove_gmail:{acc['id']}")
            ])
        # Time-based scan buttons
        text += "📅 **Scan inbox for job emails:**\n"
        keyboard.append([
            InlineKeyboardButton("⏰ Last 24h", callback_data="sync_gmail:1"),
            InlineKeyboardButton("📆 Last Week", callback_data="sync_gmail:7")
        ])
        keyboard.append([
            InlineKeyboardButton("🗓️ Last Month", callback_data="sync_gmail:30"),
            InlineKeyboardButton("📂 All Time", callback_data="sync_gmail:0")
        ])
        
    # Forwarding first: it works for everyone, needs no Google review, and never
    # asks for access to the user's mailbox.
    if os.getenv("INBOX_DOMAIN"):
        keyboard.append([InlineKeyboardButton("📮 Forward job emails to me", callback_data="show_forwarding")])

    # Only offer the OAuth route when it's actually configured — otherwise the
    # button leads to a broken page, which reads as the bot being broken.
    if os.getenv("GOOGLE_CLIENT_ID") or os.getenv("GOOGLE_CLIENT_SECRET"):
        url = f"{WEBHOOK_URL}/auth/gmail?user_id={db_user['id']}"
        keyboard.append([InlineKeyboardButton("➕ Connect Gmail directly", url=url)])

    await update.message.reply_text(
        text,
        reply_markup=InlineKeyboardMarkup(keyboard),
        parse_mode="Markdown"
    )


# Status rank — higher = more advanced stage. Never allow downgrade.
STATUS_RANK = {
    "applied": 1,
    "responded": 2,
    "assessment": 3,
    "interview": 4,
    "offer": 5,
    "rejected": 6,  # terminal — always keep if set
    "ghosted": 0,   # can always be overridden
}

async def sync_gmail_period_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handles Last 24h / Last Week / Last Month / All Time sync buttons."""
    query = update.callback_query
    await query.answer()

    days_back = int(query.data.split(":")[1])
    period_labels = {1: "Last 24 Hours", 7: "Last Week", 30: "Last Month", 0: "All Time"}
    period_label = period_labels.get(days_back, f"Last {days_back} days")

    user = update.effective_user
    db_user, _ = get_or_create_user(user.id, user.username)
    accounts = get_gmail_accounts(db_user['id'])

    if not accounts:
        await query.edit_message_text("⚠️ No Gmail accounts connected.")
        return

    await query.edit_message_text(f"🔍 Scanning inbox for **{period_label}** job emails...\nThis takes a moment.", parse_mode="Markdown")

    total_found = 0
    new_added = 0
    updated = 0
    details = ""

    for acc in accounts:
        try:
            token_dict = decrypt_token(acc["encrypted_token"])
            emails = fetch_recent_job_emails(token_dict, days_back=days_back, max_results=None)
            if not emails:
                continue

            result = await ingest_job_emails(db_user, emails)
            total_found += result['found']
            new_added += result['added']
            updated += result['updated']
            details += result['details']

            update_gmail_account_sync(acc['id'])
        except Exception as e:
            print(f"Error syncing {acc['email_address']}: {e}")
            details += f"⚠️ Failed to sync {acc['email_address']}\n"

    if total_found == 0:
        await query.edit_message_text(
            f"✅ **Scan Complete** — {period_label}\n\nNo job emails found in this period."
        )
    else:
        summary = f"✅ **Scan Complete** — {period_label}\n"
        summary += f"📧 Found **{total_found}** job emails\n"
        summary += f"➕ **{new_added}** new applications added\n"
        if updated:
            summary += f"🔄 **{updated}** status updates\n"
        summary += f"\n{details}"
        summary += "\nCheck **📊 My Apps** to see your full pipeline!"
        await query.edit_message_text(summary, parse_mode="Markdown")

async def sync_all_gmails_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Legacy sync — redirects to Last Week scan."""
    query = update.callback_query
    # Reuse period callback with 7 days
    query.data = "sync_gmail:7"
    await sync_gmail_period_callback(update, context)

async def remove_gmail_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    
    # data is like remove_gmail:account_id
    account_id = query.data.split(":")[1]
    
    # Remove from database
    remove_gmail_account(account_id)
    
    await query.edit_message_text("🗑️ Gmail account removed successfully.")

# ─── LinkedIn Job Finder ───

async def find_jobs_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "🔍 **AI LinkedIn Job Finder**\n\nWhat kind of role are you looking for today? (e.g., `Cybersecurity`, `React Developer`, `Data Analyst`)"
    )
    return FIND_JOBS_KEYWORD

async def find_jobs_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    data = query.data
    
    if data == "find_jobs_search":
        await query.edit_message_text("What kind of role are you looking for? (e.g., Remote React Developer in London)")
        return FIND_JOBS_KEYWORD
    elif data == "find_jobs_profile":
        user = update.effective_user
        db_user, _ = get_or_create_user(user.id, user.username)
        target = db_user.get('target_role')
        loc = db_user.get('location') or "Worldwide"
        
        if not target:
            await query.edit_message_text("⚠️ You haven't set a target role in your profile yet. Please search by keywords instead.")
            return ConversationHandler.END
            
        await query.edit_message_text(f"🔍 Searching LinkedIn for `{target}` jobs in `{loc}`...")
        await process_linkedin_search(update, context, db_user, target, loc, query_mode=True)
        return ConversationHandler.END

async def find_jobs_keyword_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    raw_keywords = update.message.text
    
    user = update.effective_user
    db_user, _ = get_or_create_user(user.id, user.username)
    user_loc = db_user.get("location") or "Worldwide"
    
    from ai_services import parse_job_search_query
    parsed = await parse_job_search_query(raw_keywords, default_location=None)  # Don't inject location at keyword step
    
    clean_kw = parsed.get("keywords", raw_keywords)
    context.user_data['fj_keywords'] = clean_kw
    # Pre-fill intent filters detected from keyword (user can override in next steps)
    context.user_data['fj_work_type'] = [parsed['work_type']] if parsed.get("work_type") else []
    context.user_data['fj_job_type'] = [parsed['job_type']] if parsed.get("job_type") else []
    context.user_data['fj_experience_level'] = [parsed['experience_level']] if parsed.get("experience_level") else []
    context.user_data['fj_date_posted'] = parsed.get("date_posted")
    context.user_data['fj_easy_apply'] = False

    keyboard = [[InlineKeyboardButton("Skip ➡️ (Worldwide)", callback_data="fj_loc_skip")]]
    await update.message.reply_text(
        f"🏢 **Step 1/5: Location**\n\nSearching for: `{md(clean_kw)}`\n\nWhich city or country? (e.g. `Lebanon`, `Beirut`, `Germany`, `Dubai`)\n\nAny place LinkedIn knows works — country, city or region.\n\nOr click **Skip** to search worldwide.",
        reply_markup=InlineKeyboardMarkup(keyboard),
        parse_mode="Markdown"
    )
    return FIND_JOBS_LOCATION

async def find_jobs_location_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.callback_query:
        query = update.callback_query
        await query.answer()
        location = "Worldwide"
        message_func = query.edit_message_text
    else:
        location = update.message.text
        message_func = update.message.reply_text

    # Resolve through LinkedIn's own geo service so the user sees the exact
    # place LinkedIn will search — and so typos/abbreviations get corrected.
    resolved, geo_id = resolve_location(location)
    context.user_data['fj_location'] = resolved

    header = f"📍 Location: **{md(resolved)}**"
    if not geo_id and resolved.lower() != "worldwide":
        header += "\n_(LinkedIn didn't recognise this exactly — searching by name)_"

    await message_func(
        f"{header}\n\n🏠 **Step 2/5: Work type**\n\nTap any that apply, then Continue:",
        reply_markup=filter_keyboard(WORK_TYPES, context.user_data.get('fj_work_type'), "fj_wt_", per_row=3),
        parse_mode="Markdown"
    )
    return FIND_JOBS_WORK_TYPE

async def find_jobs_work_type_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    code = query.data.replace("fj_wt_", "")

    if code not in ("done", "skip"):
        toggle_filter(context, 'fj_work_type', code)
        await query.edit_message_reply_markup(
            reply_markup=filter_keyboard(WORK_TYPES, context.user_data.get('fj_work_type'), "fj_wt_", per_row=3)
        )
        return FIND_JOBS_WORK_TYPE

    if code == "skip":
        context.user_data['fj_work_type'] = []

    await query.edit_message_text(
        "💼 **Step 3/5: Job type**\n\nTap any that apply, then Continue:",
        reply_markup=filter_keyboard(JOB_TYPES, context.user_data.get('fj_job_type'), "fj_jt_", per_row=2),
        parse_mode="Markdown"
    )
    return FIND_JOBS_JOB_TYPE

async def find_jobs_job_type_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    code = query.data.replace("fj_jt_", "")

    if code not in ("done", "skip"):
        toggle_filter(context, 'fj_job_type', code)
        await query.edit_message_reply_markup(
            reply_markup=filter_keyboard(JOB_TYPES, context.user_data.get('fj_job_type'), "fj_jt_", per_row=2)
        )
        return FIND_JOBS_JOB_TYPE

    if code == "skip":
        context.user_data['fj_job_type'] = []

    # Date posted is single-select on LinkedIn, so it advances on tap.
    keyboard = [
        [InlineKeyboardButton("⚡ Past 24 hours", callback_data="fj_dp_r86400")],
        [InlineKeyboardButton("🗓️ Past week", callback_data="fj_dp_r604800")],
        [InlineKeyboardButton("📅 Past month", callback_data="fj_dp_r2592000")],
        [InlineKeyboardButton("➡️ Skip (Any time)", callback_data="fj_dp_skip")]
    ]
    await query.edit_message_text(
        "📅 **Step 4/5: Date posted**\n\nHow recent should postings be?",
        reply_markup=InlineKeyboardMarkup(keyboard),
        parse_mode="Markdown"
    )
    return FIND_JOBS_DATE_POSTED

async def find_jobs_date_posted_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    dp = query.data.replace("fj_dp_", "")
    context.user_data['fj_date_posted'] = None if dp == "skip" else dp

    await query.edit_message_text(
        "🎓 **Step 5/5: Experience level**\n\nTap any that apply, then Continue:",
        reply_markup=filter_keyboard(EXPERIENCE_LEVELS, context.user_data.get('fj_experience_level'), "fj_exp_", per_row=2),
        parse_mode="Markdown"
    )
    return FIND_JOBS_EXPERIENCE

async def find_jobs_experience_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    code = query.data.replace("fj_exp_", "")

    if code not in ("done", "skip"):
        toggle_filter(context, 'fj_experience_level', code)
        await query.edit_message_reply_markup(
            reply_markup=filter_keyboard(EXPERIENCE_LEVELS, context.user_data.get('fj_experience_level'), "fj_exp_", per_row=2)
        )
        return FIND_JOBS_EXPERIENCE

    if code == "skip":
        context.user_data['fj_experience_level'] = []

    return await show_search_review(update, context)

async def show_search_review(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Final confirmation showing every filter, plus the matching LinkedIn URL."""
    query = update.callback_query
    kw = context.user_data.get('fj_keywords', 'Jobs')
    loc = context.user_data.get('fj_location', 'Worldwide')

    linkedin_url = build_linkedin_url(
        keywords=kw, location=loc,
        date_posted=context.user_data.get('fj_date_posted'),
        work_type=context.user_data.get('fj_work_type'),
        job_type=context.user_data.get('fj_job_type'),
        experience_level=context.user_data.get('fj_experience_level'),
        easy_apply=context.user_data.get('fj_easy_apply', False),
    )
    context.user_data['fj_linkedin_url'] = linkedin_url

    easy = context.user_data.get('fj_easy_apply', False)
    keyboard = [
        [InlineKeyboardButton(f"{'✅' if easy else '▫️'} Easy Apply only", callback_data="fj_go_easy")],
        [InlineKeyboardButton("🔗 Open this search on LinkedIn", url=linkedin_url)],
        [InlineKeyboardButton("🚀 Search", callback_data="fj_go_search")],
    ]
    await query.edit_message_text(
        f"🔎 **Review your search**\n\n"
        f"• **Keywords**: {md(kw)}\n"
        f"• **Location**: {md(loc)}\n"
        f"{filter_summary(context)}\n\n"
        f"The LinkedIn link above is this exact search — open it while signed in to compare.",
        reply_markup=InlineKeyboardMarkup(keyboard),
        parse_mode="Markdown"
    )
    return FIND_JOBS_REVIEW

async def find_jobs_go_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()

    if query.data == "fj_go_easy":
        context.user_data['fj_easy_apply'] = not context.user_data.get('fj_easy_apply', False)
        return await show_search_review(update, context)

    user = update.effective_user
    db_user, _ = get_or_create_user(user.id, user.username)

    await run_job_search(
        update, context, db_user,
        keywords=context.user_data.get('fj_keywords', 'Jobs'),
        location=context.user_data.get('fj_location', 'Worldwide'),
        date_posted=context.user_data.get('fj_date_posted'),
        work_type=context.user_data.get('fj_work_type'),
        job_type=context.user_data.get('fj_job_type'),
        experience_level=context.user_data.get('fj_experience_level'),
        easy_apply=context.user_data.get('fj_easy_apply', False),
        edit_query=query,
    )
    return ConversationHandler.END

async def run_job_search(update, context, db_user, keywords, location, date_posted=None,
                         work_type=None, job_type=None, experience_level=None,
                         easy_apply=False, edit_query=None):
    """
    Runs one LinkedIn search end to end.

    LinkedIn's signed-out endpoints honour keywords, location and date posted but
    silently ignore work type, job type and experience level. Those three are
    applied afterwards against each posting's declared LinkedIn criteria, so the
    result set matches what the same filters return on linkedin.com.
    """
    chat_id = update.effective_chat.id
    if not await require_credit(update, context, db_user):
        return

    # Meter from here: every AI call this search makes is counted and charged
    # afterwards from the API's own usage numbers, so a failed search is free.
    from ai_services import start_metering
    meter = start_metering()

    resolved_loc, _ = resolve_location(location)
    has_post_filters = bool(work_type or job_type or experience_level)

    status = f"🚀 **Searching LinkedIn...**\n\n🔍 `{md(keywords)}`\n📍 `{md(resolved_loc)}`"
    if edit_query:
        await edit_query.edit_message_text(status, parse_mode="Markdown")
    else:
        await context.bot.send_message(chat_id, status, parse_mode="Markdown")

    await context.bot.send_chat_action(chat_id=chat_id, action="typing")

    # Sized against MAX_SCORED_JOBS rather than "as many as possible": anything past
    # the scoring cap comes back unranked and unscored, which is noise rather than
    # choice. The filtered pool is larger because the filters discard part of it.
    #
    # This is also why the count here can differ from the number LinkedIn shows on
    # its own results page: LinkedIn reports everything it has, this fetches a
    # working set to rank.
    jobs = await asyncio.to_thread(
        search_linkedin_jobs,
        keywords=keywords,
        location=resolved_loc,
        limit=60 if has_post_filters else 50,
        date_posted=date_posted,
        easy_apply=easy_apply,
    )

    linkedin_url = build_linkedin_url(
        keywords=keywords, location=resolved_loc, date_posted=date_posted,
        work_type=work_type, job_type=job_type,
        experience_level=experience_level, easy_apply=easy_apply,
    )
    context.user_data['fj_linkedin_url'] = linkedin_url
    open_kb = InlineKeyboardMarkup([[InlineKeyboardButton("🔗 Open this search on LinkedIn", url=linkedin_url)]])

    if not jobs:
        await context.bot.send_message(
            chat_id,
            f"❌ LinkedIn returned no postings for `{md(keywords)}` in `{md(resolved_loc)}`.",
            reply_markup=open_kb, parse_mode="Markdown"
        )
        consume_tokens(db_user['id'], meter['total'])
        return

    if has_post_filters:
        await context.bot.send_message(
            chat_id,
            f"📥 Got **{len(jobs)}** postings. Checking each one's LinkedIn job type and seniority "
            f"(~{max(5, len(jobs) // 3)}s)...",
            parse_mode="Markdown"
        )
        await context.bot.send_chat_action(chat_id=chat_id, action="typing")
        jobs, stats = await asyncio.to_thread(
            apply_linkedin_filters, jobs,
            work_type=work_type, job_type=job_type, experience_level=experience_level,
        )
        print(f"LinkedIn filters: {stats} for '{keywords}' in '{resolved_loc}'")

        parts = []
        if stats.get('dropped'):
            parts.append(f"❌ {stats['dropped']} didn't match your filters")
        if stats['unverified']:
            parts.append(
                f"❔ {stats['unverified']} couldn't be confirmed by LinkedIn "
                f"(shown after the confirmed ones, and marked)"
            )
        if parts:
            await context.bot.send_message(
                chat_id,
                f"✅ **{stats['matched']}** confirmed matches of {stats['checked']} checked\n"
                + "\n".join(parts),
                parse_mode="Markdown",
            )

    if not jobs:
        keyboard = [
            [InlineKeyboardButton("🔓 Retry without filters", callback_data="fj_relax")],
            [InlineKeyboardButton("🔗 Open this search on LinkedIn", url=linkedin_url)],
        ]
        await context.bot.send_message(
            chat_id,
            f"🔎 **No matches**\n\nLinkedIn has postings for `{md(keywords)}` in `{md(resolved_loc)}`, "
            f"but none match all of these:\n{filter_summary(context)}",
            reply_markup=InlineKeyboardMarkup(keyboard), parse_mode="Markdown"
        )
        consume_tokens(db_user['id'], meter['total'])
        return

    from ai_services import match_jobs_to_profile
    has_cv = bool((db_user.get('resume_text') or "").strip())
    if has_cv:
        await context.bot.send_message(
            chat_id,
            f"🔍 **{len(jobs)}** jobs match your filters. Checking each one against your CV "
            f"requirement by requirement...",
            parse_mode="Markdown"
        )
    else:
        await context.bot.send_message(
            chat_id,
            f"🔍 **{len(jobs)}** jobs match your filters.\n\n"
            f"⚠️ You haven't uploaded a CV, so I can't rank these by fit — they're in "
            f"LinkedIn's own order. Upload one with **📄 My CV** to get match scores.",
            parse_mode="Markdown"
        )
    good_jobs = await match_jobs_to_profile(db_user, jobs, search_query=keywords)

    if not good_jobs:
        await context.bot.send_message(chat_id, f"❌ Couldn't score results for `{md(keywords)}`. Try different keywords.", parse_mode="Markdown")
        consume_tokens(db_user['id'], meter['total'])
        return

    context.user_data['last_search_jobs'] = good_jobs
    context.user_data['last_search_kw'] = keywords
    context.user_data['last_search_loc'] = resolved_loc

    consume_tokens(db_user['id'], meter['total'])
    print(f"Search used {meter['total']:,} tokens across {meter['calls']} calls")

    total = len(good_jobs)
    keyboard = [
        [InlineKeyboardButton("🏆 Top 5", callback_data="show_jobs:5"),
         InlineKeyboardButton("🔟 Top 10", callback_data="show_jobs:10")],
        [InlineKeyboardButton(f"📂 All ({total})", callback_data=f"show_jobs:{total}")],
        [InlineKeyboardButton("🔗 Open this search on LinkedIn", url=linkedin_url)],
    ]
    await context.bot.send_message(
        chat_id,
        f"✅ **{total} jobs found** for `{md(keywords)}` in `{md(resolved_loc)}`\n{filter_summary(context)}\n\nHow many do you want to see? (sorted by Profile Match)",
        reply_markup=InlineKeyboardMarkup(keyboard),
        parse_mode="Markdown"
    )

async def find_jobs_relax_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Re-runs the last search with the post-search filters dropped."""
    query = update.callback_query
    await query.answer()
    user = update.effective_user
    db_user, _ = get_or_create_user(user.id, user.username)

    context.user_data['fj_work_type'] = []
    context.user_data['fj_job_type'] = []
    context.user_data['fj_experience_level'] = []

    await run_job_search(
        update, context, db_user,
        keywords=context.user_data.get('fj_keywords', 'Jobs'),
        location=context.user_data.get('fj_location', 'Worldwide'),
        date_posted=context.user_data.get('fj_date_posted'),
        easy_apply=context.user_data.get('fj_easy_apply', False),
        edit_query=query,
    )

async def process_linkedin_search(update, context, db_user, raw_query, default_loc, query_mode):
    """Natural-language search path — parses the query, then runs the same pipeline."""
    # Parse query using AI — pass user's profile location as default so location is always correct
    user_default_loc = db_user.get("location") or default_loc or "Worldwide"
    parsed = await parse_job_search_query(raw_query, default_location=user_default_loc)

    keywords = parsed.get("keywords", raw_query)
    location = parsed.get("location") or user_default_loc

    context.user_data['fj_keywords'] = keywords
    context.user_data['fj_location'] = location
    context.user_data['fj_date_posted'] = parsed.get("date_posted")
    context.user_data['fj_work_type'] = [parsed['work_type']] if parsed.get("work_type") else []
    context.user_data['fj_job_type'] = [parsed['job_type']] if parsed.get("job_type") else []
    context.user_data['fj_experience_level'] = [parsed['experience_level']] if parsed.get("experience_level") else []
    context.user_data['fj_easy_apply'] = False

    await run_job_search(
        update, context, db_user,
        keywords=keywords,
        location=location,
        date_posted=parsed.get("date_posted"),
        work_type=context.user_data['fj_work_type'],
        job_type=context.user_data['fj_job_type'],
        experience_level=context.user_data['fj_experience_level'],
    )

async def show_jobs_count_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handles Top 5/10/25/All button presses after search results are scored."""
    query = update.callback_query
    await query.answer()
    count = int(query.data.split(":")[1])

    jobs = context.user_data.get('last_search_jobs', [])
    kw = context.user_data.get('last_search_kw', 'Jobs')
    loc = context.user_data.get('last_search_loc', '')
    chat_id = update.effective_chat.id

    if not jobs:
        await query.edit_message_text("⚠️ Session expired. Please run a new search.")
        return

    display = jobs[:count]
    await query.edit_message_text(
        f"📋 Showing **{len(display)}** of **{len(jobs)}** results for `{md(kw)}` in `{md(loc)}`:",
        parse_mode="Markdown"
    )
    ranked_total = sum(1 for j in jobs if j.get('scored'))
    await send_job_cards(context.bot, chat_id, display, kw, loc, total_ranked=ranked_total)

    # If there are more results, offer to load more
    shown = count
    remaining = len(jobs) - shown
    if remaining > 0:
        more_keyboard = [
            [InlineKeyboardButton(f"⬇️ Load {min(remaining, 10)} More", callback_data=f"show_jobs_more:{shown}:{min(shown+10, len(jobs))}")]
        ]
        await context.bot.send_message(
            chat_id,
            f"📄 Showing {shown}/{len(jobs)} results. {remaining} more available.",
            reply_markup=InlineKeyboardMarkup(more_keyboard)
        )

async def show_jobs_more_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Loads next batch of jobs when user clicks 'Load More'."""
    query = update.callback_query
    await query.answer()
    parts = query.data.split(":")
    start = int(parts[1])
    end = int(parts[2])

    jobs = context.user_data.get('last_search_jobs', [])
    kw = context.user_data.get('last_search_kw', 'Jobs')
    loc = context.user_data.get('last_search_loc', '')
    chat_id = update.effective_chat.id

    if not jobs:
        await query.edit_message_text("⚠️ Session expired. Please run a new search.")
        return

    display = jobs[start:end]
    await query.edit_message_text(f"📋 Loading jobs {start+1}–{end}...")
    ranked_total = sum(1 for j in jobs if j.get('scored'))
    await send_job_cards(context.bot, chat_id, display, kw, loc, start_idx=start, total_ranked=ranked_total)

    remaining = len(jobs) - end
    if remaining > 0:
        more_keyboard = [
            [InlineKeyboardButton(f"⬇️ Load {min(remaining, 10)} More", callback_data=f"show_jobs_more:{end}:{min(end+10, len(jobs))}")]
        ]
        await context.bot.send_message(
            chat_id,
            f"📄 Showing {end}/{len(jobs)} results. {remaining} more available.",
            reply_markup=InlineKeyboardMarkup(more_keyboard)
        )

async def job_analysis_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    data = query.data
    
    try:
        idx = int(data.split(":")[1])
        jobs = context.user_data.get('last_search_jobs', [])
        
        if not jobs or idx >= len(jobs):
            await query.message.reply_text("⚠️ Job data expired. Please run a new search.")
            return
            
        job = jobs[idx]
        text = (f"📊 **Full Analysis: {md(job.get('title', 'Role'))}**\n"
                f"🏢 {md(job.get('company', 'Company'))}\n──────────────────\n\n")

        if job.get('scored'):
            text += f"⭐ **Match: {job.get('score', 0)}%** — {job.get('grade', '')}\n"
            if job.get('verdict_summary'):
                text += f"💬 _{md(job['verdict_summary'])}_\n"
            fit = job.get('seniority_fit')
            if fit and fit != "match":
                text += f"📉 Seniority: you are **{md(fit)}** this role's level\n"
            field = job.get('field_fit')
            if field == "different":
                text += "🔀 **Different field** — this is a career change from your background\n"
            elif field == "adjacent":
                text += "↔️ Adjacent field — a realistic sideways move\n"
            text += "\n"

        # Show each requirement with the ruling the percentage was computed from,
        # so the number is auditable rather than something to take on faith.
        requirements = job.get('requirements') or []
        if requirements:
            icons = {"met": "✅", "partial": "🟨", "missing": "❌"}
            text += "**Requirement by requirement:**\n"
            for r in requirements:
                verdict = str(r.get('verdict', 'missing')).lower()
                # U+2217, not "*": Telegram's legacy Markdown has no backslash escape,
                # so a real asterisk opened an emphasis span and ate the footer.
                star = "∗" if str(r.get('importance', '')).lower() == "must" else ""
                text += f"{icons.get(verdict, '•')} {md(r.get('text', ''))}{star}\n"
                if r.get('evidence'):
                    text += f"     _{md(r['evidence'])}_\n"
            text += "\n∗ = required by the posting\n"
        elif job.get('unreadable'):
            text += ("⚠️ **Couldn't read this posting.**\n\n"
                     "LinkedIn didn't return the job description, so there is nothing "
                     "reliable to compare against your CV — and I'd rather tell you that "
                     "than invent requirements.\n\n"
                     "👉 Open the job on LinkedIn and check its requirements yourself.")
        else:
            matched = job.get('matched_qualifications', [])
            missing = job.get('missing_qualifications', [])
            if matched:
                text += "✅ **Matched Requirements:**\n" + "".join(f" • {md(m)}\n" for m in matched) + "\n"
            if missing:
                text += "❌ **Missing Requirements:**\n" + "".join(f" • {md(m)}\n" for m in missing)
            if not matched and not missing:
                text += "ℹ️ Detailed requirements could not be extracted for this job."

        await context.bot.send_message(
            chat_id=update.effective_chat.id,
            text=text,
            parse_mode="Markdown"
        )
    except Exception as e:
        await context.bot.send_message(
            chat_id=update.effective_chat.id,
            text="⚠️ Could not load full analysis."
        )

async def track_job_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Saves a search result into the application tracker.

    The job is looked up by its index in last_search_jobs — the same contract as
    job_analysis — so company and title reach the database whole, instead of
    truncated and re-split out of the callback data.
    """
    query = update.callback_query
    chat_id = update.effective_chat.id

    jobs = context.user_data.get('last_search_jobs', [])
    try:
        idx = int(query.data.split(":")[1])
    except (IndexError, ValueError):
        idx = -1

    if idx < 0 or idx >= len(jobs):
        await query.answer("⚠️ Job data expired. Please run a new search.", show_alert=True)
        return

    job = jobs[idx]
    await query.answer("📌 Saving...")

    user = update.effective_user
    db_user, _ = get_or_create_user(user.id, user.username)


    company = job.get('company') or "Unknown Company"
    role = job.get('title') or "Unknown Role"

    try:
        app = add_application(db_user['id'], company, role, job_url=job.get('url'))
    except Exception as e:
        print(f"Save to tracker failed ({company} — {role}): {e}")
        await context.bot.send_message(chat_id, "⚠️ Couldn't save this job. Please try again.")
        return

    await context.bot.send_message(
        chat_id,
        f"✅ **Saved to Tracker**\n\n"
        f"🏢 {md(company)}\n"
        f"💼 {md(role)}\n"
        f"📤 Status: Applied\n"
        f"📅 {datetime.now().strftime('%b %d, %Y')}",
        reply_markup=get_app_actions_keyboard(app['id']) if app else None,
        parse_mode="Markdown"
    )

# ─── CV Management ───

async def my_cv_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    db_user, _ = get_or_create_user(user.id, user.username)
    has_cv = bool(db_user.get("resume_text"))
    
    if has_cv:
        preview = db_user['resume_text'][:300] + "..." if len(db_user['resume_text']) > 300 else db_user['resume_text']
        text = f"📄 **Your Saved CV**\n──────────────────\n\n```\n{preview}\n```\n\nWhat would you like to do?"
        keyboard = [
            [InlineKeyboardButton("📤 Upload / Update CV", callback_data="cv_update")],
            [InlineKeyboardButton("🗑️ Delete CV", callback_data="cv_delete")]
        ]
    else:
        text = "📄 **No CV Saved**\n──────────────────\n\nYou haven't uploaded a CV yet. Upload your CV so the AI can match jobs and tailor applications for you!"
        keyboard = [
            [InlineKeyboardButton("📤 Upload CV", callback_data="cv_update")]
        ]
        
    await update.message.reply_text(text, reply_markup=InlineKeyboardMarkup(keyboard), parse_mode="Markdown")

async def cv_callback_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    data = query.data
    user = update.effective_user
    db_user, _ = get_or_create_user(user.id, user.username)
    
    if data == "cv_update":
        await query.edit_message_text(
            "📄 **Upload your CV**\n\nPlease send your CV as a **PDF file** or paste the text directly into the chat."
        )
        return ONBOARDING_RESUME
    elif data == "cv_delete":
        delete_user_cv(db_user['id'])
        await query.edit_message_text("🗑️ Your CV has been deleted successfully.")
        return ConversationHandler.END
        
# ─── Input Router (handles keyboard buttons) ───

async def input_router(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = update.message.text
    chat_id = update.effective_chat.id
    
    # Show typing immediately for ALL button presses — removes perceived lag
    await context.bot.send_chat_action(chat_id=chat_id, action="typing")
    
    user = update.effective_user
    db_user, _ = get_or_create_user(user.id, user.username)
    
    if text == "➕ Track":
        return await track_start(update, context)
    elif text == "📊 My Apps":
        await view_apps(update, context)
        return ConversationHandler.END
    elif text == "📈 Stats":
        await stats_command(update, context)
        return ConversationHandler.END
    elif text == "📧 Email Tracking":
        await handle_gmail(update, context)
        return ConversationHandler.END
    elif text == "📄 My CV":
        return await my_cv_command(update, context)
    elif text == "🔍 Find Jobs":
        return await find_jobs_start(update, context)
    elif text == "⭐ Searches":
        await credits_command(update, context)
        return ConversationHandler.END
    else:
        # Use AI to classify the user's natural language intent
        await context.bot.send_chat_action(chat_id=update.effective_chat.id, action="typing")
        intent = await classify_user_intent(text)
        
        if intent == "FIND_JOBS":
            user_default_loc = db_user.get("location") or "Worldwide"
            await update.message.reply_text(f"🔍 Searching LinkedIn for: `{text}`...")
            await process_linkedin_search(update, context, db_user, text, user_default_loc, query_mode=False)
            return ConversationHandler.END
        elif intent == "TAILOR_RESUME":
            await update.message.reply_text("✍️ It looks like you want to tailor your resume. Let's start the tailoring process.")
            context.user_data['job_desc'] = text
            return await tailor_process(update, context)
        elif intent == "TRACK_APP":
            await update.message.reply_text("➕ It looks like you want to track a new application.")
            return await track_start(update, context)
        else:
            await update.message.reply_text(
                "🤖 I'm your job search copilot. You can ask me to find jobs, track applications, or tailor your resume. "
                "Use the menu buttons below to navigate, or just tell me what you want to do!",
                reply_markup=get_main_keyboard(db_user)
            )
            return ConversationHandler.END
