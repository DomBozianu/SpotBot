import openmeteo_requests
import requests_cache
from retry_requests import retry

# 1. Setup
cache_session = requests_cache.CachedSession('.cache', expire_after = 3600)
retry_session = retry(cache_session, retries = 5)
openmeteo = openmeteo_requests.Client(session = retry_session)

# 2. Parameters (Added 'hourly' back in)
params = {
    "latitude": 50.6,
    "longitude": -2.44,
    "current": ["weather_code", "wind_speed_10m", "wind_direction_10m", "wind_gusts_10m", "visibility", "temperature_2m"],
    "hourly": ["wind_speed_10m", "wind_gusts_10m"], # The forecast
    "wind_speed_unit": "kn",
    "timezone": "auto"
}

responses = openmeteo.weather_api("https://api.open-meteo.com/v1/forecast", params=params)
response = responses[0]

# ---------------------------------------------------------
# 3. THE "NOW" DATA (Current)
# ---------------------------------------------------------
current = response.Current()
# Index 1 = Wind, Index 3 = Gusts (based on the 'current' list above)
now_wind = current.Variables(1).Value()
now_gust = current.Variables(3).Value()

print(f"--- ⚓ CURRENT STATUS ---")
print(f"Wind: {now_wind:.1f} kn | Gusts: {now_gust:.1f} kn")

# ---------------------------------------------------------
# 4. THE "FUTURE" DATA (Hourly)
# ---------------------------------------------------------
hourly = response.Hourly()

# .ValuesAsNumpy() gives us the whole list of 168 hours
# We will just take the first 3 hours using a slice [:3]
forecast_winds = hourly.Variables(0).ValuesAsNumpy()[:9]
forecast_gusts = hourly.Variables(1).ValuesAsNumpy()[:9]

print(f"\n--- 📈 3-HOUR TREND ---")
for i in range(9):
    print(f"Hour {i+1}: {forecast_winds[i]:.1f} kn (Gusting {forecast_gusts[i]:.1f})")