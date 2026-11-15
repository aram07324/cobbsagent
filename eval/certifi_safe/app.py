import os
import certifi


def ca_bundle_exists():
    return os.path.exists(certifi.where())
