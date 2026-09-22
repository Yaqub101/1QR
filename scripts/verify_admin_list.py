import os
import re
import urllib.request
import urllib.error
from sqlalchemy import text
from backend.database import get_engine
from backend.security.sessions import create_session

db_url = os.environ.get("DATABASE_URL", "postgresql://convocation_user:convocation_password@localhost:5432/convocation_db")
if "@localhost:" in db_url and os.path.exists("/.dockerenv"):
    db_url = db_url.replace("@localhost:", "@db:")
elif "@db:" in db_url and not os.path.exists("/.dockerenv"):
    db_url = db_url.replace("@db:", "@localhost:")

engine = get_engine(db_url)

with engine.begin() as conn:
    admin = conn.execute(text("SELECT id, username FROM users WHERE role = 'ADMIN' AND active LIMIT 1")).mappings().one_or_none()
    if not admin:
        # Check all users
        all_u = conn.execute(text("SELECT id, username, role, active FROM users")).mappings().all()
        print("Existing users:", [dict(u) for u in all_u])
        raise RuntimeError("No active ADMIN user found in users table.")
    
    admin_id = admin["id"]
    token = create_session(conn, user_id=admin_id, max_hours=1)
    print(f"Logged in as admin '{admin['username']}', session created.")

# Perform HTTP request to live server at http://127.0.0.1:8000/admin/students
req = urllib.request.Request(
    "http://127.0.0.1:8000/admin/students",
    headers={"Cookie": f"session={token}"}
)

with urllib.request.urlopen(req) as resp:
    status = resp.status
    html = resp.read().decode("utf-8")

print(f"GET /admin/students -> HTTP status {status}, {len(html)} bytes")

# Find table rows with student PRN, Name, and Photo img tags
# Each row has: <td><img src="/photo/{student_id}" ...></td>, and nearby cells with PRN and name
# Let's extract them
pattern = re.compile(
    r'<tr>\s*<td>\s*<img\s+src="(/photo/([0-9a-fA-F-]+))"\s+alt="Photo"[^>]*>\s*</td>\s*<td>\s*<a\s+href="/admin/students/[^"]+">([^<]+)</a>\s*</td>\s*<td>([^<]+)</td>',
    re.DOTALL
)

matches = pattern.findall(html)
print(f"\nExtracted {len(matches)} student rows from the first page:")
print("-" * 80)
for photo_url, student_id, name, prn in matches[:10]:
    name_clean = " ".join(name.split())
    prn_clean = prn.strip()
    print(f"Student: {name_clean:<30} | PRN: {prn_clean:<14} | Photo URL: {photo_url}")

# Verify live /photo/{student_id} HTTP requests
print("\n" + "=" * 80)
print("Verifying live HTTP GET for sample photo endpoints:")
print("=" * 80)

for photo_url, student_id, name, prn in matches[:5]:
    photo_req = urllib.request.Request(
        f"http://127.0.0.1:8000{photo_url}",
        headers={"Cookie": f"session={token}"}
    )
    with urllib.request.urlopen(photo_req) as p_resp:
        data = p_resp.read()
        print(f"  GET {photo_url} (PRN {prn}): HTTP {p_resp.status}, Content-Type: {p_resp.headers.get('Content-Type')}, Size: {len(data)} bytes")

print("\nVerification complete.")
