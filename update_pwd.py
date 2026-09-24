from sqlalchemy import create_engine, text
from backend.security.passwords import hash_password
engine = create_engine('postgresql://convocation_user:convocation_password@localhost:5432/convocation_db')
with engine.begin() as conn:
    h = hash_password('admin123')
    conn.execute(text("UPDATE users SET password_hash = :h WHERE username = 'admin'"), {'h': h})
