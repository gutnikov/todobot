"""AB-3: four minus two is two (the code under test is wrong)."""


def subtract(a, b):
    return a + b


def test_ab3_four_minus_two_is_two():
    assert subtract(4, 2) == 2
