from todobot.calc import add


def test_add_adds() -> None:
    assert add(2, 2) == 4
