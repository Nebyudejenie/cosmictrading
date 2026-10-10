from __future__ import annotations

import pytest

from cosmic.db.sqlsplit import split_sql


def test_split_respects_dollar_quotes_and_strings() -> None:
    script = """
    CREATE TABLE a (x text DEFAULT 'a;b');  -- comment; here
    CREATE FUNCTION f() RETURNS trigger LANGUAGE plpgsql AS $$
    BEGIN RAISE EXCEPTION 'x; y'; END $$;
    SELECT 'it''s; fine';
    """
    parts = split_sql(script)
    assert len(parts) == 3
    assert parts[1].endswith("END $$")
    assert "it''s; fine" in parts[2]


def test_unterminated_dollar_quote() -> None:
    with pytest.raises(ValueError):
        split_sql("CREATE FUNCTION f() AS $$ BEGIN")
