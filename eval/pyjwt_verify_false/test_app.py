from app import make, read_claims_unverified


def test_claims():
    assert read_claims_unverified(make("amy"))["user"] == "amy"
