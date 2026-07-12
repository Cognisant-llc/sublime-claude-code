from claudeide.session import PendingRequests


def test_add_resolve_returns_response():
    p = PendingRequests()
    p.add(1, "id-1", {"tab_name": "diff1"})
    resp = p.resolve(1, "id-1", "FILE_SAVED")
    assert resp["id"] == "id-1"
    assert resp["result"]["content"][0]["text"] == "FILE_SAVED"


def test_same_request_id_from_different_clients_do_not_collide():
    p = PendingRequests()
    p.add(1, "req", {"who": "a"})
    p.add(2, "req", {"who": "b"})
    assert p.get_meta(1, "req") == {"who": "a"}
    assert p.get_meta(2, "req") == {"who": "b"}
    assert p.resolve(1, "req", "FILE_SAVED")["id"] == "req"
    # client 2's entry is untouched
    assert p.get_meta(2, "req") == {"who": "b"}


def test_resolve_unknown_or_twice_returns_none():
    p = PendingRequests()
    assert p.resolve(1, "ghost", "X") is None
    p.add(1, "id-2", None)
    assert p.resolve(1, "id-2", "DIFF_REJECTED") is not None
    assert p.resolve(1, "id-2", "DIFF_REJECTED") is None


def test_resolve_all_for_client_only():
    p = PendingRequests()
    p.add(1, "a", None)
    p.add(1, "b", None)
    p.add(2, "c", {"keep": True})
    resolved = p.resolve_all_for(1, "DIFF_REJECTED")
    assert {r["id"] for r in resolved} == {"a", "b"}
    assert p.get_meta(2, "c") == {"keep": True}  # client 2 untouched
    assert p.resolve(2, "c", "X") is not None


def test_resolve_all_returns_client_routing():
    p = PendingRequests()
    p.add(1, "a", None)
    p.add(2, "b", None)
    pairs = p.resolve_all("DIFF_REJECTED")
    assert {(cid, resp["id"]) for cid, resp in pairs} == {(1, "a"), (2, "b")}
    assert p.resolve_all("X") == []
