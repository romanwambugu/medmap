from __future__ import annotations

import json
import os
import time
from pathlib import Path
from typing import Any

from fastapi import FastAPI, HTTPException, Query
from fastapi.middleware.cors import CORSMiddleware
from fastapi.middleware.gzip import GZipMiddleware
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

from .db import connect, paths
from .query import (
    ALLOWED_SERVICES, candidate_where, default_limit_for_zoom,
    diversify_ranked_rows, grid_cell_degrees,
)
from .schemas import OptimizeRequest
from .states import FIPS_TO_ABBR, STATE_NAMES, normalize_state
from .utils import candidate_feature, feature_collection, parse_bbox, record, records, scalar, state_fields


app = FastAPI(
    title="Optimal Hospital Placer API",
    version="1.0.0",
    description=(
        "Read-only analytical API over the final Stage 8 Parquet snapshot. "
        "DuckDB performs viewport filtering and interactive re-ranking; expensive ETL is never run per request."
    ),
)

# Browsers need CORS only when the site and the API are on different origins
# (split hosting). The packaged app serves both from one origin, so a hosted
# deployment can list its own origins in MEDMAP_CORS_ORIGINS (comma-separated).
# Unset, any origin may call the API, as in local development.
CORS_ORIGINS = [
    origin.strip()
    for origin in os.environ.get("MEDMAP_CORS_ORIGINS", "*").split(",")
    if origin.strip()
]

app.add_middleware(
    CORSMiddleware,
    allow_origins=CORS_ORIGINS,
    allow_credentials=False,
    allow_methods=["GET", "POST", "OPTIONS"],
    allow_headers=["*"],
)

# Map responses are large GeoJSON (up to ~1 MB for one state) and hosts such as
# Cloud Run send them uncompressed, so compress anything over 1 KB for browsers
# that accept gzip.
app.add_middleware(GZipMiddleware, minimum_size=1000)

API = "/api/v1"


def _load_json(path: Path | None) -> dict[str, Any] | None:
    if path is None or not Path(path).exists():
        return None
    return json.loads(Path(path).read_text(encoding="utf-8-sig"))


def _table_exists(con, name: str) -> bool:
    return bool(con.execute(
        "SELECT count(*) FROM information_schema.tables WHERE table_name = ?", [name]
    ).fetchone()[0])


def _candidate_query(
    *,
    state: str | None,
    bbox: str | None,
    min_score: float | None,
    min_beds: int | None,
    max_beds: int | None,
    services: list[str],
    require_all_services: bool,
    routing_refined: bool | None,
    limit: int,
    zoom: float | None,
    diversify: bool,
    score_expression: str = "stage8_score",
    score_params: list[Any] | None = None,
) -> tuple[str, list[Any]]:
    # Build ordinary row filters first. min_score is applied to query_score in
    # an outer CTE so custom weighted scores only need to be bound once.
    where, params = candidate_where(
        state=state, bbox=bbox, min_score=None, min_beds=min_beds, max_beds=max_beds,
        services=services, require_all_services=require_all_services,
        routing_refined=routing_refined, score_expression=score_expression,
    )
    score_params = score_params or []
    cell = grid_cell_degrees(zoom) if diversify else 0.0
    score_filter = ""
    outer_params: list[Any] = []
    if min_score is not None:
        score_filter = "WHERE query_score >= ?"
        outer_params.append(float(min_score))

    if cell > 0:
        sql = f"""
        WITH scored AS (
            SELECT *, ({score_expression}) AS query_score
            FROM final_candidates
            WHERE {where}
        ), score_filtered AS (
            SELECT * FROM scored {score_filter}
        ), diversified AS (
            SELECT *,
                   row_number() OVER (
                       PARTITION BY floor(longitude / {cell}), floor(latitude / {cell})
                       ORDER BY query_score DESC NULLS LAST
                   ) AS cell_rank
            FROM score_filtered
        )
        SELECT * EXCLUDE(cell_rank)
        FROM diversified
        WHERE cell_rank <= 2
        ORDER BY query_score DESC NULLS LAST
        LIMIT ?
        """
    elif diversify:
        # POST /optimize has no map zoom. Deduplicate the ranked pool by tract
        # here, then apply real distance separation in _fetch_candidates.
        sql = f"""
        WITH scored AS (
            SELECT *, ({score_expression}) AS query_score
            FROM final_candidates
            WHERE {where}
        ), score_filtered AS (
            SELECT * FROM scored {score_filter}
        ), tract_deduplicated AS (
            SELECT *, row_number() OVER (
                PARTITION BY coalesce(CAST(source_tract_geoid AS VARCHAR), CAST(site_id AS VARCHAR))
                ORDER BY query_score DESC NULLS LAST
            ) AS tract_rank
            FROM score_filtered
        )
        SELECT * EXCLUDE(tract_rank)
        FROM tract_deduplicated
        WHERE tract_rank = 1
        ORDER BY query_score DESC NULLS LAST
        LIMIT ?
        """
    else:
        sql = f"""
        WITH scored AS (
            SELECT *, ({score_expression}) AS query_score
            FROM final_candidates
            WHERE {where}
        )
        SELECT * FROM scored
        {score_filter}
        ORDER BY query_score DESC NULLS LAST
        LIMIT ?
        """
    return sql, list(score_params) + params + outer_params + [limit]


def _score_expression(weights: dict[str, float]) -> tuple[str, list[float], dict[str, float]]:
    allowed = {
        "access":"access_score",
        "capacity":"capacity_score",
        "vulnerability":"vulnerability_score",
        "configuration_fit":"configuration_fit_score",
        "cost_efficiency":"cost_efficiency_score",
        "drive_access":"drive_access_score",
        "service_fit":"service_fit_score",
    }
    total = sum(float(weights.get(k, 0)) for k in allowed)
    if total <= 0:
        raise HTTPException(status_code=400, detail={"code":"zero_weights","message":"At least one weight must be positive"})
    normalized = {k: float(weights.get(k, 0)) / total for k in allowed}
    terms = []
    params = []
    for key, col in allowed.items():
        if normalized[key] <= 0:
            continue
        terms.append(f"? * coalesce({col},0)")
        params.append(normalized[key])
    return "(" + " + ".join(terms) + ")", params, normalized


def _fetch_candidates(**kwargs) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    requested_limit = kwargs["limit"]
    distance_diversification = bool(kwargs.get("diversify") and kwargs.get("zoom") is None)
    query_kwargs = dict(kwargs)
    if distance_diversification:
        # Rank a broad pool before spreading it. SQL already removes the second
        # point from each tract, preventing near-duplicates from crowding out
        # otherwise eligible communities before the pool limit.
        query_kwargs["limit"] = min(30_000, max(1_000, requested_limit * 40))
    con = connect()
    try:
        sql, params = _candidate_query(**query_kwargs)
        df = con.execute(sql, params).fetchdf()
    finally:
        con.close()
    rows = df.to_dict(orient="records")
    separation = None
    if distance_diversification:
        rows, separation = diversify_ranked_rows(
            rows, requested_limit, bbox=parse_bbox(kwargs.get("bbox")), state=kwargs.get("state")
        )
    else:
        rows = rows[:requested_limit]
    features = [
        candidate_feature(row, rank=i + 1, score=scalar(row.get("query_score")))
        for i, row in enumerate(rows)
    ]
    meta = {
        "count":len(features),
        "truncated":len(features) >= requested_limit,
        "diversification_method": "tract_then_great_circle" if distance_diversification else None,
        "initial_separation_miles": round(separation, 1) if separation is not None else None,
        "ranked_pool_size": len(df) if distance_diversification else None,
    }
    return features, meta


@app.get(f"{API}/health", tags=["system"])
def health() -> dict[str, Any]:
    p = paths()
    con = connect()
    try:
        count = con.execute("SELECT count(*) FROM final_candidates").fetchone()[0]
    finally:
        con.close()
    return {
        "status":"ok",
        "api_version":"v1",
        "dataset_stage":8,
        "dataset_run_id":p["stage8_run_id"],
        "candidate_count":int(count),
    }


@app.get(f"{API}/meta", tags=["system"])
def meta() -> dict[str, Any]:
    p = paths()
    return {
        "api_version":"v1",
        "dataset_stage":8,
        "dataset_run_id":p["stage8_run_id"],
        "map_crs":"EPSG:4326",
        "score_scale":"0_to_1",
        "distance_unit":"miles",
        "time_unit":"minutes",
        "map_format":"GeoJSON",
        "final_candidate_source":"stage8/final_candidates.parquet",
        "score_components":[
            "access_score","capacity_score","vulnerability_score","configuration_fit_score",
            "cost_efficiency_score","drive_access_score","service_fit_score",
        ],
        "services":sorted(ALLOWED_SERVICES),
        "routing_models":{
            "approximate":"routing_refined=false; modeled travel from the Stage 6/8 approximation",
            "precise":"routing_refined=true; reserved for road-network-refined rows if added later",
        },
        "assumptions":_load_json(p.get("stage8_assumptions")),
    }


@app.get(f"{API}/summary", tags=["system"])
def summary() -> dict[str, Any]:
    p = paths()
    con = connect()
    try:
        row = con.execute("""
            SELECT
                count(*) AS sites,
                count(DISTINCT state_fips) AS states,
                sum(CASE WHEN coalesce(routing_refined,false) THEN 1 ELSE 0 END) AS routed,
                avg(stage8_score) AS mean_score,
                max(stage8_score) AS max_score,
                avg(proposed_beds) AS mean_beds,
                avg(recommended_service_count) AS mean_services
            FROM final_candidates
        """).fetchone()
    finally:
        con.close()
    return {
        "sites":int(row[0] or 0),
        "states":int(row[1] or 0),
        "routing_refined_sites":int(row[2] or 0),
        "mean_stage8_score":scalar(row[3]),
        "max_stage8_score":scalar(row[4]),
        "mean_proposed_beds":scalar(row[5]),
        "mean_recommended_services":scalar(row[6]),
        "stage8":_load_json(p.get("stage8_summary")),
        "stage7":_load_json(p.get("stage7_summary")),
    }


@app.get(f"{API}/states", tags=["lookup"])
def states_v1() -> dict[str, Any]:
    con = connect()
    try:
        df = con.execute("""
            SELECT
                lpad(CAST(state_fips AS VARCHAR), 2, '0') AS state_fips,
                count(*) AS candidate_count,
                min(longitude) AS west, min(latitude) AS south,
                max(longitude) AS east, max(latitude) AS north,
                max(stage8_score) AS best_score,
                avg(stage8_score) AS mean_score
            FROM final_candidates
            WHERE state_fips IS NOT NULL
            GROUP BY 1
            ORDER BY 1
        """).fetchdf()
    finally:
        con.close()

    out = []
    for row in df.to_dict(orient="records"):
        s = state_fields(row["state_fips"])
        s.update({
            "candidate_count":int(row["candidate_count"]),
            "bbox":[scalar(row["west"]), scalar(row["south"]), scalar(row["east"]), scalar(row["north"])],
            "best_score":scalar(row["best_score"]),
            "mean_score":scalar(row["mean_score"]),
        })
        out.append(s)
    return {"count":len(out), "data":out}


@app.get(f"{API}/services", tags=["lookup"])
def services_v1(state: str | None = None) -> dict[str, Any]:
    where, params = candidate_where(state=state)
    con = connect()
    try:
        rows = []
        for service in sorted(ALLOWED_SERVICES):
            score_col = f"service_{service}_score"
            rec_col = f"service_{service}_recommended"
            gap_col = f"service_{service}_gap"
            row = con.execute(
                f"""SELECT count(*) FILTER (WHERE coalesce({rec_col},false)),
                           count(*) FILTER (WHERE coalesce({gap_col},false)),
                           avg({score_col})
                    FROM final_candidates WHERE {where}""",
                params,
            ).fetchone()
            definition = con.execute(
                """SELECT any_value(service_name), any_value(minimum_beds),
                          any_value(recommendation_threshold), any_value(description)
                   FROM service_recommendations WHERE service_id = ?""",
                [service],
            ).fetchone()
            rows.append({
                "service_id":service,
                "service_name":definition[0] or service.replace("_", " ").title(),
                "minimum_beds":scalar(definition[1]),
                "recommendation_threshold":scalar(definition[2]),
                "description":definition[3],
                "recommended_sites":int(row[0] or 0),
                "gap_sites":int(row[1] or 0),
                "mean_score":scalar(row[2]),
            })
    finally:
        con.close()
    return {"count":len(rows), "data":rows}


@app.get(f"{API}/map/candidates", tags=["map"])
def map_candidates(
    bbox: str | None = Query(None, description="west,south,east,north"),
    state: str | None = None,
    zoom: float | None = Query(None, ge=0, le=24),
    limit: int | None = Query(None, ge=1, le=2000),
    min_score: float | None = Query(None, ge=0, le=1),
    min_beds: int | None = Query(None, ge=0),
    max_beds: int | None = Query(None, ge=0),
    service: list[str] = Query(default=[]),
    require_all_services: bool = False,
    routing_refined: bool | None = None,
    diversify: bool = True,
) -> dict[str, Any]:
    actual_limit = limit or default_limit_for_zoom(zoom)
    features, qmeta = _fetch_candidates(
        state=state, bbox=bbox, min_score=min_score, min_beds=min_beds, max_beds=max_beds,
        services=service, require_all_services=require_all_services, routing_refined=routing_refined,
        limit=actual_limit, zoom=zoom, diversify=diversify,
        score_expression="stage8_score", score_params=[],
    )
    return feature_collection(
        features,
        **qmeta,
        bbox=parse_bbox(bbox),
        state=state,
        zoom=zoom,
        score="stage8_score",
        diversified=diversify,
    )


@app.get(f"{API}/candidates/{{site_id}}", tags=["candidate"])
def candidate_detail(site_id: str, include_children: bool = True) -> dict[str, Any]:
    con = connect()
    try:
        df = con.execute("SELECT * FROM final_candidates WHERE CAST(site_id AS VARCHAR) = ? LIMIT 1", [site_id]).fetchdf()
        if df.empty:
            raise HTTPException(status_code=404, detail={"code":"candidate_not_found","message":f"No candidate {site_id}"})
        candidate = record(df.iloc[0].to_dict())
        if candidate.get("state_fips") is not None:
            candidate.update(state_fields(candidate["state_fips"]))
        county = (
            candidate.get("county_name") or candidate.get("source_county_name") or
            candidate.get("ruca_countyname23") or candidate.get("ruca_countyname20") or ""
        )
        if isinstance(county, str) and county.lower().endswith(" county"):
            county = county[:-7]
        candidate["county"] = county

        result: dict[str, Any] = {"candidate":candidate}
        if include_children:
            result["services"] = records(con.execute(
                """SELECT service_id, service_name, service_score, recommendation_threshold,
                          minimum_beds, recommended, archetype_includes, service_gap,
                          service_rank_within_site, description
                   FROM service_recommendations
                   WHERE CAST(site_id AS VARCHAR) = ?
                   ORDER BY service_rank_within_site, service_score DESC""",
                [site_id],
            ).fetchdf())
            result["access"] = records(con.execute(
                """SELECT minutes, modeled_drive_miles, modeled_straightline_radius_miles,
                          population, newly_accessible_population, existing_hospitals,
                          existing_beds, travel_time_model, routing_refined
                   FROM travel_access
                   WHERE CAST(site_id AS VARCHAR) = ?
                   ORDER BY minutes""",
                [site_id],
            ).fetchdf())
            if _table_exists(con, "candidate_hospitals"):
                result["configurations"] = records(con.execute(
                    """SELECT *
                       FROM candidate_hospitals
                       WHERE CAST(site_id AS VARCHAR) = ?
                       ORDER BY overall_score DESC NULLS LAST""",
                    [site_id],
                ).fetchdf())
        return result
    finally:
        con.close()


@app.get(f"{API}/candidates/{{site_id}}/services", tags=["candidate"])
def candidate_services(site_id: str) -> dict[str, Any]:
    con = connect()
    try:
        df = con.execute(
            """SELECT service_id, service_name, service_score, recommendation_threshold,
                      minimum_beds, size_eligible, recommended, archetype_includes,
                      service_gap, service_rank_within_site, description
               FROM service_recommendations
               WHERE CAST(site_id AS VARCHAR) = ?
               ORDER BY service_rank_within_site, service_score DESC""",
            [site_id],
        ).fetchdf()
    finally:
        con.close()
    return {"site_id":site_id, "count":len(df), "data":records(df)}


@app.get(f"{API}/candidates/{{site_id}}/access", tags=["candidate"])
def candidate_access(site_id: str) -> dict[str, Any]:
    con = connect()
    try:
        df = con.execute(
            """SELECT minutes, modeled_drive_miles, modeled_straightline_radius_miles,
                      population, newly_accessible_population, existing_hospitals,
                      existing_beds, travel_time_model, routing_refined
               FROM travel_access
               WHERE CAST(site_id AS VARCHAR) = ?
               ORDER BY minutes""",
            [site_id],
        ).fetchdf()
    finally:
        con.close()
    return {"site_id":site_id, "count":len(df), "data":records(df)}


@app.get(f"{API}/candidates/{{site_id}}/configurations", tags=["candidate"])
def candidate_configurations(site_id: str) -> dict[str, Any]:
    con = connect()
    try:
        if not _table_exists(con, "candidate_hospitals"):
            return {"site_id":site_id, "count":0, "data":[], "note":"Stage 7 candidate_hospitals.parquet not available"}
        df = con.execute(
            "SELECT * FROM candidate_hospitals WHERE CAST(site_id AS VARCHAR) = ? ORDER BY overall_score DESC NULLS LAST",
            [site_id],
        ).fetchdf()
    finally:
        con.close()
    return {"site_id":site_id, "count":len(df), "data":records(df)}


@app.post(f"{API}/optimize", tags=["optimization"])
def optimize_post(body: OptimizeRequest) -> dict[str, Any]:
    expression, score_params, normalized = _score_expression(body.weights.model_dump())
    t0 = time.perf_counter()
    features, qmeta = _fetch_candidates(
        state=body.state, bbox=body.bbox, min_score=body.min_score,
        min_beds=body.min_beds, max_beds=body.max_beds,
        services=body.services, require_all_services=body.require_all_services,
        routing_refined=body.routing_refined, limit=body.limit,
        zoom=None, diversify=body.diversify,
        score_expression=expression, score_params=score_params,
    )
    return {
        "candidates":feature_collection(features),
        "meta":{
            **qmeta,
            "weights":normalized,
            "compute_ms":round((time.perf_counter()-t0)*1000, 1),
            "state":body.state,
            "bbox":parse_bbox(body.bbox),
            "data_source":"stage8",
        },
    }


@app.get(f"{API}/map/hospitals", tags=["map"])
def map_hospitals(
    bbox: str | None = None,
    state: str | None = None,
    emergency_only: bool = False,
    limit: int = Query(5000, ge=1, le=20000),
) -> dict[str, Any]:
    con = connect()
    try:
        if not _table_exists(con, "facilities"):
            raise HTTPException(status_code=503, detail={"code":"stage5_unavailable","message":"Stage 5 facilities are unavailable"})
        cols = [r[0] for r in con.execute("DESCRIBE facilities").fetchall()]
        lat_col = next((c for c in ["latitude","lat","facility_latitude"] if c in cols), None)
        lon_col = next((c for c in ["longitude","lon","facility_longitude"] if c in cols), None)
        if not lat_col or not lon_col:
            raise HTTPException(status_code=503, detail={"code":"facility_coordinates_unavailable","message":"Facilities table has no latitude/longitude columns"})

        fallback_lat = "fallback_latitude" if "fallback_latitude" in cols else None
        fallback_lon = "fallback_longitude" if "fallback_longitude" in cols else None
        display_lat = f"coalesce({lat_col}, {fallback_lat})" if fallback_lat else lat_col
        display_lon = f"coalesce({lon_col}, {fallback_lon})" if fallback_lon else lon_col
        clauses = [f"{display_lat} IS NOT NULL", f"{display_lon} IS NOT NULL"]
        params: list[Any] = []
        if "facility_type" in cols:
            clauses.append("lower(CAST(facility_type AS VARCHAR)) = 'hospital'")
        box = parse_bbox(bbox)
        if box:
            west,south,east,north = box
            clauses += [f"{display_lon} BETWEEN ? AND ?", f"{display_lat} BETWEEN ? AND ?"]
            params += [west,east,south,north]

        try:
            sfips,_ = normalize_state(state)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail={"code":"invalid_state","message":str(exc)}) from exc
        if sfips:
            if "state_fips" in cols:
                clauses.append("lpad(CAST(state_fips AS VARCHAR),2,'0') = ?")
                params.append(sfips)
            elif "state" in cols:
                clauses.append("upper(CAST(state AS VARCHAR)) = ?")
                params.append(FIPS_TO_ABBR.get(sfips, sfips))

        if emergency_only:
            emergency_col = next((c for c in ["emergency_services","emergency","has_emergency"] if c in cols), None)
            if emergency_col:
                clauses.append(f"coalesce(CAST({emergency_col} AS BOOLEAN),false)")

        df = con.execute(
            f"SELECT * FROM facilities WHERE {' AND '.join(clauses)} LIMIT ?",
            params + [limit],
        ).fetchdf()
    finally:
        con.close()

    features = []
    for r in df.to_dict(orient="records"):
        authoritative = scalar(r.get(lat_col)) is not None and scalar(r.get(lon_col)) is not None
        lat = scalar(r.get(lat_col)) if authoritative else scalar(r.get(fallback_lat))
        lon = scalar(r.get(lon_col)) if authoritative else scalar(r.get(fallback_lon))
        props = record(r)
        props.pop(lat_col, None); props.pop(lon_col, None); props.pop("geometry", None)
        fid = str(props.get("facility_id") or props.get("id") or props.get("ccn") or len(features))
        # Stable presentation aliases keep the GeoJSON useful to any map client
        # without making it understand every upstream CMS column name.
        props.setdefault("name", props.get("facility_name") or props.get("hospital_name") or props.get("name") or "Hospital")
        props.setdefault("type", props.get("facility_type") or props.get("hospital_type") or "Hospital")
        props.setdefault("address", props.get("address") or props.get("street") or props.get("street_address") or "")
        props.setdefault("zip", props.get("zip") or props.get("zip_code") or "")
        props.setdefault("emergency", bool(props.get("emergency_services") or props.get("has_emergency") or False))
        props.setdefault("rating", props.get("rating") or props.get("hospital_overall_rating"))
        props.setdefault("loc_quality", props.get("geocode_method") if authoritative else props.get("fallback_method") or "approximate")
        props["counts_for_distance_model"] = authoritative
        features.append({"type":"Feature","id":fid,"geometry":{"type":"Point","coordinates":[lon,lat]},"properties":props})
    return feature_collection(features, count=len(features), bbox=parse_bbox(bbox), truncated=len(features)>=limit)


@app.get(f"{API}/map/population", tags=["map"])
def map_population(
    bbox: str | None = None,
    state: str | None = None,
    limit: int = Query(100000, ge=1, le=200000),
) -> dict[str, Any]:
    con = connect()
    try:
        if not _table_exists(con, "tracts"):
            raise HTTPException(status_code=503, detail={"code":"stage5_unavailable","message":"Stage 5 tracts are unavailable"})
        cols = [r[0] for r in con.execute("DESCRIBE tracts").fetchall()]
        lat_col = next((c for c in ["centroid_latitude","latitude","lat"] if c in cols), None)
        lon_col = next((c for c in ["centroid_longitude","longitude","lon"] if c in cols), None)
        geoid_col = next((c for c in ["tract_geoid","geoid","GEOID"] if c in cols), None)
        pop_col = next((c for c in ["population","total_population"] if c in cols), None)
        dist_col = next((c for c in ["distance_to_nearest_hospital_miles","nearest_hospital_miles"] if c in cols), None)
        if not lat_col or not lon_col or not pop_col:
            raise HTTPException(status_code=503, detail={"code":"tract_columns_unavailable","message":"Stage 5 tracts lack required centroid/population fields"})

        clauses = [f"{lat_col} IS NOT NULL", f"{lon_col} IS NOT NULL", f"coalesce({pop_col},0) > 0"]
        params: list[Any] = []
        box = parse_bbox(bbox)
        if box:
            west,south,east,north = box
            clauses += [f"{lon_col} BETWEEN ? AND ?", f"{lat_col} BETWEEN ? AND ?"]
            params += [west,east,south,north]

        try:
            sfips,_ = normalize_state(state)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail={"code":"invalid_state","message":str(exc)}) from exc
        if sfips and geoid_col:
            clauses.append(f"substr(CAST({geoid_col} AS VARCHAR),1,2) = ?")
            params.append(sfips)

        select_dist = f"{dist_col} AS distance_to_nearest_hospital_miles" if dist_col else "NULL AS distance_to_nearest_hospital_miles"
        df = con.execute(
            f"""SELECT {lat_col} AS latitude, {lon_col} AS longitude,
                       {pop_col} AS population, {select_dist}
                FROM tracts WHERE {' AND '.join(clauses)} LIMIT ?""",
            params + [limit],
        ).fetchdf()
    finally:
        con.close()

    features = []
    for i,r in enumerate(df.to_dict(orient="records")):
        features.append({
            "type":"Feature",
            "geometry":{"type":"Point","coordinates":[scalar(r["longitude"]),scalar(r["latitude"])]},
            "properties":{
                "population":scalar(r["population"]),
                "distance_to_nearest_hospital_miles":scalar(r["distance_to_nearest_hospital_miles"]),
            },
        })
    return feature_collection(features, count=len(features), bbox=parse_bbox(bbox), truncated=len(features)>=limit)


# ---------------- compatibility routes for the supplied React package ----------------

@app.get("/api/health", include_in_schema=False)
def compat_health():
    return health()


@app.get("/api/states", include_in_schema=False)
def compat_states():
    data = states_v1()["data"]
    return {
        "states":[
            {
                "state":r["state"],
                "name":r["name"],
                "bbox":r["bbox"],
                "candidates":r["candidate_count"],
            }
            for r in data if r.get("state")
        ]
    }


@app.get("/api/hospitals", include_in_schema=False)
def compat_hospitals(state: str | None = None):
    fc = map_hospitals(state=state, limit=20000)
    # Normalize common Stage 5 names into the current frontend's popup keys.
    for f in fc["features"]:
        p = f["properties"]
        p.setdefault("name", p.get("facility_name") or p.get("hospital_name") or "Hospital")
        p.setdefault("type", p.get("facility_type") or p.get("hospital_type") or "Hospital")
        p.setdefault("address", p.get("address") or p.get("street_address") or "")
        p.setdefault("city", p.get("city") or "")
        p.setdefault("state", p.get("state") or "")
        p.setdefault("zip", p.get("zip") or p.get("zip_code") or "")
        p.setdefault("phone", p.get("phone") or "")
        p.setdefault("ownership", p.get("ownership") or "")
        p.setdefault("emergency", bool(p.get("emergency_services") or p.get("has_emergency") or False))
        p.setdefault("rating", p.get("rating") or p.get("overall_rating"))
        p.setdefault("counts_for_coverage", True)
        p.setdefault("loc_quality", "exact")
    fc.pop("meta", None)
    return fc


@app.get("/api/population", include_in_schema=False)
def compat_population(state: str | None = None):
    fc = map_population(state=state, limit=200000)
    for f in fc["features"]:
        p = f["properties"]
        p["p"] = p.get("population") or 0
        p["d"] = p.get("distance_to_nearest_hospital_miles") or 0
    fc.pop("meta", None)
    return fc


@app.get("/api/optimize", include_in_schema=False)
def compat_optimize(
    w_population: float = Query(0.4, ge=0, le=1),
    w_distance: float = Query(0.3, ge=0, le=1),
    w_shortage: float = Query(0.2, ge=0, le=1),
    w_cost: float = Query(0.1, ge=0, le=1),
    radius: int = Query(30, ge=5, le=100),
    k: int = Query(5, ge=1, le=100),
    state: str | None = None,
    include_hospitals: bool = False,
):
    # Compatibility mapping:
    # population -> new access, distance -> drive access,
    # shortage -> equal blend of capacity/vulnerability, cost -> cost efficiency.
    raw = {
        "population":w_population,
        "distance":w_distance,
        "shortage":w_shortage,
        "cost":w_cost,
    }
    total = sum(raw.values())
    if total <= 0:
        raise HTTPException(status_code=400, detail="At least one weight must be positive")

    expr = """(
        ? * coalesce(stage8_new_access_30min_score, coalesce(new_access_score, access_score, 0))
      + ? * coalesce(drive_access_score, 0)
      + ? * ((coalesce(capacity_score,0) + coalesce(vulnerability_score,0)) / 2.0)
      + ? * coalesce(cost_efficiency_score,0)
    )"""
    score_params = [w_population/total, w_distance/total, w_shortage/total, w_cost/total]

    t0 = time.perf_counter()
    features, qmeta = _fetch_candidates(
        state=state, bbox=None, min_score=None, min_beds=None, max_beds=None,
        services=[], require_all_services=False, routing_refined=None,
        limit=k, zoom=None, diversify=True,
        score_expression=expr, score_params=score_params,
    )

    # Add factor aliases expected by the existing popup/results components.
    for f in features:
        p = f["properties"]
        p["f_population"] = scalar(p.get("stage8_new_access_30min_score") or p.get("new_access_score") or p.get("access_score") or 0)
        p["f_distance"] = scalar(p.get("drive_access_score") or 0)
        p["f_shortage"] = scalar(((p.get("capacity_score") or 0) + (p.get("vulnerability_score") or 0)) / 2)
        p["f_cost"] = scalar(p.get("cost_efficiency_score") or 0)
        p["coverage_radius_mi"] = radius  # compatibility only; Stage 8 uses time windows, not a literal coverage circle.

    response = {
        "candidates":feature_collection(features),
        "meta":{
            **qmeta,
            "weights":raw,
            "radius":radius,
            "k":k,
            "state":state,
            "compute_ms":round((time.perf_counter()-t0)*1000,1),
            "compatibility_mode":True,
            "note":"radius is retained for the old UI; final Stage 8 scoring uses drive-time/access features rather than this radius.",
        },
    }
    if include_hospitals:
        response["hospitals"] = compat_hospitals(state)
    return response


# In a packaged demo the API can serve the Vite build on the same origin. In
# development Vite still owns port 5173 and proxies /api to this application.
WEB_DIST = Path(__file__).resolve().parents[3] / "web" / "dist"
if WEB_DIST.is_dir():
    assets = WEB_DIST / "assets"
    if assets.is_dir():
        app.mount("/assets", StaticFiles(directory=assets), name="web-assets")

    @app.get("/{web_path:path}", include_in_schema=False)
    def serve_web(web_path: str):
        if web_path == "api" or web_path.startswith("api/"):
            raise HTTPException(status_code=404, detail="API endpoint not found")
        requested = (WEB_DIST / web_path).resolve()
        try:
            requested.relative_to(WEB_DIST.resolve())
        except ValueError:
            raise HTTPException(status_code=404, detail="Not found")
        if web_path and requested.is_file():
            return FileResponse(requested)
        return FileResponse(WEB_DIST / "index.html")
