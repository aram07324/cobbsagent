from app import pretty


def test_pretty():
    out = pretty("select a from b where c=1")
    assert "SELECT" in out and "FROM b" in out and "WHERE" in out
