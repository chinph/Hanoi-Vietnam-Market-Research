"""Shared paths, HTTP helpers and name normalisation used by every pipeline step."""
from __future__ import annotations

import json
import re
import time
from pathlib import Path

import requests
from unidecode import unidecode

ROOT = Path(__file__).resolve().parents[1]
RAW = ROOT / "data" / "raw"
PROCESSED = ROOT / "data" / "processed"
FIGURES = ROOT / "reports" / "figures"
for _p in (RAW, PROCESSED, FIGURES):
    _p.mkdir(parents=True, exist_ok=True)

# Identify the project to every service we call (OSM and Wikimedia both ask for this).
HEADERS = {"User-Agent": "HanoiMarketResearch/2.0 (+https://github.com/chinph/Hanoi-Vietnam-Market-Research)"}

# Bounding box that fully contains Hanoi province (south, west, north, east).
HANOI_BBOX = (20.53, 105.28, 21.39, 106.02)

# Hanoi's 30 district-level units were abolished on 2025-07-01. We analyse the former
# districts because price and population data are still published at that level, so
# boundaries are taken from OpenStreetMap as they stood before the reform.
BOUNDARY_DATE = "2025-01-01T00:00:00Z"

OVERPASS_URLS = [
    "https://overpass-api.de/api/interpreter",
    "https://overpass.kumi.systems/api/interpreter",
]


def get_with_retry(url: str, *, params=None, data=None, method="GET", tries=6, timeout=180) -> requests.Response:
    """HTTP request with exponential backoff. Public endpoints are shared, so we back off politely."""
    wait = 5
    for attempt in range(1, tries + 1):
        try:
            r = requests.request(method, url, params=params, data=data, headers=HEADERS, timeout=timeout)
            if r.status_code == 200:
                return r
            print(f"  {url} -> HTTP {r.status_code} (attempt {attempt}/{tries})")
        except requests.RequestException as exc:
            print(f"  {url} -> {type(exc).__name__} (attempt {attempt}/{tries})")
        time.sleep(wait)
        wait = min(wait * 2, 120)
    raise RuntimeError(f"Failed after {tries} attempts: {url}")


def overpass(query: str, cache_name: str, refresh: bool = False) -> dict:
    """Run an Overpass query, caching the raw JSON so the analysis is reproducible offline."""
    cache = RAW / f"{cache_name}.json"
    if cache.exists() and not refresh:
        return json.loads(cache.read_text(encoding="utf-8"))
    last_err = None
    for url in OVERPASS_URLS:
        try:
            r = get_with_retry(url, data={"data": query}, method="POST", tries=4)
            payload = r.json()
            if payload.get("remark", "").startswith("runtime error"):
                raise RuntimeError(payload["remark"])
            cache.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
            return payload
        except Exception as exc:  # try the next mirror
            last_err = exc
    raise RuntimeError(f"All Overpass mirrors failed: {last_err}")


_PREFIXES = r"^(quan|huyen|thi xa|thanh pho)\s+"
_SUFFIXES = r"\s+(district|town)$"


def norm_district(name: str) -> str:
    """Map 'Quận Ba Đình', 'Ba Đình districtHT', 'Thị Xã Sơn Tây' ... to one key: 'ba dinh'."""
    s = unidecode(str(name)).lower().strip()
    s = re.sub(r"\[.*?\]", "", s)          # footnote markers
    s = re.sub(r"(district)?ht$", r"\1", s)  # Wikipedia 'HT' footnote glued to the name
    s = re.sub(_PREFIXES, "", s)
    s = re.sub(_SUFFIXES, "", s)
    return re.sub(r"\s+", " ", s).strip()
