
import sys; sys.path.append('.')
from linkedin_services import search_linkedin_jobs
jobs = search_linkedin_jobs('"Cybersecurity Technical Manager"', location='Beirut, Lebanon', limit=50)
print('Jobs with quotes:', len(jobs))

