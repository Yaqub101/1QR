import sys
import io
import pathlib
sys.stdout.reconfigure(encoding='utf-8')
from sqlalchemy import create_engine, text
from backend.importer import read_and_validate, commit_import
from backend.database import get_engine, REQUIRED_TABLES
from backend.config import get_settings
from backend.snapshot import freeze_display_data

from tests.conftest import empty_database, run_alembic

def run():
    file_path = r'E:\JakobProjects\1Qrdata\StudentConvocationDetailReport_Fees Paid Student.xls'
    with open(file_path, "rb") as f:
        content = f.read()

    with empty_database("verify_real_import_test") as url:
        # Run alembic to create schema
        run_alembic("upgrade", "head", database_url=url)
        engine = get_engine(url)
        
        print("1. Showing preview output for the real file")
        cols, rows, mapping, preview = read_and_validate(engine, content, "StudentConvocationDetailReport_Fees Paid Student.xls")
        
        print("Validation Result:", "Valid" if preview.is_valid else "Invalid")
        print(f"To create: {len(preview.to_create)}")
        print(f"To skip: {len(preview.to_skip)}")
        print(f"Errors: {len(preview.errors)}")
        print(f"Flagged duplicates: {len(preview.flagged_duplicates)}")
        
        if preview.errors:
            print("ERRORS:")
            for e in preview.errors[:10]:
                print(f"  Row {e.row} ({e.field}): {e.message}")
            return
            
        print("\n2. Confirming 1,201 read / 1,200 created / 1 skipped")
        with engine.connect() as conn:
            summary = commit_import(preview, conn)
            # Freeze data so it matches the test condition
            freeze_display_data(conn)
            
        print(f"Summary: Read: {summary.read}, Created: {summary.created}, Skipped: {summary.skipped}")
        
        print("\n3. Confirming in the DB that NONE of the forbidden fields exist")
        with engine.connect() as conn:
            # Print table schema for students
            res = conn.execute(text("SELECT column_name FROM information_schema.columns WHERE table_name = 'students'"))
            columns = [r[0] for r in res.fetchall()]
            print("DB Columns in students table:", columns)
            forbidden = ["aadhar", "address", "guest", "payment", "cgpa", "marathi"]
            found_forbidden = any(f in c.lower() for c in columns for f in forbidden)
            if found_forbidden:
                print("FAILED: Forbidden fields found in DB schema!")
            else:
                print("SUCCESS: No forbidden fields in DB schema.")
                
            # Verify data
            row = conn.execute(text("SELECT * FROM students LIMIT 1")).mappings().fetchone()
            print("Sample row:", dict(row))
            
            # Find longest name/programme to generate PDF passes
            longest_name_row = conn.execute(text("SELECT prn, name, programme FROM students ORDER BY LENGTH(name) DESC LIMIT 1")).fetchone()
            longest_prog_row = conn.execute(text("SELECT prn, name, programme FROM students ORDER BY LENGTH(programme) DESC LIMIT 1")).fetchone()
            
        print(f"\n4. Generating PDF passes for longest strings")
        print(f"Longest Name: {longest_name_row[1]} (Length: {len(longest_name_row[1])})")
        print(f"Longest Programme: {longest_prog_row[2]} (Length: {len(longest_prog_row[2])})")
        
        # We need to run pdf generation for these PRNs
        # I'll do this in the next step using the proper backend tools if needed.

if __name__ == "__main__":
    run()
