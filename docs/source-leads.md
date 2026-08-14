# Reef Aquarium Daily Digest: Data Architecture & Aggregation Strategy

This document outlines the data sources, API specifications, and aggregation scripts required to build an automated daily digest for saltwater and reef aquarium news. It is structured to be ingested by coding assistants to bootstrap the data pipeline.

## 1. Primary Data Sources

### Industry, Husbandry, and Forum RSS Feeds
*   **Reef Builders:** `https://reefbuilders.com/feed` (Equipment, livestock, lighting)
*   **Reefs.com:** `https://reefs.com/feed` (Biology, aquaculture, podcasts)
*   **Reef2Reef Forums:** Append `.rss` to specific sub-forums (e.g., `https://www.reef2reef.com/forums/general-reef-discussion.51/index.rss`)

### Scientific & Environmental APIs
*   **NOAA CO-OPS API:** Real-time and predicted tides, water temps, and currents (US coasts).
*   **WorldTides API:** Global tidal data (Fiji, Indonesia, Australia).
*   **NOAA Coral Reef Watch:** Satellite data for thermal stress and coral bleaching (Degree Heating Weeks).

### Reefing Clubs & Public Aquariums
*   **Southern California Marine Aquarium Society (SCMAS):** Local frag swaps, meetings, and Reef-A-Palooza updates. 
*   **DFWMAS & ARC:** Major regional club announcements.
*   **Science Blogs:** Steinhart Aquarium (Hope for Reefs), Waikiki Aquarium, Monterey Bay Aquarium.

---

## 2. Pulling NOAA Tide Data

The NOAA Center for Operational Oceanographic Products and Services (CO-OPS) provides a robust API. You do not need an API key for basic requests.

**Endpoint:** `https://api.tidesandcurrents.noaa.gov/api/prod/datagetter`

### Example Request (Southern California)
To pull the daily high/low tide predictions for the Southern California coast (using La Jolla, CA - Station `8628281`), you can construct the following GET request:

```python
import requests

def get_tide_data():
    url = "https://api.tidesandcurrents.noaa.gov/api/prod/datagetter"
    
    params = {
        "begin_date": "20260813",      # Format: YYYYMMDD
        "range": "24",                 # Hours to pull
        "station": "8628281",          # La Jolla, CA (Southern CA proxy)
        "product": "predictions",      # High/low tide predictions
        "datum": "MLLW",               # Mean Lower Low Water
        "time_zone": "lst_ldt",        # Local time
        "interval": "hilo",            # Only High and Low tides
        "units": "english",            # Feet
        "format": "json"               # Output format
    }
    
    response = requests.get(url, params=params)
    return response.json()

print(get_tide_data())
```

*Note: For other locations, swap the `station` ID (e.g., Key West, FL: `8724580`, Honolulu, HI: `1612340`).*

---

## 3. Automating RSS Aggregation

The most efficient way to aggregate blog and forum posts is using Python with the `feedparser` library. You can set up a scheduled cron job (or GitHub Action) to run a script daily, pull the feeds, and filter the content based on specific tags or keywords.

### Aggregation Script Example

```python
import feedparser
import datetime

# Target RSS feeds
FEEDS = [
    "https://reefbuilders.com/feed",
    "https://reefs.com/feed"
]

# Keywords to flag as high-priority in the daily digest
PRIORITY_KEYWORDS = [
    "CADE", 
    "kalkwasser", 
    "ORP", 
    "conductivity", 
    "vodka", 
    "carbon dosing"
]

def generate_daily_digest():
    digest = []
    today = datetime.datetime.now().date()
    
    for feed_url in FEEDS:
        feed = feedparser.parse(feed_url)
        
        for entry in feed.entries:
            # Basic time parsing (needs robust handling for different timezones in production)
            entry_date = datetime.datetime(*entry.published_parsed[:6]).date()
            
            if entry_date == today:
                title = entry.title
                link = entry.link
                summary = entry.summary.lower()
                
                # Check for priority topics
                tags = [kw for kw in PRIORITY_KEYWORDS if kw.lower() in summary or kw.lower() in title.lower()]
                
                digest.append({
                    "title": title,
                    "link": link,
                    "tags": tags,
                    "source": feed.feed.title
                })
                
    return digest

# In production, this output would be formatted into Markdown, HTML, or an email template
# and pushed to a notification service like Slack, Discord, or Amazon SES.
```

## 4. Next Steps for Implementation

1.  **Dependencies:** Ensure your environment has `requests` and `feedparser` installed (`pip install requests feedparser`).
2.  **Database/Storage:** Decide if the digest needs state (e.g., keeping track of already-seen articles via a lightweight SQLite database to prevent duplicates).
3.  **Delivery Mechanism:** Build out the final formatting layer. The script can be modified to compile the JSON arrays into an HTML email or a Discord Webhook payload.
