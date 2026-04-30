from fastapi import FastAPI, HTTPException, Request  # Added Request here
from fastapi.templating import Jinja2Templates
from fastapi.responses import HTMLResponse
from typing import Optional
import os
import arrow
import openmeteo_requests
import requests_cache
import requests
from retry_requests import retry
from dotenv import load_dotenv

load_dotenv()
app = FastAPI()
templates = Jinja2Templates(directory="templates")

# 1. SETUP OPEN-METEO
cache_session = requests_cache.CachedSession('.cache', expire_after = 3600)
retry_session = retry(cache_session, retries = 5)
openmeteo = openmeteo_requests.Client(session = retry_session)

# 2. THE SPOT DATABASE
SPOTS = {
    "portland_harbour": {
        "name": "Portland Harbour",
        "lat": 50.58,
        "lon": -2.45,
        "tide_id": "0033",
        "knowledge_file": "portland_harbour.txt"
    }
}

# 3. HELPER FUNCTIONS
def get_compass_dir(degrees):
    # Converts degrees (0-360) to a compass direction
    val = int((degrees / 22.5) + 0.5)
    arr = ["N", "NNE", "NE", "ENE", "E", "ESE", "SE", "SSE", "S", "SSW", "SW", "WSW", "W", "WNW", "NW", "NNW"]
    return arr[(val % 16)]

def get_vibe(knots):
    if knots < 10: return "🧘 Too light. Go for a paddle."
    if 10 <= knots < 15: return "🪁 Foiling or big kit only."
    if 15 <= knots < 22: return "🏄 Classic freeride. 5.5m - 6.5m weather."
    if 22 <= knots < 30: return "🤙 Proper wind. Small sails only."
    return "🧨 Survival mode. Hold on tight!"

def get_weather_desc(code):
    wmo_codes = {
        0: "Clear skies—get the sunglasses out! 😎",
        1: "Mainly clear.", 2: "Partly cloudy.", 3: "Overcast. ☁️",
        45: "Foggy. 🌫️", 61: "Slight rain. 🌧️",
    }
    return wmo_codes.get(code, "Unknown conditions—use your eyes!")

# 4. DATA FETCHING FUNCTIONS (Keep these as they were)
def fetch_spot_data(lat, lon):
    weather_params = {
        "latitude": lat, "longitude": lon,
        "current": ["weather_code", "wind_speed_10m", "wind_direction_10m", "wind_gusts_10m"],
        "hourly": ["wind_speed_10m", "wind_gusts_10m"],
        "wind_speed_unit": "kn", "timezone": "auto"
    }
    weather_res = openmeteo.weather_api("https://api.open-meteo.com/v1/forecast", params=weather_params)[0]
    marine_params = {
        "latitude": lat, "longitude": lon,
        "current": ["wave_height", "wave_period"],
        "timezone": "auto",
    }
    marine_res = openmeteo.weather_api("https://marine-api.open-meteo.com/v1/marine", params=marine_params)[0]
    return weather_res, marine_res

def fetch_tide_data(station_id):
    api_key = os.getenv("ADMIRALTY_KEY")
    url = f"https://admiraltyapi.azure-api.net/uktidalapi/api/V1/Stations/{station_id}/TidalEvents"
    headers = {"Ocp-Apim-Subscription-Key": api_key}
    try:
        response = requests.get(url, headers=headers, timeout=5) # Added timeout
        if response.status_code == 200:
            return response.json()[:4]
    except:
        pass
    return []

# 5. THE MASTER LOGIC (Consolidated)
async def get_shred_report(spot_key: str):
    spot = SPOTS.get(spot_key)
    if not spot:
        return None

    # 1. Fetch Data
    weather, marine = fetch_spot_data(spot['lat'], spot['lon'])
    tides = fetch_tide_data(spot['tide_id'])
    
    # 2. Process Current Weather
    curr = weather.Current()
    # Use this safer way to map variables
    wind = curr.Variables(1).Value()
    # If the hang started here, it's likely Index 2 (Direction) causing it
    try:
        wind_dir_degrees = curr.Variables(2).Value()
        wind_dir_name = get_compass_dir(wind_dir_degrees)
    except:
        wind_dir_name = "N/A" # Fallback if direction fetch fails
        
    gust = curr.Variables(3).Value()
    desc = get_weather_desc(curr.Variables(0).Value())
    
    m_curr = marine.Current()
    wave_h = m_curr.Variables(0).Value()
    
    # 3. NEW: Process 6-Hour Trend (The part that was missing!)
    hourly = weather.Hourly()
    f_winds = hourly.Variables(0).ValuesAsNumpy()[:6]
    f_gusts = hourly.Variables(1).ValuesAsNumpy()[:6]
    trend = [
        {
            "hour": f"+{i+1}h", 
            "wind": round(float(f_winds[i]), 1), 
            "gust": round(float(f_gusts[i]), 1)
        } 
        for i in range(6)
    ]
    
    # 4. Process Tides
    tide_list = []
    for event in tides:
        event_time = arrow.get(event['DateTime']).to('Europe/London')
        tide_list.append({
            "time": event_time.format('HH:mm'),
            "type": "HIGH" if "High" in event['EventType'] else "LOW",
            "height": round(event['Height'], 1)
        })

    # 5. Local Knowledge
    wisdom = "No local knowledge found."
    path = f"spotbot_knowledge/{spot['knowledge_file']}"
    if os.path.exists(path):
        with open(path, 'r') as f:
            wisdom = f.read()[:500]

    # 6. Return everything
    return {
        "metadata": {"spot_name": spot['name'], "status": desc},
        "live": {
            "wind_knots": round(wind, 1),
            "wind_dir": wind_dir_name,
            "gusts_knots": round(gust, 1),
            "waves_m": round(wave_h, 1),
            "vibe": get_vibe(wind)
        },
        "forecast_6h": trend,  # Now 'trend' is defined!
        "tides": tide_list,
        "local_knowledge": wisdom
    }

# 6. ROUTES
@app.get("/", response_class=HTMLResponse)
async def home(request: Request):
    spot_key = request.query_params.get("spot")
    report_data = None
    if spot_key:
        report_data = await get_shred_report(spot_key)
    
    return templates.TemplateResponse(
    request=request, 
    name="index.html", 
    context={"report": report_data}
    )
# Keep this for your API/Testing
@app.get("/api/report")
async def api_report(spot: str):
    data = await get_shred_report(spot)
    if not data:
        raise HTTPException(status_code=404, detail="Spot not found")
    return data