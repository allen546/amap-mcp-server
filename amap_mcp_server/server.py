import os
import argparse
from typing import Any, Dict, Optional
import requests
from mcp.server.fastmcp import FastMCP

AMAP_MAPS_API_KEY = os.getenv("AMAP_MAPS_API_KEY")
if not AMAP_MAPS_API_KEY:
    raise ValueError("AMAP_MAPS_API_KEY environment variable is required")

mcp = FastMCP("amap-maps")


# ── internal helpers (not exposed as tools) ──────────────────────────

def _geocode(address: str, city: Optional[str] = None) -> Dict[str, Any]:
    """Geocode a Chinese address to lon,lat. Returns {"location": "lng,lat", "formatted_address": "..."} or {"error": "..."}."""
    params = {"key": AMAP_MAPS_API_KEY, "address": address}
    if city:
        params["city"] = city
    resp = requests.get("https://restapi.amap.com/v3/geocode/geo", params=params)
    resp.raise_for_status()
    data = resp.json()
    if data["status"] != "1":
        info = data.get("info") or data.get("infocode")
        hint = ""
        if info == "ENGINE_RESPONSE_DATA_ERROR":
            hint = " (hint: address and city must be in Chinese)"
        return {"error": f"Geocoding '{address}' (city={city}) failed: {info}{hint}"}
    geocodes = data.get("geocodes", [])
    if not geocodes:
        return {"error": f"No results for address '{address}'"}
    return {
        "location": geocodes[0].get("location"),
        "formatted_address": geocodes[0].get("formatted_address"),
    }


def _geocode_pair(origin_address, destination_address, origin_city=None, destination_city=None):
    """Geocode origin + destination. Returns (origin_loc, dest_loc, addresses_dict) or a dict with 'error'."""
    o = _geocode(origin_address, origin_city)
    if "error" in o:
        return {"error": f"Origin: {o['error']}"}
    d = _geocode(destination_address, destination_city)
    if "error" in d:
        return {"error": f"Destination: {d['error']}"}
    addresses = {
        "origin": {"address": origin_address, "resolved": o["formatted_address"], "coordinates": o["location"]},
        "destination": {"address": destination_address, "resolved": d["formatted_address"], "coordinates": d["location"]},
    }
    return o["location"], d["location"], addresses


def _duration_str(seconds) -> str:
    """Convert seconds (str or int) to human-readable string."""
    try:
        m = int(seconds) // 60
        return f"{m} minutes"
    except (TypeError, ValueError):
        return str(seconds)


# ── routing tools ────────────────────────────────────────────────────

@mcp.tool()
def route_transit(origin: str, destination: str, city: str = "北京") -> Dict[str, Any]:
    """Plan a public transit route (subway, bus, train) between two places.

    All inputs MUST be in Chinese. Example: origin="团结湖地铁站", destination="北京第八十中学望京校区", city="北京"

    Args:
        origin: Starting location name/address in Chinese
        destination: Ending location name/address in Chinese
        city: City name in Chinese (default: 北京). Used for both origin and destination; for cross-city trips pass the origin city.
    """
    geo = _geocode_pair(origin, destination, city, city)
    if isinstance(geo, dict):
        return geo
    origin_loc, dest_loc, addresses = geo

    try:
        resp = requests.get("https://restapi.amap.com/v3/direction/transit/integrated", params={
            "key": AMAP_MAPS_API_KEY, "origin": origin_loc, "destination": dest_loc,
            "city": city, "cityd": city,
        })
        resp.raise_for_status()
        data = resp.json()
    except requests.RequestException as e:
        return {"error": str(e)}

    if data.get("status") != "1":
        return {"error": f"Transit API: {data.get('info') or data.get('infocode')}"}

    route = data.get("route")
    if not isinstance(route, dict):
        return {"error": "No route data"}

    plans = []
    for t in (route.get("transits") or []):
        if not isinstance(t, dict):
            continue
        segments = []
        for seg in (t.get("segments") or []):
            if not isinstance(seg, dict):
                continue
            s = {}
            # walking
            w = seg.get("walking")
            if isinstance(w, dict) and w.get("distance"):
                s["walk"] = {"distance": w.get("distance"), "duration": _duration_str(w.get("duration"))}
            # bus/subway
            bus = seg.get("bus")
            if isinstance(bus, dict):
                for bl in (bus.get("buslines") or []):
                    if not isinstance(bl, dict):
                        continue
                    dep = bl.get("departure_stop") or {}
                    arr = bl.get("arrival_stop") or {}
                    s["line"] = {
                        "name": bl.get("name"),
                        "from": dep.get("name") if isinstance(dep, dict) else None,
                        "to": arr.get("name") if isinstance(arr, dict) else None,
                        "stops": bl.get("via_num"),
                        "duration": _duration_str(bl.get("duration")),
                    }
                    break  # first busline option is enough
            # railway
            rail = seg.get("railway")
            if isinstance(rail, dict) and rail.get("name"):
                s["railway"] = {"name": rail.get("name"), "trip": rail.get("trip")}
            # entrance/exit
            ent = seg.get("entrance")
            if isinstance(ent, dict) and ent.get("name"):
                s["entrance"] = ent["name"]
            ext = seg.get("exit")
            if isinstance(ext, dict) and ext.get("name"):
                s["exit"] = ext["name"]
            if s:
                segments.append(s)

        plans.append({
            "duration": _duration_str(t.get("duration")),
            "walking_distance": t.get("walking_distance"),
            "segments": segments,
        })

    return {"addresses": addresses, "distance": route.get("distance"), "plans": plans}


@mcp.tool()
def route_drive(origin: str, destination: str, origin_city: str = "北京", destination_city: str = "北京") -> Dict[str, Any]:
    """Plan a driving route between two places.

    All inputs MUST be in Chinese.

    Args:
        origin: Starting location name/address in Chinese
        destination: Ending location name/address in Chinese
        origin_city: Origin city in Chinese (default: 北京)
        destination_city: Destination city in Chinese (default: 北京)
    """
    geo = _geocode_pair(origin, destination, origin_city, destination_city)
    if isinstance(geo, dict):
        return geo
    origin_loc, dest_loc, addresses = geo

    try:
        resp = requests.get("https://restapi.amap.com/v3/direction/driving", params={
            "key": AMAP_MAPS_API_KEY, "origin": origin_loc, "destination": dest_loc,
        })
        resp.raise_for_status()
        data = resp.json()
    except requests.RequestException as e:
        return {"error": str(e)}

    if data["status"] != "1":
        return {"error": f"Driving API: {data.get('info') or data.get('infocode')}"}

    paths = []
    for p in data["route"]["paths"]:
        steps = [{"instruction": s.get("instruction"), "road": s.get("road"), "distance": s.get("distance")} for s in p.get("steps", [])]
        paths.append({"distance": p.get("distance"), "duration": _duration_str(p.get("duration")), "steps": steps})

    return {"addresses": addresses, "paths": paths}


@mcp.tool()
def route_walk(origin: str, destination: str, origin_city: str = "北京", destination_city: str = "北京") -> Dict[str, Any]:
    """Plan a walking route between two places (up to 100km).

    All inputs MUST be in Chinese.

    Args:
        origin: Starting location name/address in Chinese
        destination: Ending location name/address in Chinese
        origin_city: Origin city in Chinese (default: 北京)
        destination_city: Destination city in Chinese (default: 北京)
    """
    geo = _geocode_pair(origin, destination, origin_city, destination_city)
    if isinstance(geo, dict):
        return geo
    origin_loc, dest_loc, addresses = geo

    try:
        resp = requests.get("https://restapi.amap.com/v3/direction/walking", params={
            "key": AMAP_MAPS_API_KEY, "origin": origin_loc, "destination": dest_loc,
        })
        resp.raise_for_status()
        data = resp.json()
    except requests.RequestException as e:
        return {"error": str(e)}

    if data["status"] != "1":
        return {"error": f"Walking API: {data.get('info') or data.get('infocode')}"}

    paths = []
    for p in data["route"]["paths"]:
        steps = [{"instruction": s.get("instruction"), "road": s.get("road"), "distance": s.get("distance")} for s in p.get("steps", [])]
        paths.append({"distance": p.get("distance"), "duration": _duration_str(p.get("duration")), "steps": steps})

    return {"addresses": addresses, "paths": paths}


@mcp.tool()
def route_bike(origin: str, destination: str, origin_city: str = "北京", destination_city: str = "北京") -> Dict[str, Any]:
    """Plan a bicycle route between two places (up to 500km).

    All inputs MUST be in Chinese.

    Args:
        origin: Starting location name/address in Chinese
        destination: Ending location name/address in Chinese
        origin_city: Origin city in Chinese (default: 北京)
        destination_city: Destination city in Chinese (default: 北京)
    """
    geo = _geocode_pair(origin, destination, origin_city, destination_city)
    if isinstance(geo, dict):
        return geo
    origin_loc, dest_loc, addresses = geo

    try:
        resp = requests.get("https://restapi.amap.com/v4/direction/bicycling", params={
            "key": AMAP_MAPS_API_KEY, "origin": origin_loc, "destination": dest_loc,
        })
        resp.raise_for_status()
        data = resp.json()
    except requests.RequestException as e:
        return {"error": str(e)}

    if data.get("errcode") != 0:
        return {"error": f"Bicycling API: {data.get('info') or data.get('infocode')}"}

    paths = []
    for p in data["data"]["paths"]:
        steps = [{"instruction": s.get("instruction"), "road": s.get("road"), "distance": s.get("distance")} for s in p.get("steps", [])]
        paths.append({"distance": p.get("distance"), "duration": _duration_str(p.get("duration")), "steps": steps})

    return {"addresses": addresses, "paths": paths}


# ── POI search tools ─────────────────────────────────────────────────

@mcp.tool()
def poi_search(keywords: str, city: str = "", citylimit: bool = False) -> Dict[str, Any]:
    """Search for places/POIs by keyword (restaurants, hospitals, schools, etc).

    Args:
        keywords: Search keywords (Chinese recommended, e.g. "火锅", "北京大学")
        city: Limit search to this city (Chinese name or adcode)
        citylimit: If true, only return results within the specified city
    """
    try:
        resp = requests.get("https://restapi.amap.com/v3/place/text", params={
            "key": AMAP_MAPS_API_KEY, "keywords": keywords,
            "city": city, "citylimit": "true" if citylimit else "false",
        })
        resp.raise_for_status()
        data = resp.json()
    except requests.RequestException as e:
        return {"error": str(e)}

    if data["status"] != "1":
        return {"error": f"Search API: {data.get('info') or data.get('infocode')}"}

    return {
        "pois": [{"name": p.get("name"), "address": p.get("address"), "location": p.get("location"), "type": p.get("type"), "id": p.get("id")} for p in data.get("pois", [])],
    }


@mcp.tool()
def poi_nearby(location: str, radius: str = "1000", keywords: str = "") -> Dict[str, Any]:
    """Search for POIs near a coordinate point.

    Args:
        location: Center point as "longitude,latitude" (e.g. "116.460186,39.933734")
        radius: Search radius in meters (default 1000)
        keywords: Optional filter keywords
    """
    try:
        resp = requests.get("https://restapi.amap.com/v3/place/around", params={
            "key": AMAP_MAPS_API_KEY, "location": location,
            "radius": radius, "keywords": keywords,
        })
        resp.raise_for_status()
        data = resp.json()
    except requests.RequestException as e:
        return {"error": str(e)}

    if data["status"] != "1":
        return {"error": f"Nearby API: {data.get('info') or data.get('infocode')}"}

    return {
        "pois": [{"name": p.get("name"), "address": p.get("address"), "location": p.get("location"), "type": p.get("type"), "id": p.get("id")} for p in data.get("pois", [])],
    }


@mcp.tool()
def poi_detail(poi_id: str) -> Dict[str, Any]:
    """Get detailed info for a POI by its ID (from poi_search or poi_nearby results).

    Args:
        poi_id: The POI ID string
    """
    try:
        resp = requests.get("https://restapi.amap.com/v3/place/detail", params={
            "key": AMAP_MAPS_API_KEY, "id": poi_id,
        })
        resp.raise_for_status()
        data = resp.json()
    except requests.RequestException as e:
        return {"error": str(e)}

    if data["status"] != "1":
        return {"error": f"Detail API: {data.get('info') or data.get('infocode')}"}
    if not data.get("pois"):
        return {"error": "POI not found"}

    poi = data["pois"][0]
    result = {
        "name": poi.get("name"),
        "address": poi.get("address"),
        "location": poi.get("location"),
        "city": poi.get("cityname"),
        "type": poi.get("type"),
        "tel": poi.get("tel"),
        "business_area": poi.get("business_area"),
    }
    if poi.get("biz_ext"):
        result["extra"] = poi["biz_ext"]
    return result


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Amap MCP Server")
    parser.add_argument("transport", nargs="?", default="stdio", choices=["stdio", "sse", "streamable-http"])
    args = parser.parse_args()
    mcp.run(transport=args.transport)
