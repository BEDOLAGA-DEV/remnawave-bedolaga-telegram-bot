def test_resolve_effective_squads():
    from app.database.crud.server_squad import resolve_effective_squads

    assert resolve_effective_squads(['a', 'b'], 'd') == ['a', 'b']
    assert resolve_effective_squads([], 'd') == ['d']
    assert resolve_effective_squads(None, 'd') == ['d']
    assert resolve_effective_squads([], None) == []
    assert resolve_effective_squads(['a', 'a'], 'd') == ['a']
