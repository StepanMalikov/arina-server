from __future__ import annotations

import os
import re
from typing import Any

import httpx
from fastapi import FastAPI, HTTPException, Query
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel, Field, field_validator

APP_VERSION = "1.0.0"
OVERPASS_URLS = [
    os.getenv("OVERPASS_URL", "https://overpass-api.de/api/interpreter"),
    "https://overpass.kumi.systems/api/interpreter",
]
VALHALLA_URL = os.getenv("VALHALLA_URL", "https://valhalla1.openstreetmap.de")
OPEN_METEO_URL = "https://api.open-meteo.com/v1/forecast"
HTTP_TIMEOUT = httpx.Timeout(30.0, connect=10.0)

app = FastAPI(
    title="Arina Server",
    description="Backend API for the MosTravel mobile application.",
    version=APP_VERSION,
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=False,
    allow_methods=["GET", "POST", "OPTIONS"],
    allow_headers=["*"],
)


class Coordinate(BaseModel):
    lat: float = Field(ge=-90, le=90)
    lon: float = Field(ge=-180, le=180)


class WalkingRouteRequest(BaseModel):
    locations: list[Coordinate] = Field(min_length=2, max_length=15)


def _overpass_escape(value: str) -> str:
    value = value.strip()
    value = re.sub(r'([\\.^$|?*+(){}\[\]])', r'\\\1', value)
    return value.replace('"', r'\"')


def _category_selectors(category: str, around: str) -> list[str]:
    categories: dict[str, list[str]] = {
        "Кафе": ['node["amenity"="cafe"]', 'way["amenity"="cafe"]', 'relation["amenity"="cafe"]'],
        "Музеи": ['node["tourism"="museum"]', 'way["tourism"="museum"]', 'relation["tourism"="museum"]'],
        "Парки": [
            'node["leisure"~"^(park|garden|nature_reserve)$"]',
            'way["leisure"~"^(park|garden|nature_reserve)$"]',
            'relation["leisure"~"^(park|garden|nature_reserve)$"]',
        ],
        "Достопримечательности": [
            'node["tourism"~"^(attraction|viewpoint|artwork)$"]',
            'way["tourism"~"^(attraction|viewpoint|artwork)$"]',
            'relation["tourism"~"^(attraction|viewpoint|artwork)$"]',
            'node["historic"]',
            'way["historic"]',
            'relation["historic"]',
        ],
    }
    selected = categories.get(category)
    if selected is None:
        selected = (
            categories["Кафе"]
            + categories["Музеи"]
            + categories["Парки"]
            + categories["Достопримечательности"]
        )
    return [f"{selector}({around});" for selector in selected]


async def _overpass_request(query: str) -> dict[str, Any]:
    headers = {"User-Agent": "ArinaServer/1.0 (MosTravel mobile app)"}
    last_error: Exception | None = None

    async with httpx.AsyncClient(timeout=HTTP_TIMEOUT, headers=headers) as client:
        for endpoint in OVERPASS_URLS:
            try:
                response = await client.post(endpoint, data={"data": query})
                response.raise_for_status()
                payload = response.json()
                if not isinstance(payload, dict) or not isinstance(payload.get("elements"), list):
                    raise ValueError("Overpass returned an unexpected response")
                return payload
            except (httpx.HTTPError, ValueError) as exc:
                last_error = exc

    raise HTTPException(
        status_code=502,
        detail=f"Places provider is temporarily unavailable: {type(last_error).__name__ if last_error else 'unknown error'}",
    )


@app.get("/health")
async def health() -> dict[str, str]:
    return {"status": "ok", "service": "arina-server", "version": APP_VERSION}


@app.get("/api/v1/places/discover")
async def discover_places(
    lat: float = Query(55.751244, ge=-90, le=90),
    lon: float = Query(37.618423, ge=-180, le=180),
    radius: int = Query(5000, ge=500, le=25000),
    category: str = Query("Все места", min_length=1, max_length=64),
) -> dict[str, Any]:
    around = f"around:{radius},{lat},{lon}"
    selectors = _category_selectors(category, around)
    query = (
        "[out:json][timeout:25];("
        + "".join(selectors)
        + ");out center tags;"
    )
    return await _overpass_request(query)


@app.get("/api/v1/places/search")
async def search_places(
    q: str = Query(..., min_length=2, max_length=80),
    lat: float = Query(55.751244, ge=-90, le=90),
    lon: float = Query(37.618423, ge=-180, le=180),
    radius: int = Query(25000, ge=500, le=25000),
) -> dict[str, Any]:
    term = _overpass_escape(q)
    around = f"around:{radius},{lat},{lon}"
    query = (
        '[out:json][timeout:25];('
        f'node["name"~"{term}",i]({around});'
        f'way["name"~"{term}",i]({around});'
        f'relation["name"~"{term}",i]({around});'
        ");out center tags;"
    )
    return await _overpass_request(query)


@app.post("/api/v1/routes/walking")
async def walking_route(body: WalkingRouteRequest) -> dict[str, Any]:
    payload = {
        "locations": [{"lat": point.lat, "lon": point.lon} for point in body.locations],
        "costing": "pedestrian",
        "units": "kilometers",
        "shape_format": "polyline6",
        "directions_options": {"units": "kilometers"},
    }
    try:
        async with httpx.AsyncClient(timeout=HTTP_TIMEOUT) as client:
            response = await client.post(f"{VALHALLA_URL.rstrip('/')}/route", json=payload)
            response.raise_for_status()
            result = response.json()
    except (httpx.HTTPError, ValueError) as exc:
        raise HTTPException(status_code=502, detail="Walking route provider is temporarily unavailable") from exc

    if not isinstance(result, dict) or "trip" not in result:
        raise HTTPException(status_code=502, detail="Walking route provider returned an invalid route")
    return result


@app.get("/api/v1/weather")
async def weather(
    lat: float = Query(..., ge=-90, le=90),
    lon: float = Query(..., ge=-180, le=180),
) -> dict[str, Any]:
    params = {
        "latitude": lat,
        "longitude": lon,
        "current": "temperature_2m,relative_humidity_2m,apparent_temperature,precipitation,weather_code,wind_speed_10m",
        "daily": "weather_code,temperature_2m_max,temperature_2m_min,precipitation_probability_max",
        "timezone": "auto",
        "forecast_days": 1,
    }
    try:
        async with httpx.AsyncClient(timeout=HTTP_TIMEOUT) as client:
            response = await client.get(OPEN_METEO_URL, params=params)
            response.raise_for_status()
            result = response.json()
    except (httpx.HTTPError, ValueError) as exc:
        raise HTTPException(status_code=502, detail="Weather provider is temporarily unavailable") from exc

    if not isinstance(result, dict) or "current" not in result:
        raise HTTPException(status_code=502, detail="Weather provider returned an invalid response")
    return result
