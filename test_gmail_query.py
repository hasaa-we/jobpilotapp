import sys, asyncio
sys.path.append("c:/Users/hasan/OneDrive/Desktop/jobpilot")
from database import get_supabase
from gmail_services import fetch_recent_job_emails, decrypt_token

sb = get_supabase()
res = sb.table("gmail_accounts").select("*").execute()
if not res.data:
    print("No gmail accounts.")
    sys.exit(0)
token_dict = res.data[0]["encrypted_token"]
token = decrypt_token(token_dict)

try:
    emails = fetch_recent_job_emails(token, max_results=10, days_back=0)
    print(f"Found {len(emails)} emails with current query.")
except Exception as e:
    print(f"Error: {e}")
