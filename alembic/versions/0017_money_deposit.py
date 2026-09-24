"""activities: MONEY_RECEIVED and MONEY_RETURNED (the Registry desk's money tick boxes, Phase R4)

The Registry desk records a money deposit next to the robe: "Money received" at entry, "Money returned" after
the degree, each a plain yes/no confirmation with its own append-only event and its own per-activity unique
constraint (the existing activity_events_one_completion index covers them unchanged).

* activity_t accepts the two new activities.
* An Admin WAIVER ("money kept", e.g. for a lost robe) is allowed on MONEY_RETURNED as well as THOBE_RETURN.
* activity_step(): MONEY_RECEIVED sits with the robe (2), MONEY_RETURNED with the robe return (6), so an Admin
  reversal still refuses to reverse a step while a later one is recorded.
* student_status: robe and money are tracked separately, so the label says which is still pending:

    step 0  REGISTERED / NOT REPORTED
    step 1  REPORTED / ROBE AND MONEY PENDING | REPORTED / MONEY PENDING | REPORTED / ROBE PENDING
    step 2  ROBE AND MONEY RECEIVED / NOT QUEUED
    step 3  SEATED / NOT QUEUED
    step 4  DEGREE NOT RECEIVED
    step 5  ROBE AND MONEY NOT RETURNED | MONEY NOT RETURNED | ROBE NOT RETURNED
    step 6  LUNCH ELIGIBLE
    step 7  EXITED

  The furthest milestone reached wins (as before), so the same events in any order give the same status.

Downgrade refuses while any money event exists: it never deletes history.

Revision ID: 0017_money_deposit
Revises: 0016_optional_seating_labels
"""
from typing import Sequence, Union

from alembic import op
from sqlalchemy import text

revision: str = "0017_money_deposit"
down_revision: Union[str, Sequence[str], None] = "0016_optional_seating_labels"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

ACTIVITIES_BEFORE = "'REGISTRATION','THOBE_ALLOCATION','SEATING','QUEUE','STAGE','THOBE_RETURN','LUNCH'"
ACTIVITIES_AFTER = ("'REGISTRATION','THOBE_ALLOCATION','MONEY_RECEIVED','SEATING','QUEUE','STAGE','THOBE_RETURN',"
                    "'MONEY_RETURNED','LUNCH'")

_DROP_DOMAIN_CHECKS = """
    DO $$
    DECLARE c record;
    BEGIN
        FOR c IN SELECT conname FROM pg_constraint WHERE contypid = 'activity_t'::regtype AND contype = 'c' LOOP
            EXECUTE format('ALTER DOMAIN activity_t DROP CONSTRAINT %I', c.conname);
        END LOOP;
    END $$;
"""

_STEP = """
    CREATE OR REPLACE FUNCTION activity_step(a text) RETURNS smallint
    LANGUAGE sql IMMUTABLE STRICT PARALLEL SAFE AS $$
        SELECT CASE a
            WHEN 'REGISTRATION'     THEN 1
            WHEN 'THOBE_ALLOCATION' THEN 2
            WHEN 'MONEY_RECEIVED'   THEN 2
            WHEN 'SEATING'          THEN 3
            WHEN 'QUEUE'            THEN 4
            WHEN 'STAGE'            THEN 5
            WHEN 'THOBE_RETURN'     THEN 6
            WHEN 'MONEY_RETURNED'   THEN 6
            WHEN 'LUNCH'            THEN 7
        END::smallint
    $$
"""

_VIEW = """
    CREATE OR REPLACE VIEW student_status AS
    WITH active AS (
        SELECT e.student_id, e.activity
        FROM activity_events e
        WHERE e.kind IN ('COMPLETE','WAIVER')
          AND NOT EXISTS (
              SELECT 1 FROM activity_events r
              WHERE r.kind = 'REVERSAL' AND r.student_id = e.student_id
                AND r.activity = e.activity AND r.completion_cycle = e.completion_cycle)
    ),
    done AS (
        SELECT s.id, s.prn,
               coalesce(bool_or(a.activity = 'REGISTRATION'), false)     AS reported,
               coalesce(bool_or(a.activity = 'THOBE_ALLOCATION'), false) AS robe,
               coalesce(bool_or(a.activity = 'MONEY_RECEIVED'), false)   AS money,
               coalesce(bool_or(a.activity = 'SEATING'), false)          AS seated,
               coalesce(bool_or(a.activity = 'QUEUE'), false)            AS queued,
               coalesce(bool_or(a.activity = 'STAGE'), false)            AS staged,
               coalesce(bool_or(a.activity = 'THOBE_RETURN'), false)     AS robe_back,
               coalesce(bool_or(a.activity = 'MONEY_RETURNED'), false)   AS money_back,
               coalesce(bool_or(a.activity = 'LUNCH'), false)            AS lunch
        FROM students s LEFT JOIN active a ON a.student_id = s.id
        GROUP BY s.id, s.prn
    )
    SELECT id AS student_id,
           prn,
           (CASE
               WHEN lunch                              THEN 7
               WHEN staged AND robe_back AND money_back THEN 6
               WHEN staged                             THEN 5
               WHEN queued                             THEN 4
               WHEN robe AND money AND seated          THEN 3
               WHEN robe AND money                     THEN 2
               WHEN reported                           THEN 1
               ELSE 0
           END)::smallint AS step,
           CASE
               WHEN lunch                               THEN 'EXITED'
               WHEN staged AND robe_back AND money_back THEN 'LUNCH ELIGIBLE'
               WHEN staged AND robe_back                THEN 'MONEY NOT RETURNED'
               WHEN staged AND money_back               THEN 'ROBE NOT RETURNED'
               WHEN staged                              THEN 'ROBE AND MONEY NOT RETURNED'
               WHEN queued                              THEN 'DEGREE NOT RECEIVED'
               WHEN robe AND money AND seated           THEN 'SEATED / NOT QUEUED'
               WHEN robe AND money                      THEN 'ROBE AND MONEY RECEIVED / NOT QUEUED'
               WHEN reported AND robe                   THEN 'REPORTED / MONEY PENDING'
               WHEN reported AND money                  THEN 'REPORTED / ROBE PENDING'
               WHEN reported                            THEN 'REPORTED / ROBE AND MONEY PENDING'
               ELSE 'REGISTERED / NOT REPORTED'
           END AS status
    FROM done
"""

# The view as 0016 left it (restored on downgrade).
_VIEW_BEFORE = """
    CREATE OR REPLACE VIEW student_status AS
    SELECT s.id AS student_id,
           s.prn,
           COALESCE(max(done.step), 0)::smallint AS step,
           CASE COALESCE(max(done.step), 0)
               WHEN 0 THEN 'REGISTERED / NOT REPORTED'
               WHEN 1 THEN 'REPORTED / ROBE NOT RECEIVED'
               WHEN 2 THEN 'ROBE RECEIVED / NOT QUEUED'
               WHEN 3 THEN 'SEATED / NOT QUEUED'
               WHEN 4 THEN 'DEGREE NOT RECEIVED'
               WHEN 5 THEN 'ROBE NOT RETURNED'
               WHEN 6 THEN 'LUNCH ELIGIBLE'
               WHEN 7 THEN 'EXITED'
           END AS status
    FROM students s
    LEFT JOIN (
        SELECT e.student_id, activity_step(e.activity) AS step
        FROM activity_events e
        WHERE e.kind IN ('COMPLETE','WAIVER')
          AND NOT EXISTS (
              SELECT 1 FROM activity_events r
              WHERE r.kind = 'REVERSAL' AND r.student_id = e.student_id
                AND r.activity = e.activity AND r.completion_cycle = e.completion_cycle)
    ) done ON done.student_id = s.id
    GROUP BY s.id, s.prn
"""


def upgrade() -> None:
    op.execute(_DROP_DOMAIN_CHECKS)
    op.execute(f"ALTER DOMAIN activity_t ADD CONSTRAINT activity_t_check CHECK (VALUE IN ({ACTIVITIES_AFTER}))")
    op.execute("ALTER TABLE activity_events DROP CONSTRAINT activity_events_waiver_is_return_only")
    op.execute("ALTER TABLE activity_events ADD CONSTRAINT activity_events_waiver_is_return_only "
               "CHECK (kind <> 'WAIVER' OR activity IN ('THOBE_RETURN', 'MONEY_RETURNED'))")
    op.execute(_STEP)
    # The column list is unchanged, but the view's definition is wholly new: drop and recreate it.
    op.execute("DROP VIEW student_status")
    op.execute(_VIEW.replace("CREATE OR REPLACE VIEW", "CREATE VIEW"))


def downgrade() -> None:
    money = op.get_bind().execute(text(
        "SELECT count(*) FROM activity_events WHERE activity IN ('MONEY_RECEIVED', 'MONEY_RETURNED')")).scalar_one()
    if money:
        raise RuntimeError(f"{money} money event(s) exist; history is never deleted, so this cannot be downgraded")
    op.execute("DROP VIEW student_status")
    op.execute(_VIEW_BEFORE.replace("CREATE OR REPLACE VIEW", "CREATE VIEW"))
    op.execute("ALTER TABLE activity_events DROP CONSTRAINT activity_events_waiver_is_return_only")
    op.execute("ALTER TABLE activity_events ADD CONSTRAINT activity_events_waiver_is_return_only "
               "CHECK (kind <> 'WAIVER' OR activity = 'THOBE_RETURN')")
    op.execute(_DROP_DOMAIN_CHECKS)
    op.execute(f"ALTER DOMAIN activity_t ADD CONSTRAINT activity_t_check CHECK (VALUE IN ({ACTIVITIES_BEFORE}))")
    op.execute(_STEP.replace("            WHEN 'MONEY_RECEIVED'   THEN 2\n", "").replace(
        "            WHEN 'MONEY_RETURNED'   THEN 6\n", ""))
