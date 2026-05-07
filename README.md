# 🌊 Windsurf SpotBot: The Local Legend
An AI-powered decision engine that synthesizes live marine data with local "pro-tip" knowledge to give windsurfers definitive gear and safety advice.

### Key Features
* **Live Data Integration:** Real-time fetching from Open-Meteo (Wind/Waves) and UK Admiralty API (Tidal Curves).
* **Physics Engine:** Custom calculations for Wave Power (kW/m), Tidal Flow (Rule of Twelfths), and Wind Quality (Onshore vs. Cross-shore).
* **AI Local Legend:** A RAG-based LLM persona that "reads" local spot guides to provide contextual advice.
* **Dynamic Gear Matrix:** Rider-weight specific sail and board recommendations.

### Tech Stack
* **Backend:** FastAPI (Python)
* **Frontend:** Jinja2 Templates, Bootstrap 5, Chart.js, Leaflet.js
* **Cloud:** Google Cloud Run, Docker, Secret Manager
* **AI:** OpenAI / OpenRouter (Llama 3.1)

### Top Features
Unlike generic weather apps, SpotBot uses **vector-based wind analysis** to determine if a breeze is "clean" cross-shore or "messy" onshore based on the specific shoreline bearing of each beach.