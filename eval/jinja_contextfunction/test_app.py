from app import render


def test_render():
    assert render("amy") == "Hi amy"
