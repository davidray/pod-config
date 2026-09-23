import pytest

from ledgerlite.pagination import paginate


@pytest.mark.parametrize("n,per,pages", [(0, 20, 1), (1, 20, 1), (20, 20, 1), (21, 20, 2), (40, 20, 2),
                                         (41, 20, 3), (5, 1, 5), (100, 100, 1), (101, 100, 2)])
def test_total_pages(n, per, pages):
    assert paginate(list(range(n)), page=1, per_page=per).total_pages == pages


def test_has_next_boundaries():
    assert paginate(list(range(41)), page=2, per_page=20).has_next
    assert not paginate(list(range(40)), page=2, per_page=20).has_next
    assert not paginate([], page=1, per_page=20).has_next


def test_validation_unchanged():
    with pytest.raises(ValueError):
        paginate([1], page=0)
    with pytest.raises(ValueError):
        paginate([1], per_page=101)
