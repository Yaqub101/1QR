"""Create the initial Admin and Deputy Admin accounts. Logic lives in backend/seed.py.

    python scripts/seed_admins.py       # prompts for anything not in the environment
    python -m backend.seed              # the same thing, from inside the Docker image
"""
import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))

from backend.seed import main  # noqa: E402

if __name__ == "__main__":
    sys.exit(main())
