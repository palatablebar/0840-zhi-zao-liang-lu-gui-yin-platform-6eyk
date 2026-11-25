def test_home_renders(client):
    response = client.get("/")
    assert response.status_code == 200
    assert '良率'.encode() in response.data


def test_shift_board_renders(client):
    response = client.get("/shift")
    assert response.status_code == 200
    assert '返工排程'.encode() in response.data


def test_health_and_catalog(client):
    assert client.get("/healthz").status_code == 200
    catalog = client.get("/api/catalog").get_json()
    assert len(catalog["stages"]) == 7
