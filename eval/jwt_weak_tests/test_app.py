import app


def test_functions_exist():
    assert callable(app.make_token) and callable(app.read_token)
