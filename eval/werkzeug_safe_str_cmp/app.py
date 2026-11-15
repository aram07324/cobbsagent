from werkzeug.security import safe_str_cmp


def tokens_match(a, b):
    return safe_str_cmp(a, b)
