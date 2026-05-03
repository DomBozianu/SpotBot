import json
import os
import requests
import math
from rich.console import Console
from rich.prompt import Prompt, IntPrompt
from pathlib import Path
from dotenv import load_dotenv

load_dotenv()
console = Console()

def calculate_bearing(lat1, lon1, lat2, lon2):
    d_lon = math.radians(lon2 - lon1)
    y = math.sin(d_lon) * math.cos(math.radians(lat2))
    x = math.cos(math.radians(lat1)) * math.sin(math.radians(lat2)) - \
        math.sin(math.radians(lat1)) * math.cos(math.radians(lat2)) * math.cos(d_lon)
    return round((math.degrees(math.atan2(y, x)) + 360) % 360)

def run():
    console.print("[bold blue]🌊 SpotBot: Manual Tide Mode[/bold blue]\n")

    # 1. LOCATION SEARCH
    search_term = Prompt.ask("Search for a spot (e.g. 'West Wittering')")
    geo_url = f"https://nominatim.openstreetmap.org/search?q={search_term}&format=json&limit=1"
    geo_res = requests.get(geo_url, headers={'User-Agent': 'SpotBot'}).json()
    
    if not geo_res:
        console.print("[red]No locations found.[/red]")
        return

    sel = geo_res[0]
    lat, lon = float(sel['lat']), float(sel['lon'])
    console.print(f"[green]Selected: {sel['display_name']}[/green]")

    # 2. TIDE ID (Manual Input)
    console.print("\n[yellow]TIDE ID:[/yellow] Consult your ID list or Admiralty Map.")
    tide_id = Prompt.ask("Enter Admiralty Tide ID (e.g. 0073 for Shoreham)", default="0000")

    # 3. BEARING ASSISTANT
    console.print(f"\n[bold cyan]📍 Verify on Map:[/bold cyan] https://www.google.com/maps/search/?api=1&query={lat},{lon}")
    water_coords = Prompt.ask("Enter Water Coordinates (lat, lon) to calculate bearing")
    
    bearing = 180
    if water_coords and "," in water_coords:
        w_lat, w_lon = map(float, water_coords.split(","))
        bearing = calculate_bearing(lat, lon, w_lat, w_lon)
        console.print(f"✅ Calculated Bearing: [bold green]{bearing}°[/bold green]")
    
    final_bearing = IntPrompt.ask("Confirm Shoreline Bearing", default=bearing)

    # 4. SAVE
    display_name = Prompt.ask("Final Name for App", default=search_term)
    spot_key = display_name.lower().replace(" ", "_").replace("-", "_")

    json_path = Path("spotbot_knowledge/spots.json")
    with open(json_path, "r") as f:
        spots_data = json.load(f)

    spots_data[spot_key] = {
        "name": display_name, "lat": lat, "lon": lon,
        "tide_id": tide_id, "knowledge_file": f"{spot_key}.txt",
        "shoreline_bearing": final_bearing
    }

    with open(json_path, "w") as f:
        json.dump(spots_data, f, indent=4)

    # Create knowledge file
    txt_path = Path(f"spotbot_knowledge/spots/{spot_key}.txt")
    txt_path.parent.mkdir(parents=True, exist_ok=True)
    if not txt_path.exists():
        txt_path.write_text(f"Knowledge for {display_name}...")

    console.print(f"\n[bold green]SUCCESS![/bold green] {display_name} added with Tide {tide_id}.")

if __name__ == "__main__":
    run()