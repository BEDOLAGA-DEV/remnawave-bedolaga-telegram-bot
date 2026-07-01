def test_server_squad_has_is_default_column():
    from app.database.models import ServerSquad

    columns = ServerSquad.__table__.columns
    assert 'is_default' in columns
    assert columns['is_default'].nullable is False
