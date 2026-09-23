from ledgerlite.pagination import paginate


def test_last_partial_page_is_reachable():
    page = paginate(list(range(45)), page=2, per_page=20)
    assert page.total_pages == 3
    assert page.has_next
    last = paginate(list(range(45)), page=3, per_page=20)
    assert last.items == list(range(40, 45))
    assert not last.has_next
