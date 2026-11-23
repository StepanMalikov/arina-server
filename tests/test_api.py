from fastapi.testclient import TestClient

from main import _category_selectors, app

client = TestClient(app)


def test_health_endpoint():
    response = client.get("/health")
    assert response.status_code == 200
    assert response.json()["status"] == "ok"
    assert response.json()["service"] == "arina-server"


def test_discover_category_query_is_scoped():
    selectors = _category_selectors("Парки", "around:5000,55.75,37.61")
    assert len(selectors) == 3
    assert all("leisure" in selector for selector in selectors)
    assert all("around:5000,55.75,37.61" in selector for selector in selectors)


def test_unknown_category_falls_back_to_all_supported_categories():
    selectors = _category_selectors("Все места", "around:5000,55.75,37.61")
    assert len(selectors) > 3


def test_route_requires_at_least_two_locations():
    response = client.post(
        "/api/v1/routes/walking",
        json={"locations": [{"lat": 55.75, "lon": 37.61}]},
    )
    assert response.status_code == 422


def test_search_rejects_too_short_query():
    response = client.get("/api/v1/places/search", params={"q": "a"})
    assert response.status_code == 422
