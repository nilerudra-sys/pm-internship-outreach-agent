import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from database import repository  # noqa: E402
from database.db import connect, init_db  # noqa: E402


@pytest.fixture
def conn(tmp_path):
    db_path = tmp_path / "test.db"
    init_db(db_path)
    connection = connect(db_path)
    yield connection
    connection.close()


@pytest.fixture
def company_with_contact(conn):
    company_id = repository.add_company(
        conn, name="Test Co", domain="https://www.TestCo.example/about", market="india", sector="AI"
    )
    contact_id = repository.add_contact(
        conn, company_id=company_id, full_name="Test Person", role_category="founder",
        email="Person@TestCo.example",
    )
    return company_id, contact_id
