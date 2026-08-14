#!/usr/bin/env python3
"""Build gasplan's roads + boundaries payloads (roads: OSM; boundaries: Natural Earth).

Ported from the measured prototype at
~/.claude/jobs/c7fb3204/tmp/geodata/{roads_common,shp_to_geojson,export_payloads}.py
-- tolerances/subsets/label choices there are settled, not re-derived here.

Roads pipeline per region: reuse the SAME .osm.pbf tools/build_fuel_data.py
downloaded (via its --keep-pbf flag, same --work-dir) -> `osmium tags-filter
w/highway=motorway,trunk` -> `osmium export` to GeoJSONSeq -> merge contiguous
same-(highway,ref,name) way fragments into continuous chains (OSM splits long
roads into short fragments -- ~66k ways collapse to ~7.8k chains on the
measured CO+NV+UT+AB+MX sample; skipping this step is why naive per-way
Douglas-Peucker barely simplifies anything) -> Douglas-Peucker simplify each
chain at --tolerance-m -> checkpoint (ref kept, name dropped, per region).
After all regions, chains are concatenated, sorted by geographic locality,
delta+zigzag varint encoded (delta state carried ACROSS chains, not reset per
chain -- same locality trick as build_fuel_data.py's stations), gzipped,
base64'd. Wire format matches roads_common.encode_ways() exactly (see
decode_roads() below, mirrors the JS decoder byte-for-byte, asserted in
selfcheck()).

Boundaries pipeline (independent of --regions/pbfs): download Natural Earth
50m admin-0 countries + admin-1 states/provinces + coastline shapefiles,
convert to GeoJSON with pyshp, filter to USA/CAN/MEX, run through
topojson-server (geo2topo, quantized) and topojson-simplify (toposimplify -S
0.1 -f) to produce a simplified quantized TopoJSON, gzip, base64.

KNOWN SOURCE-DATA GAP (not a bug, do not work around): Natural Earth's 50m
admin-1 (states/provinces) layer only covers 9 countries. Mexico is NOT one
of them, so the boundaries payload has a Mexico country outline but no
Mexican state boundaries at this scale. Recorded in geo_manifest.json.

Roads data (c) OpenStreetMap contributors, ODbL 1.0:
    https://www.openstreetmap.org/copyright
Natural Earth data is public domain, no attribution required:
    https://www.naturalearthdata.com/about/terms-of-use/

Usage:
    # Roads for one region (pbf must already exist at <work-dir>/<tag>.osm.pbf,
    # produced by `build_fuel_data.py --keep-pbf` against the SAME --work-dir):
    python3 tools/build_geo_data.py --regions north-america/us/colorado \
        --work-dir build/work --out-dir build/out --skip-boundaries

    # Combine-only pass across all regions (checkpoints already built above,
    # no pbf needed) + boundaries (built once, independent of regions):
    python3 tools/build_geo_data.py --regions north-america/us,north-america/canada,north-america/mexico \
        --work-dir build/work --out-dir build/out

Idempotent: each region's merged+simplified chain list is checkpointed to
<work-dir>/roads__<tag>.json; re-running skips a region whose checkpoint
already exists (pass --force to redo). The region's big .osm.pbf is deleted
right after that region's checkpoint is written (this script is the last
consumer of it -- build_fuel_data.py's --keep-pbf left it there for exactly
this). Missing pbf is a hard error (not a silent re-download) since a second
multi-GB download defeats the whole point of --keep-pbf.
"""
import argparse
import base64
import json
import os
import subprocess
import sys
import urllib.request
import zipfile
from collections import defaultdict

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from varint import write_uvarint, zigzag, unzigzag, write_str, read_uvarint, read_str, gzip9

DEFAULT_REGIONS = ["north-america/us", "north-america/canada", "north-america/mexico"]
DEFAULT_TOLERANCE_M = 1000
HIGHWAYS = ("motorway", "trunk")
OSM_ATTRIBUTION = "© OpenStreetMap contributors, ODbL 1.0 (https://www.openstreetmap.org/copyright)"
NE_ATTRIBUTION = "Made with Natural Earth. Free vector map data, public domain -- no attribution required (https://www.naturalearthdata.com/about/terms-of-use/)"
NE_SCALE = "50m"
NE_BASE = "https://naciscdn.org/naturalearth/50m"
NE_LAYERS = {
    "countries": f"{NE_BASE}/cultural/ne_50m_admin_0_countries.zip",
    "states": f"{NE_BASE}/cultural/ne_50m_admin_1_states_provinces.zip",
    "coastline": f"{NE_BASE}/physical/ne_50m_coastline.zip",
}
NA_ISO3 = {"USA", "CAN", "MEX"}
TOPO_QUANTIZATION = 100000  # matches the measured prototype's geo2topo -q (see topo/50m_raw.topojson transform)
MEXICO_ADMIN1_GAP_NOTE = (
    "Natural Earth's 50m admin-1 (states/provinces) layer covers only 9 countries, "
    "and Mexico is not one of them -- this boundaries payload has a Mexico country "
    "outline but no Mexican state boundaries. That is a Natural Earth source-data "
    "gap at 50m, not a bug in this script; do not try to work around it."
)


def run(cmd):
    print("  $ " + " ".join(cmd), file=sys.stderr)
    subprocess.run(cmd, check=True)


def region_tag(region):
    return region.strip("/").replace("/", "_")


# ---------------------------------------------------------------------------
# Roads: geojsonseq -> ways -> merged chains -> simplified (ported from
# roads_common.py's load_ways / merge_chains / douglas_peucker / way_length_m)
# ---------------------------------------------------------------------------

def load_ways(path):
    ways = []
    with open(path) as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            d = json.loads(line)
            geom = d["geometry"]
            if geom["type"] != "LineString":
                continue
            props = d["properties"]
            hw = props.get("highway")
            if hw not in HIGHWAYS:
                continue
            ways.append({
                "coords": geom["coordinates"],  # [lon, lat]
                "highway": hw,
                "name": props.get("name"),
                "ref": props.get("ref"),
            })
    return ways


def merge_chains(ways):
    """Join OSM way fragments sharing (highway, ref, name) that connect
    end-to-end at a point touched by exactly 2 such fragments. Must run
    BEFORE Douglas-Peucker -- OSM splits long roads into short fragments
    (measured ~9.5 points average), and per-fragment simplification barely
    simplifies anything (100m vs 2000m tolerance differed <10% without this
    step). name is used here only to disambiguate which fragments may join;
    it is stripped from the returned records afterward (roads payload keeps
    ref, drops name -- settled)."""
    groups = defaultdict(list)
    for i, w in enumerate(ways):
        groups[(w["highway"], w["ref"], w["name"])].append(i)

    merged = []
    for key, idxs in groups.items():
        def key_of(c):
            return (c[0], c[1])
        touch_count = defaultdict(int)
        for i in idxs:
            c = ways[i]["coords"]
            touch_count[key_of(c[0])] += 1
            touch_count[key_of(c[-1])] += 1
        touch = defaultdict(list)
        for i in idxs:
            c = ways[i]["coords"]
            if touch_count[key_of(c[0])] == 2:
                touch[key_of(c[0])].append(i)
            if touch_count[key_of(c[-1])] == 2:
                touch[key_of(c[-1])].append(i)

        visited = set()
        for i in idxs:
            if i in visited:
                continue
            visited.add(i)
            chain = list(ways[i]["coords"])

            def extend(end):
                nonlocal chain
                while True:
                    coord = chain[-1] if end == "tail" else chain[0]
                    ck = key_of(coord)
                    candidates = [j for j in touch.get(ck, []) if j not in visited]
                    if len(candidates) != 1:
                        break
                    j = candidates[0]
                    visited.add(j)
                    c2 = ways[j]["coords"]
                    if end == "tail":
                        chain = chain + (c2[1:] if key_of(c2[0]) == ck else list(reversed(c2))[1:])
                    else:
                        chain = (c2[:-1] if key_of(c2[-1]) == ck else list(reversed(c2))[:-1]) + chain

            extend("tail")
            extend("head")
            merged.append({"coords": chain, "highway": ways[i]["highway"], "ref": ways[i]["ref"]})
    return merged


def _perp_dist_m(p, a, b):
    import math
    lat0 = math.radians(p[1])
    mx = 111320.0 * math.cos(lat0)
    my = 111320.0
    ax, ay = a[0] * mx, a[1] * my
    bx, by = b[0] * mx, b[1] * my
    px, py = p[0] * mx, p[1] * my
    dx, dy = bx - ax, by - ay
    if dx == 0 and dy == 0:
        return math.hypot(px - ax, py - ay)
    t = ((px - ax) * dx + (py - ay) * dy) / (dx * dx + dy * dy)
    t = max(0.0, min(1.0, t))
    cx, cy = ax + t * dx, ay + t * dy
    return math.hypot(px - cx, py - cy)


def douglas_peucker(coords, tol_m):
    if len(coords) < 3:
        return coords
    keep = [False] * len(coords)
    keep[0] = keep[-1] = True
    stack = [(0, len(coords) - 1)]
    while stack:
        i0, i1 = stack.pop()
        if i1 - i0 < 2:
            continue
        a, b = coords[i0], coords[i1]
        maxd = -1.0
        idx = -1
        for i in range(i0 + 1, i1):
            d = _perp_dist_m(coords[i], a, b)
            if d > maxd:
                maxd = d
                idx = i
        if maxd > tol_m:
            keep[idx] = True
            stack.append((i0, idx))
            stack.append((idx, i1))
    return [c for c, k in zip(coords, keep) if k]


# ---------------------------------------------------------------------------
# Roads wire format: encode/decode (encode = roads_common.encode_ways, exact;
# decode mirrors the JS decodeRoads() from the prototype's build_verify.py)
# ---------------------------------------------------------------------------

def encode_chains(chains, scale=1e5):
    """ref-only labels (name table always present but empty -- matches the
    measured/verified reference payload's wire format byte-for-byte so the
    existing JS decoder needs no changes). Chains sorted by first-point geo
    locality so the running prev-point delta, carried ACROSS chains, stays
    small (same trick build_fuel_data.py uses for stations)."""
    refs = sorted({c["ref"] for c in chains if c["ref"]})
    ridx = {r: i for i, r in enumerate(refs)}

    def sortkey(c):
        lon, lat = c["coords"][0]
        return (round(lat, 1), round(lon, 1), lat, lon)
    ordered = sorted(chains, key=sortkey)

    buf = bytearray()
    write_uvarint(buf, len(ordered))
    write_uvarint(buf, len(refs))
    for r in refs:
        write_str(buf, r)
    write_uvarint(buf, 0)  # n_names -- always 0, names dropped (settled)

    prev_lon = prev_lat = 0
    for c in ordered:
        coords = c["coords"]
        write_uvarint(buf, len(coords))
        buf.append(1 if c["highway"] == "motorway" else 0)
        write_uvarint(buf, ridx.get(c["ref"], -1) + 1)
        write_uvarint(buf, 0)  # name_idx -- always 0 (no name table)
        for lon, lat in coords:
            loni = round(lon * scale)
            lati = round(lat * scale)
            write_uvarint(buf, zigzag(loni - prev_lon))
            write_uvarint(buf, zigzag(lati - prev_lat))
            prev_lon, prev_lat = loni, lati
    return bytes(buf)


def decode_chains(b, scale=1e5):
    """Mirrors the JS decodeRoads() decoder exactly. Used only by selfcheck()."""
    pos = [0]
    n = read_uvarint(b, pos)
    n_refs = read_uvarint(b, pos)
    refs = [read_str(b, pos) for _ in range(n_refs)]
    n_names = read_uvarint(b, pos)
    names = [read_str(b, pos) for _ in range(n_names)]
    chains = []
    prev_lon = prev_lat = 0
    for _ in range(n):
        npts = read_uvarint(b, pos)
        is_motorway = b[pos[0]]
        pos[0] += 1
        ref_idx = read_uvarint(b, pos)
        name_idx = read_uvarint(b, pos)
        coords = []
        for _ in range(npts):
            prev_lon += unzigzag(read_uvarint(b, pos))
            prev_lat += unzigzag(read_uvarint(b, pos))
            coords.append([prev_lon / scale, prev_lat / scale])
        chains.append({
            "highway": "motorway" if is_motorway else "trunk",
            "ref": refs[ref_idx - 1] if ref_idx else None,
            "name": names[name_idx - 1] if name_idx else None,
            "coords": coords,
        })
    return chains


def selfcheck():
    """Round-trip a tiny synthetic chain set through encode_chains/decode_chains
    and assert values survive -- this is the wire format the front end's JS
    decoder depends on, silent drift here breaks road rendering."""
    sample = [
        {"highway": "motorway", "ref": "I-70", "coords": [[-105.5, 39.7], [-105.0, 39.6], [-104.8, 39.5]]},
        {"highway": "trunk", "ref": None, "coords": [[-110.1, 40.0], [-109.9, 40.1]]},
        {"highway": "motorway", "ref": "I-15", "coords": [[-115.0, 36.0], [-114.9, 36.2], [-114.8, 36.4], [-114.7, 36.5]]},
    ]
    raw = encode_chains(sample)
    gz = gzip9(raw)
    import gzip as _gzip
    back = _gzip.decompress(gz)
    assert back == raw, "gzip round-trip mismatch"
    decoded = decode_chains(raw)
    assert len(decoded) == len(sample)
    by_ref = {c["ref"]: c for c in sample}
    seen = set()
    for d in decoded:
        s = by_ref[d["ref"]]
        seen.add(d["ref"])
        assert d["highway"] == s["highway"], (d, s)
        assert d["name"] is None, "names must never round-trip (dropped by design)"
        for (lon1, lat1), (lon2, lat2) in zip(d["coords"], s["coords"]):
            assert abs(lon1 - lon2) < 1e-4 and abs(lat1 - lat2) < 1e-4, (d, s)
    assert seen == set(by_ref)
    print("selfcheck OK: roads encode/decode round-trip matches for", len(sample), "synthetic chains", file=sys.stderr)


# ---------------------------------------------------------------------------
# Roads: per-region checkpointed extraction
# ---------------------------------------------------------------------------

def process_region_roads(region, work_dir, tolerance_m, force):
    tag = region_tag(region)
    checkpoint = os.path.join(work_dir, f"roads__{tag}.json")
    if os.path.exists(checkpoint) and not force:
        print(f"[{region}] roads checkpoint exists, skipping (use --force to redo)", file=sys.stderr)
        chains = json.load(open(checkpoint))
        return chains, {"cached": True}

    pbf_path = os.path.join(work_dir, f"{tag}.osm.pbf")
    if not os.path.exists(pbf_path):
        raise SystemExit(
            f"[{region}] {pbf_path} not found. build_geo_data.py extracts roads from the SAME "
            f".osm.pbf tools/build_fuel_data.py already downloaded -- run:\n"
            f"    python3 tools/build_fuel_data.py --regions {region} --work-dir {work_dir} --keep-pbf\n"
            f"first (same --work-dir), then re-run this script. Refusing to download a second "
            f"multi-GB copy of the same extract."
        )

    print(f"[{region}] filtering highway=motorway,trunk + exporting geojsonseq", file=sys.stderr)
    roads_pbf = os.path.join(work_dir, f"{tag}.roads.osm.pbf")
    geojsonseq = os.path.join(work_dir, f"{tag}.roads.geojsonseq")
    run(["osmium", "tags-filter", pbf_path, "w/highway=motorway,trunk", "-o", roads_pbf, "--overwrite"])
    run(["osmium", "export", roads_pbf, "-f", "geojsonseq", "-o", geojsonseq, "--overwrite"])

    # This script is the last consumer of the big pbf (build_fuel_data.py's
    # --keep-pbf left it here for exactly this) -- delete it now.
    os.remove(pbf_path)
    print(f"[{region}] deleted {pbf_path} to free disk space", file=sys.stderr)

    ways = load_ways(geojsonseq)
    chains = merge_chains(ways)
    simplified = [{**c, "coords": douglas_peucker(c["coords"], tolerance_m)} for c in chains]
    # name was only needed to disambiguate merge grouping; drop it from the checkpoint.
    for c in simplified:
        c.pop("name", None)

    print(f"[{region}] raw ways={len(ways)} merged chains={len(chains)} tolerance_m={tolerance_m}", file=sys.stderr)
    json.dump(simplified, open(checkpoint, "w"))
    return simplified, {"raw_ways": len(ways), "chains": len(chains), "cached": False}


# ---------------------------------------------------------------------------
# Boundaries: Natural Earth shapefiles -> GeoJSON (ported from shp_to_geojson.py)
# -> topojson-server/topojson-simplify (subprocess via npx)
# ---------------------------------------------------------------------------

def download_small(url, dest):
    req = urllib.request.Request(url)
    with urllib.request.urlopen(req, timeout=30) as resp, open(dest, "wb") as f:
        f.write(resp.read())


def fetch_natural_earth(work_dir):
    """Download + unzip the 3 NE 50m shapefiles (~2MB total). Cheap enough
    that no disk-headroom check or checkpoint skip is worth the complexity
    (unlike the multi-GB OSM extracts) -- always re-fetches."""
    ne_dir = os.path.join(work_dir, "ne")
    os.makedirs(ne_dir, exist_ok=True)
    shp_paths = {}
    for layer, url in NE_LAYERS.items():
        stem = os.path.basename(url)[:-4]  # strip .zip
        zip_path = os.path.join(ne_dir, stem + ".zip")
        extract_dir = os.path.join(ne_dir, stem)
        shp_path = os.path.join(extract_dir, stem + ".shp")
        if not os.path.exists(shp_path):
            print(f"[boundaries] downloading {url}", file=sys.stderr)
            download_small(url, zip_path)
            with zipfile.ZipFile(zip_path) as zf:
                zf.extractall(extract_dir)
        shp_paths[layer] = shp_path
    return shp_paths


def shp_to_geojson_features(shp_path, iso_field_candidates, filter_na, bbox_clip=None):
    import shapefile
    sf = shapefile.Reader(shp_path)
    fields = [f[0] for f in sf.fields[1:]]
    feats = []
    for sr in sf.iterShapeRecords():
        rec = dict(zip(fields, sr.record))
        if filter_na:
            iso = None
            for cand in iso_field_candidates:
                if cand in rec and rec[cand]:
                    iso = rec[cand]
                    break
            if iso not in NA_ISO3:
                continue
        if bbox_clip:
            minx, miny, maxx, maxy = sr.shape.bbox
            cminx, cminy, cmaxx, cmaxy = bbox_clip
            if maxx < cminx or minx > cmaxx or maxy < cminy or miny > cmaxy:
                continue
        props = {}
        for k in ("NAME", "name", "ADMIN", "iso_a3", "ISO_A3", "adm0_a3", "name_en"):
            if k in rec:
                props[k] = rec[k]
        feats.append({"type": "Feature", "properties": props, "geometry": sr.shape.__geo_interface__})
    return {"type": "FeatureCollection", "features": feats}


def build_boundaries(work_dir, out_dir):
    shp_paths = fetch_natural_earth(work_dir)

    countries = shp_to_geojson_features(shp_paths["countries"], ["ADM0_A3", "adm0_a3", "ISO_A3", "iso_a3"], filter_na=True)
    states = shp_to_geojson_features(shp_paths["states"], ["adm0_a3", "iso_a2"], filter_na=True)
    # coastline is global lines with no country attribution -- rough NA bbox clip
    # (incl. Aleutians/Caribbean edge), same as the measured prototype.
    coastline = shp_to_geojson_features(shp_paths["coastline"], [], filter_na=False, bbox_clip=(-170, 5, -50, 75))
    print(f"[boundaries] countries={len(countries['features'])} states={len(states['features'])} "
          f"coastline={len(coastline['features'])}", file=sys.stderr)

    geo_dir = os.path.join(work_dir, "ne")
    countries_path = os.path.join(geo_dir, "countries_na.geojson")
    states_path = os.path.join(geo_dir, "states_na.geojson")
    coastline_path = os.path.join(geo_dir, "coastline_na.geojson")
    json.dump(countries, open(countries_path, "w"))
    json.dump(states, open(states_path, "w"))
    json.dump(coastline, open(coastline_path, "w"))

    raw_topo = os.path.join(geo_dir, f"{NE_SCALE}_raw.topojson")
    simplified_topo = os.path.join(geo_dir, f"{NE_SCALE}_simplified.topojson")
    run(["npx", "--yes", "-p", "topojson-server@3", "geo2topo", "-q", str(TOPO_QUANTIZATION),
         "-o", raw_topo,
         f"countries={countries_path}", f"states={states_path}", f"coastline={coastline_path}"])
    run(["npx", "--yes", "-p", "topojson-simplify@3", "toposimplify", "-S", "0.1", "-f",
         "-o", simplified_topo, raw_topo])

    topo_bytes = open(simplified_topo, "rb").read()
    # sanity: verify it's well-formed topology JSON with the 3 expected objects
    topo = json.loads(topo_bytes)
    assert topo.get("type") == "Topology", "toposimplify output isn't a Topology"
    assert set(topo["objects"]) == {"countries", "states", "coastline"}, topo["objects"]

    gz = gzip9(topo_bytes)
    b64 = base64.b64encode(gz).decode("ascii")
    open(os.path.join(out_dir, "boundaries_b64.txt"), "w").write(b64)

    return {
        "attribution": NE_ATTRIBUTION,
        "license": "public domain",
        "source_scale": NE_SCALE,
        "layers": ["admin-0 countries", "admin-1 states/provinces", "coastline"],
        "filtered_to_iso3": sorted(NA_ISO3),
        "mexico_admin1_gap": MEXICO_ADMIN1_GAP_NOTE,
        "topojson_quantization": TOPO_QUANTIZATION,
        "toposimplify_flags": "-S 0.1 -f",
        "feature_counts": {"countries": len(countries["features"]), "states": len(states["features"]),
                            "coastline": len(coastline["features"])},
        "bytes_raw": len(topo_bytes),
        "bytes_gzip9": len(gz),
        "bytes_base64": len(b64),
    }


# ---------------------------------------------------------------------------
# Orchestration
# ---------------------------------------------------------------------------

def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--regions", default=",".join(DEFAULT_REGIONS),
                     help="Comma-separated Geofabrik paths, same convention as build_fuel_data.py --regions. "
                          "Each region's <work-dir>/<tag>.osm.pbf must already exist (from that script's --keep-pbf).")
    ap.add_argument("--work-dir", default="build/work", help="Scratch dir; must match build_fuel_data.py's --work-dir.")
    ap.add_argument("--out-dir", default="build/out", help="Where roads_b64.txt, boundaries_b64.txt, geo_manifest.json go.")
    ap.add_argument("--force", action="store_true", help="Redo regions even if a roads checkpoint exists.")
    ap.add_argument("--tolerance-m", type=float, default=DEFAULT_TOLERANCE_M,
                     help=f"Douglas-Peucker tolerance in meters (default {DEFAULT_TOLERANCE_M}). Only affects "
                          "regions whose checkpoint doesn't exist yet, or is rebuilt with --force.")
    ap.add_argument("--skip-boundaries", action="store_true",
                     help="Skip the Natural Earth boundaries build (used during the per-region roads loop "
                          "so boundaries are only built once).")
    args = ap.parse_args()

    selfcheck()

    os.makedirs(args.work_dir, exist_ok=True)
    os.makedirs(args.out_dir, exist_ok=True)

    regions = [r.strip() for r in args.regions.split(",") if r.strip()]
    all_chains = []
    chain_count_by_region = {}
    raw_way_count_by_region = {}
    for region in regions:
        chains, stats = process_region_roads(region, args.work_dir, args.tolerance_m, args.force)
        all_chains.extend(chains)
        chain_count_by_region[region] = len(chains)
        if "raw_ways" in stats:
            raw_way_count_by_region[region] = stats["raw_ways"]

    print(f"combined chains across {len(regions)} region(s): {len(all_chains)}", file=sys.stderr)

    raw_bytes = encode_chains(all_chains)
    gz_bytes = gzip9(raw_bytes)
    b64 = base64.b64encode(gz_bytes).decode("ascii")
    open(os.path.join(args.out_dir, "roads_b64.txt"), "w").write(b64)

    geo_manifest = {
        "roads": {
            "attribution": OSM_ATTRIBUTION,
            "license": "ODbL 1.0",
            "highways": list(HIGHWAYS),
            "labels": "ref kept, name dropped",
            "tolerance_m": args.tolerance_m,
            "regions": regions,
            "chain_count_by_region": chain_count_by_region,
            "raw_way_count_by_region": raw_way_count_by_region,
            "chain_count_total": len(all_chains),
            "wire_format": "roads_varint_v1",
            "bytes_raw": len(raw_bytes),
            "bytes_gzip9": len(gz_bytes),
            "bytes_base64": len(b64),
        },
    }

    if args.skip_boundaries:
        geo_manifest["boundaries"] = {"skipped": True}
    else:
        geo_manifest["boundaries"] = build_boundaries(args.work_dir, args.out_dir)

    manifest_path = os.path.join(args.out_dir, "geo_manifest.json")
    json.dump(geo_manifest, open(manifest_path, "w"), indent=2)
    print(json.dumps(geo_manifest, indent=2))
    print(f"\nwrote {args.out_dir}/roads_b64.txt"
          + ("" if args.skip_boundaries else f" and {args.out_dir}/boundaries_b64.txt")
          + f" and {manifest_path}", file=sys.stderr)


if __name__ == "__main__":
    main()
