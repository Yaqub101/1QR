"""Seed or update faculty login credentials for registration stations.

Usage:
    # Run with default database URL from environment / .env:
    python scripts/seed_faculty_credentials.py

    # Run with custom DATABASE_URL (e.g. Render external URL):
    DATABASE_URL=postgres://... python scripts/seed_faculty_credentials.py

    # Or pass explicit database URL:
    python scripts/seed_faculty_credentials.py --database-url postgresql://...
"""
from __future__ import annotations

import argparse
import os
import pathlib
import sys
from typing import Any, Mapping, Optional, Sequence

from sqlalchemy import create_engine, text
from sqlalchemy.engine import Engine

# Ensure repository root is on sys.path
REPO_ROOT = pathlib.Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from backend.audit import write_audit
from backend.security import passwords
from backend.security.sessions import revoke_user_sessions

# Embedded credentials dataset directly from Registration_Id_with_credentials.xlsx
# Guaranteed to work even if the xlsx file is not present in deployment containers.
CREDENTIALS_DATA: list[dict[str, Any]] = [
    {
        "sr": 1,
        "dept": "Arch",
        "username": "arch_rs",
        "password": "XST1678%",
        "coordinators": [
            {"name": "Prof. M. R. Deshmukh", "phone": "9422749918", "email": "mkashid@mgmu.ac.in"},
            {"name": "Prof. Medha naik", "phone": "7387550350", "email": "mnaik@mgmu.ac.in"},
        ],
    },
    {
        "sr": 2,
        "dept": "CSE",
        "username": "cse_rs",
        "password": "DLI0813*",
        "coordinators": [
            {"name": "Dr. J.D. Pagare ( Incharge)", "phone": "9823705514", "email": "jpagare@mgmu.ac.in"},
            {"name": "Ms. A.R.Tungar", "phone": "9067090978", "email": "atungar@mgmu.ac.in"},
            {"name": "Dr.. Archana Telgaonkar", "phone": "7796054648", "email": "atalegaokar@mgmu.ac.in"},
            {"name": "Ms.Sonal Sarnaik", "phone": "8177955555", "email": "ssarnaik@mgmu.ac.in"},
        ],
    },
    {
        "sr": 3,
        "dept": "Civil",
        "username": "civil_rs",
        "password": "NBG8029*",
        "coordinators": [
            {"name": "Prof. S.M.Siddiq", "phone": "7588812258", "email": "msiddiq@mgmu.ac.in"},
            {"name": "Prof. N.B.Sonar", "phone": "9819306354", "email": "nsonar@mgmu.ac.in"},
        ],
    },
    {
        "sr": 4,
        "dept": "Electrical",
        "username": "electrical_rs",
        "password": "WMI1369$",
        "coordinators": [
            {"name": "C.B.Ingole", "phone": "7972590046", "email": "cingole@mgmu.ac.in"},
        ],
    },
    {
        "sr": 5,
        "dept": "Electronics",
        "username": "electronic_rs",
        "password": "LEU8003*",
        "coordinators": [
            {"name": "Prof.V.A.Kulkarni", "phone": "9145354343", "email": "vkulkarni@mgmu.ac.in"},
            {"name": "Dr.S.D.Gavraskar", "phone": "9405602274", "email": "sgavaraskar@mgmu.ac.in"},
        ],
    },
    {
        "sr": 6,
        "dept": "Mechanical",
        "username": "mechanical_rs",
        "password": "FSP2779!",
        "coordinators": [
            {"name": "Dr. A. L. Chel", "phone": "91580 98381", "email": "achel@mgmu.ac.in"},
            {"name": "Prof D. S. Khedkar", "phone": "83901 00891", "email": "dkhedekar@mgmu.ac.in"},
            {"name": "Prof. R. A.Kathar", "phone": "75886 95628", "email": "rkathar@mgmu.ac.in"},
        ],
    },
    {
        "sr": 7,
        "dept": "Chemical",
        "username": "chemical_rs",
        "password": "PKI0679%",
        "coordinators": [
            {"name": "Dr. V A Gite", "phone": "8275311749", "email": "vgite@mgmu.ac.in"},
        ],
    },
    {
        "sr": 8,
        "dept": "MCA",
        "username": "mca_rs",
        "password": "EAC4913%",
        "coordinators": [
            {"name": "Mr. G.S.Koleshwar", "phone": "9860741911", "email": "gkoleshwar@mgmu.ac.in"},
        ],
    },
    {
        "sr": 9,
        "dept": "Social Sciences & Humanities",
        "username": "socialscie_rs",
        "password": "TBX1905#",
        "coordinators": [
            {"name": "Dr. Zainab Minhaj Khan", "phone": "7507635296", "email": "zkhan1@mgmu.ac.in"},
            {"name": "Dr. Amrapali Jogdand", "phone": "7057187824", "email": "ajogdand@mgmu.ac.in"},
            {"name": "Dr. Aswad Gowher", "phone": "9156469099", "email": "agowher@mgmu.ac.in"},
        ],
    },
    {
        "sr": 10,
        "dept": "IHM",
        "username": "ihm_rs",
        "password": "APX5063!",
        "coordinators": [
            {"name": "Bhagyashri Mathdewaru", "phone": "9158687723", "email": "bmathdewaru@mgmu.ac.in"},
            {"name": "Aishwarya Thavare", "phone": "9923364501", "email": "athavare@mgmu.ac.in"},
        ],
    },
    {
        "sr": 11,
        "dept": "IICT",
        "username": "iict_rs",
        "password": "RTI9484*",
        "coordinators": [
            {"name": "Dr Minakshi Rajput", "phone": "8830662725", "email": "mrajput@mgmu.ac.in"},
            {"name": "Ms Banani Adhikari", "phone": "7391866571", "email": "badhikari@mgmu.ac.in"},
            {"name": "Mr Vijay Kolte", "phone": "9423817565", "email": "vkolte@mgmu.ac.in"},
            {"name": "Mr. Asra Anjum", "phone": "9764179168", "email": "akhwaja@mgmu.ac.in"},
            {"name": "Mr. Parmeshwar Khope", "phone": "8600784380", "email": "pkhope@mgmu.ac.in"},
        ],
    },
    {
        "sr": 12,
        "dept": "SOET",
        "username": "soet_rs",
        "password": "ERP7263*",
        "coordinators": [
            {"name": "Mr.Amol Parihar", "phone": "8390161548", "email": "aparihar@mgmu.ac.in"},
        ],
    },
    {
        "sr": 13,
        "dept": "MAHAGAMI",
        "username": "mahagami_rs",
        "password": "SOM4443#",
        "coordinators": [
            {"name": "Vilas S. Lokhande", "phone": "9860700230", "email": "vlokhande@mgmu.ac.in"},
        ],
    },
    {
        "sr": 14,
        "dept": "NSBT",
        "username": "nsbt_rs",
        "password": "JGK6910%",
        "coordinators": [
            {"name": "Dr. Sanvedi Rane", "phone": "+918275225896", "email": "sanvedi.rane@nsbtmgmu.co.in"},
            {"name": "Rekha Joshi", "phone": "7057185766", "email": "rekhaupadhye567@gmail.com"},
        ],
    },
    {
        "sr": 15,
        "dept": "GYP",
        "username": "gyp_rs",
        "password": "WOI7900@",
        "coordinators": [
            {"name": "Dr Azade S. Y.", "phone": "/", "email": "sazade@mgmu.ac.in"},
            {"name": "Dr Pratibha Bhishe", "phone": "7843083953", "email": "pbhishe@mgmu.ac.in"},
            {"name": "Dr Bali Thorat", "phone": "9922332930", "email": "bthorat@mgmu.ac.in"},
        ],
    },
    {
        "sr": 16,
        "dept": "SBAS",
        "username": "sbas_rs",
        "password": "YSU0311*",
        "coordinators": [
            {"name": "Dr. P. D Shinde", "phone": "9325211386", "email": "pshinde1@mgmu.ac.in"},
            {"name": "Dr. Deepak Kawade", "phone": "8669178223", "email": "dkawade@mgmu.ac.in"},
        ],
    },
    {
        "sr": 17,
        "dept": "LSoD",
        "username": "lsod_rs",
        "password": "HXZ4368@",
        "coordinators": [
            {"name": "Ms. Kalyani Belkar", "phone": "8855006156", "email": "kbelkar@mgmu.ac.in"},
            {"name": "Ms. Priya Waghmare", "phone": "7410114420", "email": "pwaghmare@mgmu.ac.in"},
            {"name": "Ms. Shrushti Ambulgekar", "phone": "8999758026", "email": "sambulgekar@mgmu.ac.in"},
        ],
    },
    {
        "sr": 18,
        "dept": "IOMR",
        "username": "iomr_rs",
        "password": "DPP0620$",
        "coordinators": [
            {"name": "Dr.Mohini Shinde", "phone": "9607159191", "email": "mshinde@mgmu.ac.in"},
            {"name": "Mrs. Aarti Kulkarni", "phone": "9028247927", "email": "akulkarni@mgmu.ac.in"},
            {"name": "Mr. Amol Gaikwad", "phone": "9923695770", "email": "agaikwad@mgmu.ac.in"},
            {"name": "Mr. Swami Dandgaval", "phone": "9075312878", "email": "sdandgaval@mgmu.ac.in"},
            {"name": "Mr.Balaji Kadam", "phone": "9503656158", "email": "accountsiomr@mgmu.ac.in"},
            {"name": "Mr.Abhishek Akade", "phone": "9031323030", "email": "office.iomr@mgmu.ac.in"},
        ],
    },
    {
        "sr": 19,
        "dept": "FIDS",
        "username": "fids_rs",
        "password": "FMN9301$",
        "coordinators": [
            {"name": "Dr. Majushri Landge", "phone": "8806603344", "email": "mlandge@mgmu.ac.in"},
        ],
    },
    {
        "sr": 20,
        "dept": "IBT",
        "username": "ibt_rs",
        "password": "JQX3894@",
        "coordinators": [
            {"name": "Ms. B.S.Ghule", "phone": "8625988141", "email": "bghule@mgmu.ac.in"},
            {"name": "Dr. A.B. Chudiwal", "phone": "9404477577", "email": "anupriyajain77@gmail.com"},
            {"name": "Ms. Devyani Kadam", "phone": "9130372528", "email": "dkadam@mgmu.ac.in"},
            {"name": "Ms. Varsha Kadam", "phone": "922991955", "email": "varshakadam541@gmail.com"},
            {"name": "Ms. Megha Salvi", "phone": "7020427951", "email": "msalvi@mgmu.ac.in"},
            {"name": "Ms. Arpita Kharat", "phone": "9022281910", "email": "akharat@mgmu.ac.in"},
        ],
    },
]


def parse_excel_credentials(file_path: str) -> list[dict[str, Any]]:
    """Parse credentials dynamically from the provided Excel workbook."""
    import openpyxl

    wb = openpyxl.load_workbook(file_path)
    sheet = wb.active
    rows = list(sheet.iter_rows(values_only=True))

    data: list[dict[str, Any]] = []
    current_dept: Optional[str] = None
    current_sr: Optional[int] = None
    current_user: Optional[str] = None
    current_pwd: Optional[str] = None
    coords: list[dict[str, Optional[str]]] = []

    for r in rows[4:]:  # Data starts after header row
        sr, dept, coord, phone, email, username, pwd = r[:7]
        if sr is not None or (username is not None and str(username).strip() != ""):
            if current_user:
                data.append({
                    "sr": current_sr,
                    "dept": current_dept,
                    "username": current_user,
                    "password": current_pwd,
                    "coordinators": coords,
                })
            current_sr = sr
            current_dept = dept.strip() if isinstance(dept, str) else dept
            current_user = username.strip() if isinstance(username, str) else username
            current_pwd = str(pwd).strip() if pwd is not None else None
            coords = []

        if coord:
            coords.append({
                "name": coord.strip() if isinstance(coord, str) else str(coord),
                "phone": str(phone).strip() if phone is not None else None,
                "email": email.strip() if isinstance(email, str) else email,
            })

    if current_user:
        data.append({
            "sr": current_sr,
            "dept": current_dept,
            "username": current_user,
            "password": current_pwd,
            "coordinators": coords,
        })
    return data


def format_full_name(dept: str, coordinators: Sequence[Mapping[str, Any]]) -> str:
    """Format full_name for operator display, e.g. 'CSE - Dr. J.D. Pagare'."""
    if coordinators and coordinators[0].get("name"):
        primary = coordinators[0]["name"]
        return f"{dept} - {primary}"
    return f"{dept} Desk"


def seed_faculty_credentials(
    engine: Engine,
    credentials: Sequence[Mapping[str, Any]],
    *,
    role: str = "REGISTRY",
) -> dict[str, list[str]]:
    """Upsert credentials in the database in a single atomic transaction."""
    created: list[str] = []
    updated: list[str] = []

    # Pre-validate all passwords before opening the database transaction
    for item in credentials:
        username = str(item["username"]).strip().lower()
        password = str(item["password"]).strip()
        passwords.validate_password(password, role)

    with engine.begin() as conn:
        for item in credentials:
            username = str(item["username"]).strip()
            password = str(item["password"]).strip()
            dept = str(item.get("dept", "")).strip()
            coords = item.get("coordinators", [])
            full_name = format_full_name(dept, coords)
            p_hash = passwords.hash_password(password)

            existing = conn.execute(
                text("SELECT id, username, full_name, role FROM users WHERE lower(username) = lower(:u)"),
                {"u": username},
            ).mappings().one_or_none()

            audit_details = {
                "username": username,
                "role": role,
                "department": dept,
                "coordinators": [c["name"] for c in coords if "name" in c],
                "contacts": [c["phone"] for c in coords if c.get("phone")],
                "emails": [c["email"] for c in coords if c.get("email")],
            }

            if existing:
                user_id = existing["id"]
                conn.execute(
                    text(
                        "UPDATE users "
                        "SET password_hash = :h, full_name = :n, role = :r, active = true, updated_at = now() "
                        "WHERE id = :id"
                    ),
                    {"h": p_hash, "n": full_name, "r": role, "id": user_id},
                )
                revoke_user_sessions(conn, user_id)
                write_audit(
                    conn,
                    "PASSWORD_RESET",
                    operator_id=None,
                    details={"action": "FACULTY_CREDENTIALS_UPDATE", **audit_details, "user_id": user_id},
                )
                updated.append(username)
            else:
                user_id = conn.execute(
                    text(
                        "INSERT INTO users (username, password_hash, full_name, role, active) "
                        "VALUES (:u, :h, :n, :r, true) RETURNING id"
                    ),
                    {"u": username, "h": p_hash, "n": full_name, "r": role},
                ).scalar_one()
                write_audit(
                    conn,
                    "USER_CREATED",
                    operator_id=None,
                    details={"action": "FACULTY_CREDENTIALS_SEED", **audit_details, "user_id": user_id},
                )
                created.append(username)

    return {"created": created, "updated": updated}


def resolve_database_url(cli_arg: Optional[str] = None) -> str:
    url = cli_arg or os.environ.get("DATABASE_URL")
    if not url:
        try:
            from backend.config import get_settings
            url = get_settings().database_url
        except Exception:
            url = "postgresql://convocation_user:convocation_password@localhost:5432/convocation_db"
    if url.startswith("postgres://"):
        url = url.replace("postgres://", "postgresql://", 1)
    return url


def main() -> int:
    parser = argparse.ArgumentParser(description="Seed or update faculty credentials in database")
    parser.add_argument("--database-url", "--db-url", help="PostgreSQL connection string")
    parser.add_argument("--excel", help="Path to Registration_Id_with_credentials.xlsx")
    args = parser.parse_args()

    db_url = resolve_database_url(args.database_url)

    # Determine credential dataset
    credentials: list[dict[str, Any]] = CREDENTIALS_DATA
    excel_candidates = [
        args.excel,
        str(REPO_ROOT / "Registration_Id_with_credentials.xlsx"),
        "Registration_Id_with_credentials.xlsx",
    ]
    for cand in excel_candidates:
        if cand and os.path.isfile(cand):
            try:
                credentials = parse_excel_credentials(cand)
                print(f"[+] Loaded {len(credentials)} entries from Excel: {cand}")
                break
            except Exception as exc:
                print(f"[!] Note: Could not parse Excel ({cand}): {exc}. Using embedded credentials.")
                credentials = CREDENTIALS_DATA
                break
    else:
        print(f"[i] Using embedded credentials ({len(credentials)} entries).")

    print(f"Connecting to database...")
    engine = create_engine(db_url)
    try:
        res = seed_faculty_credentials(engine, credentials)
    except Exception as exc:
        print(f"[-] Seed error: {exc}", file=sys.stderr)
        return 1
    finally:
        engine.dispose()

    total_created = len(res["created"])
    total_updated = len(res["updated"])

    print("=" * 60)
    print("FACULTY CREDENTIALS SEED COMPLETE")
    print("=" * 60)
    print(f"Created accounts : {total_created}")
    print(f"Updated accounts : {total_updated}")
    print(f"Total processed  : {total_created + total_updated}")
    print("-" * 60)
    for c in credentials:
        u = c["username"]
        status = "CREATED" if u in res["created"] else "UPDATED"
        coords = c.get("coordinators", [])
        c_name = coords[0]["name"] if coords else "—"
        print(f"  [{status:<7}] {u:<16} ({c['dept']:<18}) - {c_name}")
    print("=" * 60)
    print("All logins updated successfully. Role: REGISTRY.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
