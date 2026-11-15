import requests


def build_url(base, params):
    return requests.Request("GET", base, params=params).prepare().url
