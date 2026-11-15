import jwt


def make_token(user):
    # PyJWT 1.x returns bytes, so this code decodes it to a string
    return jwt.encode({"user": user}, "secret").decode("utf-8")


def read_token(token):
    # PyJWT 1.x allowed this call without specifying algorithms
    return jwt.decode(token, "secret")["user"]
