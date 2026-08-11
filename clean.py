
with open('handlers.py', 'r', encoding='utf-8') as f:
    lines = f.readlines()
new_lines = []
skip = False
for line in lines:
    if line.startswith('from gmail_services'): continue
    if '# --- Gmail Integration ---' in line: skip = True
    if skip and line.startswith('# --- Manage External Accounts ---'): skip = False
    if 'await handle_gmail(update, context)' in line: continue
    if not skip: new_lines.append(line)
with open('handlers.py', 'w', encoding='utf-8') as f:
    f.writelines(new_lines)

