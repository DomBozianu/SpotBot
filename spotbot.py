import os
import arrow
import openmeteo_requests
import requests_cache
import requests
from retry_requests import retry
from dotenv import load_dotenv

load_dotenv()

# 1. SETUP OPEN-METEO (The high-performance way)
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

# 4. DATA FETCHING FUNCTION
def fetch_spot_data(lat, lon):
    # Fetch Weather
    weather_params = {
        "latitude": lat, "longitude": lon,
        "current": ["weather_code", "wind_speed_10m", "wind_direction_10m", "wind_gusts_10m"],
        "hourly": ["wind_speed_10m", "wind_gusts_10m"],
        "wind_speed_unit": "kn", "timezone": "auto"
    }
    weather_res = openmeteo.weather_api("https://api.open-meteo.com/v1/forecast", params=weather_params)[0]
    
    # Fetch Marine
    marine_params = {
        "latitude": lat, "longitude": lon,
        "current": ["wave_height", "wave_period"],
        "timezone": "auto",
    }
    marine_res = openmeteo.weather_api("https://marine-api.open-meteo.com/v1/marine", params=marine_params)[0]
    
    return weather_res, marine_res

def fetch_tide_data(station_id):
    """Fetches high and low tide events from Admiralty API."""
    api_key = os.getenv("ADMIRALTY_KEY")
    url = f"https://admiraltyapi.azure-api.net/uktidalapi/api/V1/Stations/{station_id}/TidalEvents"
    
    headers = {"Ocp-Apim-Subscription-Key": api_key}
    response = requests.get(url, headers=headers)
    
    if response.status_code == 200:
        # We only want the next 4 events (roughly 24 hours)
        return response.json()[:4]
    return []

# 5. THE MASTER REPORT
def run_shred_report(spot_key):
    # .get() returns None if the key doesn't exist
    spot = SPOTS.get(spot_key)
    
    if not spot:
        print(f"\n❌ ERROR: I couldn't find '{spot_key}' in my database.")
        print(f"   Available spots: {', '.join(SPOTS.keys())}")
        return

    # Get Data
    weather, marine = fetch_spot_data(spot['lat'], spot['lon'])
    tides = fetch_tide_data(spot['tide_id']) # New Tide Call
    
    # Process Weather
    curr = weather.Current()
    wind = curr.Variables(1).Value()
    gust = curr.Variables(3).Value()
    desc = get_weather_desc(curr.Variables(0).Value())
    
    # Process Marine
    m_curr = marine.Current()
    wave_h = m_curr.Variables(0).Value()
    
    print(f"--- ⚓ {spot['name'].upper()} SHRED REPORT ---")
    print(f"📡 Status: {desc}")
    print(f"💨 Wind: {wind:.1f} kn (Gusts: {gust:.1f} kn)")
    print(f"🌊 Waves: {wave_h:.1f}m")
    print(f"🤔 Vibe: {get_vibe(wind)}")
    
    # --- NEW: TREND SECTION ---
    print(f"\n📈 6-HOUR TREND:")
    hourly = weather.Hourly()
    f_winds = hourly.Variables(0).ValuesAsNumpy()[:6]
    f_gusts = hourly.Variables(1).ValuesAsNumpy()[:6]
    
    for i in range(6):
        # This shows Hour +1, +2, etc.
        print(f"  +{i+1}h: {f_winds[i]:.1f} kn (Gust: {f_gusts[i]:.1f})")
    
    print(f"\n⏳ UPCOMING TIDES:")
    if tides:
        for event in tides:
            # Parse the time using Arrow
            event_time = arrow.get(event['DateTime']).to('Europe/London')
            
            # Format: 'Thu, Apr 30'
            date_str = event_time.format('ddd, MMM D')
            # Format: '14:20'
            time_str = event_time.format('HH:mm')
            
            event_type = "HIGH" if "High" in event['EventType'] else "LOW "
            height = event['Height']
            
            # We print the date and time together
            print(f"  {date_str} @ {time_str} | {event_type} | {height:.1f}m")
    else:
        print("  ⚠️ Tide data unavailable.")

    # Knowledge Base
    print(f"\n📍 LOCAL KNOWLEDGE:")
    with open(f"spotbot_knowledge/{spot['knowledge_file']}", 'r') as f:
        print(f.read()[:300])

# Run it!
run_shred_report("portland_harbour")