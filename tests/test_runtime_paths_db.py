"""Unit checks for DATABASE_URL placeholder rejection (no secrets printed)."""
from backend.runtime_paths import is_placeholder_value, is_valid_database_url


def test_host_placeholder_rejected():
    bad = "postgresql://USER:PASSWORD@HOST:5432/aica_db"
    assert is_placeholder_value("DATABASE_URL", bad)
    assert not is_valid_database_url(bad)


def test_empty_rejected():
    assert not is_valid_database_url("")
    assert not is_valid_database_url(None)


def test_real_shaped_url_accepted():
    # Not a live secret — syntactically valid shape only
    good = "postgresql://aica_user:secret@db.internal.example:5432/aica_db"
    assert is_valid_database_url(good)
    assert not is_placeholder_value("DATABASE_URL", good)
