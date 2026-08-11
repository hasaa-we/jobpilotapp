import sys, asyncio
sys.path.append("c:/Users/hasan/OneDrive/Desktop/jobpilot")
from database import get_supabase
from gmail_services import fetch_recent_job_emails, decrypt_token
from ai_services import parse_job_email

async def main():
    sb = get_supabase()
    res = sb.table("gmail_accounts").select("*").execute()
    token_dict = res.data[0]["encrypted_token"]
    token = decrypt_token(token_dict)

    emails = fetch_recent_job_emails(token, max_results=10, days_back=30)
    for i, e in enumerate(emails):
        parsed = await parse_job_email(e['subject'], e['sender'], e['body'])
        print(f"[{i}] Subject: {e['subject']}")
        print(f"    AI Parsed: {parsed}")

asyncio.run(main())
