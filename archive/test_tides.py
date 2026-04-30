import requests
import os
from dotenv import load_dotenv
import arrow

load_dotenv()
API_KEY = os.getenv("ADMIRALTY_KEY")

# 🎯 THE CORRECT ID WE JUST FOUND
STATION_ID = "0033" 
url = f"https://admiraltyapi.azure-api.net/uktidalapi/api/V1/Stations/{STATION_ID}/TidalEvents"

headers = {
    "Ocp-Apim-Subscription-Key": API_KEY.strip()
}

response = requests.get(url, headers=headers)

if response.status_code == 200:
    tides = response.json()
    print(f"--- 🌊 PORTLAND TIDES (ID: {STATION_ID}) ---")
    
    # Just show the next 4 events
    for event in tides[:4]:
        # Convert UTC to local UK time
        event_time = arrow.get(event['DateTime']).to('Europe/London')
        event_type = event['EventType']
        event_height = event['Height']
        
        # Friendly icons for high and low
        icon = "⬆️" if event_type == "HighWater" else "⬇️"
        
        # .format('ddd D MMM') gives you: "Mon 1 May"
        # .format('HH:mm') gives you: "14:30"
        date_str = event_time.format('ddd D MMM')
        time_str = event_time.format('HH:mm')
        
        print(f"{icon} {date_str} | {event_type:9}: {time_str} ({event_height:.1f}m)")