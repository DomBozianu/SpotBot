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
code = current.Variables(0).Value() # Index 0 = weather_code
now_wind = current.Variables(1).Value()
now_gust = current.Variables(3).Value()

# The Marine API is a different URL
marine_url = "https://marine-api.open-meteo.com/v1/marine"

marine_params = {
	"latitude": 50.6,
	"longitude": -2.44,
	"current": ["wave_height", "wave_period", "wave_direction"],
	"timezone": "auto",
}

# We make the call just like the weather one
marine_responses = openmeteo.weather_api(marine_url, params=marine_params)
marine_res = marine_responses[0]
marine_current = marine_res.Current()

wave_height = marine_current.Variables(0).Value()
wave_period = marine_current.Variables(1).Value()

# A simple "Salty" translation table
wmo_codes = {
    0: "Clear skies—get the sunglasses out! 😎",
    1: "Mainly clear.",
    2: "Partly cloudy.",
    3: "Overcast and moody. ☁️",
    45: "Foggy. Keep an eye on the shore! 🌫️",
    61: "Slight rain. You're getting wet anyway, right? 🌧️",
    85: "Snow showers? Are you sure you want to go out? ❄️",
}

# How to use it:
# We use .get() so if the code isn't in our list, it doesn't crash.
weather_description = wmo_codes.get(code, "Unknown conditions—use your eyes!")

def get_vibe(knots):
    if knots < 10: return "🧘 Too light. Go for a paddle."
    if 10 <= knots < 15: return "🪁 Foiling or big kit only."
    if 15 <= knots < 22: return "🏄 Classic freeride conditions. Get the 5.5m - 6.5m out."
    if 22 <= knots < 30: return "🤙 Proper wind. Small sails only."
    return "🧨 Survival mode. Hold on tight!"

# Let's use it!
vibe = get_vibe(now_wind)

print(f"--- ⚓ CURRENT STATUS ---")
print(f"Wind: {now_wind:.1f} kn | Gusts: {now_gust:.1f} kn")
print(f"🧭 Wind Direction: {current.Variables(2).Value():.0f}°")
print(f"🌊 Sea State: {wave_height}m waves every {wave_period} seconds")
print(f"📡 Condition: {weather_description}")
print(f"The Vibe: {vibe}")


# ---------------------------------------------------------
# 4. THE "FUTURE" DATA (Hourly)
# ---------------------------------------------------------
hourly = response.Hourly()

# .ValuesAsNumpy() gives us the whole list of 168 hours
# We will just take the first 3 hours using a slice [:3]
forecast_winds = hourly.Variables(0).ValuesAsNumpy()[:6]
forecast_gusts = hourly.Variables(1).ValuesAsNumpy()[:6]

print(f"\n--- 📈 6-HOUR TREND ---")
for i in range(6):
    print(f"Hour {i+1}: {forecast_winds[i]:.1f} kn (Gusting {forecast_gusts[i]:.1f})")




