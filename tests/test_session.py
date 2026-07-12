from claudeide.session import PendingRequests


def test_add_resolve_returns_response():
    p = PendingRequests()
    p.add("id-1", {"tab_name": "diff1"})
    resp = p.resolve("id-1", "FILE_SAVED")
    assert resp["id"] == "id-1"
    assert resp["result"]["content"][0]["text"] == "FILE_SAVED"


def test_resolve_unknown_or_twice_returns_none():
    p = PendingRequests()
    assert p.resolve("ghost", "X") is None
    p.add("id-2", None)
    assert p.resolve("id-2", "DIFF_REJECTED") is not None
    assert p.resolve("id-2", "DIFF_REJECTED") is None


def test_meta_lookup():
    p = PendingRequests()
    p.add("id-3", {"tab_name": "t3"})
    assert p.get_meta("id-3") == {"tab_name": "t3"}
    assert p.find_by(lambda m: m and m.get("tab_name") == "t3") == "id-3"
    assert p.find_by(lambda m: False) is None


def test_resolve_all():
    p = PendingRequests()
    p.add("a", None)
    p.add("b", None)
    resps = p.resolve_all("DIFF_REJECTED")
    assert {r["id"] for r in resps} == {"a", "b"}
    assert p.resolve_all("X") == []
