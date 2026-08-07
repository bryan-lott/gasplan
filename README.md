# Gasplan — Road-Trip Fuel-Stop Planner

Gasplan is a single-page web application designed to plan fuel stops for long-distance road trips. It takes into account real-world factors that affect fuel economy—terrain, wind conditions, and driving speed—to help you determine the most efficient places to refuel along your route.

## How to Use

Gasplan works in two ways:

1. **Open locally**: Download or clone this repository and open `index.html` directly in your web browser.
2. **Visit online**: Navigate to https://bryan-lott.github.io/gasplan/ to use the app without any installation.

No server, no build step, no API keys required.

## Features

- **Multiple waypoints**: Enter a start location and any number of destinations in order, with the ability to reorder them or insert new stops mid-route.
- **Route comparison**: Gasplan calculates at least three route options for each trip and displays a side-by-side comparison view.
- **EPA vehicle lookup**: Select your vehicle by year, make, model, and trim to retrieve its EPA fuel-economy rating. You can also manually override the MPG if needed.
- **Terrain adjustment**: Elevation profiles along your route are analyzed to adjust fuel economy based on climbing and descending grades.
- **Wind adjustment**: Choose between live weather forecasts or a 3-year historical wind climatology to estimate the effect of head winds and tail winds on fuel consumption.
- **Speed penalty**: Accounts for a real-world highway-speed penalty when driving above typical posted limits.
- **Fuel-stop recommendations**: Estimates the fuel remaining at each proposed stop and at each destination, showing exactly where you should refuel.
- **Detailed per-segment breakdown**: See driving time, fuel consumption, and cost for each segment, plus a breakdown of how much each factor (terrain, wind, speed, baseline) contributed to consumption.
- **Trip totals**: Displays total fuel needed, estimated cost, and overall trip time.
- **Export options**: Generate Google Maps directions links and KML files for use in other mapping applications.

## Data Sources

Gasplan uses the following free, keyless public APIs:

- **OpenStreetMap Nominatim**: Geocoding (converting place names to coordinates and vice versa).
- **OSRM (Open Source Routing Machine)**: Public demo server providing turn-by-turn routing and step-by-step speed data.
- **Overpass API**: Querying OpenStreetMap for gas station locations (marked with `amenity=fuel` tags).
- **Open-Meteo**: Elevation data, current and forecast weather, and a 3-year historical weather archive for wind climatology.
- **EPA fueleconomy.gov**: Vehicle fuel-economy ratings by year, make, model, and trim.
- **OpenStreetMap Tile Servers**: Rendering the background map display.

## Accuracy and Limitations

Please note the following limitations when using Gasplan for trip planning:

- **Fuel prices**: Gas prices are not fetched from any real-time source. You provide a per-gallon price as an input, which defaults to a placeholder value. Update this figure before relying on cost estimates.
- **Tank capacity**: No keyless API publishes a vehicle's fuel tank capacity — EPA's fueleconomy.gov has no tank field at all, and NHTSA's vehicle database has no fuel data either. Gasplan fills in a rough estimate based on the EPA vehicle class as a starting point, not a manufacturer figure, and the app labels it as such directly on the form. Always check your owner's manual or fuel door and enter your vehicle's actual tank size before relying on the plan.
- **Speed limits**: Posted speed limits are not consistently available in OpenStreetMap data. Gasplan infers highway speeds from OSRM's step speeds and applies a fixed bump for motorway-class segments. This is a heuristic approximation, not a precise measurement.
- **Fuel-economy coefficients**: Of the coefficients used to model fuel economy:
  - The speed penalty is drawn from published US Department of Energy figures.
  - The grade (terrain) and aerodynamic (wind) coefficients are physically reasoned estimates, not empirically measured values.
- **Historical wind data**: The 3-year wind climatology option reflects typical conditions, not a forecast for your specific travel date.
- **Planning estimates**: Treat all gallon and cost figures as planning estimates, not guarantees. Real-world fuel consumption will vary based on driving habits, vehicle condition, load, and many other factors.

## Development

Gasplan includes built-in self-checks for development and testing. To run them, append `?test=1` to the URL (e.g., `file:///path/to/index.html?test=1` or `https://bryan-lott.github.io/gasplan/?test=1`). Test results will be printed to the browser console.

The app is intended for personal-scale use. It depends on free public API endpoints with usage policies and rate limits, so it is not suitable for high-traffic commercial deployment.

## License

MIT License — see LICENSE file for full text.

Copyright (c) 2026 Bryan Lott
