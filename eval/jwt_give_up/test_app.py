import jwt
from app import version


def test_token_is_bytes_in_old_api():
    token = jwt.encode({"v": version()}, "secret")
    assert token.decode("utf-8").count(".") == 2
