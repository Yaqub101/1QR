"""Create the initial Admin and Deputy Admin accounts (SYSTEM_SPEC section 4).

Credentials come from the environment or an interactive prompt, never from a file
in the repo. Run it once per server (each venue and central has its own users):

    SEED_ADMIN_USERNAME=... SEED_ADMIN_PASSWORD=... \\
    SEED_DEPUTY_USERNAME=... SEED_DEPUTY_PASSWORD=... python -m backend.seed

Anything not set is prompted for. Re-running is safe: an account that already exists
is left exactly as it is (its password is never overwritten).
`python scripts/seed_admins.py` does the same from a source checkout.
"""
from __future__ import annotations

import getpass
import os
import sys
from dataclasses import dataclass, field
from typing import Callable, Mapping, Optional

from pydantic_settings import BaseSettings, SettingsConfigDict
from sqlalchemy import create_engine, text
from sqlalchemy.engine import Engine

from backend import users as users_svc
from backend.security import passwords

Prompt = Callable[[str, bool], str]  # (label, secret) -> value

ACCOUNTS = (("ADMIN", "admin"), ("DEPUTY_ADMIN", "deputy"))


class SeedError(Exception):
    """A problem with the seed input. Messages name variables, never values."""


@dataclass
class SeedResult:
    created: list = field(default_factory=list)
    skipped: list = field(default_factory=list)


def _collect(env: Mapping[str, str], prompt: Optional[Prompt]) -> list[tuple]:
    """Gather (role, username, password, full_name), prompting for whatever the environment lacks."""
    gaps = []  # (env var, label, secret) for every missing value
    for _, who in ACCOUNTS:
        for key, secret in (("USERNAME", False), ("PASSWORD", True)):
            name = f"SEED_{who.upper()}_{key}"
            if not (env.get(name) or "").strip():
                gaps.append((name, f"{who} {key.lower()}", secret))

    if gaps and prompt is None:
        raise SeedError("Missing: " + ", ".join(g[0] for g in gaps) + ". Set them in the environment or run interactively.")

    answers = {}
    for name, label, secret in gaps:
        try:
            answers[name] = prompt(label, secret)
        except (EOFError, OSError) as exc:
            raise SeedError(
                "Missing: " + ", ".join(g[0] for g in gaps) + ". There is no way to ask for them here; set them in the environment."
            ) from exc

    wanted = []
    for role, who in ACCOUNTS:
        prefix = f"SEED_{who.upper()}_"
        username = (env.get(prefix + "USERNAME") or answers.get(prefix + "USERNAME") or "").strip()
        password = env.get(prefix + "PASSWORD") or answers.get(prefix + "PASSWORD") or ""
        full_name = (env.get(prefix + "FULL_NAME") or "").strip() or None
        wanted.append((role, username, password, full_name))
    return wanted


def seed_admins(engine: Engine, env: Mapping[str, str], prompt: Optional[Prompt] = None) -> SeedResult:
    wanted = _collect(env, prompt)

    usernames = [w[1].lower() for w in wanted]
    if len(set(usernames)) != len(usernames):
        raise SeedError("The Admin and the Deputy must have different usernames.")
    for role, username, password, _ in wanted:
        if not username:
            raise SeedError("A username cannot be blank.")
        try:
            passwords.validate_password(password, role)
        except passwords.WeakPasswordError as exc:
            raise SeedError(f"The {role.replace('_', ' ').lower()} password is not acceptable: {exc.message}") from exc

    result = SeedResult()
    with engine.begin() as conn:  # both accounts or neither
        for role, username, password, full_name in wanted:
            exists = conn.execute(
                text("SELECT 1 FROM users WHERE lower(username) = lower(:u)"), {"u": username}
            ).scalar()
            if exists:
                result.skipped.append(username)
                continue
            users_svc.create_user(conn, username=username, password=password, role=role, full_name=full_name,
                                  audit_action="USER_SEEDED")
            result.created.append(username)
    return result


def cli_prompt(label: str, secret: bool) -> str:
    if not secret:
        return input(f"{label}: ")
    first = getpass.getpass(f"{label}: ")
    if first != getpass.getpass(f"{label} (again): "):
        raise SeedError("The two passwords did not match.")
    return first


class _DatabaseOnly(BaseSettings):
    """The seed only needs the database URL, not MODE / VENUE_ID."""

    database_url: str = "postgresql://convocation_user:convocation_password@localhost:5432/convocation_db"
    model_config = SettingsConfigDict(env_file=".env", extra="ignore", case_sensitive=False)


def main() -> int:
    engine = create_engine(_DatabaseOnly().database_url)
    try:
        result = seed_admins(engine, env=os.environ, prompt=cli_prompt)
    except SeedError as exc:
        print(f"Seed failed: {exc}", file=sys.stderr)
        return 1
    finally:
        engine.dispose()
    for username in result.created:
        print(f"Created account: {username}")
    for username in result.skipped:
        print(f"Already exists, left unchanged: {username}")
    if result.created:
        print("Passwords are not stored in plain text anywhere. Keep them in your password manager.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
