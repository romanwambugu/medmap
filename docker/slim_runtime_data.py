"""Trim the embedded data snapshot to what the API actually reads.

The Docker build runs this on its own copy of the prepared snapshot (stage
"data-slim" in docker/Dockerfile), so neither data/ nor docker/runtime-data is
ever changed. Running it again on an already trimmed copy changes nothing.

- Stage 5 tract_features.parquet: only /api/v1/map/population reads it, and it
  uses just the centroid, tract GEOID, population and nearest-hospital distance
  (see api/main.py). The other ~1,300 columns, geometry included, are dropped:
  about 620 MB down to about 2 MB.
- Stage 5 county_features.parquet (about 117 MB): no API route queries it.

If an API route starts reading more tract columns, add them to TRACT_COLUMNS.
"""
from __future__ import annotations

import hashlib
import json
from pathlib import Path
import sys

import duckdb

# Every column name the API looks for in the tract table (api/main.py,
# population_v1). Only the ones present in the file are kept.
TRACT_COLUMNS = [
    "centroid_latitude", "latitude", "lat",
    "centroid_longitude", "longitude", "lon",
    "tract_geoid", "geoid", "GEOID",
    "population", "total_population",
    "distance_to_nearest_hospital_miles", "nearest_hospital_miles",
]
UNUSED_FILES = ["county_features.parquet"]


def stage5_run(root: Path) -> Path | None:
    pointer = root / "stage5" / "latest_success.json"
    if not pointer.is_file():
        return None
    info = json.loads(pointer.read_text(encoding="utf-8-sig"))
    run_id = Path(str(info["run_directory"]).replace("\\", "/")).name
    run = pointer.parent / "runs" / run_id
    return run if run.is_dir() else None


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def main(root: Path) -> int:
    run = stage5_run(root)
    if run is None:
        print("No Stage 5 snapshot found; nothing to trim.")
        return 0

    changed: dict[str, str] = {}
    tracts = run / "tract_features.parquet"
    if tracts.is_file():
        con = duckdb.connect()
        source = "read_parquet('" + tracts.as_posix().replace("'", "''") + "')"
        columns = [r[0] for r in con.execute(f"DESCRIBE SELECT * FROM {source}").fetchall()]
        keep = [c for c in TRACT_COLUMNS if c in columns]
        if keep and len(keep) < len(columns):
            before_rows = con.execute(f"SELECT count(*) FROM {source}").fetchone()[0]
            trimmed = tracts.with_suffix(".trimmed.parquet")
            selected = ", ".join('"' + c + '"' for c in keep)
            con.execute(
                f"COPY (SELECT {selected} FROM {source}) "
                f"TO '{trimmed.as_posix()}' (FORMAT parquet, COMPRESSION zstd)"
            )
            after_rows = con.execute(
                f"SELECT count(*) FROM read_parquet('{trimmed.as_posix()}')"
            ).fetchone()[0]
            if after_rows != before_rows:
                raise RuntimeError(f"Row count changed: {before_rows} -> {after_rows}")
            before_mb = tracts.stat().st_size / 1e6
            trimmed.replace(tracts)
            print(f"tract_features.parquet: {len(columns)} -> {len(keep)} columns, "
                  f"{before_mb:,.0f} MB -> {tracts.stat().st_size / 1e6:,.1f} MB, {after_rows} rows")
            changed[tracts.name] = f"kept columns {keep}"
        con.close()

    for name in UNUSED_FILES:
        path = run / name
        if path.is_file():
            print(f"{name}: removed ({path.stat().st_size / 1e6:,.0f} MB, unused by the API)")
            path.unlink()
            changed[name] = "removed, unused by the API"

    manifest_path = root / "package_manifest.json"
    if changed and manifest_path.is_file():
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        files = []
        for entry in manifest.get("files", []):
            path = root / entry["path"]
            if not path.is_file():
                continue
            if Path(entry["path"]).name in changed:
                entry = {**entry, "bytes": path.stat().st_size, "sha256": sha256(path)}
            files.append(entry)
        manifest["files"] = files
        manifest["trimmed_for_image"] = changed
        manifest_path.write_text(json.dumps(manifest, indent=2), encoding="utf-8")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(Path(sys.argv[1] if len(sys.argv) > 1 else "/data")))
