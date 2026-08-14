"""Shared varint/zigzag/gzip helpers for gasplan's binary payload encoders.

Used by build_fuel_data.py (stations) and build_geo_data.py (roads). Keep
this module dependency-free (stdlib only) since it ships nowhere near the
front end -- it's a build-time-only helper imported by both build scripts.
"""
import gzip
import io


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


def gzip9(data):
    """gzip -9 with mtime pinned to 0 so identical input always produces
    identical output bytes (plain gzip.compress() bakes in wall-clock time)."""
    buf = io.BytesIO()
    with gzip.GzipFile(fileobj=buf, mode="wb", compresslevel=9, mtime=0) as f:
        f.write(data)
    return buf.getvalue()
