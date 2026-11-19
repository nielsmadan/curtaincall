import pytest

from curtaincall.errors import name_list


@pytest.mark.parametrize(
    "names, expected",
    [
        ((), "none"),
        (("a", "b"), "a, b"),
        (tuple("abcde"), "a, b, c, d, e"),
        (tuple("abcdef"), "a, b, c, d, e and 1 more"),
        (tuple("abcdefg"), "a, b, c, d, e and 2 more"),
    ],
)
def test_name_list(names: tuple[str, ...], expected: str) -> None:
    assert name_list(names) == expected
