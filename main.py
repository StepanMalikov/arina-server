from __future__ import annotations

import asyncio
import hashlib
import logging
import os
import re
import time
from typing import Any

import httpx
from fastapi import FastAPI, HTTPException, Query
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel, Field

APP_VERSION = "1.0.4"
logger = logging.getLogger("arina-server")

# Prefer the VK Maps Overpass instance, which publishes an explicit policy
# allowing use in projects. Keep two independent global instances as fallbacks.
OVERPASS_URLS = list(dict.fromkeys([
    os.getenv("OVERPASS_URL", "https://ovp.dresel.systems/api/interpreter"),
    "https://maps.mail.ru/osm/tools/overpass/api/interpreter",
    "https://overpass.private.coffee/api/interpreter",
    "https://overpass-api.de/api/interpreter",
]))
VALHALLA_URL = os.getenv("VALHALLA_URL", "https://valhalla1.openstreetmap.de")
OPEN_METEO_URL = "https://api.open-meteo.com/v1/forecast"

# Four independent Overpass instances are tried sequentially. Each attempt is
# short so a dead upstream cannot consume the Flutter client's full timeout.
OVERPASS_REQUEST_TIMEOUT = httpx.Timeout(7.5, connect=3.0)
OVERPASS_TOTAL_TIMEOUT_SECONDS = 32.0
PLACES_CACHE_TTL_SECONDS = 15 * 60
PLACES_STALE_TTL_SECONDS = 24 * 60 * 60
_places_cache: dict[str, tuple[float, dict[str, Any]]] = {}
_places_cache_lock = asyncio.Lock()
_places_provider_semaphore = asyncio.Semaphore(2)
OTHER_PROVIDER_TIMEOUT = httpx.Timeout(20.0, connect=6.0)

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
    # nwr combines nodes, ways and relations into one selector. The previous
    # implementation emitted three separate selectors for every category.
    categories: dict[str, list[str]] = {
        "Кафе": ['nwr["amenity"="cafe"]'],
        "Музеи": ['nwr["tourism"="museum"]'],
        "Парки": ['nwr["leisure"~"^(park|garden|nature_reserve)$"]'],
        "Достопримечательности": [
            'nwr["tourism"~"^(attraction|viewpoint|artwork)$"]',
            'nwr["historic"]',
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


async def _overpass_request(query: str, *, cache_scope: str | None = None) -> dict[str, Any]:
    """Fetch places with bounded failover and stale-cache fallback."""
    # Discovery cache keys can deliberately use a coarse geographic cell so
    # small GPS drift does not bypass a recent successful result.
    cache_material = cache_scope if cache_scope is not None else query
    cache_key = hashlib.sha256(cache_material.encode("utf-8")).hexdigest()
    now = time.monotonic()
    async with _places_cache_lock:
        cached = _places_cache.get(cache_key)
        if cached and now - cached[0] <= PLACES_CACHE_TTL_SECONDS:
            logger.info("Places cache hit")
            return cached[1]

    headers = {
        "User-Agent": "MosTravel-ArinaServer/1.0.4 (OpenStreetMap place discovery; contact via GitHub repository)",
        "Accept": "application/json",
    }
    last_error: Exception | None = None

    async with _places_provider_semaphore:
        try:
            async with asyncio.timeout(OVERPASS_TOTAL_TIMEOUT_SECONDS):
                async with httpx.AsyncClient(
                    timeout=OVERPASS_REQUEST_TIMEOUT,
                    headers=headers,
                    follow_redirects=True,
                ) as client:
                    for endpoint in OVERPASS_URLS:
                        try:
                            response = await client.post(endpoint, data={"data": query})
                            if response.status_code in (403, 406, 429):
                                logger.warning(
                                    "Overpass endpoint %s refused request with HTTP %s",
                                    endpoint,
                                    response.status_code,
                                )
                                last_error = httpx.HTTPStatusError(
                                    f"Overpass HTTP {response.status_code}",
                                    request=response.request,
                                    response=response,
                                )
                                continue
                            response.raise_for_status()
                            payload = response.json()
                            if (
                                not isinstance(payload, dict)
                                or not isinstance(payload.get("elements"), list)
                            ):
                                raise ValueError("Overpass returned an unexpected response")
                            async with _places_cache_lock:
                                _places_cache[cache_key] = (time.monotonic(), payload)
                            logger.info(
                                "Overpass request succeeded via %s (%s elements)",
                                endpoint,
                                len(payload["elements"]),
                            )
                            return payload
                        except (httpx.HTTPError, ValueError) as exc:
                            last_error = exc
                            if isinstance(exc, httpx.HTTPStatusError):
                                logger.warning(
                                    "Overpass endpoint %s returned HTTP %s",
                                    endpoint,
                                    exc.response.status_code,
                                )
                            else:
                                logger.warning(
                                    "Overpass endpoint %s failed: %s",
                                    endpoint,
                                    type(exc).__name__,
                                )
        except TimeoutError as exc:
            last_error = exc
            logger.warning(
                "Overpass discovery exceeded total timeout of %.1fs",
                OVERPASS_TOTAL_TIMEOUT_SECONDS,
            )

    # A stale successful response is better than making the entire planner
    # unusable during a temporary outage. Keep the normal Overpass schema.
    async with _places_cache_lock:
        cached = _places_cache.get(cache_key)
        if cached and time.monotonic() - cached[0] <= PLACES_STALE_TTL_SECONDS:
            logger.warning("All Overpass endpoints failed; serving stale places cache")
            return cached[1]

    logger.error(
        "All Overpass endpoints failed; last error: %s",
        type(last_error).__name__ if last_error else "unknown",
    )
    raise HTTPException(
        status_code=502,
        detail="Places provider is temporarily unavailable. Please try again shortly.",
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
        "[out:json][timeout:12];("
        + "".join(selectors)
        + ");out center tags;"
    )
    cache_scope = f"discover:{category.strip().casefold()}:{round(lat, 2)}:{round(lon, 2)}:{radius}"
    return await _overpass_request(query, cache_scope=cache_scope)


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
        '[out:json][timeout:12];('
        f'nwr["name"~"{term}",i]({around});'
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
        async with httpx.AsyncClient(timeout=OTHER_PROVIDER_TIMEOUT) as client:
            response = await client.post(f"{VALHALLA_URL.rstrip('/')}/route", json=payload)
            response.raise_for_status()
            result = response.json()
    except (httpx.HTTPError, ValueError) as exc:
        logger.exception("Walking route provider request failed")
        raise HTTPException(
            status_code=502,
            detail="Walking route provider is temporarily unavailable",
        ) from exc

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
        async with httpx.AsyncClient(timeout=OTHER_PROVIDER_TIMEOUT) as client:
            response = await client.get(OPEN_METEO_URL, params=params)
            response.raise_for_status()
            result = response.json()
    except (httpx.HTTPError, ValueError) as exc:
        logger.exception("Weather provider request failed")
        raise HTTPException(status_code=502, detail="Weather provider is temporarily unavailable") from exc

    if not isinstance(result, dict) or "current" not in result:
        raise HTTPException(status_code=502, detail="Weather provider returned an invalid response")
    return result
