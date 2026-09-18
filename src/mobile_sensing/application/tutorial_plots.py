"""Offline scientific figures for the joint Lausanne tutorial; no file exports."""

import numpy as np

FLEET_LABELS = {
    "bus_1": "Bus 1",
    "bus_3": "Bus 3",
    "bus_7": "Bus 7",
    "bus": "Bus",
    "postal": "Postal",
    "taxi": "Taxi",
    "ride_hailing": "Ride-hailing",
}
COLORS = {
    "bus_1": "#2666a5",
    "bus_3": "#7046a5",
    "bus_7": "#b6456c",
    "bus": "#2b6d9a",
    "postal": "#c88635",
    "taxi": "#158a83",
    "ride_hailing": "#158a83",
}


def display_figure(figure):
    """Render inline from a memory buffer, without backend magic or file writes."""
    from io import BytesIO
    from IPython.display import Image, display

    buffer = BytesIO()
    figure.savefig(buffer, format="png", dpi=110, bbox_inches="tight")
    display(Image(data=buffer.getvalue()))


def _map(ax, grid, environment, column, norm, title):
    environment.boundary.plot(ax=ax, color="#f0f2f4", edgecolor="#adb5bf", linewidth=0.45)
    positive = grid.loc[grid[column] > 0]
    if not positive.empty:
        positive.plot(ax=ax, column=column, cmap="viridis", norm=norm, linewidth=0)
    ax.set_title(title, fontsize=11, loc="left", pad=10)
    ax.set_axis_off()


def plot_coverage(fleets):
    """Comparable scales within each column; full fixed catalog in the denominator."""
    import matplotlib.pyplot as plt
    from matplotlib.colors import PowerNorm
    from matplotlib.cm import ScalarMappable

    figure, axes = plt.subplots(
        len(fleets), 2, figsize=(12, 3.5 * len(fleets)), layout="constrained", squeeze=False
    )
    for column_index, (column, label) in enumerate(
        (
            ("mean_sensing_minutes", "Mean fleet sensing (vehicle-minutes / cell / day)"),
            ("mean_per_vehicle_minutes", "Mean per-vehicle sensing (minutes / cell / day)"),
        )
    ):
        maximum = max(float(value[1][column].max()) for value in fleets.values())
        norm = PowerNorm(0.5, vmin=0, vmax=max(maximum, 1e-12))
        for row, (fleet, (summary, grid, _, environment)) in enumerate(fleets.items()):
            size = summary["fleets"][0]["catalog_size"]
            title = f"{FLEET_LABELS.get(fleet, fleet)} · {'fleet total' if column_index == 0 else 'per vehicle'} · N = {size}"
            _map(axes[row, column_index], grid, environment, column, norm, title)
        figure.colorbar(
            ScalarMappable(norm=norm, cmap="viridis"),
            ax=axes[:, column_index],
            shrink=0.65,
            label=label,
            fraction=0.035,
        )
    return figure


def plot_daily_profiles(fleets):
    import matplotlib.pyplot as plt

    figure, axes = plt.subplots(
        3,
        len(fleets),
        figsize=(max(13, 3.5 * len(fleets)), 8.2),
        sharex=True,
        layout="constrained",
        squeeze=False,
    )
    for column, (fleet, (_, _, time, _)) in enumerate(fleets.items()):
        starts = time.start_s.to_numpy() / 3600
        widths = (time.end_s.to_numpy() - time.start_s.to_numpy()) / 3600
        for row, (key, label) in enumerate(
            (
                ("mean_sensing_hours", "Sensing (vehicle-hours / bin)"),
                ("mean_arrivals", "Task releases / bin"),
                ("mean_active_vehicles", "Active vehicles (time-weighted)"),
            )
        ):
            ax = axes[row, column]
            ax.bar(
                starts,
                time[key],
                width=widths * 0.9,
                align="edge",
                color=COLORS.get(fleet, "#2b6d9a"),
                alpha=0.9,
            )
            ax.set(ylabel=label, xlim=(0, 24), ylim=(0, None), xticks=[0, 6, 12, 18, 24])
            ax.grid(axis="y", alpha=0.18)
            ax.spines[["top", "right"]].set_visible(False)
        axes[0, column].set_title(FLEET_LABELS.get(fleet, fleet), loc="left", fontweight="bold")
        axes[-1, column].set_xlabel("Local hour")
    return figure


def plot_frontiers(frontiers, *, nondominated_only=False):
    import matplotlib.pyplot as plt
    from matplotlib.ticker import FormatStrFormatter

    figure, ax = plt.subplots(figsize=(10, 5.3), layout="constrained")
    seen = set()
    colors = ("#1763C6", "#D85D0B", "#803AB5", "#12805D")
    ordered = sorted(frontiers, key=lambda result: result[0]["budget"]["budget_minor"])
    frontier_ids = {
        portfolio_id
        for _, frame in ordered
        for portfolio_id in frame.loc[frame.nondominated.fillna(False), "portfolio_id"]
    }
    for index, (view, frame) in enumerate(ordered):
        fresh = frame[~frame.portfolio_id.isin(seen)]
        seen.update(fresh.portfolio_id)
        fresh_frontier = fresh[fresh.portfolio_id.isin(frontier_ids)]
        budget = view["budget"]["budget_minor"] / view["budget"]["minor_unit_scale"]
        color = colors[index % len(colors)]
        displayed = fresh_frontier if nondominated_only else fresh
        if not displayed.empty:
            ax.scatter(
                displayed.utility_p05,
                displayed.utility_mean,
                s=62 if nondominated_only else 35,
                color=color,
                alpha=1 if nondominated_only else 0.18,
                marker="D" if nondominated_only else "o",
                edgecolors="white",
                linewidths=0.8 if nondominated_only else 0.6,
                label=f"First feasible at budget {budget:g}",
                zorder=2,
            )
        if not nondominated_only and not fresh_frontier.empty:
            ax.scatter(
                fresh_frontier.utility_p05,
                fresh_frontier.utility_mean,
                s=62,
                color=color,
                alpha=1,
                marker="D",
                edgecolors="white",
                linewidths=0.8,
                zorder=3,
            )
    ax.set(xlabel="Worst-case utility (5th percentile)", ylabel="Mean utility")
    ax.xaxis.set_major_formatter(FormatStrFormatter("%.5f"))
    ax.yaxis.set_major_formatter(FormatStrFormatter("%.5f"))
    ax.grid(alpha=0.18)
    ax.spines[["top", "right"]].set_visible(False)
    ax.legend(frameon=False, ncol=3, loc="upper left", bbox_to_anchor=(0, 1.13))
    return figure


def plot_frontier_maps(maps, environment):
    import matplotlib.pyplot as plt
    from matplotlib.colors import PowerNorm
    from matplotlib.cm import ScalarMappable

    columns = min(2, len(maps))
    rows = int(np.ceil(len(maps) / columns))
    figure, axes = plt.subplots(
        rows, columns, figsize=(12, 4.2 * rows), squeeze=False, layout="constrained"
    )
    norm = PowerNorm(
        0.5,
        vmin=0,
        vmax=max(1e-12, max(float(grid.mean_sensing_minutes.max()) for _, grid in maps)),
    )
    for ax, (point, grid) in zip(axes.flat, maps):
        counts = ", ".join(
            f"{FLEET_LABELS.get(f, f)} {n}" for f, n in point["count_by_fleet"].items()
        )
        title = f"Budget {point['budget']:g} · {counts}\nMean {point['utility_mean']:.5f} · P05 {point['utility_p05']:.5f}"
        _map(ax, grid, environment, "mean_sensing_minutes", norm, title)
    for ax in list(axes.flat)[len(maps) :]:
        ax.set_axis_off()
    figure.colorbar(
        ScalarMappable(norm=norm, cmap="viridis"),
        ax=list(axes.flat),
        label="Mean sensing (vehicle-minutes / cell / day)",
        shrink=0.65,
        fraction=0.025,
    )
    return figure


def plot_sampled_vehicle_time(temporal, *, title):
    """Plot reporting-bin sensing minutes for a fixed display sample."""

    import matplotlib.pyplot as plt

    figure, ax = plt.subplots(figsize=(11, 5.2), layout="constrained")
    for vehicle_id, rows in temporal.groupby("vehicle_id", sort=True):
        centres = (rows.start_hour.to_numpy() + rows.end_hour.to_numpy()) / 2
        ax.plot(centres, rows.sensing_minutes, marker="o", markersize=2.5, label=vehicle_id)
    ax.set(
        title=title,
        xlabel="Local hour",
        ylabel="Sensing duration (minutes / reporting bin)",
        xlim=(0, 24),
        xticks=[0, 6, 12, 18, 24],
        ylim=(0, None),
    )
    ax.grid(alpha=0.18)
    ax.spines[["top", "right"]].set_visible(False)
    ax.legend(frameon=False, ncol=2, fontsize=8)
    return figure


def plot_sampled_vehicle_space(spatial, environment, *, title):
    """Plot ten full-day physical-vehicle exposure matrices on one color scale."""

    import matplotlib.pyplot as plt
    from matplotlib.colors import PowerNorm
    from matplotlib.cm import ScalarMappable

    columns = 5
    rows = int(np.ceil(len(spatial) / columns))
    figure, axes = plt.subplots(
        rows, columns, figsize=(15, 3.4 * rows), squeeze=False, layout="constrained"
    )
    maximum = max(float(grid.sensing_minutes.max()) for grid in spatial.values())
    norm = PowerNorm(0.5, vmin=0, vmax=max(maximum, 1e-12))
    for ax, (vehicle_id, grid) in zip(axes.flat, spatial.items(), strict=False):
        _map(ax, grid, environment, "sensing_minutes", norm, vehicle_id)
    for ax in list(axes.flat)[len(spatial) :]:
        ax.set_axis_off()
    figure.suptitle(title, fontweight="bold")
    figure.colorbar(
        ScalarMappable(norm=norm, cmap="viridis"),
        ax=list(axes.flat),
        label="Full-day sensing duration (minutes / cell)",
        shrink=0.72,
        fraction=0.025,
    )
    return figure


def animate_vehicle_trajectory(trajectory, environment, *, vehicle_id, frame_minutes=15):
    """Create an inline, unsaved animation of cumulative movement and current position."""

    import matplotlib.pyplot as plt
    from matplotlib.animation import FuncAnimation
    from matplotlib.collections import LineCollection

    if trajectory.empty:
        raise ValueError("Selected vehicle has no movement trajectory")
    figure, ax = plt.subplots(figsize=(8, 7), layout="constrained")
    environment.boundary.plot(ax=ax, color="#eef1f4", edgecolor="#9aa5b1", linewidth=0.5)
    collection = LineCollection([], colors="#1763C6", linewidths=1.8, alpha=0.9)
    ax.add_collection(collection)
    (marker,) = ax.plot([], [], "o", color="#D85D0B", markersize=6, zorder=3)
    clock = ax.text(0.02, 0.98, "", transform=ax.transAxes, va="top", ha="left", fontweight="bold")
    ax.set_axis_off()
    ax.set_title(f"{vehicle_id} · cumulative trajectory", loc="left")
    frames = np.arange(0, 24 * 60 + frame_minutes, frame_minutes) * 60

    def coordinate_parts(geometry):
        if geometry.geom_type == "LineString":
            return [np.asarray(geometry.coords)]
        return [np.asarray(part.coords) for part in geometry.geoms]

    def update(time_s):
        completed = trajectory.loc[trajectory.start_s <= time_s]
        segments = []
        position = None
        for row in completed.itertuples(index=False):
            geometry = row.geometry
            if time_s < row.end_s:
                fraction = max(0.0, min(1.0, (time_s - row.start_s) / (row.end_s - row.start_s)))
                from shapely.ops import substring

                geometry = substring(geometry, 0, fraction, normalized=True)
            for values in coordinate_parts(geometry):
                if len(values):
                    segments.append(values)
                    position = values[-1]
        collection.set_segments(segments)
        if position is not None:
            marker.set_data([position[0]], [position[1]])
        clock.set_text(f"{int(time_s // 3600):02d}:{int(time_s % 3600 // 60):02d}")
        return collection, marker, clock

    return FuncAnimation(figure, update, frames=frames, interval=100, blit=False, repeat=True)
