"""Step 1 - collect raw data from four public sources and save tidy CSV/GeoJSON snapshots.

    python src/fetch_data.py            # uses cached raw files when present
    python src/fetch_data.py --refresh  # re-downloads everything

Sources
  1. District population & area  - Wikipedia 'Hanoi', revision of 2025-05-29 (pinned oldid)
  2. Land price per m2            - mogi.vn/gia-nha-dat (average asking price, snapshot date recorded)
  3. District boundaries          - OpenStreetMap via Overpass, as of 2025-01-01 (pre-reform)
  4. Points of interest           - OpenStreetMap via Overpass, current
"""
from __future__ import annotations

import argparse
import io
import json
import re
from datetime import date

import pandas as pd
from bs4 import BeautifulSoup
from shapely.geometry import LineString, mapping
from shapely.ops import polygonize, unary_union

from common import BOUNDARY_DATE, HANOI_BBOX, PROCESSED, RAW, get_with_retry, norm_district, overpass

WIKI_OLDID = 1292944335  # last revision before the July 2025 reform with the 30-district table


# --------------------------------------------------------------------------- population
def fetch_population(refresh: bool) -> pd.DataFrame:
    cache = RAW / f"wikipedia_hanoi_{WIKI_OLDID}.html"
    if refresh or not cache.exists():
        r = get_with_retry("https://en.wikipedia.org/w/index.php", params={"oldid": WIKI_OLDID})
        cache.write_text(r.text, encoding="utf-8")
    tables = pd.read_html(io.StringIO(cache.read_text(encoding="utf-8")))
    # The administrative-divisions table is the one with these exact column names.
    table = next(t for t in tables if {"Name", "Area (km2)", "Population"} <= set(map(str, t.columns)))
    df = table[["Name", "Area (km2)", "Population"]].copy()
    df.columns = ["name_raw", "area_km2_wiki", "population"]
    # Drop group headers ("12 urban districts (Quận)") and subtotals: rows whose area is not numeric.
    df["area_km2_wiki"] = pd.to_numeric(df["area_km2_wiki"], errors="coerce")
    df["population"] = pd.to_numeric(df["population"], errors="coerce")
    df = df.dropna(subset=["area_km2_wiki", "population"])
    df = df[~df["name_raw"].str.contains("total", case=False)]
    df["district_type"] = "rural"
    df.loc[df.index[:12], "district_type"] = "urban"  # table lists the 12 quận first
    df.loc[df["name_raw"].str.contains("Sơn Tây"), "district_type"] = "town"
    df["key"] = df["name_raw"].map(norm_district)
    df["district"] = df["name_raw"].str.replace(r"(district)?HT$|\s*district$", "", regex=True).str.strip()
    assert len(df) == 30, f"expected 30 districts, got {len(df)}"
    return df[["key", "district", "district_type", "area_km2_wiki", "population"]]


# --------------------------------------------------------------------------- land price
def _parse_vnd_million(text: str) -> float:
    """'214 triệu/m' -> 214.0 ; '91,7 triệu/m' -> 91.7 ; '1,2 tỷ/m' -> 1200.0 (all in million VND)."""
    m = re.search(r"([\d.,]+)\s*(tỷ|triệu|nghìn)", text)
    if not m:
        return float("nan")
    value = float(m.group(1).replace(".", "").replace(",", "."))
    return value * {"tỷ": 1000, "triệu": 1, "nghìn": 0.001}[m.group(2)]


def fetch_prices(refresh: bool) -> pd.DataFrame:
    cache = RAW / "mogi_gia_nha_dat.html"
    if refresh or not cache.exists():
        cache.write_text(get_with_retry("https://mogi.vn/gia-nha-dat").text, encoding="utf-8")
    soup = BeautifulSoup(cache.read_text(encoding="utf-8"), "lxml")
    rows = []
    for table in soup.find_all("div", class_="mt-table"):
        for row in table.find_all("div", class_="mt-row"):
            cells = list(row.stripped_strings)
            if cells and any(k in cells[0] for k in ("Ba Đình", "Hoàn Kiếm")):
                hanoi_table = table
                break
        else:
            continue
        break
    for row in hanoi_table.find_all("div", class_="mt-row"):
        cells = list(row.stripped_strings)
        change = next((c for c in cells if c.endswith("%")), None)
        direction = -1 if "▼" in cells else 1
        rows.append({
            "key": norm_district(cells[0]),
            "price_mvnd_m2": _parse_vnd_million(" ".join(cells[1:3])),
            "price_change_pct": direction * float(change.rstrip("%").replace(",", ".")) if change else float("nan"),
        })
    df = pd.DataFrame(rows)
    df["price_snapshot"] = date.today().isoformat() if refresh or not (PROCESSED / "prices.csv").exists() \
        else pd.read_csv(PROCESSED / "prices.csv")["price_snapshot"].iloc[0]
    return df


# --------------------------------------------------------------------------- boundaries
def fetch_boundaries(refresh: bool, keys: set[str]) -> dict:
    s, w, n, e = HANOI_BBOX
    q = f"""[out:json][timeout:240][date:"{BOUNDARY_DATE}"];
    rel["boundary"="administrative"]["admin_level"="6"]({s},{w},{n},{e});
    out geom;"""
    data = overpass(q, "osm_district_boundaries_2025-01-01", refresh)
    features = []
    for el in data["elements"]:
        key = norm_district(el["tags"].get("name", ""))
        if key not in keys:  # bbox also catches neighbouring provinces' districts
            continue
        lines = [LineString([(p["lon"], p["lat"]) for p in m["geometry"]])
                 for m in el.get("members", []) if m["type"] == "way" and m.get("role") in ("outer", "") and "geometry" in m]
        poly = unary_union(list(polygonize(unary_union(lines))))
        features.append({"type": "Feature", "properties": {"key": key, "osm_id": el["id"], "name": el["tags"]["name"]},
                         "geometry": mapping(poly)})
    found = {f["properties"]["key"] for f in features}
    assert found == keys, f"missing boundaries: {keys - found}"
    return {"type": "FeatureCollection", "features": features}


# --------------------------------------------------------------------------- POIs
# Each category maps to Overpass tag filters. 'competitor' is what a café brand competes with;
# the others are footfall / demand generators that a site-selection analyst would look for.
POI_CATEGORIES = {
    "cafe":        ['["amenity"="cafe"]', '["shop"="coffee"]', '["shop"="tea"]'],
    "restaurant":  ['["amenity"="restaurant"]', '["amenity"="fast_food"]', '["amenity"="food_court"]'],
    "nightlife":   ['["amenity"~"^(bar|pub|nightclub)$"]'],
    "office":      ['["office"]', '["building"~"^(office|commercial)$"]'],
    "education":   ['["amenity"~"^(university|college|school)$"]'],
    "tourism":     ['["tourism"~"^(hotel|hostel|guest_house|attraction|museum)$"]'],
    "retail":      ['["shop"~"^(mall|department_store|supermarket|convenience)$"]'],
    "transit":     ['["highway"="bus_stop"]', '["railway"~"^(station|halt)$"]', '["public_transport"="station"]'],
}


def fetch_pois(refresh: bool) -> pd.DataFrame:
    s, w, n, e = HANOI_BBOX
    frames = []
    for cat, filters in POI_CATEGORIES.items():
        body = "\n".join(f"nwr{f}({s},{w},{n},{e});" for f in filters)
        q = f"[out:json][timeout:240];({body});out center tags;"
        data = overpass(q, f"osm_poi_{cat}", refresh)
        recs = []
        for el in data["elements"]:
            lat = el.get("lat", el.get("center", {}).get("lat"))
            lon = el.get("lon", el.get("center", {}).get("lon"))
            if lat is None:
                continue
            t = el.get("tags", {})
            recs.append({"osm_type": el["type"], "osm_id": el["id"], "category": cat, "lat": lat, "lon": lon,
                         "name": t.get("name"), "brand": t.get("brand"), "subtype": t.get("amenity") or t.get("shop")
                         or t.get("tourism") or t.get("office") or t.get("highway") or t.get("railway")})
        frames.append(pd.DataFrame(recs))
        print(f"  {cat:<11} {len(recs):>6} features")
    pois = pd.concat(frames, ignore_index=True)
    # A feature can match two filters inside a category (e.g. office=* on a commercial building).
    return pois.drop_duplicates(["osm_type", "osm_id", "category"])


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--refresh", action="store_true")
    refresh = ap.parse_args().refresh

    print("Population (Wikipedia)…")
    pop = fetch_population(refresh)
    pop.to_csv(PROCESSED / "population.csv", index=False)

    print("Land prices (mogi.vn)…")
    prices = fetch_prices(refresh)
    prices.to_csv(PROCESSED / "prices.csv", index=False)
    print(f"  {len(prices)} districts priced; unmatched: {set(prices.key) - set(pop.key) or 'none'}")

    print("District boundaries (OSM, pre-reform)…")
    geo = fetch_boundaries(refresh, set(pop.key))
    (PROCESSED / "districts.geojson").write_text(json.dumps(geo, ensure_ascii=False), encoding="utf-8")

    print("Points of interest (OSM)…")
    pois = fetch_pois(refresh)
    pois.to_csv(PROCESSED / "pois_raw.csv", index=False)
    print("Done.")


if __name__ == "__main__":
    main()
