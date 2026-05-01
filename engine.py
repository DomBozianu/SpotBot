import os
import arrow
import requests
import requests_cache
import openmeteo_requests
from retry_requests import retry
from openai import OpenAI, AsyncOpenAI  # Once you add the AI
from dotenv import load_dotenv
from pathlib import Path
import json
from datetime import datetime, timedelta

# This finds the folder where engine.py lives
env_path = Path(__file__).parent / ".env"

# This forces it to load THAT specific file
load_dotenv(dotenv_path=env_path)
# Initialize the client HERE so all functions can see it
# This is fast because it doesn't actually "connect" until the first request
client = AsyncOpenAI(
    base_url="https://openrouter.ai/api/v1",
    api_key=os.getenv("OPENROUTER_KEY"),
    timeout=30.0, # Increase timeout to 20 seconds to prevent 504s
    max_retries=2
)
# 1. SETUP OPEN-METEO
cache_session = requests_cache.CachedSession('.cache', expire_after = 3600)
retry_session = retry(cache_session, retries = 5)
openmeteo = openmeteo_requests.Client(session = retry_session)

def load_spots():
    spots_path = Path(__file__).parent / "spots.json"
    if spots_path.exists():
        with open(spots_path, "r") as f:
            return json.load(f)
    return {}

SPOTS = load_spots()

def get_relative_wind(wind_deg, shoreline_deg):
    """
    Calculates if wind is Onshore, Offshore, or Cross-shore.
    shoreline_deg: The direction the beach FACES.
    """
    if shoreline_deg is None: return "Unknown"
    
    # Calculate the difference
    diff = abs(wind_deg - shoreline_deg) % 360
    
    if diff > 180:
        diff = 360 - diff
        
    if diff < 30: return "🚫 Onshore"      # Blowing straight onto the beach
    if diff > 150: return "🚩 Offshore"    # Blowing away from the beach
    if 60 < diff < 120: return "💎 Cross-shore"
    return "📈 Cross-on/off"

def get_compass_info(degrees):
    # Mapping degrees to Full Words and Unicode Arrows
    # 0° is North (Wind blowing FROM the North, arrow points DOWN)
    val = int((degrees / 22.5) + 0.5)
    
    directions = [
        {"word": "North", "arrow": "⬇️"}, {"word": "N-East", "arrow": "↙️"},
        {"word": "East", "arrow": "⬅️"}, {"word": "S-East", "arrow": "↖️"},
        {"word": "South", "arrow": "⬆️"}, {"word": "S-West", "arrow": "↗️"},
        {"word": "West", "arrow": "➡️"}, {"word": "N-West", "arrow": "↘️"}
    ]
    # We use % 8 because we simplified the list to the main 8 points
    index = (val // 2) % 8 
    return directions[index]

def get_vibe(knots):
    if knots < 10: return "🧘 Too light. Go for a paddle."
    if 10 <= knots < 15: return "🪁 Foiling or big kit only."
    if 15 <= knots < 22: return "🏄 Classic freeride. 5.5m - 6.5m weather."
    if 22 <= knots < 30: return "🤙 Proper wind. Small sails only."
    return "🧨 Survival mode. Hold on tight!"

def get_weather_desc(code):
    wmo_codes = {
        0: "Clear skies—get the sunglasses out! 😎",
        1: "Mainly clear.", 
        2: "Partly cloudy.", 
        3: "Overcast. ☁️",
        45: "Foggy. 🌫️", 
        48: "Depositing rime fog.",
        51: "Light drizzle. 💧",
        61: "Slight rain. 🌧️",
        63: "Moderate rain. 🌧️",
        71: "Slight snow fall.",
        80: "Slight rain showers. 🌦️",
        81: "Moderate rain showers.",
        95: "Thunderstorms. ⚡"
    }
    return wmo_codes.get(code, f"Code {code}—Use your eyes! 👀")

# 4. DATA FETCHING FUNCTIONS (Keep these as they were)
def fetch_spot_data(lat, lon):
    weather_params = {
        "latitude": lat, "longitude": lon,
        "current": ["weather_code", "wind_speed_10m", "wind_direction_10m", "wind_gusts_10m"],
        "hourly": ["wind_speed_10m", "wind_gusts_10m"],
        "daily": ["sunrise", "sunset"],
        "wind_speed_unit": "kn", 
        "timezone": "auto"
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
    if not api_key:
        return []
        
    url = f"https://admiraltyapi.azure-api.net/uktidalapi/api/V1/Stations/{station_id}/TidalEvents"
    headers = {"Ocp-Apim-Subscription-Key": api_key}
    
    try:
        # We use a 3-second timeout. If it's slow, we skip it so the app doesn't hang.
        response = requests.get(url, headers=headers, timeout=3) 
        if response.status_code == 200:
            return response.json()[:4]
    except Exception as e:
        print(f"Tide API Timeout or Error: {e}")
    return []

async def get_ai_recommendation(report, user_weight):
    try:
        # 1. Load Knowledge
        from pathlib import Path
        gear_path = Path(__file__).parent / "spotbot_knowledge" / "gear" / "windsurf_chart.txt"
        gear_kb = gear_path.read_text(encoding='utf-8') if gear_path.exists() else "Use standard physics."

        # 2. Safe Extraction
        live = report.get('live', {})
        tides_raw = report.get('tides', [])
        
        if isinstance(tides_raw, list) and len(tides_raw) > 0:
            tide_val = str(tides_raw[0]) 
        else:
            tide_val = "Tide info unavailable"
        
        # Define all variables used in the prompt
        wind_val = live.get('wind_knots', 'Unknown')
        gust_val = live.get('gusts_knots', 'Unknown')
        wave_val = live.get('wave_height', 'Flat')
        wave_p   = live.get('wave_period', 'N/A') # ADDED THIS
        spot_val = report.get('metadata', {}).get('spot_name', 'this spot')
        wisdom   = report.get('local_knowledge', 'No local tips.')

        # 3. Build the Prompt
        prompt = f"""
    You are the 'Local Legend Master' (LLM) the local windsurfing expert. You use physics and local grit to give gear advice.
    
    GEAR KNOWLEDGE BASE:
    {gear_kb}
    
    FULL FORECAST DATA:
    - Spot: {spot_val}
    - Wind: {wind_val} kts (Gusting {gust_val} kts)
    - Waves: {wave_val}m at {wave_p}s
    - Tide: {tide_val}
    - Local Wisdom: {wisdom}
    
    USER PROFILE:
    - Weight: {user_weight}kg
    
    DECISION LOGIC:
    1. Identify base sail size from Matrix for {user_weight}kg.
    2. Assess 'Survival Factor': If gusts > 30kts or waves > 1.5m, suggest sizing down board volume (Weight + 0L to +10L).
    3. Assessment for Speed: If flat water and high wind, mention a 'Speed Needle' (Weight - 20L) for experts.
    4. CRITICAL: A board volume (L) is NEVER the same as a sail size (m2). If you suggest a 7.5L board, you are wrong. 
    
    TASK:
    Start your response with "SENDINESS: [X]/10".
    Then give your recommendation in 2 punchy, salty sentences. Mention specific sail m² and board Liters (L).
    """

        # 4. THE ACTUAL AI CALL
        try:
            response = await client.chat.completions.create(
                model="meta-llama/llama-3.1-8b-instruct", 
                messages=[{"role": "system", "content": prompt}]
            )
            
            # Check if we actually got a valid response back
            if response and response.choices:
                return response.choices[0].message.content
            else:
                print("DEBUG: AI returned an empty response object")
                return "The Legend is staring at the horizon... (Empty AI response)"

        except Exception as api_e:
            print(f"AI API Error: {api_e}")
            return "The Legend is lost in the fog. API issue."

    except Exception as e: # <--- THIS IS PROBABLY WHAT IS MISSING OR MISALIGNED
        print(f"General Error: {e}")
        return "The Legend had a breakdown."

# 5. THE MASTER LOGIC (Consolidated)
async def get_shred_report(spot_key: str, user_weight:str = "75"):
    spot = SPOTS.get(spot_key)
    if not spot:
        return None

    # 1. Fetch Data
    weather, marine = fetch_spot_data(spot['lat'], spot['lon'])
    tides = fetch_tide_data(spot['tide_id'])
    
    # 2. Weather Processing
    curr = weather.Current()
    wind_spd = curr.Variables(1).Value()
    wind_deg = curr.Variables(2).Value()
    gust_spd = curr.Variables(3).Value()

    #sunrisesunset stuff
    daily = weather.Daily()
    
    def get_sun_time(index):
        try:
            # IMPORTANT: Sunrise/Sunset use ValuesInt64AsNumpy, not ValuesAsNumpy
            ts_arr = daily.Variables(index).ValuesInt64AsNumpy()
            ts = ts_arr[0] if hasattr(ts_arr, "__len__") else ts_arr
            
            if ts == 0: return "--:--"
            
            # Convert Unix timestamp to HH:MM
            return datetime.fromtimestamp(float(ts)).strftime("%H:%M")
        except Exception as e:
            print(f"Sun Error: {e}")
            return "--:--"

    sunrise = get_sun_time(0) # Index 0 is sunrise
    sunset = get_sun_time(1)  # Index 1 is sunset
    today_date = datetime.now().strftime("%a, %b %d")

    # NEW: Relative Wind Logic
    shore_deg = spot.get('shoreline_bearing')
    rel_wind = get_relative_wind(wind_deg, shore_deg)
    dir_info = get_compass_info(wind_deg)
    
    # 3. Marine Processing (Waves + Period)
    m_curr = marine.Current()
    wave_h = m_curr.Variables(0).Value()
    wave_p = m_curr.Variables(1).Value() # Extracting Period
    
    # 4. Hourly Trends (Slicing 12 hours)
    hourly = weather.Hourly()
    f_winds = hourly.Variables(0).ValuesAsNumpy()[:12]
    f_gusts = hourly.Variables(1).ValuesAsNumpy()[:12]
    start_time = datetime.fromtimestamp(hourly.Time(), tz=None)
    
    trend = []
    for i in range(12):
        # Calculate the actual time for each hour slot
        current_hour_dt = start_time + timedelta(hours=i)
        
        trend.append({
            "hour": current_hour_dt.strftime("%H:%M"), # Results in "14:00", "15:00", etc.
            "wind": round(float(f_winds[i]), 1), 
            "gust": round(float(f_gusts[i]), 1)
        })

    # Trend logic for the "Trend" Tile
    current_wind = float(wind_spd)
    future_wind = float(f_winds[2]) # Looking 3 hours ahead for a better "trend" feel
    
    if future_wind > current_wind + 2:
        trend_icon = "📈 Building"
    elif future_wind < current_wind - 2:
        trend_icon = "📉 Dropping"
    else:
        trend_icon = "➡️ Steady"

    # 4. Process Tides
    tide_list = []
    tide_state = "Tide info unavailable"  # <--- ADD THIS DEFAULT LINE HERE
    
    if tides:  # Only run this if we actually got data from the API
        for event in tides:
            event_time = arrow.get(event['DateTime']).to('Europe/London')
            tide_list.append({
                "date": event_time.format('MMM DD'), # Added this (e.g., 'Apr 30')
                "day": event_time.format('ddd'),     # Added this (e.g., 'Thu')
                "time": event_time.format('HH:mm'),
                "type": "HIGH" if "High" in event['EventType'] else "LOW",
                "height": round(event['Height'], 1)
            })
        
        # Now define the state based on the first upcoming event
        if tide_list:
            next_event = tide_list[0]
            tide_state = f"Heading to {next_event['type']}"

    # 8. Local Wisdom
    wisdom = "No local knowledge found."
    path = Path(__file__).parent / "spotbot_knowledge" / "spots" / spot['knowledge_file']
    if path.exists():
        wisdom = path.read_text(encoding='utf-8')[:500]
    else:
        print(f"DEBUG: Wisdom file not found at {path.absolute()}")

    # 6. Return everything
    report_data = {
        "metadata": {
            "spot_name": spot['name'], 
            "status": get_weather_desc(curr.Variables(0).Value()),
            "date": today_date,
            "sunrise": sunrise,
            "sunset": sunset,
            "tide_summary": tide_state,
            "tide_list": tide_list
        },
        "live": {
            "wind_knots": round(wind_spd, 1),
            "wind_dir": dir_info['word'],
            "wind_arrow": dir_info['arrow'],
            "wind_relative": rel_wind,
            "wind_trend": trend_icon,
            "gusts_knots": round(gust_spd, 1),
            "waves_m": round(wave_h, 1),
            "wave_period": round(wave_p, 1),
            "vibe": "The legend is checking the horizon..." # Temporary
        },
        "forecast_12h": trend,
        "tides": tide_list,
        "local_knowledge": wisdom
    }

    # 🔥 CALL THE AI BRAIN
    #report_data['live']['vibe'] = await get_ai_recommendation(report_data, user_weight)

    return report_data
