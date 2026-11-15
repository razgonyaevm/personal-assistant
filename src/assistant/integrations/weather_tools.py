from __future__ import annotations

from typing import Any
import httpx

from assistant.tools import ToolRegistry

WMO_WEATHER_CODES: dict[int, str] = {
    0: "Ясно ☀️",
    1: "В основном ясно 🌤️",
    2: "Переменная облачность ⛅",
    3: "Пасмурно ☁️",
    45: "Туман 🌫️",
    48: "Изморозь, оседающий туман 🌫️",
    51: "Лёгкая морось 🌦️",
    53: "Умеренная морось 🌧️",
    55: "Плотная морось 🌧️",
    56: "Ледяная морось 🧊🌧️",
    57: "Сильная ледяная морось 🧊🌧️",
    61: "Небольшой дождь 🌦️",
    63: "Умеренный дождь 🌧️",
    65: "Сильный дождь 🌧️🌧️",
    66: "Ледяной дождь 🧊🌧️",
    67: "Сильный ледяной дождь 🧊🌧️",
    71: "Небольшой снегопад 🌨️",
    73: "Снегопад 🌨️",
    75: "Сильный снегопад ❄️🌨️",
    77: "Снежные зёрна ❄️",
    80: "Кратковременный дождь 🌦️",
    81: "Ливень 🌧️",
    82: "Шквальный ливень ⛈️",
    85: "Снегопад с дождём 🌨️🌧️",
    86: "Сильный снегопад с дождём ❄️🌧️",
    95: "Гроза ⛈️",
    96: "Гроза с небольшим градом ⛈️🧊",
    99: "Гроза с сильным градом ⛈️🧊",
}

DEFAULT_CITY = "Санкт-Петербург"
DEFAULT_LAT = 59.9386
DEFAULT_LON = 30.3141


def _geocode_city(city_name: str) -> tuple[float, float, str, str]:
    normalized = city_name.strip().lower()
    if normalized in ("санкт-петербург", "питер", "спб", "spb", "saint petersburg", "st petersburg"):
        return DEFAULT_LAT, DEFAULT_LON, "Санкт-Петербург", "Россия"

    url = "https://geocoding-api.open-meteo.com/v1/search"
    params = {"name": city_name, "count": 1, "language": "ru", "format": "json"}
    with httpx.Client(timeout=6.0) as client:
        resp = client.get(url, params=params)
        resp.raise_for_status()
        data = resp.json()

    results = data.get("results")
    if not results:
        raise ValueError(f"Город '{city_name}' не найден геокодером.")

    r = results[0]
    return (
        float(r["latitude"]),
        float(r["longitude"]),
        r.get("name", city_name),
        r.get("country", ""),
    )


def get_weather_forecast(
        city: str = DEFAULT_CITY,
        days: int = 1,
) -> dict[str, Any]:
    days = max(1, min(7, int(days)))
    lat, lon, resolved_city, country = _geocode_city(city)

    url = "https://api.open-meteo.com/v1/forecast"
    params = {
        "latitude": lat,
        "longitude": lon,
        "current": [
            "temperature_2m",
            "relative_humidity_2m",
            "apparent_temperature",
            "precipitation",
            "weather_code",
            "wind_speed_10m",
            "wind_direction_10m",
        ],
        "daily": [
            "weather_code",
            "temperature_2m_max",
            "temperature_2m_min",
            "apparent_temperature_max",
            "apparent_temperature_min",
            "precipitation_sum",
            "precipitation_probability_max",
            "wind_speed_10m_max",
        ],
        "timezone": "auto",
        "forecast_days": days,
    }

    with httpx.Client(timeout=7.0) as client:
        resp = client.get(url, params=params)
        resp.raise_for_status()
        data = resp.json()

    current_raw = data.get("current", {})
    wcode = current_raw.get("weather_code", 0)

    current_summary = {
        "temperature_c": current_raw.get("temperature_2m"),
        "apparent_temperature_c": current_raw.get("apparent_temperature"),
        "humidity_pct": current_raw.get("relative_humidity_2m"),
        "precipitation_mm": current_raw.get("precipitation"),
        "wind_speed_kmh": current_raw.get("wind_speed_10m"),
        "weather_description": WMO_WEATHER_CODES.get(wcode, f"Код {wcode}"),
    }

    daily_raw = data.get("daily", {})
    dates = daily_raw.get("time", [])
    forecast_days = []
    for i, d in enumerate(dates):
        code = daily_raw.get("weather_code", [])[i] if i < len(daily_raw.get("weather_code", [])) else 0
        forecast_days.append(
            {
                "date": d,
                "temp_max_c": daily_raw.get("temperature_2m_max", [])[i],
                "temp_min_c": daily_raw.get("temperature_2m_min", [])[i],
                "apparent_max_c": daily_raw.get("apparent_temperature_max", [])[i],
                "apparent_min_c": daily_raw.get("apparent_temperature_min", [])[i],
                "precipitation_sum_mm": daily_raw.get("precipitation_sum", [])[i],
                "precipitation_probability_pct": daily_raw.get("precipitation_probability_max", [])[i],
                "wind_max_kmh": daily_raw.get("wind_speed_10m_max", [])[i],
                "weather_description": WMO_WEATHER_CODES.get(code, f"Код {code}"),
            }
        )

    return {
        "city": resolved_city,
        "country": country,
        "coordinates": {"lat": lat, "lon": lon},
        "timezone": data.get("timezone"),
        "current": current_summary,
        "forecast": forecast_days,
    }


def register_weather_tools(registry: ToolRegistry) -> None:
    registry.register(
        name="get_weather_forecast",
        description=(
            "Возвращает текущую погоду и прогноз на 1–7 дней для указанного города "
            "(по умолчанию — Санкт-Петербург). Содержит температуру, ощущение, осадки, "
            "вероятность дождя/снега и ветер."
        ),
        parameters={
            "type": "object",
            "properties": {
                "city": {
                    "type": "string",
                    "description": "Название города на русском или английском (по умолчанию 'Санкт-Петербург').",
                },
                "days": {
                    "type": "integer",
                    "description": "Количество дней прогноза (от 1 до 7, по умолчанию 1).",
                },
            },
            "required": [],
        },
        handler=get_weather_forecast,
    )
