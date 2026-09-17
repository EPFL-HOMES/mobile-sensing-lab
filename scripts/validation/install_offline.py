"""Install the built wheel in isolated core/web environments from verified lock caches."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tomllib
import venv


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("root", type=Path)
    parser.add_argument("cache", type=Path)
    args = parser.parse_args()
    repo = Path(__file__).resolve().parents[2]
    root = args.root.resolve()
    root.mkdir(parents=True, exist_ok=True)
    wheelhouse = root / "wheelhouse"
    wheelhouse.mkdir(exist_ok=True)
    lock = tomllib.loads((repo / "poetry.lock").read_text())
    hashes = {
        f["file"]: f["hash"].removeprefix("sha256:") for p in lock["package"] for f in p["files"]
    }
    for path in args.cache.rglob("*.whl"):
        if (
            path.name in hashes
            and hashlib.sha256(path.read_bytes()).hexdigest() == hashes[path.name]
        ):
            shutil.copy2(path, wheelhouse / path.name)
    constraints = root / "constraints.txt"
    constraints.write_text(
        "\n".join(f"{p['name']}=={p['version']}" for p in lock["package"]) + "\n"
    )
    wheel = repo / "dist" / "mobile_sensing-0.1.0-py3-none-any.whl"
    environment = dict(os.environ)
    environment.pop("PYTHONPATH", None)
    environment.update(PIP_NO_INDEX="1", PYTHONNOUSERSITE="1")
    records = {}
    for name, extra in (("core", ""), ("web", "[web]")):
        target = root / name
        if target.exists():
            raise FileExistsError(f"refusing to reuse isolated environment: {target}")
        venv.EnvBuilder(with_pip=True).create(target)
        python = target / "bin" / "python"
        command = [
            str(python),
            "-m",
            "pip",
            "install",
            "--no-index",
            "--find-links",
            str(wheelhouse),
            "-c",
            str(constraints),
            str(wheel) + extra,
        ]
        result = subprocess.run(command, env=environment, text=True, capture_output=True)
        (root / f"{name}-install.log").write_text(result.stdout + result.stderr)
        result.check_returncode()
        freeze = subprocess.check_output(
            [str(python), "-m", "pip", "list", "--format=json"], env=environment, text=True
        )
        packages = json.loads(freeze)
        if name == "core":
            assert not {"fastapi", "uvicorn", "starlette", "python-multipart"} & {
                p["name"].lower() for p in packages
            }
        probe = subprocess.check_output(
            [
                str(python),
                "-I",
                "-c",
                "import mobile_sensing, mobile_sensing.application; print(mobile_sensing.__file__)",
            ],
            env=environment,
            text=True,
        ).strip()
        assert str(target) in probe and "/src/" not in probe
        subprocess.run([str(python), "-m", "pip", "check"], env=environment, check=True)
        records[name] = {
            "command": command,
            "packages": packages,
            "import_path": probe,
            "isolated_import": True,
        }
    (root / "installation.json").write_text(
        json.dumps(
            {
                "python": sys.version,
                "wheel_sha256": hashlib.sha256(wheel.read_bytes()).hexdigest(),
                "environments": records,
            },
            indent=2,
        )
        + "\n"
    )
    print(root / "installation.json")


if __name__ == "__main__":
    main()
