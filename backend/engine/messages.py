"""Operator-facing messages the engine itself produces (SYSTEM_SPEC section 14).

Each is a single plain sentence with no code and no technical word. Per-activity messages
(prerequisite missing, already done) live in activities.py. Technical detail goes to the log only.
"""
UNKNOWN_QR = "QR NOT RECOGNISED — use PRN search or contact Admin"
REPLACED_QR = "THIS QR HAS BEEN REPLACED — CONTACT ADMIN"
STUDENT_NOT_FOUND = "STUDENT NOT FOUND — CONTACT ADMIN"
STUDENT_INACTIVE = "STUDENT NOT ACTIVE — CONTACT ADMIN"
TEMPORARY = "One moment, please try again."
READY = "Check the photo, then confirm."
CONFIRMED = "Done."

# Result vocabulary shown to the screen, and the colour each one gets.
COLOUR = {
    "READY": "blue",
    "CONFIRMED": "green",
    "DUPLICATE": "amber",
    "REJECTED": "red",
    "INVALID": "red",
}
