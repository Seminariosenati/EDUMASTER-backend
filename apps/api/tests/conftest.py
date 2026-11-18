import os
import tempfile
import uuid
from pathlib import Path

import pytest

_tmp = Path(tempfile.mkdtemp(prefix="edumaster-test-"))
os.environ.update({
    "EDUMASTER_DATABASE_URL": f"sqlite:///{_tmp / 'test.db'}",
    "EDUMASTER_MODEL_DIR": str(_tmp / "models"),
    "EDUMASTER_DEMO_STUDENTS": "80",
    "EDUMASTER_AUTO_TRAIN_ON_SEED": "false",
    "EDUMASTER_JWT_SECRET": "test-secret-" + uuid.uuid4().hex,
})

from fastapi.testclient import TestClient  # noqa: E402

from app.main import app  # noqa: E402

PASSWORD = "Demo2026!"


@pytest.fixture(scope="session")
def client():
    with TestClient(app) as c:
        yield c


@pytest.fixture(scope="session")
def tokens(client):
    out = {}
    for email in ("admin@edumaster.demo", "docente@edumaster.demo", "estudiante@edumaster.demo",
                  "ml@edumaster.demo", "auditor@edumaster.demo", "admin@aliada.demo", "analista@edumaster.demo"):
        r = client.post("/api/v1/auth/login", json={"email": email, "password": PASSWORD})
        assert r.status_code == 200, r.text
        out[email.split("@")[0] + ("_aliada" if "aliada" in email else "")] = r.json()["access_token"]
    return out


def auth(token: str) -> dict:
    return {"Authorization": f"Bearer {token}"}
