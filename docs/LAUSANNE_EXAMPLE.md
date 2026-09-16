# Lausanne weekday example

**[Example] Lausanne — Five-Fleet Weekday** opens one retained simulation and one portfolio analysis. Duplicate the reference to edit. Inputs cover the union of 28 Lausanne boundary features, 17,553 100 m cells and 2024 population mass 315,757. Zero-population and zero-exposure cells remain included. Routing uses EPSG:2056 and an assumed 30 km/h speed.

| Fleet | Demand | Supply and execution |
|---|---|---|
| Bus 1 | Complete GTFS route `92-1-V-j26-1` | Separate inferred catalog; Scheduled |
| Bus 3 | Complete GTFS route `92-3-S-j26-1` | Separate inferred catalog; Scheduled |
| Bus 7 | Complete GTFS route `92-7-P-j26-1` | Separate inferred catalog; Scheduled |
| Postal | Poisson total, expectation 2,000; population-weighted offline location tasks at 07:00; 120 s service, quantity 1 | 20 vehicles; 07:00–18:00 operating bounds; starts uniformly 07:00–08:00; 8-hour shifts; capacity 100; One-shot, ≥15 min replenishment and final depot return |
| Ride-hailing | Poisson total, expectation 600; online population-weighted OD; pickup 60 s, drop-off 30 s | 40 vehicles; 8-hour shifts; Sequential, maximum pickup 15 min; idling after service |

Postal starts fully loaded at a synthetic facility at Lausanne railway station (6.6290923032° E, 46.5167918355° N). At most 100 unit consignments fit in each load; One-shot allows bounded depot reloads within the shift. Same-node tasks remain individual services, preferentially consecutive. Task locations are conditioned on directed depot round-trip reachability, with rejected spatial mass retained in the resolution report.

Ride-hailing demand shares are 5% during 00–06, 18% during 06–09, 13% during 09–12, 20% during 12–16, 24% during 16–19, 12% during 19–21 and 8% during 21–24. Four vehicles activate at midnight; twelve each activate uniformly during 05–06, 09–11 and 15–16. No previous-day ride-hailing shifts are synthesized. Shares and volumes are synthetic operator assumptions for an ordinary nonholiday weekday, not estimates of total regional demand.

The observation is 14 January 2026, 00:00–24:00 Europe/Zurich; ten joint replications, hourly reporting. GTFS service dates share an absolute execution timeline; preceding trips execute before midnight, outside reported exposure. Separate bus fleets preclude interlining across the selected routes by design. Duties are inferred operational identities, not registered vehicles.

Sensing includes powered movement, service, waiting and idling; stationary depot stays and off-duty intervals are excluded. Timetable inference classifies unassigned idle gaps of at least 60 minutes as off duty. Catalogs remain frozen across replications.

Portfolio settings: uniform spatial utility weights, exponential saturation 5 minutes, counts in steps of five for each of the five fleets including full endpoints, budgets 10/20/30/40, unit cost 1, and 100 sampling rounds per feasible count portfolio. Each sample draws a joint replication and concrete vehicles, applies nonlinear utility, then contributes to empirical mean and P05. P05 is not a guaranteed minimum.

Demonstration: open Project → inspect the five fleet settings → inspect Fleet results and daily profiles → compare portfolio frontiers and coverage → duplicate and modify. The notebook uses these same defaults and displays results without persistent exports. Actual run identifiers, catalog counts, route checks and elapsed times are retained in the bundle audit; historical evidence is not overwritten.
