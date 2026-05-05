import os
import json
import re
import math
import asyncio
import requests
from pathlib import Path
from difflib import get_close_matches
from dotenv import load_dotenv
from rich.console import Console
from rich.table import Table
from rich.prompt import Prompt, Confirm
from openai import AsyncOpenAI
from tavily import TavilyClient

load_dotenv()
console = Console()

# --- PATH CONFIGS ---
LIVE_SPOTS_PATH = Path("spotbot_knowledge/spots.json")
SCOUT_TARGETS_PATH = Path("spotbot_knowledge/scout_targets.json")

# --- UTILITY FUNCTIONS ---

def get_coordinates(spot_name):
    """Finds lat/lon by intelligently broadening the search if initial attempts fail."""
    import time
    headers = {'User-Agent': 'SpotBot-Scout-Bot-v2'}
    
    # Cascade: 
    # 1. Exactly what the Agent found (e.g., "Gott Bay, Tiree")
    # 2. Just the beach name + " beach" (e.g., "Gott Bay beach")
    # 3. Just the beach name (e.g., "Gott Bay")
    
    primary_name = spot_name.split(',')[0].strip()
    
    search_attempts = [
        spot_name,
        f"{primary_name} beach",
        primary_name
    ]
    
    for attempt in search_attempts:
        url = f"https://nominatim.openstreetmap.org/search?q={attempt}&format=json&limit=3"
        try:
            # Respect Nominatim's usage policy (1 request per second)
            time.sleep(1) 
            res = requests.get(url, headers=headers)
            data = res.json()
            
            if data:
                table = Table(title=f"Location Results for: {attempt}")
                table.add_column("ID")
                table.add_column("Name")
                for i, r in enumerate(data):
                    table.add_row(str(i+1), r['display_name'][:70])
                console.print(table)
                
                idx = int(Prompt.ask("Select Location", choices=[str(i+1) for i in range(len(data))])) - 1
                return float(data[idx]['lat']), float(data[idx]['lon']), data[idx]['display_name']
        except Exception:
            continue
            
    return None

def calculate_shoreline_bearing(lat, lon):
    overpass_url = "https://overpass-api.de/api/interpreter"
    query = f'[out:json];way["natural"~"coastline|beach|shoreline"](around:5000,{lat},{lon});out geom;'
    try:
        res = requests.get(overpass_url, params={'data': query}, timeout=15)
        elements = res.json().get('elements', [])
        if not elements: return 180
            
        nodes = elements[0]['geometry']
        p1, p2 = nodes[0], nodes[-1]
        
        # Calculate angle of the coastline segment
        # Using (lon, lat) to get a bearing relative to North
        angle = math.degrees(math.atan2(p2['lon'] - p1['lon'], p2['lat'] - p1['lat']))
        
        # Add 90 degrees to point offshore
        bearing = round((angle + 90) % 360)
        return bearing
    except:
        return 180


def fuzzy_check_scout(user_input):
    """Checks wishlist for matches."""
    if not SCOUT_TARGETS_PATH.exists(): return user_input
    scouts = json.loads(SCOUT_TARGETS_PATH.read_text(encoding='utf-8'))
    matches = get_close_matches(user_input, scouts, n=1, cutoff=0.6)
    if matches and matches[0].lower() != user_input.lower():
        if Confirm.ask(f"Did you mean [bold cyan]'{matches[0]}'[/bold cyan] from your wishlist?"):
            return matches[0]
    return user_input

# --- AGENTIC DISCOVERY ---

async def discover_specific_spot(area_name):
    """Forcefully extracts clean beach names with no conversational fluff."""
    tavily = TavilyClient(api_key=os.getenv("TAVILY_API_KEY"))
    search = tavily.search(query=f"best windsurfing launch spots beaches in {area_name} Scotland", max_results=5)
    
    client = AsyncOpenAI(base_url="https://openrouter.ai/api/v1", api_key=os.getenv("OPENROUTER_API_KEY"))
    
    # We use a system message here to tell the AI to stop talking and start acting like a CSV
    prompt = f"""
    You are a data extraction tool. Based on these search results: {search['results']}
    
    TASK: Identify the 3 most iconic windsurfing beaches in {area_name}.
    FORMAT: List only the names separated by commas. 
    NO numbers, NO introductory text, NO 'Here are the spots', NO bolding.
    
    Example: Gott Bay, Balevullin, The Maze
    """
    
    res = await client.chat.completions.create(
        model="meta-llama/llama-3.1-8b-instruct",
        messages=[
            {"role": "system", "content": "You only output raw, comma-separated strings. No prose."},
            {"role": "user", "content": prompt}
        ],
        temperature=0.0
    )
    
    # Regex to clean up any accidental numbering or 'Tiree' repeats the AI might add
    raw_content = res.choices[0].message.content.strip()
    # Remove things like "1. ", "2. ", and extra newlines
    clean_content = re.sub(r'\d+\.\s*', '', raw_content).replace('\n', ',')
    
    # Split, clean whitespace, and ensure Area Context is attached for the geocoder
    spots = [s.strip() for s in clean_content.split(",") if s.strip()]
    formatted_spots = []
    for s in spots:
        if area_name.lower() not in s.lower():
            formatted_spots.append(f"{s}, {area_name}")
        else:
            formatted_spots.append(s)
            
    # Final safety slice to 3 spots
    formatted_spots = list(dict.fromkeys(formatted_spots))[:3]
    
    console.print(f"\n[bold cyan]Launch Sites found in {area_name}:[/bold cyan]")
    for i, s in enumerate(formatted_spots):
        console.print(f" {i+1}. {s}")
    
    choice = Prompt.ask("Which spot shall we scout?", choices=[str(i+1) for i in range(len(formatted_spots))])
    return formatted_spots[int(choice)-1]

async def generate_knowledge_file(spot_name):
    """Research and write the guide."""
    tavily = TavilyClient(api_key=os.getenv("TAVILY_API_KEY"))
    client = AsyncOpenAI(base_url="https://openrouter.ai/api/v1", api_key=os.getenv("OPENROUTER_API_KEY"))
    
    # Hunter search for Tide ID and Hazards
    search = tavily.search(query=f"windsurfing spot guide hazards facilities and Admiralty tide station ID for {spot_name}", search_depth="advanced")
    context = "\n".join([f"Source: {r['content']}" for r in search['results']])

    prompt = f"Using this research: {context}\nWrite a windsurf guide for {spot_name}. Include 'Suggested Tide ID: [4-digits]' based on the text or nearest port."
    
    res = await client.chat.completions.create(
        model="meta-llama/llama-3.1-8b-instruct",
        messages=[{"role": "user", "content": prompt}],
        temperature=0.1
    )
    return res.choices[0].message.content

def update_scout_list(completed_name):
    """Removes a spot from the scout list once it is live."""
    if not SCOUT_TARGETS_PATH.exists(): return
    
    try:
        scouts = json.loads(SCOUT_TARGETS_PATH.read_text(encoding='utf-8'))
        # Filter out the one we just finished (case-insensitive)
        updated = [s for s in scouts if s.lower() != completed_name.lower()]
        
        if len(updated) != len(scouts):
            SCOUT_TARGETS_PATH.write_text(json.dumps(updated, indent=4), encoding='utf-8')
            console.print(f"[yellow]Done! Removed {completed_name} from your wishlist.[/yellow]")
    except Exception as e:
        console.print(f"[red]Could not update scout list: {e}[/red]")

# --- MAIN ---

async def main():
    try:
        console.print("[bold cyan]SpotBot: Add Spot Agent[/bold cyan]")
        
        user_input = Prompt.ask("What is the name of the spot?")
        verified_name = fuzzy_check_scout(user_input)
        
        target_name = verified_name
        if Confirm.ask(f"Is '{verified_name}' a general area? (e.g. Tiree vs Gott Bay)"):
            target_name = await discover_specific_spot(verified_name)

        # --- GEOCODING ---
        console.print(f"[yellow]Geocoding {target_name}...[/yellow]")
        coords = get_coordinates(target_name)
        if not coords:
            console.print("[red]Geocoding failed. Try a more specific name.[/red]")
            return
            
        lat, lon, full_name = coords
        
        # --- BEARING ---
        bearing = calculate_shoreline_bearing(lat, lon)
        console.print(f"[green]Scouting: {full_name}[/green]")
        console.print(f"[blue]Shoreline Bearing: {bearing}°[/blue]")
        bearing = Prompt.ask("Confirm bearing", default = bearing)

        # --- RESEARCH ---
        content = ""
        if Confirm.ask("Run agentic research?"):
            content = await generate_knowledge_file(target_name)
            if not content or "Error" in content:
                console.print("[red]Research failed to generate content.[/red]")
                return
            console.print(f"\n[bold green]--- DRAFT ---\n{content}\n")

        # --- TIDE & SAVE ---
        tide_id = "0000"
        match = re.search(r"Tide ID:\s*(\d{4})", content)
        if match: tide_id = match.group(1)
        tide_id = Prompt.ask("Confirm Tide ID", default=tide_id)

        key = target_name.lower().replace(" ", "_").replace(",", "").strip()
        
        # Load and Merge JSON
        live_spots = {}
        if LIVE_SPOTS_PATH.exists():
            try:
                live_spots = json.loads(LIVE_SPOTS_PATH.read_text(encoding='utf-8'))
            except: live_spots = {}
            
        live_spots[key] = {
            "name": target_name, "lat": lat, "lon": lon, 
            "tide_id": tide_id, "knowledge_file": f"{key}.txt", 
            "shoreline_bearing": int(bearing)
        }
        
        LIVE_SPOTS_PATH.write_text(json.dumps(live_spots, indent=4), encoding='utf-8')

        if content and Confirm.ask("Save .txt guide?"):
            kb_path = Path(f"spotbot_knowledge/spots/{key}.txt")
            kb_path.parent.mkdir(parents=True, exist_ok=True)
            kb_path.write_text(content, encoding='utf-8')
            console.print(f"[green]✅ Saved to {key}.txt[/green]")
            
            update_scout_list(verified_name)
            update_scout_list(target_name)

    except Exception as e:
        console.print(f"[bold red]FATAL ERROR: {e}[/bold red]")
        import traceback
        console.print(traceback.format_exc()) # This will show the exact line that failed

if __name__ == "__main__":
    asyncio.run(main())