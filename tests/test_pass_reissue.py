"""Tests for QR pass download, reissue, permission isolation, and admin visibility.

Verifies:
1. Permissions: PASS_MANAGEMENT_PERMISSION is granted to REGISTRY, ADMIN, and DEPUTY_ADMIN only.
   It NEVER appears in any activity list, domain, or state machine configuration.
2. Download Pass: GET /station/api/pass/{student_id} returns an A6 PDF with Cache-Control: no-store,
   writes PASS_DOWNLOADED audit log (no raw tokens), and requires authorization.
3. Pass Layout: A student with sequence_no=None NEVER renders "None" or blank "SEQ" label.
4. Reissue Pass: POST /station/api/reissue-pass validates non-empty reason, atomically deactivates
   old token, generates new token, writes QR_REISSUED audit log (no raw tokens), and requires authorization.
5. Station Scanner Invalidation: Old token is rejected as "QR CANCELLED — REISSUED" at all stations
   (Registry, Queue, Lunch). New token is accepted.
6. Mid-Journey Reissue: Student queued or staged continues journey on new token without losing queue
   position or stage progress.
7. Concurrency: Two concurrent reissues for the same student serialize cleanly via row-locking;
   leaves exactly ONE active token.
8. Admin Visibility: /admin/reports/reissued-qrs displays the audit trail, and /admin/exceptions
   shows the count of reissued passes.
"""
import io
import uuid
import pypdf
import pytest
from sqlalchemy import text

import backend.qr_tokens as qr_tokens
from backend import passes as passes_svc, users as users_svc
from backend.engine import messages
from backend.engine.activities import ACTIVITY_CONFIGS
from backend.security import permissions
from tests.test_auth import PASSWORD, api_login, new_client
from tests.test_schema import _run_threads
from tests.test_station_engine import (  # noqa: F401
    _CLIENTS,
    admin,
    apps,
    confirm,
    engine,
    events_of,
    make_student,
    operator,
    q,
    scan,
    seed_events,
    world,
)


@pytest.fixture(scope="module", autouse=True)
def _fresh_client_cache(world):
    _CLIENTS.clear()
    yield
    _CLIENTS.clear()


@pytest.fixture(scope="module")
def deputy_admin_client(engine, apps):
    with engine.begin() as c:
        users_svc.create_user(c, username="eng-deputy", password=PASSWORD, role="DEPUTY_ADMIN")
    client = new_client(apps)
    assert api_login(client, "eng-deputy").status_code == 200
    return client


def get_student_tokens(engine, student_id):
    with engine.connect() as c:
        return c.execute(
            text("SELECT id, token, active, deactivated_at, deactivated_by "
                 "FROM qr_tokens WHERE student_id = :s ORDER BY id"),
            {"s": student_id},
        ).mappings().all()


def get_active_token(engine, student_id):
    with engine.connect() as c:
        return c.execute(
            text("SELECT token FROM qr_tokens WHERE student_id = :s AND active"),
            {"s": student_id},
        ).scalar_one_or_none()


# -------------------------------------------------------------------------------------------------
# 1. PERMISSIONS
# -------------------------------------------------------------------------------------------------
class TestPermissions:
    def test_permission_not_in_any_activity_list_or_domain(self, engine):
        perm = permissions.PASS_MANAGEMENT_PERMISSION
        assert perm == "station:pass_management"

        # 1. Never in ACTIVITIES tuple
        assert perm not in permissions.ACTIVITIES, f"{perm} must not be in ACTIVITIES"

        # 2. Never in any role's activity tuple in OPERATOR_ROLE_ACTIVITIES
        for role, acts in permissions.OPERATOR_ROLE_ACTIVITIES.items():
            assert perm not in acts, f"{perm} must not be in OPERATOR_ROLE_ACTIVITIES for {role}"

        # 3. Never in ACTIVITY_CONFIGS
        assert perm not in ACTIVITY_CONFIGS, f"{perm} must not be in ACTIVITY_CONFIGS"

        # 4. Never in database activity domain/enum/check constraints
        with engine.connect() as c:
            checks = c.execute(text(
                "SELECT check_clause FROM information_schema.check_constraints "
                "WHERE constraint_name LIKE '%activity%'"
            )).scalars().all()
            for clause in checks:
                assert perm not in clause, f"{perm} leaked into database check constraint: {clause}"

    def test_permission_role_matrix(self):
        perm = permissions.PASS_MANAGEMENT_PERMISSION
        # Allowed roles
        assert permissions.has_permission("REGISTRY", perm) is True
        assert permissions.has_permission("ADMIN", perm) is True
        assert permissions.has_permission("DEPUTY_ADMIN", perm) is True

        # Disallowed operator roles
        for role in ("SEATING", "QUEUE", "STAGE", "LUNCH", "CALLER"):
            assert permissions.has_permission(role, perm) is False, f"{role} should NOT have {perm}"


# -------------------------------------------------------------------------------------------------
# 2. PASS DOWNLOAD
# -------------------------------------------------------------------------------------------------
class TestPassDownload:
    def test_download_authorization(self, apps, world, engine, deputy_admin_client):
        s = make_student(engine)
        path = f"/station/api/pass/{s.id}"

        # 1. Unauthenticated -> 401
        anon = new_client(apps)
        assert anon.get(path).status_code == 401

        # 2. Unauthorized operator roles -> 403
        for role in ("QUEUE", "LUNCH", "SEATING"):
            op_client = operator(apps, world, role)
            res = op_client.get(path)
            assert res.status_code == 403
            assert res.json()["detail"]["code"] == "FORBIDDEN"

        # 3. Authorized roles: REGISTRY, ADMIN, DEPUTY_ADMIN -> 200
        reg_client = operator(apps, world, "REGISTRATION")
        res_reg = reg_client.get(path)
        assert res_reg.status_code == 200

        admin_client = admin(apps)
        res_admin = admin_client.get(path)
        assert res_admin.status_code == 200

        res_deputy = deputy_admin_client.get(path)
        assert res_deputy.status_code == 200

    def test_download_headers_content_and_audit(self, apps, world, engine):
        s = make_student(engine)
        reg_client = operator(apps, world, "REGISTRATION")

        res = reg_client.get(f"/station/api/pass/{s.id}")
        assert res.status_code == 200
        assert res.headers["content-type"] == "application/pdf"
        assert res.headers["cache-control"] == "no-store"
        assert f'filename="pass-{s.prn}.pdf"' in res.headers["content-disposition"]
        assert res.content.startswith(b"%PDF-")

        # Verify audit log row
        with engine.connect() as c:
            row = c.execute(
                text("SELECT action, student_id, details FROM audit_log "
                     "WHERE action = 'PASS_DOWNLOADED' AND student_id = :sid ORDER BY id DESC LIMIT 1"),
                {"sid": s.id},
            ).mappings().one()
            assert row["action"] == "PASS_DOWNLOADED"
            assert str(row["student_id"]) == str(s.id)
            assert row["details"]["prn"] == s.prn
            assert row["details"]["station"] == "REGISTRY"
            # Ensure raw token was NOT logged
            assert s.token not in str(row["details"])

    def test_download_invalid_student_and_inactive_refused(self, apps, world, engine):
        reg_client = operator(apps, world, "REGISTRATION")

        # Non-existent UUID -> 404
        bad_id = str(uuid.uuid4())
        res = reg_client.get(f"/station/api/pass/{bad_id}")
        assert res.status_code == 404
        assert res.json()["detail"]["code"] == "STUDENT_NOT_FOUND"

        # Malformed ID -> 404
        res = reg_client.get("/station/api/pass/not-a-uuid")
        assert res.status_code == 404
        assert res.json()["detail"]["code"] == "STUDENT_NOT_FOUND"

        # Inactive student -> 409
        s_inactive = make_student(engine, active=False)
        res = reg_client.get(f"/station/api/pass/{s_inactive.id}")
        assert res.status_code == 409
        assert res.json()["detail"]["code"] == "STUDENT_NOT_ACTIVE"

        # Active student without token -> 409
        s_no_tok = make_student(engine, active=True, token=False)
        res = reg_client.get(f"/station/api/pass/{s_no_tok.id}")
        assert res.status_code == 409
        assert res.json()["detail"]["code"] == "NO_TOKEN"


# -------------------------------------------------------------------------------------------------
# 3. PASS LAYOUT (Null sequence number)
# -------------------------------------------------------------------------------------------------
class TestPassLayout:
    def test_sequence_number_null_never_renders_none_or_blank_label(self, engine):
        # 1. Student without sequence number (sequence_no=None)
        with engine.begin() as c:
            sid_no_seq = c.execute(
                text("INSERT INTO students (prn, name, programme, school, sequence_no, status) "
                     "VALUES ('PRN_UNNUMBERED', 'Student Unnumbered', 'B.Tech IT', 'School of Eng', NULL, 'ACTIVE') "
                     "RETURNING id")
            ).scalar_one()
            c.execute(text("INSERT INTO qr_tokens (student_id, token) VALUES (:s, :t)"),
                      {"s": sid_no_seq, "t": "TOK_NO_SEQ_000000000000000000"})

        with engine.connect() as c:
            rows_no_seq = passes_svc.load_passes(c, student_id=str(sid_no_seq))
        data_no_seq = passes_svc.to_pass_data(rows_no_seq)[0]
        assert data_no_seq.sequence_no is None

        pdf_result_no_seq = passes_svc.render_single(data_no_seq, "Convocation 2026")
        reader_no_seq = pypdf.PdfReader(io.BytesIO(pdf_result_no_seq.pdf))
        page_text_no_seq = reader_no_seq.pages[0].extract_text()

        assert "PRN_UNNUMBERED" in page_text_no_seq
        assert "None" not in page_text_no_seq
        assert "SEQ NO." not in page_text_no_seq
        assert "SEQ" not in page_text_no_seq

        # 2. Student WITH sequence number
        with engine.begin() as c:
            sid_with_seq = c.execute(
                text("INSERT INTO students (prn, name, programme, school, sequence_no, status) "
                     "VALUES ('PRN_WITH_SEQ', 'Student With Seq', 'B.Tech IT', 'School of Eng', 4242, 'ACTIVE') "
                     "RETURNING id")
            ).scalar_one()
            c.execute(text("INSERT INTO qr_tokens (student_id, token) VALUES (:s, :t)"),
                      {"s": sid_with_seq, "t": "TOK_WITH_SEQ_0000000000000000"})

        with engine.connect() as c:
            rows_with_seq = passes_svc.load_passes(c, student_id=str(sid_with_seq))
        data_with_seq = passes_svc.to_pass_data(rows_with_seq)[0]
        assert data_with_seq.sequence_no == 4242

        pdf_result_with_seq = passes_svc.render_single(data_with_seq, "Convocation 2026")
        reader_with_seq = pypdf.PdfReader(io.BytesIO(pdf_result_with_seq.pdf))
        page_text_with_seq = reader_with_seq.pages[0].extract_text()

        assert "PRN_WITH_SEQ" in page_text_with_seq
        assert "SEQ NO. 4242" in page_text_with_seq


# -------------------------------------------------------------------------------------------------
# 4. REISSUE ENDPOINT & VALIDATION
# -------------------------------------------------------------------------------------------------
class TestReissuePass:
    def test_reissue_authorization(self, apps, world, engine, deputy_admin_client):
        s = make_student(engine)
        payload = {"student_id": str(s.id), "reason": "Lost pass"}

        # 1. Anonymous -> 401
        anon = new_client(apps)
        assert anon.post("/station/api/reissue-pass", json=payload).status_code == 401

        # 2. Disallowed operator roles -> 403
        for role in ("QUEUE", "LUNCH", "SEATING"):
            op_client = operator(apps, world, role)
            res = op_client.post("/station/api/reissue-pass", json=payload)
            assert res.status_code == 403
            assert res.json()["detail"]["code"] == "FORBIDDEN"

        # 3. Authorized roles: REGISTRY, ADMIN, DEPUTY_ADMIN -> 200
        reg_client = operator(apps, world, "REGISTRATION")
        res_reg = reg_client.post("/station/api/reissue-pass", json=payload)
        assert res_reg.status_code == 200
        assert res_reg.json()["ok"] is True

        admin_client = admin(apps)
        res_admin = admin_client.post("/station/api/reissue-pass", json=payload)
        assert res_admin.status_code == 200
        assert res_admin.json()["ok"] is True

        res_deputy = deputy_admin_client.post("/station/api/reissue-pass", json=payload)
        assert res_deputy.status_code == 200
        assert res_deputy.json()["ok"] is True

    def test_reissue_validation(self, apps, world, engine):
        s = make_student(engine)
        reg_client = operator(apps, world, "REGISTRATION")

        # Blank reason refused
        for bad_reason in ("", "   ", "\t\n"):
            res = reg_client.post("/station/api/reissue-pass", json={"student_id": str(s.id), "reason": bad_reason})
            assert res.status_code == 400
            assert res.json()["detail"]["code"] == "REASON_REQUIRED"

        # Reason exceeding max length (500 chars)
        res = reg_client.post("/station/api/reissue-pass", json={"student_id": str(s.id), "reason": "a" * 501})
        assert res.status_code == 400
        assert res.json()["detail"]["code"] == "REASON_TOO_LONG"

        # Inactive student refused
        s_inactive = make_student(engine, active=False)
        res = reg_client.post("/station/api/reissue-pass", json={"student_id": str(s_inactive.id), "reason": "Valid reason"})
        assert res.status_code == 409
        assert res.json()["detail"]["code"] == "STUDENT_NOT_ACTIVE"

    def test_reissue_audit_and_token_invalidation(self, apps, world, engine):
        s = make_student(engine)
        initial_token = s.token
        reg_client = operator(apps, world, "REGISTRATION")

        res = reg_client.post("/station/api/reissue-pass", json={
            "student_id": str(s.id),
            "reason": "Lost card in morning rush",
        })
        assert res.status_code == 200
        data = res.json()
        assert data["ok"] is True
        assert data["pass_url"] == f"/station/api/pass/{s.id}"
        # Response must NOT expose raw token
        assert "token" not in data
        assert initial_token not in str(data)

        # Database checks: exactly one active token, old one deactivated with reason
        tokens = get_student_tokens(engine, s.id)
        assert len(tokens) == 2
        old_tok_row = next(t for t in tokens if not t["active"])
        new_tok_row = next(t for t in tokens if t["active"])

        assert old_tok_row["token"] == initial_token
        assert old_tok_row["deactivated_at"] is not None
        assert old_tok_row["deactivated_by"] == world.op_ids["REGISTRATION"]

        assert new_tok_row["token"] != initial_token
        assert len(new_tok_row["token"]) == 32

        # Audit log checks: QR_REISSUED logged with reason and metadata, no raw tokens
        with engine.connect() as c:
            audit = c.execute(
                text("SELECT action, reason, details FROM audit_log "
                     "WHERE action = 'QR_REISSUED' AND student_id = :sid ORDER BY id DESC LIMIT 1"),
                {"sid": s.id},
            ).mappings().one()
            assert audit["reason"] == "Lost card in morning rush"
            assert audit["details"]["old_token_id"] == old_tok_row["id"]
            assert audit["details"]["new_token_id"] == new_tok_row["id"]
            assert audit["details"]["prn"] == s.prn
            assert initial_token not in str(audit["details"])
            assert new_tok_row["token"] not in str(audit["details"])


# -------------------------------------------------------------------------------------------------
# 5. STATION SCANNER BEHAVIOUR AFTER REISSUE
# -------------------------------------------------------------------------------------------------
class TestStationScannerInvalidation:
    def test_old_token_rejected_at_all_stations(self, apps, world, engine):
        s = make_student(engine)
        old_token = s.token
        reg_client = operator(apps, world, "REGISTRATION")

        # Reissue QR
        reissue_res = reg_client.post("/station/api/reissue-pass", json={
            "student_id": str(s.id),
            "reason": "Damaged QR pass barcode",
        })
        assert reissue_res.status_code == 200
        new_token = get_active_token(engine, s.id)
        assert new_token and new_token != old_token

        # Check scan of OLD token at REGISTRY
        reg_scan_old = reg_client.post("/scan", json={"activity": "REGISTRY", "token": old_token}).json()
        assert reg_scan_old["result"] == "INVALID"
        assert reg_scan_old["message"] == messages.REPLACED_QR

        # Check scan of OLD token at QUEUE
        queue_client = operator(apps, world, "QUEUE")
        q_scan_old = queue_client.post("/scan", json={"activity": "QUEUE", "token": old_token}).json()
        assert q_scan_old["result"] == "INVALID"
        assert q_scan_old["message"] == messages.REPLACED_QR

        # Check scan of OLD token at LUNCH
        lunch_client = operator(apps, world, "LUNCH")
        lunch_scan_old = lunch_client.post("/scan", json={"activity": "LUNCH", "token": old_token}).json()
        assert lunch_scan_old["result"] == "INVALID"
        assert lunch_scan_old["message"] == messages.REPLACED_QR

        # Check scan of NEW token at REGISTRY is recognized and READY
        reg_scan_new = reg_client.post("/scan", json={"activity": "REGISTRY", "token": new_token}).json()
        assert reg_scan_new["result"] == "READY"


# -------------------------------------------------------------------------------------------------
# 6. MID-JOURNEY REISSUE & CONTINUATION
# -------------------------------------------------------------------------------------------------
class TestMidJourneyReissue:
    def test_reissue_while_student_is_in_queue_continues_journey(self, apps, world, engine):
        s = make_student(engine)
        initial_token = s.token
        reg_client = operator(apps, world, "REGISTRATION")

        # 1. Registry desk: complete ENTRY (Registration + Robe)
        scan_res = reg_client.post("/scan", json={"activity": "REGISTRY", "token": initial_token}).json()
        assert scan_res["result"] == "READY"
        conf_res = reg_client.post("/confirm", json={
            "activity": "REGISTRY",
            "token": initial_token,
            "step": "ENTRY",
            "marks": ["THOBE_ALLOCATION"],
        }).json()
        assert conf_res["result"] == "CONFIRMED"

        # 2. Queue station: student queues up
        queue_client = operator(apps, world, "QUEUE")
        q_scan = queue_client.post("/scan", json={"activity": "QUEUE", "token": initial_token}).json()
        assert q_scan["result"] == "READY"
        q_conf = queue_client.post("/confirm", json={"activity": "QUEUE", "token": initial_token}).json()
        assert q_conf["result"] == "CONFIRMED"

        # Verify student is in queue
        with engine.connect() as c:
            q_row = c.execute(
                text("SELECT status, queue_position FROM queue WHERE student_id = :sid"),
                {"sid": s.id},
            ).mappings().one()
            assert q_row["status"] == "QUEUED"
            q_pos = q_row["queue_position"]

        # 3. Mid-journey reissue at Registry desk
        reissue_res = reg_client.post("/station/api/reissue-pass", json={
            "student_id": str(s.id),
            "reason": "Lost pass while waiting in queue line",
        }).json()
        assert reissue_res["ok"] is True
        new_token = get_active_token(engine, s.id)
        assert new_token != initial_token

        # 4. Queue status & position must be intact
        with engine.connect() as c:
            q_row_after = c.execute(
                text("SELECT status, queue_position FROM queue WHERE student_id = :sid"),
                {"sid": s.id},
            ).mappings().one()
            assert q_row_after["status"] == "QUEUED"
            assert q_row_after["queue_position"] == q_pos

        # 5. Stage: Operator records Stage degree (seeded or driven)
        seed_events(engine, s, ["STAGE"])

        # 6. Robe Return (Registry scan):
        # Old token is rejected
        return_scan_old = reg_client.post("/scan", json={"activity": "REGISTRY", "token": initial_token}).json()
        assert return_scan_old["result"] == "INVALID"
        assert return_scan_old["message"] == messages.REPLACED_QR

        # New token is accepted for RETURN
        return_scan_new = reg_client.post("/scan", json={"activity": "REGISTRY", "token": new_token}).json()
        assert return_scan_new["result"] == "READY"
        assert return_scan_new["step"] == "RETURN"

        # Confirm robe return
        return_conf = reg_client.post("/confirm", json={
            "activity": "REGISTRY",
            "token": new_token,
            "step": "RETURN",
            "marks": ["THOBE_RETURN"],
        }).json()
        assert return_conf["result"] == "CONFIRMED"

        # 7. Lunch:
        lunch_client = operator(apps, world, "LUNCH")
        # Old token rejected
        lunch_scan_old = lunch_client.post("/scan", json={"activity": "LUNCH", "token": initial_token}).json()
        assert lunch_scan_old["result"] == "INVALID"
        assert lunch_scan_old["message"] == messages.REPLACED_QR

        # New token accepted
        lunch_scan_new = lunch_client.post("/scan", json={"activity": "LUNCH", "token": new_token}).json()
        assert lunch_scan_new["result"] == "READY"
        lunch_conf = lunch_client.post("/confirm", json={"activity": "LUNCH", "token": new_token}).json()
        assert lunch_conf["result"] == "CONFIRMED"

        # All events completed for student
        completed_activities = [e["activity"] for e in events_of(engine, s)]
        for act_name in ("REGISTRATION", "THOBE_ALLOCATION", "QUEUE", "STAGE", "THOBE_RETURN", "LUNCH"):
            assert act_name in completed_activities


# -------------------------------------------------------------------------------------------------
# 7. CONCURRENCY: TWO SIMULTANEOUS REISSUES
# -------------------------------------------------------------------------------------------------
class TestConcurrentReissues:
    def test_two_concurrent_reissues_leave_exactly_one_active_token(self, apps, world, engine):
        s = make_student(engine)
        reg_client1 = operator(apps, world, "REGISTRATION")
        reg_client2 = new_client(apps)
        assert api_login(reg_client2, "eng-registration").status_code == 200

        clients = [reg_client1, reg_client2]

        def call_reissue(i):
            c = clients[i]
            res = c.post("/station/api/reissue-pass", json={
                "student_id": str(s.id),
                "reason": f"Simultaneous operator click {i}",
            })
            return res.status_code, res.json()

        results = _run_threads(call_reissue, 2)
        status_codes = [r[0] for r in results]
        assert status_codes == [200, 200]

        # In DB: exactly 1 active token, 2 deactivated tokens (total 3 tokens)
        tokens = get_student_tokens(engine, s.id)
        assert len(tokens) == 3
        active_tokens = [t for t in tokens if t["active"]]
        assert len(active_tokens) == 1

        # In audit_log: 2 QR_REISSUED rows
        with engine.connect() as c:
            reissued_logs = c.execute(
                text("SELECT count(*) FROM audit_log WHERE action = 'QR_REISSUED' AND student_id = :sid"),
                {"sid": s.id},
            ).scalar_one()
            assert reissued_logs == 2


# -------------------------------------------------------------------------------------------------
# 8. ADMIN VISIBILITY: REISSUED QRS REPORT & EXCEPTIONS VIEW
# -------------------------------------------------------------------------------------------------
class TestAdminVisibility:
    def test_admin_reissued_report_and_exceptions_count(self, apps, world, engine):
        s = make_student(engine)
        reg_client = operator(apps, world, "REGISTRATION")
        unique_reason = f"Audit trail visibility test {uuid.uuid4().hex[:6]}"

        # Perform reissue
        reissue_res = reg_client.post("/station/api/reissue-pass", json={
            "student_id": str(s.id),
            "reason": unique_reason,
        })
        assert reissue_res.status_code == 200

        # 1. Admin views reissued-qrs report
        admin_client = admin(apps)
        rep_res = admin_client.get("/admin/reports/reissued-qrs")
        assert rep_res.status_code == 200
        html = rep_res.text
        assert "Reissued QR passes" in html
        assert s.prn in html
        assert unique_reason in html
        assert "eng-registration" in html

        # 2. Admin views exceptions page and sees reissued count
        exc_res = admin_client.get("/admin/exceptions")
        assert exc_res.status_code == 200
        exc_html = exc_res.text
        assert "/admin/reports/reissued-qrs" in exc_html
        assert "Reissued QRs (" in exc_html

        # 3. Non-admin operator cannot access admin reports
        reg_report_res = reg_client.get("/admin/reports/reissued-qrs")
        assert reg_report_res.status_code in (403, 302, 303, 307)
