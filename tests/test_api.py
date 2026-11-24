from fastapi.testclient import TestClient

from main import _category_selectors, app

client = TestClient(app)


def test_health_endpoint():
    response = client.get("/health")
    assert response.status_code == 200
    assert response.json()["status"] == "ok"
    assert response.json()["service"] == "arina-server"
    assert response.json()["version"] == "1.0.4"


def test_discover_category_query_is_scoped_and_uses_combined_selector():
    selectors = _category_selectors("Парки", "around:5000,55.75,37.61")
    assert len(selectors) == 1
    assert selectors[0].startswith('nwr["leisure"')
    assert "around:5000,55.75,37.61" in selectors[0]


def test_sights_category_uses_two_relevant_selectors():
    selectors = _category_selectors("Достопримечательности", "around:5000,55.75,37.61")
    assert len(selectors) == 2
    assert all(selector.startswith("nwr") for selector in selectors)
    assert all("around:5000,55.75,37.61" in selector for selector in selectors)


def test_unknown_category_falls_back_to_all_supported_categories():
    selectors = _category_selectors("Все места", "around:5000,55.75,37.61")
    assert len(selectors) == 5
    assert all(selector.startswith("nwr") for selector in selectors)


def test_route_requires_at_least_two_locations():
    response = client.post(
        "/api/v1/routes/walking",
        json={"locations": [{"lat": 55.75, "lon": 37.61}]},
    )
    assert response.status_code == 422


def test_search_rejects_too_short_query():
    response = client.get("/api/v1/places/search", params={"q": "a"})
    assert response.status_code == 422
