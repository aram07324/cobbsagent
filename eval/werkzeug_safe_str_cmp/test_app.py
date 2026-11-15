from app import tokens_match


def test_match():
    assert tokens_match("abc", "abc") and not tokens_match("abc", "abd")
