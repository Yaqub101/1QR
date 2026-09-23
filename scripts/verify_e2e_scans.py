"""scripts/verify_e2e_scans.py — Live End-to-End Role-Based QR Scan Verification.

Verifies:
1. QR decodability against live PDF and database.
2. Role gating for all 8 roles (Reporting, Robe Allocation, Seating, Queue, Stage, Robe Return, Lunch, Admin).
3. Sequential/prerequisite enforcement.
4. Duplicate scan handling.
5. Full 7-activity journey for one student with intermediate status checks and final EXITED check.
6. Stage/LED behavior and privacy snapshot validation.
"""
from __future__ import annotations

import json
import os
import sys
import urllib.request
import urllib.error
from typing import Any, Dict, Optional, Tuple

import pypdfium2
import zxingcpp
from sqlalchemy import text
from backend.database import get_engine
from backend.config import get_settings

BASE_URL = os.environ.get("VERIFY_BASE_URL", "http://127.0.0.1:8000")


def http_req(
    path: str,
    method: str = "GET",
    body: Optional[dict] = None,
    token: Optional[str] = None,
    raw_response: bool = False,
) -> Tuple[int, Any, dict]:
    url = f"{BASE_URL}{path}"
    headers = {"Accept": "application/json"}
    data = None
    if body is not None:
        headers["Content-Type"] = "application/json"
        data = json.dumps(body).encode("utf-8")
    if token:
        headers["Authorization"] = f"Bearer {token}"

    req = urllib.request.Request(url, data=data, headers=headers, method=method)
    try:
        with urllib.request.urlopen(req) as resp:
            status = resp.status
            resp_headers = dict(resp.headers)
            content = resp.read()
            if raw_response:
                return status, content, resp_headers
            try:
                return status, json.loads(content.decode("utf-8")), resp_headers
            except Exception:
                return status, content.decode("utf-8", errors="replace"), resp_headers
    except urllib.error.HTTPError as err:
        status = err.code
        resp_headers = dict(err.headers)
        content = err.read()
        try:
            return status, json.loads(content.decode("utf-8")), resp_headers
        except Exception:
            return status, content.decode("utf-8", errors="replace"), resp_headers


def login(username: str, password: str) -> str:
    status, body, _ = http_req("/api/login", method="POST", body={"username": username, "password": password})
    if status != 200 or "token" not in body:
        raise RuntimeError(f"Login failed for {username}: HTTP {status} {body}")
    return body["token"]


def main():
    settings = get_settings()
    engine = get_engine(settings.database_url)

    print("=" * 80)
    print("END-TO-END ROLE-BASED QR SCAN VERIFICATION")
    print("=" * 80)

    # -------------------------------------------------------------------------
    # TASK 1: QR DECODABILITY
    # -------------------------------------------------------------------------
    print("\n" + "=" * 80)
    print("TASK 1: QR Decodability Verification")
    print("=" * 80)

    admin_token = login("admin", "adminpassword123")
    print(f"Logged in as Admin (session token: {admin_token[:10]}...)")

    with engine.connect() as conn:
        sample_student = conn.execute(
            text(
                "SELECT s.id, s.prn, s.name, s.programme, t.token "
                "FROM students s "
                "JOIN qr_tokens t ON t.student_id = s.id "
                "WHERE t.active AND s.status = 'ACTIVE' "
                "ORDER BY s.prn LIMIT 1"
            )
        ).mappings().one()

    test_sid = str(sample_student["id"])
    test_prn = sample_student["prn"]
    expected_token = sample_student["token"]

    print(f"Test Student: {sample_student['name']} (PRN: {test_prn}, ID: {test_sid})")
    print(f"Requesting generated pass PDF via: GET /admin/api/students/{test_sid}/pass.pdf")

    pdf_status, pdf_bytes, pdf_headers = http_req(
        f"/admin/api/students/{test_sid}/pass.pdf",
        method="GET",
        token=admin_token,
        raw_response=True,
    )
    print(f"HTTP Status: {pdf_status}")
    print(f"Content-Type: {pdf_headers.get('Content-Type')}")
    print(f"Content-Length: {len(pdf_bytes)} bytes")

    if pdf_status != 200 or len(pdf_bytes) == 0:
        raise RuntimeError(f"Failed to fetch pass PDF: HTTP {pdf_status}")

    # Extract image and decode QR
    doc = pypdfium2.PdfDocument(pdf_bytes)
    page = doc[0]
    pil_image = page.render(scale=3).to_pil()
    barcode_result = zxingcpp.read_barcode(pil_image)

    decoded_token = barcode_result.text if barcode_result else None
    barcode_format = barcode_result.format if barcode_result else None

    print("\n--- QR Code Verification Result ---")
    print(f"QR Decoder Format : {barcode_format}")
    print(f"Decoded QR String : {decoded_token}")
    print(f"Database Token    : {expected_token}")
    print(f"Exact Match       : {decoded_token == expected_token}")

    if decoded_token != expected_token:
        print("ERROR: QR Decoded token does NOT match DB token!", file=sys.stderr)
        sys.exit(1)

    # -------------------------------------------------------------------------
    # TASK 2: ROLE GATING FOR EVERY ROLE
    # -------------------------------------------------------------------------
    print("\n" + "=" * 80)
    print("TASK 2: Role Gating Verification (All Roles)")
    print("=" * 80)

    roles_config = [
        ("REGISTRATION", "1", "12345678", "REGISTRATION", "THOBE_ALLOCATION"),
        ("THOBE_ALLOCATION", "2", "12345678", "THOBE_ALLOCATION", "REGISTRATION"),
        ("SEATING", "3", "12345678", "SEATING", "REGISTRATION"),
        ("QUEUE", "4", "12345678", "QUEUE", "REGISTRATION"),
        ("STAGE", "5", "12345678", "STAGE", "REGISTRATION"),
        ("THOBE_RETURN", "6", "12345678", "THOBE_RETURN", "REGISTRATION"),
        ("LUNCH", "7", "12345678", "LUNCH", "REGISTRATION"),
    ]

    for role_name, username, password, allowed_act, disallowed_act in roles_config:
        print(f"\n--- Testing Role: {role_name} (User: '{username}') ---")
        user_token = login(username, password)
        print(f"Login successful. Session Token: {user_token[:10]}...")

        # 1. Allowed activity
        print(f"1) Attempting ALLOWED activity: '{allowed_act}'")
        status_allow, body_allow, _ = http_req(
            "/scan",
            method="POST",
            body={"token": expected_token, "activity": allowed_act},
            token=user_token,
        )
        print(f"HTTP Status: {status_allow}")
        print(f"Response Body: {json.dumps(body_allow, indent=2)}")
        assert status_allow == 200, f"Expected HTTP 200 for allowed activity, got {status_allow}"

        # 2. Disallowed activity
        print(f"2) Attempting DISALLOWED activity: '{disallowed_act}'")
        status_deny, body_deny, _ = http_req(
            "/scan",
            method="POST",
            body={"token": expected_token, "activity": disallowed_act},
            token=user_token,
        )
        print(f"HTTP Status: {status_deny}")
        print(f"Response Body: {json.dumps(body_deny, indent=2)}")
        assert status_deny == 403, f"Expected HTTP 403 for disallowed activity, got {status_deny}"
        assert body_deny.get("detail", {}).get("code") == "FORBIDDEN"

    # Admin verification
    print("\n--- Testing Role: ADMIN (User: 'admin') ---")
    admin_tok = login("admin", "adminpassword123")
    print("1) Attempting Activity 'REGISTRATION' as ADMIN (Admin has all 7 activity permissions):")
    status_admin, body_admin, _ = http_req(
        "/scan",
        method="POST",
        body={"token": expected_token, "activity": "REGISTRATION"},
        token=admin_tok,
    )
    print(f"HTTP Status: {status_admin}")
    print(f"Response Body: {json.dumps(body_admin, indent=2)}")
    assert status_admin == 200

    print("2) Verifying Admin boundary: Operator '1' accessing Admin-only endpoint (/admin/api/dashboard):")
    status_op_admin, body_op_admin, _ = http_req(
        "/admin/api/dashboard",
        method="GET",
        token=login("1", "12345678"),
    )
    print(f"HTTP Status: {status_op_admin}")
    print(f"Response Body: {json.dumps(body_op_admin, indent=2)}")
    assert status_op_admin == 403, f"Expected HTTP 403 for operator on admin endpoint, got {status_op_admin}"

    print("3) Attempting non-existent activity name as Admin:")
    status_bad_act, body_bad_act, _ = http_req(
        "/scan",
        method="POST",
        body={"token": expected_token, "activity": "NON_EXISTENT_ACTIVITY"},
        token=admin_tok,
    )
    print(f"HTTP Status: {status_bad_act}")
    print(f"Response Body: {json.dumps(body_bad_act, indent=2)}")
    assert status_bad_act == 404

    # -------------------------------------------------------------------------
    # TASK 3: SEQUENTIAL / PREREQUISITE ENFORCEMENT
    # -------------------------------------------------------------------------
    print("\n" + "=" * 80)
    print("TASK 3: Sequential / Prerequisite Enforcement")
    print("=" * 80)

    # Pick fresh test student A
    with engine.connect() as conn:
        student_a = conn.execute(
            text(
                "SELECT s.id, s.prn, s.name, t.token "
                "FROM students s "
                "JOIN qr_tokens t ON t.student_id = s.id "
                "WHERE t.active AND s.status = 'ACTIVE' "
                "  AND s.id NOT IN (SELECT student_id FROM activity_events) "
                "ORDER BY s.prn LIMIT 1"
            )
        ).mappings().one()

    token_a = student_a["token"]
    prn_a = student_a["prn"]
    print(f"Fresh Test Student A: {student_a['name']} (PRN: {prn_a})")

    # Pair 1: Robe Allocation BEFORE Reporting
    print("\n--- Prerequisite Pair 1: THOBE_ALLOCATION before REGISTRATION ---")
    tok_thobe = login("2", "12345678")
    print("Step 3.1: Operator '2' attempts /confirm for THOBE_ALLOCATION (Prereq REGISTRATION missing):")
    status_t1, body_t1, _ = http_req(
        "/confirm",
        method="POST",
        body={"token": token_a, "activity": "THOBE_ALLOCATION"},
        token=tok_thobe,
    )
    print(f"HTTP Status: {status_t1}")
    print(f"Response Body: {json.dumps(body_t1, indent=2)}")
    assert status_t1 == 200
    assert body_t1["result"] == "REJECTED"
    assert "REPORTING PENDING" in body_t1["message"]

    print("\nStep 3.2: Operator '1' confirms REGISTRATION:")
    tok_reg = login("1", "12345678")
    status_r1, body_r1, _ = http_req(
        "/confirm",
        method="POST",
        body={"token": token_a, "activity": "REGISTRATION"},
        token=tok_reg,
    )
    print(f"HTTP Status: {status_r1}")
    print(f"Response Body: {json.dumps(body_r1, indent=2)}")
    assert status_r1 == 200
    assert body_r1["result"] == "CONFIRMED"

    print("\nStep 3.3: Operator '2' retries THOBE_ALLOCATION (Prereq now met):")
    status_t2, body_t2, _ = http_req(
        "/confirm",
        method="POST",
        body={"token": token_a, "activity": "THOBE_ALLOCATION"},
        token=tok_thobe,
    )
    print(f"HTTP Status: {status_t2}")
    print(f"Response Body: {json.dumps(body_t2, indent=2)}")
    assert status_t2 == 200
    assert body_t2["result"] == "CONFIRMED"

    # Pair 2: Queue BEFORE Seating
    print("\n--- Prerequisite Pair 2: QUEUE before SEATING ---")
    tok_queue = login("4", "12345678")
    print("Step 3.4: Operator '4' attempts /confirm for QUEUE (Prereq SEATING missing):")
    status_q1, body_q1, _ = http_req(
        "/confirm",
        method="POST",
        body={"token": token_a, "activity": "QUEUE"},
        token=tok_queue,
    )
    print(f"HTTP Status: {status_q1}")
    print(f"Response Body: {json.dumps(body_q1, indent=2)}")
    assert status_q1 == 200
    assert body_q1["result"] == "REJECTED"
    assert "SEATING PENDING" in body_q1["message"]

    print("\nStep 3.5: Operator '3' confirms SEATING:")
    tok_seat = login("3", "12345678")
    status_s1, body_s1, _ = http_req(
        "/confirm",
        method="POST",
        body={"token": token_a, "activity": "SEATING"},
        token=tok_seat,
    )
    print(f"HTTP Status: {status_s1}")
    print(f"Response Body: {json.dumps(body_s1, indent=2)}")
    assert status_s1 == 200
    assert body_s1["result"] == "CONFIRMED"

    print("\nStep 3.6: Operator '4' retries QUEUE (Prereq now met):")
    status_q2, body_q2, _ = http_req(
        "/confirm",
        method="POST",
        body={"token": token_a, "activity": "QUEUE"},
        token=tok_queue,
    )
    print(f"HTTP Status: {status_q2}")
    print(f"Response Body: {json.dumps(body_q2, indent=2)}")
    assert status_q2 == 200
    assert body_q2["result"] == "CONFIRMED"

    # -------------------------------------------------------------------------
    # TASK 4: DUPLICATE SCAN HANDLING
    # -------------------------------------------------------------------------
    print("\n" + "=" * 80)
    print("TASK 4: Duplicate Scan Handling")
    print("=" * 80)

    print(f"Student A was registered at Step 3.2. Response 1 was CONFIRMED:")
    print(f"Original confirmation: {json.dumps(body_r1, indent=2)}")

    print(f"\nScanning Student A again for REGISTRATION (Operator '1'):")
    status_dup, body_dup, _ = http_req(
        "/scan",
        method="POST",
        body={"token": token_a, "activity": "REGISTRATION"},
        token=tok_reg,
    )
    print(f"HTTP Status: {status_dup}")
    print(f"Response Body: {json.dumps(body_dup, indent=2)}")
    assert status_dup == 200
    assert body_dup["result"] == "DUPLICATE"
    assert body_dup["colour"] == "amber"
    assert body_dup["earlier"]["time"] == body_r1["event"]["time"]
    assert body_dup["earlier"]["event_id"] == body_r1["event"]["event_id"]

    print(f"\nAttempting /confirm Student A again for REGISTRATION (Operator '1'):")
    status_dup_c, body_dup_c, _ = http_req(
        "/confirm",
        method="POST",
        body={"token": token_a, "activity": "REGISTRATION"},
        token=tok_reg,
    )
    print(f"HTTP Status: {status_dup_c}")
    print(f"Response Body: {json.dumps(body_dup_c, indent=2)}")
    assert status_dup_c == 200
    assert body_dup_c["result"] == "DUPLICATE"
    assert body_dup_c["colour"] == "amber"

    # -------------------------------------------------------------------------
    # TASK 5: FULL SEVEN-ACTIVITY JOURNEY FOR ONE STUDENT
    # -------------------------------------------------------------------------
    print("\n" + "=" * 80)
    print("TASK 5: Full Seven-Activity Journey for One Student")
    print("=" * 80)

    # Pick fresh test student B
    with engine.connect() as conn:
        student_b = conn.execute(
            text(
                "SELECT s.id, s.prn, s.name, t.token "
                "FROM students s "
                "JOIN qr_tokens t ON t.student_id = s.id "
                "WHERE t.active AND s.status = 'ACTIVE' "
                "  AND s.id NOT IN (SELECT student_id FROM activity_events) "
                "ORDER BY s.prn LIMIT 1"
            )
        ).mappings().one()

    sid_b = str(student_b["id"])
    tok_b = student_b["token"]
    prn_b = student_b["prn"]
    print(f"Full-Journey Student B: {student_b['name']} (PRN: {prn_b}, ID: {sid_b})")

    def get_student_status(sid: str) -> Tuple[int, str]:
        with engine.connect() as c:
            row = c.execute(
                text("SELECT step, status FROM student_status WHERE student_id = :s"),
                {"s": sid},
            ).mappings().one()
            return row["step"], row["status"]

    step0, status0 = get_student_status(sid_b)
    print(f"Initial DB Status: step {step0} -> '{status0}'")
    assert status0 == "REGISTERED / NOT REPORTED"

    # Step 1: Reporting
    print("\n--- 1/7: REGISTRATION ---")
    st1, bd1, _ = http_req("/confirm", "POST", {"token": tok_b, "activity": "REGISTRATION"}, token=login("1", "12345678"))
    print(f"HTTP Status: {st1}\nResponse: {json.dumps(bd1, indent=2)}")
    assert st1 == 200 and bd1["result"] == "CONFIRMED"
    s1_step, s1_status = get_student_status(sid_b)
    print(f"Post-Reporting DB Status: step {s1_step} -> '{s1_status}'")
    assert s1_status == "REPORTED / ROBE NOT RECEIVED"

    # Step 2: Robe Allocation
    print("\n--- 2/7: THOBE_ALLOCATION ---")
    st2, bd2, _ = http_req("/confirm", "POST", {"token": tok_b, "activity": "THOBE_ALLOCATION"}, token=login("2", "12345678"))
    print(f"HTTP Status: {st2}\nResponse: {json.dumps(bd2, indent=2)}")
    assert st2 == 200 and bd2["result"] == "CONFIRMED"
    s2_step, s2_status = get_student_status(sid_b)
    print(f"Post-Robe DB Status: step {s2_step} -> '{s2_status}'")
    assert s2_status == "NOT SEATED"

    # Step 3: Seating
    print("\n--- 3/7: SEATING ---")
    st3, bd3, _ = http_req("/confirm", "POST", {"token": tok_b, "activity": "SEATING"}, token=login("3", "12345678"))
    print(f"HTTP Status: {st3}\nResponse: {json.dumps(bd3, indent=2)}")
    assert st3 == 200 and bd3["result"] == "CONFIRMED"
    s3_step, s3_status = get_student_status(sid_b)
    print(f"Post-Seating DB Status: step {s3_step} -> '{s3_status}'")
    assert s3_status == "NOT QUEUED"

    # Step 4: Queue
    print("\n--- 4/7: QUEUE ---")
    st4, bd4, _ = http_req("/confirm", "POST", {"token": tok_b, "activity": "QUEUE"}, token=login("4", "12345678"))
    print(f"HTTP Status: {st4}\nResponse: {json.dumps(bd4, indent=2)}")
    assert st4 == 200 and bd4["result"] == "CONFIRMED"
    s4_step, s4_status = get_student_status(sid_b)
    print(f"Post-Queue DB Status: step {s4_step} -> '{s4_status}'")
    assert s4_status == "DEGREE NOT RECEIVED"

    # Step 5: Stage
    print("\n--- 5/7: STAGE ---")
    tok_stage = login("5", "12345678")
    # Claim stage control
    st_claim, bd_claim, _ = http_req("/stage/takeover", "POST", {}, token=tok_stage)
    print(f"Stage Takeover HTTP Status: {st_claim}")

    # Advance queue until Student B is on stage
    while True:
        st_dn, bd_dn, _ = http_req("/stage/display-next", "POST", {}, token=tok_stage)
        print(f"Stage Display-Next HTTP Status: {st_dn}")
        current = bd_dn.get("state", {}).get("current")
        if current and current.get("student_id") == sid_b:
            print(f"Student B ({student_b['name']}) is now on stage!")
            print(f"Display-Next Response for Student B: {json.dumps(bd_dn, indent=2)}")
            break
        elif current:
            print(f"Student {current.get('name')} was ahead in queue, completing to advance to Student B...")
            http_req("/stage/complete", "POST", {}, token=tok_stage)
        else:
            raise RuntimeError("Nobody on stage and Student B not reached!")

    # Complete Stage for Student B
    st5, bd5, _ = http_req("/stage/complete", "POST", {}, token=tok_stage)
    print(f"Stage Complete for Student B HTTP Status: {st5}\nResponse: {json.dumps(bd5, indent=2)}")
    assert st5 == 200 and bd5.get("ok") is True
    s5_step, s5_status = get_student_status(sid_b)
    print(f"Post-Stage DB Status: step {s5_step} -> '{s5_status}'")
    assert s5_status == "ROBE NOT RETURNED"

    # Step 6: Robe Return
    print("\n--- 6/7: THOBE_RETURN ---")
    st6, bd6, _ = http_req("/confirm", "POST", {"token": tok_b, "activity": "THOBE_RETURN"}, token=login("6", "12345678"))
    print(f"HTTP Status: {st6}\nResponse: {json.dumps(bd6, indent=2)}")
    assert st6 == 200 and bd6["result"] == "CONFIRMED"
    s6_step, s6_status = get_student_status(sid_b)
    print(f"Post-Robe Return DB Status: step {s6_step} -> '{s6_status}'")
    assert s6_status == "LUNCH ELIGIBLE"
    assert s6_status != "EXITED", "Student MUST NOT be EXITED before Lunch!"

    # Step 7: Lunch
    print("\n--- 7/7: LUNCH ---")
    st7, bd7, _ = http_req("/confirm", "POST", {"token": tok_b, "activity": "LUNCH"}, token=login("7", "12345678"))
    print(f"HTTP Status: {st7}\nResponse: {json.dumps(bd7, indent=2)}")
    assert st7 == 200 and bd7["result"] == "CONFIRMED"
    s7_step, s7_status = get_student_status(sid_b)
    print(f"Post-Lunch DB Status: step {s7_step} -> '{s7_status}'")
    assert s7_status == "EXITED", f"Expected EXITED, got {s7_status}"
    print("\nCONFIRMED: Student status transitioned through all 7 steps and became EXITED ONLY AFTER LUNCH.")

    # -------------------------------------------------------------------------
    # TASK 6: STAGE/LED BEHAVIOR AND PRIVACY SNAPSHOT VALIDATION
    # -------------------------------------------------------------------------
    print("\n" + "=" * 80)
    print("TASK 6: Stage / Public LED Behavior Verification")
    print("=" * 80)

    # Pick fresh test student C
    with engine.connect() as conn:
        student_c = conn.execute(
            text(
                "SELECT s.id, s.prn, s.name, t.token "
                "FROM students s "
                "JOIN qr_tokens t ON t.student_id = s.id "
                "WHERE t.active AND s.status = 'ACTIVE' "
                "  AND s.id NOT IN (SELECT student_id FROM activity_events) "
                "ORDER BY s.prn LIMIT 1"
            )
        ).mappings().one()

    sid_c = str(student_c["id"])
    tok_c = student_c["token"]
    prn_c = student_c["prn"]
    print(f"LED Test Student C: {student_c['name']} (PRN: {prn_c}, ID: {sid_c})")

    # Complete Reg, Robe, Seating so Student C is ready for Queue
    http_req("/confirm", "POST", {"token": tok_c, "activity": "REGISTRATION"}, token=login("1", "12345678"))
    http_req("/confirm", "POST", {"token": tok_c, "activity": "THOBE_ALLOCATION"}, token=login("2", "12345678"))
    http_req("/confirm", "POST", {"token": tok_c, "activity": "SEATING"}, token=login("3", "12345678"))

    # Stage operator ensures home state
    tok_stage = login("5", "12345678")
    http_req("/stage/takeover", "POST", {}, token=tok_stage)
    http_req("/stage/home", "POST", {}, token=tok_stage)

    # 1. LED State BEFORE Queue scan
    print("\n--- 1. Public LED State BEFORE Queue Scan (GET /led/state) ---")
    st_led1, bd_led1, _ = http_req("/led/state", "GET")
    print(f"HTTP Status: {st_led1}")
    print(f"LED Payload:\n{json.dumps(bd_led1, indent=2)}")
    assert bd_led1["mode"] == "HOME"
    assert bd_led1["student"] is None

    # 2. Operator 4 confirms QUEUE
    print("\n--- 2. Operator '4' (QUEUE) confirms Student C into the Queue ---")
    st_q_c, bd_q_c, _ = http_req("/confirm", "POST", {"token": tok_c, "activity": "QUEUE"}, token=login("4", "12345678"))
    print(f"HTTP Status: {st_q_c}")
    print(f"Queue Confirm Result: {json.dumps(bd_q_c, indent=2)}")
    assert st_q_c == 200 and bd_q_c["result"] == "CONFIRMED"

    # 3. LED State AFTER Queue scan
    print("\n--- 3. Public LED State AFTER Queue Scan (GET /led/state) ---")
    st_led2, bd_led2, _ = http_req("/led/state", "GET")
    print(f"HTTP Status: {st_led2}")
    print(f"LED Payload:\n{json.dumps(bd_led2, indent=2)}")
    print("VERIFICATION: Confirming Queue scan did NOT change the public LED:")
    assert bd_led2["mode"] == "HOME", "LED mode must remain HOME after Queue scan!"
    assert bd_led2["student"] is None, "LED student must remain null after Queue scan!"
    print("-> PASSED: Public LED is completely unchanged by Queue scan.")

    # 4. Stage Operator presses DISPLAY NEXT
    print("\n--- 4. Stage Operator calls POST /stage/display-next ---")
    st_dn2, bd_dn2, _ = http_req("/stage/display-next", "POST", {}, token=tok_stage)
    print(f"HTTP Status: {st_dn2}")
    print(f"Display Next Response: {json.dumps(bd_dn2, indent=2)}")
    assert st_dn2 == 200

    # 5. LED State AFTER DISPLAY NEXT
    print("\n--- 5. Public LED State AFTER 'DISPLAY NEXT' (GET /led/state) ---")
    st_led3, bd_led3, _ = http_req("/led/state", "GET")
    print(f"HTTP Status: {st_led3}")
    print(f"LED Payload:\n{json.dumps(bd_led3, indent=2)}")
    assert bd_led3["mode"] == "SHOWING"
    led_student = bd_led3["student"]
    assert led_student is not None, "LED student must NOT be null after display-next!"

    print("\n--- 6. LED Privacy & Field Whitelist Audit ---")
    print(f"Keys present on LED student object: {list(led_student.keys())}")
    allowed_keys = {"name", "photo_url", "programme", "school", "award"}
    actual_keys = set(led_student.keys())
    assert actual_keys == allowed_keys, f"LED payload keys {actual_keys} do not match allowed whitelist {allowed_keys}!"

    # Explicit check for prohibited fields
    prohibited_fields = ["prn", "phone", "email", "student_id", "id", "sequence_no", "seat", "seat_no"]
    for field in prohibited_fields:
        assert field not in led_student, f"PROHIBITED field '{field}' leaked into LED payload!"
        assert field not in bd_led3, f"PROHIBITED field '{field}' leaked into top-level LED payload!"

    # Verify photo_url is opaque
    photo_url = led_student["photo_url"]
    assert photo_url.startswith("/led/photo/"), f"Unexpected photo URL format: {photo_url}"
    assert sid_c not in photo_url, f"Student UUID leaked in photo URL: {photo_url}"
    assert prn_c not in photo_url, f"Student PRN leaked in photo URL: {photo_url}"

    print("-> APPROVED FIELDS ONLY: 'name', 'photo_url', 'programme', 'school', 'award'")
    print("-> ABSOLUTELY NO PRN, phone, email, sequence number, seat, or internal student UUID")
    print(f"-> Photo URL is opaque: '{photo_url}'")
    print("-> PASSED: Stage / LED Privacy & Control verified completely.")

    # Clean up stage: complete student C
    http_req("/stage/complete", "POST", {}, token=tok_stage)
    print("\nAll 6 verification tasks completed successfully!")


if __name__ == "__main__":
    main()
