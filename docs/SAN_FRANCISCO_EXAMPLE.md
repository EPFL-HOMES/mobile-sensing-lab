# San Francisco example

Spatial utility weights remain uniform. New exponential utility reaches 99% at five cumulative sensing minutes within each full-day utility interval (`sample-utility@4`); both P05 and standard-deviation analyses use this definition.

**[Example] San Francisco — Taxi Weekday** is an OSM-derived geographic example with one synthetic taxi fleet. It opens retained results without online acquisition.

The study polygon is the San Francisco OSM boundary intersected with `[-122.53, 37.70, -122.35, 37.84]`; offshore islands and SFO airport are excluded. A one-kilometre routing buffer is retained. The 100 m grid uses EPSG:32610 and the assumed operating speed is 20 km/h.

Prepared features are residential area, commercial locations, transportation objects, public-service objects and leisure activity. They are spatial proxies, not population or observed trip counts. Each feature is normalized independently over the complete prepared grid before mixture coefficients are applied.

Taxi origins use

\[
0.35\,q_{\mathrm{residential}}+0.25\,q_{\mathrm{commercial}}+
0.20\,q_{\mathrm{transportation}}+0.10\,q_{\mathrm{public}}+0.10\,q_{\mathrm{leisure}},
\]

and destinations use coefficients `(0.30, 0.30, 0.20, 0.10, 0.10)` in the same order. Demand is an online Poisson process with expected daily total 2,000 and shares 6%, 16%, 13%, 21%, 23%, 12% and 9% over intervals 00–06, 06–09, 09–12, 12–16, 16–19, 19–21 and 21–24. Pickup and drop-off services last 60 and 30 seconds.

The fixed catalog has 100 taxis. Shift-group shares are 10% at midnight and 30% each during 05–06, 09–11 and 15–16; every shift lasts eight hours. Sequential nearest matching uses a 15-minute maximum pickup time. Idle taxis use reproducible random cruising.

The observation is 14 January 2026, 00:00–24:00 America/Los_Angeles; ten joint replications and hourly reporting. Portfolio analysis uses 100 allocation rounds, unit cost one `sensor`, budgets 10 through 100 in increments of 10, five-vehicle count steps, a full-day utility interval, and five-minute exponential saturation. Separate empirical-P05 worst-case and standard-deviation frontiers share the same physical run and allocation samples.

All demand volumes, feature coefficients, staffing, shifts, services and speeds are scenario assumptions rather than calibrated city-wide estimates. OSM provenance and raw acquisition receipts remain retained with the example.

Maintainers rebuild the reference through the public application services:

```bash
python -m mobile_sensing.application.san_francisco_example --root ./results/sf-build --stage compute
```
