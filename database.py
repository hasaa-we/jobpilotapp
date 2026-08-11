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

def get_user_by_inbox_token(token: str):
    if not token:
        return None
    supabase = get_supabase()
    response = supabase.table("users").select("*").eq("inbox_token", token).execute()
    return response.data[0] if response.data else None
