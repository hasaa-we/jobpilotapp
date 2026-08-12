import os
from supabase import create_client, Client
from dotenv import load_dotenv

load_dotenv()

SUPABASE_URL = os.getenv("SUPABASE_URL")
SUPABASE_KEY = os.getenv("SUPABASE_KEY")

def get_supabase() -> Client:
    return create_client(SUPABASE_URL, SUPABASE_KEY)

# ─── User Management ───

def get_or_create_user(telegram_id: int, username: str = None, full_name: str = None):
    supabase = get_supabase()
    response = supabase.table("users").select("*").eq("telegram_id", telegram_id).execute()
    
    if response.data:
        return response.data[0], False
    
    new_user = {
        "telegram_id": telegram_id,
        "username": username,
        "full_name": full_name or username,
        "plan": "free"
    }
    result = supabase.table("users").insert(new_user).execute()
    return result.data[0], True

def update_user_profile(user_id: str, **kwargs):
    supabase = get_supabase()
    supabase.table("users").update(kwargs).eq("id", user_id).execute()

def get_user(user_id: str):
    supabase = get_supabase()
    response = supabase.table("users").select("*").eq("id", user_id).execute()
    return response.data[0] if response.data else None

# ─── Application Tracking ───

def add_application(user_id: str, company: str, role: str, job_url: str = None, job_description: str = None):
    supabase = get_supabase()
    data = {
        "user_id": user_id,
        "company": company,
        "role": role,
        "status": "applied"
    }
    if job_url: data["job_url"] = job_url
    if job_description: data["job_description"] = job_description
    
    result = supabase.table("applications").insert(data).execute()
    return result.data[0] if result.data else None

def update_application_status(app_id: str, status: str):
    supabase = get_supabase()
    supabase.table("applications").update({
        "status": status,
        "last_updated": "now()"
    }).eq("id", app_id).execute()

def get_applications(user_id: str, status: str = None, limit: int = 20):
    supabase = get_supabase()
    query = supabase.table("applications").select("*").eq("user_id", user_id)
    if status:
        query = query.eq("status", status)
    response = query.order("applied_date", desc=True).limit(limit).execute()
    return response.data

def get_application(app_id: str):
    supabase = get_supabase()
    response = supabase.table("applications").select("*").eq("id", app_id).execute()
    return response.data[0] if response.data else None

def delete_application(app_id: str):
    supabase = get_supabase()
    supabase.table("applications").delete().eq("id", app_id).execute()


def get_follow_up_candidates(user_id: str):
    """Get applications older than 7 days with no response and no follow-up sent."""
    supabase = get_supabase()
    response = supabase.table("applications").select("*").eq("user_id", user_id).eq("status", "applied").eq("follow_up_sent", False).order("applied_date", desc=False).limit(10).execute()
    return response.data

def mark_follow_up_sent(app_id: str):
    supabase = get_supabase()
    supabase.table("applications").update({"follow_up_sent": True}).eq("id", app_id).execute()

# ─── Generated Documents ───

def save_generated_doc(user_id: str, doc_type: str, original_text: str, generated_text: str, match_score: int = None, application_id: str = None):
    supabase = get_supabase()
    data = {
        "user_id": user_id,
        "doc_type": doc_type,
        "original_text": original_text,
        "generated_text": generated_text
    }
    if match_score: data["match_score"] = match_score
    if application_id: data["application_id"] = application_id
    
    result = supabase.table("generated_docs").insert(data).execute()
    return result.data[0] if result.data else None

# ─── Stats ───

def get_user_stats(user_id: str) -> dict:
    supabase = get_supabase()
    response = supabase.table("applications").select("status, company").eq("user_id", user_id).execute()
    data = response.data or []
    
    stats = {
        "total": len(data),
        "applied": 0,
        "responded": 0,
        "interview": 0,
        "offer": 0,
        "rejected": 0,
        "ghosted": 0
    }
    for item in data:
        s = item.get("status", "applied")
        stats[s] = stats.get(s, 0) + 1
    
    if stats["total"] > 0:
        stats["response_rate"] = round(((stats["responded"] + stats["interview"] + stats["offer"]) / stats["total"]) * 100)
    else:
        stats["response_rate"] = 0
        
    return stats

# ─── Gmail Accounts ───

def get_gmail_accounts(user_id: str):
    supabase = get_supabase()
    response = supabase.table("gmail_accounts").select("*").eq("user_id", user_id).execute()
    return response.data

def add_gmail_account(user_id: str, email_address: str, encrypted_token: str):
    supabase = get_supabase()
    data = {
        "user_id": user_id,
        "email_address": email_address,
        "encrypted_token": encrypted_token
    }
    # We use upsert so if they reconnect the same email, it just updates the token
    result = supabase.table("gmail_accounts").upsert(data, on_conflict="user_id,email_address").execute()
    return result.data[0] if result.data else None

def update_gmail_account_sync(account_id: str):
    supabase = get_supabase()
    supabase.table("gmail_accounts").update({"last_sync": "now()"}).eq("id", account_id).execute()

def remove_gmail_account(account_id: str):
    supabase = get_supabase()
    supabase.table("gmail_accounts").delete().eq("id", account_id).execute()


def delete_user_cv(user_id: str):
    supabase = get_supabase()
    supabase.table("users").update({"resume_text": None}).eq("id", user_id).execute()

# ─── Forwarding Inbox ───

def get_or_create_inbox_token(user_id: str) -> str:
    """
    The user's private forwarding token, minted on first use.

    Recruiting mail is matched back to a user purely by the address it was sent to,
    so this token is a credential: anyone who learns it can post applications into
    that account. It's random rather than derived from the user id for exactly that
    reason.
    """
    import secrets
    supabase = get_supabase()

    existing = supabase.table("users").select("inbox_token").eq("id", user_id).execute()
    if existing.data and existing.data[0].get("inbox_token"):
        return existing.data[0]["inbox_token"]

    token = secrets.token_hex(5)   # 10 hex chars
    supabase.table("users").update({"inbox_token": token}).eq("id", user_id).execute()
    return token

# ─── Search Credits ───

FREE_SEARCHES_PER_MONTH = 5   # resets on the 1st
CREDITS_PER_PACK = 200        # what one purchase buys


def _current_period() -> str:
    from datetime import datetime, timezone
    return datetime.now(timezone.utc).strftime("%Y-%m")


def get_credits(user_id: str) -> dict:
    """
    Returns {'free_left', 'paid', 'total'}.

    The free allowance rolls over lazily: if the stored period isn't this month,
    it already counts as reset. Nothing scheduled has to run for that to be true.
    """
    supabase = get_supabase()
    response = supabase.table("users").select(
        "search_credits, free_searches_used, free_period"
    ).eq("id", user_id).execute()
    if not response.data:
        return {"free_left": 0, "paid": 0, "total": 0}

    row = response.data[0]
    paid = row.get("search_credits") or 0
    used = row.get("free_searches_used") or 0
    if row.get("free_period") != _current_period():
        used = 0                                    # new month, allowance restored

    free_left = max(0, FREE_SEARCHES_PER_MONTH - used)
    return {"free_left": free_left, "paid": paid, "total": free_left + paid}


def consume_credit(user_id: str, amount: int = 1) -> bool:
    """
    Spends one search: the free monthly allowance first, then purchased credits.

    Free before paid matters — spending someone's purchase while they still have
    free searches left would be taking money for nothing.

    Read-then-write, so two searches fired at the same instant could both pass the
    check. Losing a credit occasionally is the right way to fail here; the
    alternative is a database function for something worth a fraction of a cent.
    """
    balance = get_credits(user_id)
    if balance["total"] < amount:
        return False

    supabase = get_supabase()
    period = _current_period()

    from_free = min(amount, balance["free_left"])
    from_paid = amount - from_free

    update = {
        "free_searches_used": (FREE_SEARCHES_PER_MONTH - balance["free_left"]) + from_free,
        "free_period": period,
    }
    if from_paid:
        update["search_credits"] = balance["paid"] - from_paid

    supabase.table("users").update(update).eq("id", user_id).execute()
    return True


def add_credits(user_id: str, credits: int, stripe_event_id: str,
                amount_cents: int = None, currency: str = None) -> bool:
    """
    Credits a purchase, once.

    Stripe retries webhooks until it gets a 2xx, so the same payment arrives more
    than once. The unique constraint on stripe_event_id is what makes a repeat a
    no-op instead of free credits.
    """
    supabase = get_supabase()
    try:
        supabase.table("credit_purchases").insert({
            "user_id": user_id,
            "stripe_event_id": stripe_event_id,
            "credits": credits,
            "amount_cents": amount_cents,
            "currency": currency,
        }).execute()
    except Exception as e:
        print(f"Purchase {stripe_event_id} already recorded, not crediting again: {e}")
        return False

    supabase.table("users").update(
        {"search_credits": get_credits(user_id)["paid"] + credits}
    ).eq("id", user_id).execute()
    return True


def get_user_by_telegram_id(telegram_id: int):
    supabase = get_supabase()
    response = supabase.table("users").select("*").eq("telegram_id", telegram_id).execute()
    return response.data[0] if response.data else None


def get_user_by_inbox_token(token: str):
    if not token:
        return None
    supabase = get_supabase()
    response = supabase.table("users").select("*").eq("inbox_token", token).execute()
    return response.data[0] if response.data else None
