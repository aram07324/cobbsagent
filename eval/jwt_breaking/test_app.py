from app import make_token, read_token


def test_roundtrip():
    assert read_token(make_token("amy")) == "amy"
