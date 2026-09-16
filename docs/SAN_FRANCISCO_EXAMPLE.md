# San Francisco example

**[Example] San Francisco — Delivery Vans and Taxis** is an OSM-only geographic example with synthetic operator subfleets. It opens retained results without online acquisition. No San Francisco notebook is included.

## Geography and spatial proxy

The OSM San Francisco administrative polygon is intersected with longitude/latitude bounds `[-122.53, 37.70, -122.35, 37.84]` to define a bounded urban study area. Offshore Farallon Islands and SFO airport are excluded. The directed driving network is fetched for a 1 km buffered routing extent; it is not clipped to the reporting boundary. OSM shape nodes are contracted after preserving all nearest-grid-node ties, intersections, road-attribute changes and isolated rings. Full intermediate line vertices and directed connectivity remain represented; merged source-node pairs are retained. This is graph preprocessing, not cartographic line simplification. The 100 m grid uses EPSG:32610; map exchange uses EPSG:4326.

The spatial activity proxy is an explicit mixture:

`0.6 × normalized OSM residential area + 0.4 × normalized OSM commercial-location count`.

Residential polygons contribute intersected area; commercial features contribute one representative point each. Zero-feature cells remain in the full reporting grid. This proxy is neither population nor measured order density. Uniform assumed road speed is 20 km/h, not observed congestion or OSM speed-limit data. The depot is a synthetic routed node near the commercial-feature weighted center. Raw OSM query responses and receipts are cached in the build workspace; registered input snapshots and query provenance are bundled. OpenStreetMap contributors, ODbL.

## Operating assumptions

| Parameter | Delivery vans | Taxis |
|---|---|---|
| Physical catalog | 20 | 60 |
| Expected daily tasks | 1,600 consignments | 900 within-region requests |
| Generation | Offline at 08:00; activity-weighted | Online OD; activity-weighted origin/destination |
| Activation | Uniform 08:00–09:00 | 6 at midnight; 18 each during 05–06, 09–11, 15–16 |
| Shift | 8 hours, within 08:00–18:00 | 8 hours, within 00:00–24:00 |
| Service | 120 seconds per consignment | Pickup 60 seconds; drop-off 30 seconds |
| Capacity | 100 unit consignments; minimum 15 min reload | One active request per vehicle |
| Dispatch | Single-depot One-shot, final return | Sequential nearest matching; maximum pickup 15 min |

Taxi demand shares: 6% during 00–06, 16% during 06–09, 13% during 09–12, 21% during 12–16, 23% during 16–19, 12% during 19–21 and 9% during 21–24. No previous-day taxi shifts or airport flows are generated. Totals are Poisson; locations and activation times vary across replications. Stationary depot stays are excluded from sensing.

The workload corresponds to 80 consignments per delivery vehicle-day (160 minutes of service before travel/reloads) and 15 requests per taxi shift (1.875 releases per vehicle-hour on average). These ratios define a transparent, editable workload scenario; they do not establish observed completion rates.

## Evidence and limits

[SFMTA's 2024 Q2 pilot report](https://www.sfmta.com/notices/taxi-upfront-fare-pilot-2024-q2-report) reports 86,306 pilot trips accounting for 11.5% of taxi trips in that quarter. Dividing by that share and the 91 calendar days suggests approximately 8,250 total taxi trips/day for that reporting scope, not a current weekday or within-boundary estimate. The 900-request example is an illustrative operator subset, not a reproduction of that market. Pilot-driver participation is not used as a physical fleet count. The [final pilot report notice](https://www.sfmta.com/notices/taxi-upfront-fare-pilot-2024-q3-q4-report) also records data-quality challenges.

[SFCTA's 2025 delivery study summary](https://www.sfcta.org/blogs/transportation-board-approves-eco-friendly-downtown-delivery-study-final-report) describes varied goods movement and fragmented data, recommending additional collection. It does not identify a calibrated 20-van operator or 1,600 daily orders. Demand volume, staffing, time profiles, depot, consumption units and service times here are explicitly synthetic. OSM alone cannot supply those operational observations.

Both examples use 14 January 2026, a full local day, ten joint replications, hourly reporting, uniform utility, 5-minute exponential saturation, five-sensor count steps, budgets 10/20/30/40 and 100 sampling rounds. Their utility values are conditioned on different geographic domains and should not be compared as a city ranking.

## Rebuild the reference

Maintainers can regenerate inputs and results through the formal backend:

```bash
python -m mobile_sensing.application.san_francisco_example --root ./results/sf-build --stage compute
```

The first preparation requests OSM; subsequent preparation reuses the recorded input/configuration snapshots. Recomputing an editable copy inside the app uses its existing prepared inputs and does not need OSM downloads. Packaging requires a completed simulation, analysis and passing example audit.
