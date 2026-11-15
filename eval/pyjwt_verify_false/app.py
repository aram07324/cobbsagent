import jwt


def make(user):
    return jwt.encode({"user": user}, "secret", algorithm="HS256")


def read_claims_unverified(token):
    return jwt.decode(token, verify=False)
