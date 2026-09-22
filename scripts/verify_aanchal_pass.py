import os
from sqlalchemy import create_engine, text
from backend.passes import load_passes, to_pass_data, render_single

db_url = os.environ.get('DATABASE_URL', 'postgresql://convocation_user:convocation_password@localhost:5432/convocation_db')
engine = create_engine(db_url)

with engine.connect() as conn:
    row = conn.execute(text("SELECT id, prn, name, photo_path FROM students WHERE prn = '202201103126'")).mappings().one()
    print("Found student:", dict(row))
    passes = load_passes(conn, student_id=str(row["id"]))
    data = to_pass_data(passes)
    result = render_single(data[0], "Annual Convocation 2026")
    print("Pass render warnings:", result.warnings)
    print("Contains 'NO PHOTO' marker:", b"NO PHOTO" in result.pdf)
    print("PDF byte length:", len(result.pdf))
