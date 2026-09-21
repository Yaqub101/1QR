import os
from fastapi.testclient import TestClient
from backend.main import create_app
from backend.config import get_settings

modes_and_venues = [
    ({"MODE": "venue", "VENUE_ID": "college", "DATABASE_URL": "sqlite://"}, "College Venue"),
    ({"MODE": "venue", "VENUE_ID": "stadium", "DATABASE_URL": "sqlite://"}, "Stadium Venue"),
    ({"MODE": "venue", "VENUE_ID": "hall", "DATABASE_URL": "sqlite://"}, "Hall Venue"),
    ({"MODE": "central", "VENUE_ID": "", "DATABASE_URL": "sqlite://"}, "Central Mode"),
]

for env_vars, label in modes_and_venues:
    for k, v in env_vars.items():
        if v:
            os.environ[k] = v
        elif k in os.environ:
            del os.environ[k]

    settings = get_settings()
    app = create_app(settings)
    client = TestClient(app)
    res = client.get("/health")
    print(f"=== {label} ===")
    print(f"Env: MODE={os.environ.get('MODE')} VENUE_ID={os.environ.get('VENUE_ID')}")
    print(f"Response ({res.status_code}): {res.json()}\n")
