from app import make_retry


def test_retry():
    r = make_retry()
    assert r.total == 3 and r.backoff_factor == 0.5
