from app import safe


def test_safe():
    assert safe("<a>&") == "&lt;a&gt;&amp;"
