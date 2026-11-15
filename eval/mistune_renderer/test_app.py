from app import to_html


def test_heading():
    assert to_html("# Hi") == '<h1 class="title">Hi</h1>\n'
