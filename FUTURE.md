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

## Max vehicle speed

Let the user declare a top speed the vehicle can actually sustain. A Pinzgauer
tops out around 65 mph, a Unimog rather less, and plenty of older trucks and
loaded rigs won't hold an interstate limit up a grade. The planner currently
assumes every vehicle drives whatever OSRM thinks the road flows at.

### Why

Two things go wrong for a slow vehicle today, and the second is the expensive
one:

1. **Fuel is estimated at a speed the vehicle can't reach.** `speedFactor()`
   (`index.html:1088`) penalizes economy above a 60 mph baseline, so a vehicle
   OSRM routes at 75 gets a penalty it would never actually incur.
2. **The ETA is simply wrong, and nothing else notices.** Trip duration comes
   straight from OSRM (`index.html:2541`, `route.duration`) and is never
   recomputed from the speed the app itself assumed. On a long interstate leg a
   55 mph vehicle can arrive hours after the quoted time — and because refuel
   planning keys off arrival fuel and leg timing, a wrong ETA quietly degrades
   the stop plan too, not just the number on screen.

### What already exists

More than you'd expect. There is already an additive speed offset:
`#over` — "MPH over the posted limit", default `5` (`index.html:816`) — which
reaches the economy model as:

```javascript
const actualMph = calibMph + overLimitMph;   // index.html:1147
```

So the ceiling itself is a clamp on a value that's already computed and already
plumbed end to end. Roughly:

```javascript
const actualMph = Math.min(calibMph + overLimitMph, maxVehicleMph);
```

That plus a `maxMph` field on the vehicle object (`vehicleState()`,
`index.html:3126`) and one input next to `#over` gets the *fuel* side correct.

### The actual work

Duration. `route.duration` is OSRM's, computed for a normal vehicle, and the
app has no path today that recomputes time from its own adjusted speed. Doing
this properly means deriving trip time from the per-step speeds the app already
builds in `stepSpeeds()` (`index.html:1119`) after the clamp is applied, rather
than trusting OSRM's total — and then feeding that back into ETA, leg times,
and arrival-fuel.

Worth noting this is a latent gap *already*: `#over` defaults to 5 mph and
likewise doesn't move the ETA. A speed ceiling just makes the discrepancy large
enough to be obviously wrong instead of quietly wrong.

Two smaller wrinkles once the clamp exists:

- `MOTORWAY_SPEED_BUMP = 1.15` (`index.html:972`) multiplies OSRM's speed on
  motorways. A capped vehicle should be clamped *after* that bump, not before,
  or the cap silently doesn't bind.
- `speedFactor()`'s curve is the DOE passenger-car midpoint (7% per 5 mph over
  60). It is not a claim about a Unimog, whose economy at its own top speed is
  dominated by aerodynamics and gearing the curve knows nothing about. The cap
  will make the estimate *less wrong*, not right. Don't oversell it, and leave
  the manual MPG override (`#mpg`, `index.html:798`) as the real escape hatch —
  a user who knows their rig's real number should always be able to just type it.

### Smallest shippable slice

Add `maxMph` to the vehicle state and clamp `actualMph` — accept that ETA stays
OSRM's for now, and say so in the UI rather than pretending otherwise. Then do
the duration recomputation as a follow-up, since it's a bigger change that also
fixes the pre-existing `#over` gap.

### Where it would touch

- `index.html:1147` — `actualMph`, where the clamp goes
- `index.html:972` — `MOTORWAY_SPEED_BUMP`, clamp must come after it
- `index.html:1119` — `stepSpeeds()`, the per-step data a real ETA would use
- `index.html:2541` — `etaHours`, currently a straight scaling of OSRM duration
- `index.html:3126` — `vehicleState()`, add the field so it round-trips in the
  shared trip hash
- `index.html:816` — `#over`, the input this one sits next to
