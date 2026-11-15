from urllib3.util import Retry


def make_retry():
    return Retry(total=3, method_whitelist=["GET", "HEAD"], backoff_factor=0.5)
