from app import render


def test_render_escapes_input():
    assert render("a<b") == "<p><b>a&lt;b</b></p>"
