# Future features

Ideas not yet built. Each entry says what it is, why it's wanted, and what
actually blocks it — so a future reader can tell "not done yet" apart from
"tried, doesn't work".

---

## Fuel price filter

Let the planner prefer cheaper stations: skip a stop that's meaningfully more
expensive than one a few miles further on, and show the cost difference so the
choice is the user's rather than the algorithm's.

### Why

Cost is already the whole point of the app — it computes gallons, dollars, and
range. But every station is currently priced identically, so the plan optimizes
for *distance and detour only*. On a long trip the spread between the cheapest
and dearest station on a corridor is routinely 40–80¢/gal, which is a larger
swing than most of the routing decisions the planner currently agonizes over.

### What blocks it

**There is no per-station price data anywhere in the app or its dataset.**

- `#price` (`index.html:814`) is a single user-entered $/gal applied to the
  entire trip.
- The prebuilt fuel dataset (`tools/build_fuel_data.py:255`) carries
  `lat, lon, name, brand, operator, opening_hours, fuel` per station. No price.
- Runtime station objects carry `{lat, lon, name, brand, open, tags}`
  (`index.html:2337`, `index.html:2378`). No price.

OSM does not carry fuel prices in any usable density — the tags exist but are
almost never populated and go stale immediately. So this feature is a *data
sourcing* problem first and a UI problem second. Building the filter UI before
the data exists would just be a control with nothing behind it.

### Options for the data

| Source | Granularity | Cost | Notes |
|---|---|---|---|
| EIA open data API | Regional / state weekly average | Free, public | Reliable, no key hassles, but an *average* — cannot rank two stations in the same town |
| AAA gas price feed | State / metro average | Free-ish, no official API | Same granularity ceiling as EIA, scraping is fragile |
| GasBuddy / OPIS | True per-station | Paid, ToS-restricted | The only thing that makes a real per-station filter possible |
| Crowd-sourced in-app | Per-station | Free | Needs a backend and users; this app is a static single file with no server, so this would change the project's shape entirely |

The honest read: a genuine *per-station* price filter needs a commercial feed.
Everything free tops out at regional averages.

### Smallest shippable slice

Don't build the filter. Build the part that's useful without per-station data:

Have `tools/build_fuel_data.py` also fetch EIA regional averages during the
existing refresh workflow (`.github/workflows/refresh-fuel-data.yml`) and ship
them in the `data` branch payload. Then auto-fill `#price` from the region the
trip starts in, instead of defaulting to a hardcoded `4`. The field stays
editable — it becomes a better default, not a constraint.

That is a real accuracy win on the cost estimate, it reuses the refresh
pipeline that already exists, and it requires no new dependency, no backend,
and no paid feed. It also produces the price plumbing a true filter would need
later, so it isn't throwaway work.

Revisit the actual filter only if a per-station feed becomes available.

### Where it would touch

- `tools/build_fuel_data.py:255` — dataset schema
- `.github/workflows/refresh-fuel-data.yml` — refresh job
- `index.html:814` — the `#price` input and its default
- `index.html:3708` — `render()`, if per-station prices ever appear in `#stops`
- `index.html:1989` — `filterFuelType()`, the existing and only station filter,
  which is the natural place a price predicate would sit alongside

---

## `#over` doesn't move the ETA

Only the fuel-economy estimate feels `#over` ("MPH over the posted limit",
default `5`, `index.html:820`) — the trip's reported ETA/duration does not. A
driver who consistently runs 5+ over gets a fuel estimate that reflects it
(`buildFuelAxis`'s `uncappedMph = calibMph + overLimitMph`, `index.html:1210`)
but an ETA that's still OSRM's raw `route.duration`, unadjusted (see
`etaHours`, `index.html:2616`). Those two numbers can quietly disagree.

### Why

Two knobs exist for "not going the speed OSRM assumed" — `#maxMph` (a
vehicle's hard ceiling — shipped, see `applySpeedCap`/`speedCapRatio`) and
`#over` (typically driving faster than the limit). Shipping the cap gave
`route.duration` a real recompute path, built on the per-step speeds
`stepSpeeds()` (`index.html:1122`) already exposes: a step's duration is
stretched by exactly how much its implied speed exceeds the cap. `#over` was
never wired into that path and still isn't, so it moves the gallons/dollars
estimate but not the "arrive by" time shown anywhere in the app.

### What blocks it

Not the plumbing — the recompute path already proves route/leg/step durations
can be rebuilt from a per-step speed adjustment without breaking anything
downstream (see the `applySpeedCap` checks in the `?test=1` suite). The catch
is direction: `applySpeedCap` is provably safe *only* because every adjustment
is a slowdown (a mechanical ceiling the vehicle cannot exceed), which is what
makes it idempotent and guarantees a duration never decreases. `#over` pushes
the other way — faster than OSRM's own routing assumption — and "how much
faster does a real drive actually run vs. OSRM's free-flow estimate" isn't a
bounded, physically-forced number the way a vehicle's top speed is. Wiring
`#over` into the same mechanism needs that question answered first, not just
the wiring.

### Where it would touch

- `index.html:820` — `#over`, the input
- `index.html:1210` — `buildFuelAxis`'s `uncappedMph`, where `#over` currently
  only feeds the fuel-economy estimate
- `index.html:2616` — `etaHours`, still a straight scaling of OSRM's
  unadjusted `route.duration`
- `index.html:1122` — `stepSpeeds()`, the per-step data an `#over`-aware ETA
  would need — the same seam `applySpeedCap` already uses
