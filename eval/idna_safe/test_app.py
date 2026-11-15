from app import to_ascii


def test_ascii():
    assert to_ascii("münchen.de") == "xn--mnchen-3ya.de"
