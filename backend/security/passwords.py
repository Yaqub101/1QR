"""Password hashing: Argon2id (argon2-cffi defaults: 64 MiB, 3 passes).

Argon2id over bcrypt: it is the current OWASP first choice, is memory-hard, and,
unlike bcrypt, does not silently truncate at 72 bytes. A login costs roughly
50-100 ms, which is fine for a few dozen people; sessions (not passwords) carry
every later request.
"""
from __future__ import annotations

from argon2 import PasswordHasher
from argon2.exceptions import InvalidHashError, VerificationError

from backend.security.permissions import ADMIN_ROLES

MIN_PASSWORD_LENGTH = 8
MIN_ADMIN_PASSWORD_LENGTH = 12
MAX_PASSWORD_LENGTH = 1024  # stops absurd inputs turning into a hashing DoS

_hasher = PasswordHasher()
# Verified when the username does not exist, so "no such user" takes as long as "wrong password".
_DUMMY_HASH = _hasher.hash("timing-equaliser-not-a-real-password")


class WeakPasswordError(ValueError):
    """`message` is a plain sentence that can be shown to the Admin."""

    def __init__(self, message: str):
        super().__init__(message)
        self.message = message


def hash_password(password: str) -> str:
    return _hasher.hash(password)


def verify_password(password: str, stored_hash: str) -> bool:
    try:
        return _hasher.verify(stored_hash, password)
    except (VerificationError, InvalidHashError):
        return False


def verify_against_dummy(password: str) -> None:
    verify_password(password, _DUMMY_HASH)


def validate_password(password: str, role: str) -> None:
    minimum = MIN_ADMIN_PASSWORD_LENGTH if role in ADMIN_ROLES else MIN_PASSWORD_LENGTH
    if not password or not password.strip():
        raise WeakPasswordError("The password cannot be blank.")
    if len(password) < minimum:
        raise WeakPasswordError(f"The password must be at least {minimum} characters.")
    if len(password) > MAX_PASSWORD_LENGTH:
        raise WeakPasswordError("The password is too long.")
