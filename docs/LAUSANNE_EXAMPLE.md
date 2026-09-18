# Lausanne weekday example

**[Example] Lausanne — Bus, Postal and Taxi Weekday** retains one joint simulation plus separate worst-case (empirical P05) and standard-deviation portfolio analyses. The environment covers the union of 28 Lausanne boundary features on the supplied 100 m grid. Population, OSM commercial activity and OSM public services are retained as separate spatial features. Routing uses EPSG:2056 and an assumed 30 km/h speed.

| Fleet | Demand | Supply and execution |
|---|---|---|
| Bus | Complete GTFS routes `92-1-V-j26-1`, `92-3-S-j26-1`, `92-7-P-j26-1` in one fleet | One combined inferred duty catalog; Scheduled dispatch |
| Postal | Exactly 2,000 tasks released at 06:00; origin mixture `0.8 population + 0.2 public_services`; 120 s service, quantity 1 | 20 vehicles; 06:00–21:00 bounds; starts uniformly 06:00–12:00; 8-hour shifts; capacity 100; One-shot with replenishment and final depot return |
| Taxi | Poisson total with expectation 800; online OD; origin and destination mixtures `0.5 population + 0.3 commercial + 0.2 public_services`; pickup 60 s, drop-off 30 s | 80 vehicles; 8-hour shifts; Sequential, maximum pickup 15 min; random cruising after service |

Postal starts fully loaded at a synthetic facility at Lausanne railway station (6.6290923032° E, 46.5167918355° N). Four demand-balanced service areas are frozen before replications. The depot does not determine area membership.

Taxi demand shares are 5% during 00–06, 18% during 06–09, 13% during 09–12, 20% during 12–16, 24% during 16–19, 12% during 19–21 and 8% during 21–24. Shift-group shares are 10% at midnight and 30% each during 05–06, 09–11 and 15–16. These are synthetic operator assumptions, not calibrated regional demand.

The observation is 14 January 2026, 00:00–24:00 Europe/Zurich; ten joint replications and hourly reporting. Sensing uses operating duration. Stationary depot residence and inferred off-duty intervals are excluded.

Portfolio cost unit is `sensor`, with unit cost one per equipped vehicle. Budgets are 0, 20, 40, 60, 80 and 100 sensors. Counts use five-vehicle steps plus each catalog endpoint; each feasible portfolio receives 100 allocation rounds. Utility uses population spatial weights, a full-day temporal interval and five-minute exponential saturation (99% local utility at five minutes). Separate retained analyses use empirical P05 and sample standard deviation as the risk coordinate.

Demand, duties, feature proxies, depot, speeds, shift shares and costs remain explicit demonstration assumptions. Actual identifiers, diagnostics and elapsed times are retained in the bundle evidence.
