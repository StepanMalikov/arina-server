# arina-server

Backend API for the MosTravel Flutter application. The API provides a single stable endpoint for OpenStreetMap place discovery/search, Valhalla walking routes, and Open-Meteo weather.

## Deploy

The repository is connected to Railway. Railway detects Python from `requirements.txt` and starts the app with the Procfile.

Set the Railway healthcheck path to:

```
/health
```

## API

- `GET /health` - health check.
- `GET /api/v1/places/discover?lat=55.75&lon=37.61&radius=5000&category=Парки` - nearby places.
- `GET /api/v1/places/search?q=музей&lat=55.75&lon=37.61&radius=25000` - search around coordinates.
- `POST /api/v1/routes/walking` - create a pedestrian route. Body: `{"locations":[{"lat":55.75,"lon":37.61},{"lat":55.76,"lon":37.62}]}`.
- `GET /api/v1/weather?lat=55.75&lon=37.61` - current weather and today's forecast.

Place endpoints return the standard Overpass JSON structure; route and weather endpoints preserve the provider response structure to simplify migration of the existing Flutter service.

## Providers

- OpenStreetMap Overpass API (with a fallback endpoint)
- Valhalla public routing server
- Open-Meteo

The API does not store personal data. Saved routes and favorites remain in the app's local storage until account-based synchronization is designed.
