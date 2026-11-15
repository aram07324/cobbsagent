from jinja2 import escape


def safe(text):
    return str(escape(text))
