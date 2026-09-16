"""Installed-runtime Lausanne acceptance: cold chain or verified warm reload."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
import platform
import resource
import socket
import time
import tracemalloc

from mobile_sensing.application import HeadlessApplication, LausanneSmokeConfig, run_lausanne_smoke
from mobile_sensing.contracts import EnvironmentArtifactRef, ArtifactRef, PortfolioConfig
from mobile_sensing.environment import PreparedEnvironmentReader
from mobile_sensing.portfolio import UtilityWeightResource


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("root", type=Path)
    parser.add_argument("--warm-report", type=Path)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    repo = Path(__file__).resolve().parents[2]

    def forbidden(*_args, **_kwargs):
        raise RuntimeError("external networking is disabled for release acceptance")

    socket.socket.connect = forbidden
    socket.socket.connect_ex = forbidden
    socket.create_connection = forbidden
    config = LausanneSmokeConfig.model_validate_json(
        (repo / "docs/templates/m07/lausanne_smoke.json").read_bytes()
    )
    environment = None
    warm = None
    if args.warm_report:
        previous = json.loads(args.warm_report.read_text())
        tracemalloc.start()
        started = time.perf_counter()
        environment = PreparedEnvironmentReader(args.root).read(
            EnvironmentArtifactRef.model_validate(previous["artifacts"]["environment"])
        )
        warm = {
            "verified_prepared_reload_s": time.perf_counter() - started,
            "python_tracemalloc_peak_bytes": tracemalloc.get_traced_memory()[1],
        }
        tracemalloc.stop()
    elif args.root.exists():
        raise FileExistsError("cold release run requires an absent artifact root")
    report = run_lausanne_smoke(
        artifact_root=args.root,
        data_root=repo / "data/Lausanne",
        config=config,
        prepared_environment=environment,
    )
    application = HeadlessApplication(args.root)
    exposure = ArtifactRef.model_validate(report["artifacts"]["exposure"])
    value = json.loads((repo / "docs/templates/m08a/lausanne_counts.json").read_text())
    value["exposure_id"] = exposure.artifact_id
    portfolio = PortfolioConfig.model_validate_json(json.dumps(value))
    weights = UtilityWeightResource.model_validate_json(
        (repo / "docs/templates/m08a/lausanne_uniform_weights.json").read_bytes()
    )
    started = time.perf_counter()
    preview = application.preview_portfolios(exposure, portfolio)
    samples = application.evaluate_portfolio_samples(exposure, portfolio, weights)
    analysis = application.summarize_portfolios(samples.reference, portfolio)
    report["portfolio_timing_s"] = time.perf_counter() - started
    report["artifacts"]["portfolio_samples"] = samples.reference.model_dump(mode="json")
    report["artifacts"]["portfolio_analysis"] = analysis.reference.model_dump(mode="json")
    report["portfolio"] = {
        "replications_R": config.replications,
        "sampling_rounds_J": portfolio.sampling_rounds,
        "preview": preview.model_dump(mode="json"),
        "resolved_config": portfolio.model_dump(mode="json"),
        "weights": weights.model_dump(mode="json"),
    }
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(report, indent=2) + "\n")
    measurement = {
        "mode": "warm_verified_artifact_reload" if warm else "cold_preparation",
        "warm": warm,
        "timing_s": report["timing_s"],
        "memory": report["memory"],
        "process_peak_rss_bytes": resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
        * (1 if platform.system() == "Darwin" else 1024),
        "machine": platform.platform(),
        "python": platform.python_version(),
        "network": "socket connect/connect_ex/create_connection denied; only immutable local inputs",
    }
    args.output.with_suffix(".measurement.json").write_text(
        json.dumps(measurement, indent=2) + "\n"
    )
    print(json.dumps(measurement))


if __name__ == "__main__":
    main()
