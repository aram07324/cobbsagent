from app import ca_bundle_exists


def test_exists():
    assert ca_bundle_exists()
