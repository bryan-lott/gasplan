#!/usr/bin/env python3
"""Build gasplan's North America fuel-station payload from OpenStreetMap.

Pipeline per region: download a Geofabrik .osm.pbf -> `osmium tags-filter
nwr/amenity=fuel` -> `osmium export` to GeoJSONSeq -> reduce ways/relations to
centroids -> dedupe stations within 40m -> checkpoint as JSON. After all
regions are processed, stations are concatenated, dictionary-coded, delta+
zigzag varint encoded, gzipped, and base64'd into the exact wire format the
gasplan front end decodes (see decode_binary() below, which mirrors the JS
decoder byte-for-byte and is asserted against in selfcheck()).

Wire format (do not change without updating the JS decoder in lockstep):
    uvarint  n_stations
    uvarint  n_brands ; n_brands x [uvarint len, utf8 bytes]
    uvarint  n_ops    ; n_ops    x [uvarint len, utf8 bytes]
    n_stations x:
        uvarint  zigzag(delta lat*1e6 from previous station, 0 initially)
        uvarint  zigzag(delta lon*1e6 from previous station, 0 initially)
        uvarint  brand_idx + 1   (0 = none)
        uvarint  operator_idx + 1 (0 = none)
        byte     fuel flag bitfield (bit0 diesel,1 e85,2 lpg,3 unleaded,
                 4 midgrade,5 premium,6 any octane grade)
        str      name (uvarint len, utf8 bytes; empty = none)
        str      opening_hours (uvarint len, utf8 bytes; empty = none)
Stations are sorted by (round(lat,1), round(lon,1), lat, lon) for coordinate
locality before delta-coding. Whole buffer is gzip level 9, then base64.
This is scheme 3 ("build_binary") from the measured prototype at
~/.claude/jobs/c7fb3204/tmp/fueldata/encode.py — reproduced here verbatim.

Data (c) OpenStreetMap contributors, available under the Open Database
License (ODbL) 1.0: https://www.openstreetmap.org/copyright

Usage:
    # Production (full US + Canada + Mexico; big, slow, needs ~15GB disk headroom):
    python3 tools/build_fuel_data.py --out-dir build/out

    # Fast local/CI smoke test with small extracts:
    python3 tools/build_fuel_data.py --regions north-america/us/delaware,north-america/us/district-of-columbia \
        --out-dir build/out

Idempotent: each region's deduped station list is checkpointed to
<work-dir>/stations__<region>.json. Re-running skips any region whose
checkpoint already exists (pass --force to redo). Delete --work-dir for a
clean rebuild. The big .pbf for each region is deleted right after the
`tags-filter` pass (the only pass that touches the full multi-GB file); only
small filtered files remain on disk after that.
"""
import argparse
import base64
import gzip
import io
import json
import math
import os
import re
import shutil
import struct
import subprocess
import sys
import time
import urllib.request
from datetime import datetime, timezone

GEOFABRIK_BASE = "https://download.geofabrik.de"
DEFAULT_REGIONS = ["north-america/us", "north-america/canada", "north-america/mexico"]
DEDUPE_THRESH_M = 40.0
FUEL_ORDER = ["fuel:diesel", "fuel:e85", "fuel:lpg", "fuel:unleaded", "fuel:midgrade", "fuel:premium"]
ALLOWED_FUEL = set(FUEL_ORDER)
OCTANE_RE = re.compile(r"^fuel:octane_")
ATTRIBUTION = "© OpenStreetMap contributors, ODbL 1.0 (https://www.openstreetmap.org/copyright)"

# ---------------------------------------------------------------------------
# Download with disk-headroom guard
# ---------------------------------------------------------------------------

def remote_size(url):
    req = urllib.request.Request(url, method="HEAD")
    with urllib.request.urlopen(req, timeout=30) as r:
        return int(r.headers.get("Content-Length", 0))


def ensure_free_space(path, needed_bytes, margin=1.15, label=""):
    free = shutil.disk_usage(path).free
    required = int(needed_bytes * margin)
    if free < required:
        raise SystemExit(
            f"Not enough disk space to download {label}: need ~{required/1e9:.2f} GB "
            f"({needed_bytes/1e9:.2f} GB file + {int((margin-1)*100)}% margin), "
            f"only {free/1e9:.2f} GB free at {path}. Aborting before download to avoid "
            f"a corrupt partial file / wedged runner."
        )
    print(f"  disk check: {free/1e9:.2f} GB free, need ~{required/1e9:.2f} GB for {label}", file=sys.stderr)


def download(url, dest, label=""):
    size = remote_size(url)
    ensure_free_space(os.path.dirname(dest) or ".", size, label=label)
    tmp = dest + ".part"
    t0 = time.time()
    with urllib.request.urlopen(url, timeout=60) as resp, open(tmp, "wb") as f:
        shutil.copyfileobj(resp, f, length=1024 * 1024)
    os.replace(tmp, dest)
    dt = time.time() - t0
    got = os.path.getsize(dest)
    print(f"  downloaded {got/1e6:.1f} MB in {dt:.0f}s ({got/1e6/max(dt,0.1):.1f} MB/s)", file=sys.stderr)
    return got


# ---------------------------------------------------------------------------
# osmium subprocess steps
# ---------------------------------------------------------------------------

def run(cmd):
    print("  $ " + " ".join(cmd), file=sys.stderr)
    subprocess.run(cmd, check=True)


def osmium_extract_date(pbf_path):
    """Pull osmosis_replication_timestamp out of `osmium fileinfo` before we delete the pbf."""
    out = subprocess.run(["osmium", "fileinfo", pbf_path], capture_output=True, text=True, check=True).stdout
    m = re.search(r"osmosis_replication_timestamp=(\S+)", out)
    return m.group(1) if m else None


def tags_filter_and_export(pbf_path, work_dir, tag):
    fuel_pbf = os.path.join(work_dir, f"{tag}.fuel.osm.pbf")
    geojsonseq = os.path.join(work_dir, f"{tag}.fuel.geojsonseq")
    run(["osmium", "tags-filter", pbf_path, "nwr/amenity=fuel", "-o", fuel_pbf, "--overwrite"])
    run(["osmium", "export", fuel_pbf, "-f", "geojsonseq", "-o", geojsonseq, "--overwrite"])
    return geojsonseq


# ---------------------------------------------------------------------------
# GeoJSONSeq -> station records (ways/relations -> centroid), from extract.py
# ---------------------------------------------------------------------------

def centroid(coords, gtype):
    if gtype == "Point":
        return coords
    if gtype == "LineString":
        ring = coords[:-1] if len(coords) > 1 and coords[0] == coords[-1] else coords
        lon = sum(p[0] for p in ring) / len(ring)
        lat = sum(p[1] for p in ring) / len(ring)
        return [lon, lat]
    if gtype == "MultiPolygon":
        pts = []
        for poly in coords:
            for ring in poly:
                r = ring[:-1] if len(ring) > 1 and ring[0] == ring[-1] else ring
                pts.extend(r)
        lon = sum(p[0] for p in pts) / len(pts)
        lat = sum(p[1] for p in pts) / len(pts)
        return [lon, lat]
    raise ValueError(gtype)


def fuel_tags(props):
    return {k: v for k, v in props.items() if k in ALLOWED_FUEL or OCTANE_RE.match(k)}


def load_raw_stations(geojsonseq_path):
    raw = []
    with open(geojsonseq_path) as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            d = json.loads(line)
            props = d["properties"]
            gtype = d["geometry"]["type"]
            try:
                lon, lat = centroid(d["geometry"]["coordinates"], gtype)
            except (ValueError, ZeroDivisionError):
                continue
            raw.append({
                "lon": lon, "lat": lat,
                "name": props.get("name"),
                "brand": props.get("brand"),
                "operator": props.get("operator"),
                "opening_hours": props.get("opening_hours"),
                "fuel": fuel_tags(props),
            })
    return raw


# ---------------------------------------------------------------------------
# Dedupe: union-find over a geo-grid, 40m threshold (from extract.py)
# ---------------------------------------------------------------------------

def haversine_m(lon1, lat1, lon2, lat2):
    R = 6371000.0
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dphi = math.radians(lat2 - lat1)
    dl = math.radians(lon2 - lon1)
    a = math.sin(dphi / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 2 * R * math.asin(math.sqrt(a))


def completeness(r):
    score = int(bool(r["name"])) + int(bool(r["brand"])) + int(bool(r["operator"])) + int(bool(r["opening_hours"]))
    return score + len(r["fuel"])


def dedupe(raw, thresh_m=DEDUPE_THRESH_M):
    cell = 0.005  # ~500m buckets for candidate lookup
    buckets = {}
    for i, r in enumerate(raw):
        key = (round(r["lon"] / cell), round(r["lat"] / cell))
        buckets.setdefault(key, []).append(i)

    parent = list(range(len(raw)))

    def find(x):
        while parent[x] != x:
            parent[x] = parent[parent[x]]
            x = parent[x]
        return x

    def union(a, b):
        ra, rb = find(a), find(b)
        if ra != rb:
            parent[rb] = ra

    for key, idxs in buckets.items():
        cx, cy = key
        neighbor_idxs = []
        for dx in (-1, 0, 1):
            for dy in (-1, 0, 1):
                neighbor_idxs.extend(buckets.get((cx + dx, cy + dy), []))
        for a in idxs:
            for b in neighbor_idxs:
                if b <= a:
                    continue
                if haversine_m(raw[a]["lon"], raw[a]["lat"], raw[b]["lon"], raw[b]["lat"]) <= thresh_m:
                    union(a, b)

    groups = {}
    for i in range(len(raw)):
        groups.setdefault(find(i), []).append(i)

    merged = []
    for idxs in groups.values():
        best = max(idxs, key=lambda i: completeness(raw[i]))
        b = raw[best]
        fuel, name, brand, operator, opening_hours = {}, None, None, None, None
        for i in idxs:
            r = raw[i]
            fuel.update({k: v for k, v in r["fuel"].items() if k not in fuel})
            name = name or r["name"]
            brand = brand or r["brand"]
            operator = operator or r["operator"]
            opening_hours = opening_hours or r["opening_hours"]
        merged.append({
            "lon": b["lon"], "lat": b["lat"], "name": name, "brand": brand,
            "operator": operator, "opening_hours": opening_hours, "fuel": fuel,
        })
    return merged


# ---------------------------------------------------------------------------
# Binary wire format encode/decode (encode = encode.py's build_binary, exact)
# ---------------------------------------------------------------------------

def write_uvarint(buf, n):
    while True:
        b = n & 0x7F
        n >>= 7
        if n:
            buf.append(b | 0x80)
        else:
            buf.append(b)
            return


def zigzag(n):
    return (n << 1) ^ (n >> 63) if n < 0 else (n << 1)


def unzigzag(n):
    return (n >> 1) if (n & 1) == 0 else -((n + 1) >> 1)


def write_str(buf, s):
    b = (s or "").encode("utf-8")
    write_uvarint(buf, len(b))
    buf.extend(b)


def gzip9(data):
    """gzip -9 with mtime pinned to 0 so identical input always produces
    identical output bytes (plain gzip.compress() bakes in wall-clock time)."""
    buf = io.BytesIO()
    with gzip.GzipFile(fileobj=buf, mode="wb", compresslevel=9, mtime=0) as f:
        f.write(data)
    return buf.getvalue()


def fuel_bits(fuel):
    bits = 0

    def isyes(v):
        return isinstance(v, str) and v.lower().startswith("yes")

    for i, k in enumerate(FUEL_ORDER):
        if isyes(fuel.get(k)):
            bits |= 1 << i
    if any(isyes(v) for k, v in fuel.items() if k.startswith("fuel:octane_")):
        bits |= 1 << 6
    return bits


def encode_binary(stations):
    """stations: list of dicts with lat, lon, name, brand, operator, opening_hours, fuel."""
    brands = sorted({s["brand"] for s in stations if s["brand"]})
    ops = sorted({s["operator"] for s in stations if s["operator"]})
    bidx = {b: i for i, b in enumerate(brands)}
    oidx = {o: i for i, o in enumerate(ops)}
    ordered = sorted(stations, key=lambda s: (round(s["lat"], 1), round(s["lon"], 1), s["lat"], s["lon"]))

    buf = bytearray()
    write_uvarint(buf, len(ordered))
    write_uvarint(buf, len(brands))
    for b in brands:
        write_str(buf, b)
    write_uvarint(buf, len(ops))
    for o in ops:
        write_str(buf, o)

    prev_lat = prev_lon = 0
    for s in ordered:
        lat_i = round(s["lat"] * 1e6)
        lon_i = round(s["lon"] * 1e6)
        write_uvarint(buf, zigzag(lat_i - prev_lat))
        write_uvarint(buf, zigzag(lon_i - prev_lon))
        prev_lat, prev_lon = lat_i, lon_i
        write_uvarint(buf, bidx.get(s["brand"], -1) + 1)
        write_uvarint(buf, oidx.get(s["operator"], -1) + 1)
        buf.append(fuel_bits(s["fuel"]))
        write_str(buf, s["name"])
        write_str(buf, s["opening_hours"])
    return bytes(buf)


def read_uvarint(b, pos):
    result = shift = 0
    while True:
        byte = b[pos[0]]
        pos[0] += 1
        result |= (byte & 0x7F) << shift
        if not (byte & 0x80):
            return result
        shift += 7


def read_str(b, pos):
    n = read_uvarint(b, pos)
    s = b[pos[0]:pos[0] + n].decode("utf-8")
    pos[0] += n
    return s


def decode_binary(b):
    """Mirrors the JS parseBinary() decoder exactly. Used only by selfcheck()."""
    pos = [0]
    n = read_uvarint(b, pos)
    n_brands = read_uvarint(b, pos)
    brands = [read_str(b, pos) for _ in range(n_brands)]
    n_ops = read_uvarint(b, pos)
    ops = [read_str(b, pos) for _ in range(n_ops)]
    stations = []
    prev_lat = prev_lon = 0
    for _ in range(n):
        prev_lat += unzigzag(read_uvarint(b, pos))
        prev_lon += unzigzag(read_uvarint(b, pos))
        brand_idx = read_uvarint(b, pos)
        op_idx = read_uvarint(b, pos)
        fuel = b[pos[0]]
        pos[0] += 1
        name = read_str(b, pos)
        hours = read_str(b, pos)
        stations.append({
            "lat": prev_lat / 1e6, "lon": prev_lon / 1e6,
            "brand": brands[brand_idx - 1] if brand_idx else None,
            "operator": ops[op_idx - 1] if op_idx else None,
            "fuel": fuel, "name": name or None, "hours": hours or None,
        })
    return stations


def selfcheck():
    """Round-trip a tiny synthetic dataset through encode_binary/decode_binary
    and assert the decoded values match what the JS decoder would produce.
    Run automatically on every invocation -- this is the wire format the
    front end depends on, silent drift here breaks the whole feature."""
    sample = [
        {"lat": 39.9854766, "lon": -105.2492586, "name": "King Soopers Fuel Center",
         "brand": "King Soopers", "operator": None, "opening_hours": "24/7",
         "fuel": {"fuel:diesel": "yes", "fuel:octane_87": "yes"}},
        {"lat": 39.6020021, "lon": -105.2227553, "name": None, "brand": None,
         "operator": None, "opening_hours": None, "fuel": {}},
        {"lat": 45.0, "lon": -63.5, "name": "Irving", "brand": "Irving",
         "operator": "Irving Oil", "opening_hours": "Mo-Su 06:00-22:00",
         "fuel": {"fuel:unleaded": "yes", "fuel:premium": "yes"}},
    ]
    raw = encode_binary(sample)
    gz = gzip9(raw)
    back = gzip.decompress(gz)
    assert back == raw, "gzip round-trip mismatch"
    decoded = decode_binary(raw)
    assert len(decoded) == len(sample)
    by_key = {(round(s["lat"], 5), round(s["lon"], 5)): s for s in sample}
    for d in decoded:
        s = by_key[(round(d["lat"], 5), round(d["lon"], 5))]
        assert d["brand"] == s["brand"], (d, s)
        assert d["operator"] == s["operator"], (d, s)
        assert d["name"] == s["name"], (d, s)
        assert d["hours"] == s["opening_hours"], (d, s)
        assert d["fuel"] == fuel_bits(s["fuel"]), (d, s)
    print("selfcheck OK: encode/decode round-trip matches for", len(sample), "synthetic stations", file=sys.stderr)


# ---------------------------------------------------------------------------
# Orchestration
# ---------------------------------------------------------------------------

def region_tag(region):
    return region.strip("/").replace("/", "_")


def process_region(region, work_dir, force, min_free_margin, keep_pbf=False):
    tag = region_tag(region)
    checkpoint = os.path.join(work_dir, f"stations__{tag}.json")
    meta_path = os.path.join(work_dir, f"meta__{tag}.json")
    if os.path.exists(checkpoint) and not force:
        print(f"[{region}] checkpoint exists, skipping (use --force to redo)", file=sys.stderr)
        stations = json.load(open(checkpoint))
        meta = json.load(open(meta_path)) if os.path.exists(meta_path) else {}
        return stations, meta.get("source_date")

    url = f"{GEOFABRIK_BASE}/{region}-latest.osm.pbf"
    pbf_path = os.path.join(work_dir, f"{tag}.osm.pbf")
    print(f"[{region}] downloading {url}", file=sys.stderr)
    download(url, pbf_path, label=region)
    source_date = osmium_extract_date(pbf_path)

    print(f"[{region}] filtering amenity=fuel + exporting geojsonseq", file=sys.stderr)
    geojsonseq = tags_filter_and_export(pbf_path, work_dir, tag)

    # The big national/provincial pbf is the only multi-GB file on disk;
    # delete it now that osmium has produced the tiny filtered outputs --
    # unless a sibling roads/geo extraction (tools/build_geo_data.py, see
    # its --work-dir contract) still needs to read amenity=fuel's sibling
    # highway=motorway/trunk ways out of the SAME pbf before it's gone.
    # --keep-pbf defers deletion to that later step so the multi-GB file is
    # only ever downloaded once per region.
    if keep_pbf:
        print(f"[{region}] --keep-pbf set, leaving {pbf_path} on disk for a sibling extractor", file=sys.stderr)
    else:
        os.remove(pbf_path)
        print(f"[{region}] deleted {pbf_path} to free disk space", file=sys.stderr)

    raw = load_raw_stations(geojsonseq)
    merged = dedupe(raw)
    print(f"[{region}] raw={len(raw)} distinct_after_dedupe={len(merged)}", file=sys.stderr)

    json.dump(merged, open(checkpoint, "w"))
    json.dump({"source_date": source_date, "raw": len(raw), "distinct": len(merged)}, open(meta_path, "w"))

    # Filtered pbf + geojsonseq are small (single/double-digit MB even for
    # full countries); keep them around as debugging checkpoints rather than
    # deleting, they don't threaten disk headroom the way the source pbf did.
    return merged, source_date


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--regions", default=",".join(DEFAULT_REGIONS),
                     help="Comma-separated Geofabrik paths (relative to download.geofabrik.de, "
                          "no '-latest.osm.pbf' suffix), e.g. north-america/us,north-america/canada,"
                          "north-america/mexico or north-america/us/colorado for a small test.")
    ap.add_argument("--work-dir", default="build/work", help="Scratch dir for downloads/checkpoints.")
    ap.add_argument("--out-dir", default="build/out", help="Where payload_b64.txt and manifest.json go.")
    ap.add_argument("--force", action="store_true", help="Redo regions even if a checkpoint exists.")
    ap.add_argument("--keep-pbf", action="store_true",
                     help="Don't delete each region's downloaded .osm.pbf after the stations tags-filter "
                          "pass -- leaves it in --work-dir for tools/build_geo_data.py to extract roads "
                          "from (same file, no second download). That script deletes the pbf when done.")
    ap.add_argument("--min-free-margin", type=float, default=1.15,
                     help="Required free disk as a multiple of the download size (default 1.15 = 15%% headroom).")
    args = ap.parse_args()

    selfcheck()

    os.makedirs(args.work_dir, exist_ok=True)
    os.makedirs(args.out_dir, exist_ok=True)

    regions = [r.strip() for r in args.regions.split(",") if r.strip()]
    all_stations = []
    per_region = {}
    source_dates = {}
    for region in regions:
        stations, source_date = process_region(region, args.work_dir, args.force, args.min_free_margin, args.keep_pbf)
        all_stations.extend(stations)
        per_region[region] = len(stations)
        if source_date:
            source_dates[region] = source_date

    print(f"combined stations before cross-region dedupe: {len(all_stations)}", file=sys.stderr)
    # ponytail: regions are disjoint Geofabrik extracts (separate countries/
    # states), so a second global 40m dedupe pass is skipped here. If
    # adjoining extracts ever double-count a border station, add one more
    # dedupe(all_stations) call -- the function already supports it.

    raw_bytes = encode_binary(all_stations)
    gz_bytes = gzip9(raw_bytes)
    b64 = base64.b64encode(gz_bytes).decode("ascii")

    payload_path = os.path.join(args.out_dir, "payload_b64.txt")
    with open(payload_path, "w") as f:
        f.write(b64)

    manifest = {
        "attribution": ATTRIBUTION,
        "license": "ODbL 1.0",
        "build_date": datetime.now(timezone.utc).isoformat(),
        "regions": regions,
        "source_extract_dates": source_dates,
        "station_count_by_region": per_region,
        "station_count_total": len(all_stations),
        "dedupe_threshold_m": DEDUPE_THRESH_M,
        "wire_format": "binary_varint_v1",
        "bytes_raw": len(raw_bytes),
        "bytes_gzip9": len(gz_bytes),
        "bytes_base64": len(b64),
        "bytes_per_station_gzip_b64": round(len(b64) / max(len(all_stations), 1), 2),
    }
    manifest_path = os.path.join(args.out_dir, "manifest.json")
    json.dump(manifest, open(manifest_path, "w"), indent=2)

    print(json.dumps(manifest, indent=2))
    print(f"\nwrote {payload_path} and {manifest_path}", file=sys.stderr)


if __name__ == "__main__":
    main()
