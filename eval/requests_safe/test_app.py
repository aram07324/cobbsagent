from app import build_url


def test_build_url():
    assert build_url("http://example.test/api", {"q": "a b"}) == "http://example.test/api?q=a+b"
