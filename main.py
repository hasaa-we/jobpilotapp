import os
import hmac
import asyncio
import hashlib
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request, HTTPException
from fastapi.responses import RedirectResponse
from telegram import Update, BotCommand
from telegram.ext import Application, CommandHandler, MessageHandler, CallbackQueryHandler, filters, ConversationHandler, ContextTypes
from dotenv import load_dotenv

from database import update_user_profile, add_gmail_account, get_user_by_inbox_token
from gmail_services import (
    get_auth_url, exchange_code_for_token, encrypt_token, verify_oauth_state
)
from email_ingest import parse_raw_email, address_token, detect_forwarding_confirmation

from handlers import (
    start_command, input_router,
    onboarding_resume, onboarding_resume_pdf, onboarding_role,
    sync_all_gmails_callback, sync_gmail_period_callback, remove_gmail_callback,
    track_start, track_company, track_role,
    tailor_start, tailor_process,
    interview_start, interview_company, interview_role,
    follow_ups, stats_command, help_command, cancel,
    button_handler,
    ONBOARDING_RESUME, ONBOARDING_ROLE,
    TRACK_COMPANY, TRACK_ROLE,
    RESUME_JOB_DESC,
    INTERVIEW_COMPANY, INTERVIEW_ROLE,
    FIND_JOBS_KEYWORD, FIND_JOBS_LOCATION, FIND_JOBS_WORK_TYPE,
    FIND_JOBS_JOB_TYPE, FIND_JOBS_DATE_POSTED, FIND_JOBS_EXPERIENCE,
    FIND_JOBS_REVIEW,
    find_jobs_callback, find_jobs_keyword_handler,
    find_jobs_location_handler, find_jobs_work_type_handler,
    find_jobs_job_type_handler, find_jobs_date_posted_handler,
    find_jobs_experience_handler, find_jobs_go_handler, find_jobs_relax_callback,
    cv_callback_handler, job_analysis_callback, track_job_callback,
    show_jobs_count_callback, show_jobs_more_callback,
    ingest_job_emails, forwarding_command, forwarding_callback
)

load_dotenv()

TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
# Render publishes the service's real URL as RENDER_EXTERNAL_URL, so the webhook
# points at the right host without hardcoding it. An explicit WEBHOOK_URL still
# wins, which is what a custom domain in front of the app needs.
WEBHOOK_URL = (os.getenv("WEBHOOK_URL") or os.getenv("RENDER_EXTERNAL_URL") or "").rstrip("/")

# Shared with Telegram so forged updates can be rejected. Derived from the bot token
# when unset, which keeps existing deployments working without a new env var while
# staying unguessable to anyone who doesn't already control the bot.
WEBHOOK_SECRET = os.getenv("WEBHOOK_SECRET") or hashlib.sha256(
    f"tg-webhook:{TELEGRAM_BOT_TOKEN}".encode()
).hexdigest()[:48]

# ─── Build PTB Application ───
ptb_app = Application.builder().token(TELEGRAM_BOT_TOKEN).read_timeout(60).write_timeout(60).build()

# ─── Register Commands ───
ptb_app.add_handler(CommandHandler("help", help_command))
ptb_app.add_handler(CommandHandler("stats", stats_command))
ptb_app.add_handler(CommandHandler("followups", follow_ups))
ptb_app.add_handler(CommandHandler("forwarding", forwarding_command))

# ─── Main Conversation Handler ───
conv_handler = ConversationHandler(
    entry_points=[
        CommandHandler("start", start_command),
        CommandHandler("strat", start_command),
        CommandHandler("track", track_start),
        CommandHandler("resume", tailor_start),
        CommandHandler("interview", interview_start),
        CallbackQueryHandler(find_jobs_callback, pattern="^find_jobs_"),
        CallbackQueryHandler(cv_callback_handler, pattern="^cv_"),
        MessageHandler(filters.TEXT & ~filters.COMMAND, input_router)
    ],
    states={
        ONBOARDING_RESUME: [
            MessageHandler(filters.Document.PDF, onboarding_resume_pdf),
            MessageHandler(filters.TEXT & ~filters.COMMAND, onboarding_resume)
        ],
        ONBOARDING_ROLE: [MessageHandler(filters.TEXT & ~filters.COMMAND, onboarding_role)],
        TRACK_COMPANY: [MessageHandler(filters.TEXT & ~filters.COMMAND, track_company)],
        TRACK_ROLE: [MessageHandler(filters.TEXT & ~filters.COMMAND, track_role)],
        RESUME_JOB_DESC: [MessageHandler(filters.TEXT & ~filters.COMMAND, tailor_process)],
        INTERVIEW_COMPANY: [MessageHandler(filters.TEXT & ~filters.COMMAND, interview_company)],
        INTERVIEW_ROLE: [MessageHandler(filters.TEXT & ~filters.COMMAND, interview_role)],
        FIND_JOBS_KEYWORD: [MessageHandler(filters.TEXT & ~filters.COMMAND, find_jobs_keyword_handler)],
        FIND_JOBS_LOCATION: [
            CallbackQueryHandler(find_jobs_location_handler, pattern="^fj_loc_"),
            MessageHandler(filters.TEXT & ~filters.COMMAND, find_jobs_location_handler)
        ],
        FIND_JOBS_WORK_TYPE: [CallbackQueryHandler(find_jobs_work_type_handler, pattern="^fj_wt_")],
        FIND_JOBS_JOB_TYPE: [CallbackQueryHandler(find_jobs_job_type_handler, pattern="^fj_jt_")],
        FIND_JOBS_DATE_POSTED: [CallbackQueryHandler(find_jobs_date_posted_handler, pattern="^fj_dp_")],
        FIND_JOBS_EXPERIENCE: [CallbackQueryHandler(find_jobs_experience_handler, pattern="^fj_exp_")],
        FIND_JOBS_REVIEW: [CallbackQueryHandler(find_jobs_go_handler, pattern="^fj_go_")]
    },
    fallbacks=[CommandHandler("cancel", cancel)]
)

async def error_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Log the error and send a friendly message to the user."""
    print(f"Update {update} caused error {context.error}")
    try:
        if update and update.effective_message:
            await update.effective_message.reply_text(
                "⚠️ Oops! Something went wrong on my end. Please try again."
            )
    except Exception:
        pass

ptb_app.add_handler(conv_handler)
ptb_app.add_handler(CallbackQueryHandler(sync_all_gmails_callback, pattern="^sync_all_gmails$"))
ptb_app.add_handler(CallbackQueryHandler(sync_gmail_period_callback, pattern="^sync_gmail:"))
ptb_app.add_handler(CallbackQueryHandler(remove_gmail_callback, pattern="^remove_gmail:"))
ptb_app.add_handler(CallbackQueryHandler(job_analysis_callback, pattern="^job_analysis:"))
ptb_app.add_handler(CallbackQueryHandler(track_job_callback, pattern="^track_job:"))
ptb_app.add_handler(CallbackQueryHandler(find_jobs_relax_callback, pattern="^fj_relax$"))
ptb_app.add_handler(CallbackQueryHandler(forwarding_callback, pattern="^show_forwarding$"))
ptb_app.add_handler(CallbackQueryHandler(show_jobs_count_callback, pattern="^show_jobs:\d+$"))
ptb_app.add_handler(CallbackQueryHandler(show_jobs_more_callback, pattern="^show_jobs_more:"))
ptb_app.add_handler(CallbackQueryHandler(button_handler))
ptb_app.add_error_handler(error_handler)

# ─── FastAPI App ───

@asynccontextmanager
async def lifespan(app: FastAPI):
    # Startup
    await ptb_app.initialize()
    await ptb_app.start()
    
    # Set bot commands for the / menu
    commands = [
        BotCommand("start", "Set up your profile"),
        BotCommand("track", "Log a new application"),
        BotCommand("resume", "Tailor resume for a job"),
        BotCommand("interview", "Prep for an interview"),
        BotCommand("followups", "Check pending follow-ups"),
        BotCommand("forwarding", "Auto-track jobs by forwarding email"),
        BotCommand("stats", "Your dashboard"),
        BotCommand("help", "Available commands"),
        BotCommand("cancel", "Cancel current action")
    ]
    await ptb_app.bot.set_my_commands(commands)
    
    # Set webhook. The secret token is echoed back by Telegram on every call, which
    # is the only thing distinguishing a genuine update from a forged one — without
    # it, anyone who learns this URL can POST an update claiming any telegram_id and
    # act as that user.
    webhook_url = f"{WEBHOOK_URL}/webhook"
    await ptb_app.bot.set_webhook(
        url=webhook_url,
        allowed_updates=Update.ALL_TYPES,
        secret_token=WEBHOOK_SECRET,
    )
    print(f"Webhook set to {webhook_url}")
    
    yield
    
    # Shutdown
    await ptb_app.stop()
    await ptb_app.shutdown()

app = FastAPI(lifespan=lifespan)

@app.post("/webhook")
async def webhook(request: Request):
    if not hmac.compare_digest(
        request.headers.get("x-telegram-bot-api-secret-token", ""), WEBHOOK_SECRET
    ):
        raise HTTPException(status_code=403, detail="forbidden")

    data = await request.json()
    update = Update.de_json(data, ptb_app.bot)
    # Process in background to avoid webhook timeouts
    asyncio.create_task(ptb_app.process_update(update))
    return {"ok": True}

@app.get("/auth/gmail")
async def auth_gmail(user_id: str):
    """Redirects to Google OAuth consent screen."""
    url = get_auth_url(user_id)
    if url:
        return RedirectResponse(url)
    return {"error": "Could not generate auth URL"}

@app.get("/auth/gmail/callback")
async def auth_gmail_callback(state: str, code: str):
    """Handles Google OAuth callback and saves token."""
    # `state` used to be the bare user id, so anyone could complete this callback
    # for any account — attaching their mailbox to someone else's profile, or a
    # victim's mailbox to their own. It is now signed and time-limited.
    user_id = verify_oauth_state(state)
    if not user_id:
        return {"error": "This link has expired or is invalid. Please start again from the bot."}

    token_data = exchange_code_for_token(code)
    if token_data and token_data.get('email_address'):
        encrypted = encrypt_token(token_data)
        add_gmail_account(user_id, token_data['email_address'], encrypted)
        return {"success": True, "message": f"Gmail ({token_data['email_address']}) connected successfully! You can close this tab and return to Telegram."}
    return {"error": "Failed to authenticate or fetch email address"}

@app.post("/inbound/email")
async def inbound_email(request: Request):
    """
    Receives an email forwarded to a user's private address.

    The Cloudflare Worker posts the raw RFC822 message here rather than parsing it
    itself, so one code path covers both a Gmail filter forwarding single messages
    and "forward as attachment" carrying a whole backfill.
    """
    secret = os.getenv("INBOUND_SECRET")
    if not secret or not hmac.compare_digest(
        request.headers.get("x-inbound-secret", ""), secret
    ):
        # Anyone who can post here can write applications into someone's account.
        raise HTTPException(status_code=401, detail="unauthorized")

    payload = await request.json()
    raw = payload.get("raw") or ""
    token = address_token(payload.get("to") or "")

    db_user = get_user_by_inbox_token(token)
    if not db_user:
        # 200, not an error: a wrong address is the sender's problem, and retries
        # would achieve nothing.
        print(f"Inbound email for unknown token {token!r}")
        return {"ok": True, "ignored": "unknown address"}

    records = parse_raw_email(raw.encode("utf-8", errors="replace") if isinstance(raw, str) else raw)
    if not records:
        return {"ok": True, "ignored": "unreadable email"}

    # Gmail's forwarding confirmation is addressed here, not to the user, so relay it
    # before classification — otherwise it looks like a non-job email and is dropped,
    # leaving the user unable to finish setting up forwarding at all.
    confirmations = []
    job_records = []
    for record in records:
        found = detect_forwarding_confirmation(record)
        (confirmations.append(found) if found else job_records.append(record))

    # Logged so an unrecognised variant can be diagnosed from the Render logs
    # rather than by guessing at Gmail's wording.
    for record in records:
        print(f"Inbound: from={record.get('sender_email')!r} subject={record.get('subject')!r}")

    for confirmation in confirmations:
        message = "📬 **Gmail forwarding confirmation**\n\n"
        if confirmation.get("code"):
            message += f"Your confirmation code:\n\n`{confirmation['code']}`\n_(tap to copy)_\n\n"
            message += "Paste it into Gmail → Settings → *Forwarding and POP/IMAP* → **Verify**.\n"
        if confirmation.get("link"):
            message += f"\nOr just tap this link to confirm:\n{confirmation['link']}\n"
        if not confirmation.get("code") and not confirmation.get("link"):
            # Couldn't parse it — hand over the text so the user isn't stuck.
            message += (
                "I couldn't read the code automatically, so here's the email:\n\n"
                f"{confirmation.get('excerpt', '')}"
            )
        try:
            await ptb_app.bot.send_message(
                chat_id=db_user['telegram_id'], text=message, parse_mode="Markdown"
            )
        except Exception as e:
            print(f"Could not relay confirmation as Markdown ({e}); retrying as plain text")
            try:
                await ptb_app.bot.send_message(chat_id=db_user['telegram_id'], text=message)
            except Exception as inner:
                print(f"Could not relay confirmation to {db_user.get('telegram_id')}: {inner}")

    if not job_records:
        return {"ok": True, "confirmations": len(confirmations)}

    result = await ingest_job_emails(db_user, job_records)

    # Always say something. Silence after forwarding an email is indistinguishable
    # from the pipeline being broken, and "already tracked" is a real outcome —
    # a follow-up about a job you've already logged changes nothing.
    if result['added'] or result['updated']:
        message = (f"📥 **From your forwarded email**\n"
                   f"➕ {result['added']} new · 🔄 {result['updated']} updated\n\n"
                   f"{result['details']}")
    elif result['found']:
        message = (f"📥 **Already up to date**\n\n"
                   f"I recognised {result['found']} job email(s), but nothing changed — "
                   f"they're already tracked at the same stage or later.")
    else:
        message = ("📭 **Nothing to track**\n\n"
                   "I got your email but couldn't identify a company and role in it, "
                   "so I didn't add anything.")

    try:
        await ptb_app.bot.send_message(
            chat_id=db_user['telegram_id'], text=message, parse_mode="Markdown"
        )
    except Exception as e:
        print(f"Could not notify {db_user.get('telegram_id')}: {e}")

    return {"ok": True, "processed": len(records), **{k: result[k] for k in ('found', 'added', 'updated')}}


@app.get("/health")
async def health():
    return {"status": "alive", "bot": "JobPilot"}

if __name__ == "__main__":
    import uvicorn
    # Hosts assign the port; a hardcoded 8000 fails their health checks.
    uvicorn.run(app, host="0.0.0.0", port=int(os.getenv("PORT", "8000")))
