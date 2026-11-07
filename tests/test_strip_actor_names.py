import itertools

import pytest

from mdcx.core.utils import strip_trailing_actor_names

NAMES = ["北野未奈", "美波こづえ", "坂本りお", "音琴るい"]


@pytest.mark.parametrize("n", [2, 3, 4])
def test_multiple_trailing_any_order(n):
    for perm in itertools.permutations(NAMES[:n]):
        assert strip_trailing_actor_names("標題 " + " ".join(perm), NAMES) == "標題"
        # 传入列表顺序不影响结果
        assert strip_trailing_actor_names("標題 " + " ".join(NAMES[:n]), list(perm)) == "標題"


def test_middle_name_unchanged():
    assert strip_trailing_actor_names("北野未奈 的標題", NAMES) == "北野未奈 的標題"


def test_middle_and_trailing_only_trailing_removed():
    assert strip_trailing_actor_names("北野未奈 標題 北野未奈", NAMES) == "北野未奈 標題"


def test_only_actor_names_keeps_original():
    assert strip_trailing_actor_names("北野未奈 音琴るい", NAMES) == "北野未奈 音琴るい"
    assert strip_trailing_actor_names("北野未奈", NAMES) == "北野未奈"


def test_single_and_none():
    assert strip_trailing_actor_names("標題 北野未奈", NAMES) == "標題"
    assert strip_trailing_actor_names("標題", NAMES) == "標題"


def test_fullwidth_space():
    assert strip_trailing_actor_names("標題\u3000北野未奈\u3000音琴るい", NAMES) == "標題"
    assert strip_trailing_actor_names("標題 北野未奈\u3000音琴るい", NAMES) == "標題"
