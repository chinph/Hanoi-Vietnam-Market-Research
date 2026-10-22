"""Step 2 - join the four sources into analysis-ready tables.

Outputs (data/processed/)
  pois.csv               every POI with the former district it falls inside
  district_features.csv  one row per district: demographics, price, supply and demand metrics
  grid_500m.csv          500 m x 500 m cells for the urban core, for micro-location analysis
"""
from __future__ import annotations

import json

import numpy as np
import pandas as pd
import shapely
from pyproj import Transformer
from shapely.geometry import shape

from common import PROCESSED

# UTM zone 48N is the metric projection that covers Hanoi, so areas and distances are in metres.
to_utm = Transformer.from_crs("EPSG:4326", "EPSG:32648", always_xy=True).transform

DEMAND_CATS = ["office", "education", "tourism", "retail", "transit"]
FNB_CATS = ["cafe", "restaurant", "nightlife"]
GRID_M = 500


def load_districts() -> pd.DataFrame:
    geo = json.loads((PROCESSED / "districts.geojson").read_text(encoding="utf-8"))
    rows = []
    for f in geo["features"]:
        g = shape(f["geometry"])
        rows.append({"key": f["properties"]["key"], "geom": g,
                     "geom_utm": shapely.transform(g, lambda xy: np.column_stack(to_utm(xy[:, 0], xy[:, 1])))})
    d = pd.DataFrame(rows)
    d["area_km2_osm"] = d["geom_utm"].map(lambda g: g.area / 1e6)
    return d


def assign_district(pois: pd.DataFrame, districts: pd.DataFrame) -> pd.Series:
    out = pd.Series(pd.NA, index=pois.index, dtype="object")
    for key, geom in zip(districts["key"], districts["geom"]):
        inside = shapely.contains_xy(geom, pois["lon"].to_numpy(), pois["lat"].to_numpy())
        out[inside & out.isna().to_numpy()] = key
    return out


def build_grid(pois: pd.DataFrame, districts: pd.DataFrame, keys: list[str]) -> tuple[pd.DataFrame, pd.DataFrame]:
    """Square grid over the chosen districts; counts per category per cell, plus each POI's cell."""
    x, y = to_utm(pois["lon"].to_numpy(), pois["lat"].to_numpy())
    p = pois.assign(x=x, y=y)
    sub = districts[districts["key"].isin(keys)]
    minx, miny, maxx, maxy = shapely.union_all(sub["geom_utm"].tolist()).bounds
    p = p[(p.x >= minx) & (p.x < maxx) & (p.y >= miny) & (p.y < maxy)].copy()
    p["gx"] = ((p.x - minx) // GRID_M).astype(int)
    p["gy"] = ((p.y - miny) // GRID_M).astype(int)
    counts = p.pivot_table(index=["gx", "gy"], columns="category", values="osm_id", aggfunc="count", fill_value=0)
    counts = counts.reset_index()
    # Cell centre back to lat/lon and the district that contains it.
    cx = minx + (counts.gx + 0.5) * GRID_M
    cy = miny + (counts.gy + 0.5) * GRID_M
    to_wgs = Transformer.from_crs("EPSG:32648", "EPSG:4326", always_xy=True).transform
    counts["lon"], counts["lat"] = to_wgs(cx.to_numpy(), cy.to_numpy())
    counts["key"] = assign_district(counts, sub)
    return counts.dropna(subset=["key"]), p[["osm_type", "osm_id", "category", "gx", "gy"]]


def main() -> None:
    pop = pd.read_csv(PROCESSED / "population.csv")
    prices = pd.read_csv(PROCESSED / "prices.csv")
    districts = load_districts()
    pois = pd.read_csv(PROCESSED / "pois_raw.csv")

    pois["key"] = assign_district(pois, districts)
    outside = pois["key"].isna().mean()
    pois = pois.dropna(subset=["key"])
    print(f"POIs inside Hanoi: {len(pois):,} ({outside:.1%} of bbox features fell outside the province)")

    counts = pois.pivot_table(index="key", columns="category", values="osm_id", aggfunc="count", fill_value=0)
    counts.columns = [f"n_{c}" for c in counts.columns]

    cafes = pois[pois.category == "cafe"]
    # Branded chain outlets are the direct competitors of a mid-range café brand, and OSM maps
    # them more uniformly than independents (brand tags are curated), so we keep them separately.
    chain = cafes.groupby("key")["brand"].agg(n_cafe_chain="count", cafe_chain_share=lambda s: s.notna().mean())

    df = (pop.merge(districts[["key", "area_km2_osm"]], on="key")
             .merge(prices, on="key", how="left")
             .merge(counts, left_on="key", right_index=True, how="left")
             .merge(chain, left_on="key", right_index=True, how="left"))
    count_cols = list(counts.columns) + ["n_cafe_chain"]
    df[count_cols] = df[count_cols].fillna(0).astype(int)

    area = df["area_km2_wiki"]  # official area; the OSM area is kept only as a validation check
    df["density_per_km2"] = df["population"] / area
    for c in FNB_CATS + DEMAND_CATS:
        df[f"{c}_per_km2"] = df[f"n_{c}"] / area
    df["cafe_per_10k"] = df["n_cafe"] / df["population"] * 1e4
    df["fnb_per_10k"] = df[[f"n_{c}" for c in FNB_CATS]].sum(axis=1) / df["population"] * 1e4
    df["cafe_share_of_fnb"] = df["n_cafe"] / df[[f"n_{c}" for c in FNB_CATS]].sum(axis=1)
    df["n_poi_total"] = df[[f"n_{c}" for c in FNB_CATS + DEMAND_CATS]].sum(axis=1)
    df.to_csv(PROCESSED / "district_features.csv", index=False)
    print(f"district_features.csv: {df.shape}")

    urban = df.loc[df.district_type == "urban", "key"].tolist()
    grid, cells = build_grid(pois, districts, urban)
    grid.to_csv(PROCESSED / "grid_500m.csv", index=False)
    pois.merge(cells, on=["osm_type", "osm_id", "category"], how="left").to_csv(PROCESSED / "pois.csv", index=False)
    print(f"grid_500m.csv: {len(grid):,} non-empty cells in the 12 urban districts")


if __name__ == "__main__":
    main()
