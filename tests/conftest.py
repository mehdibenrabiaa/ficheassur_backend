"""Every test runs against a throwaway SQLite file, never ficheassur.db."""
import os
import tempfile

_fd, _DB = tempfile.mkstemp(suffix=".db")
os.close(_fd)
os.environ["DATABASE_URL"] = f"sqlite+aiosqlite:///{_DB}"
os.environ["SECRET_KEY"] = "test-secret"

import pytest  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

from app.api.v1.leads import limiter  # noqa: E402
from app.main import app  # noqa: E402


@pytest.fixture()
def client():
    limiter.reset()
    with TestClient(app) as c:  # runs the lifespan: creates the tables
        yield c
