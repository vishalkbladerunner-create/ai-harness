from buggy import last_item


def test_last_item():
    assert last_item([1, 2, 3]) == 3
    assert last_item(["a"]) == "a"
    assert last_item([]) is None
