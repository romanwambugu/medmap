import { fmt, oneDecimal, points } from "../../lib/format.js";
import { scoreBreakdown, topReasons } from "../../lib/weights.js";

// How each hospital's position was found (Stage 3 ETL; see
// docs/hospital-locations.md). Approximate ones are drawn as hollow rings.
const LOCATION_NOTES = {
  address: { text: "Located from its street address (U.S. Census Geocoder)." },
  address_alt: { text: "Located from its street address in CMS's Provider of Services file (U.S. Census Geocoder)." },
  osm: { text: "Located from OpenStreetMap's map of this hospital." },
  census_street_interpolation: { text: "Located from its street address (U.S. Census Geocoder)." },
  census_tie_resolved: { text: "Located by resolving an ambiguous U.S. Census Geocoder address match." },
  census_alternate_address: { text: "Located from an alternate address linked by its exact CMS identifier." },
  openstreetmap_hospital: { text: "Located from a conservatively matched OpenStreetMap hospital feature." },
  zcta: { approximate: true, text: "Approximate location: its address couldn't be matched to a map, so it's shown at the center of its ZIP code." },
  zcta_centroid: { approximate: true, text: "Approximate display location: no authoritative point was found, so it is shown at its ZIP area's center and excluded from access calculations." },
  zip3: { approximate: true, text: "Approximate location: its address couldn't be matched to a map, so it's shown at the center of a nearby ZIP code." },
  county: { approximate: true, text: "Approximate location: its address couldn't be matched to a map, so it's shown at the center of its county." },
};

function HospitalItem({ h }) {
  return (
    <div className="popup-item">
      <h3>{h.name}</h3>
      <p className="sub">{h.type}</p>
      <dl>
        <dt>Address</dt>
        <dd>{h.address}<br />{h.city}, {h.state} {h.zip}</dd>
        <dt>Phone</dt><dd>{h.phone}</dd>
        <dt>Ownership</dt><dd>{h.ownership}</dd>
        <dt>Emergency</dt><dd>{h.emergency ? "Yes" : "No"}</dd>
        <dt>CMS rating</dt><dd>{h.rating ? `${h.rating} / 5` : "Not rated"}</dd>
      </dl>
      {!h.counts_for_coverage && <p className="caveat">Not counted as existing coverage when scoring sites.</p>}
      {LOCATION_NOTES[h.loc_quality] && (
        <p className={LOCATION_NOTES[h.loc_quality].approximate ? "caveat" : "location-note"}>
          {LOCATION_NOTES[h.loc_quality].text}
        </p>
      )}
    </div>
  );
}

export function HospitalPopup({ hospitals }) {
  const many = hospitals.length > 1;
  return (
    <div className="popup">
      {many && <p className="sub">{hospitals.length} hospitals at this spot.</p>}
      <div className={many ? "popup-list multi" : "popup-list"}>
        {hospitals.map((h) => <HospitalItem key={h.id} h={h} />)}
      </div>
    </div>
  );
}

/** "Mainly because A, and B." from the site's strongest factors. */
function WhyThisSite({ reasons }) {
  let text;
  if (reasons.length === 2) text = `Mainly because ${reasons[0].reason}, and ${reasons[1].reason}.`;
  else if (reasons.length === 1) text = `Mainly because ${reasons[0].reason}.`;
  else text = "No single factor stands out: it earns moderate points across several.";
  return (
    <div className="why-site">
      <h4>Why this site?</h4>
      <p>{text}</p>
    </div>
  );
}

export function CandidatePopup({ site, weights }) {
  const rows = scoreBreakdown(site, weights);
  const reasons = topReasons(rows);
  const flags = [site.in_mua && "MUA/P", site.in_hpsa && "HPSA"].filter(Boolean).join(", ") || "None";
  const minutesSaved = site.population_weighted_mean_minutes_saved;
  const nearestMiles = site.nearest_existing_hospital_estimated_drive_miles ?? site.nearest_hospital_mi;
  return (
    <div className="popup">
      <h3>#{site.rank}: {site.county} County, {site.state}</h3>
      <p className="sub">Census tract {site.tract_id} · {site.proposed_beds ?? "—"} proposed beds</p>
      <WhyThisSite reasons={reasons} />
      <div className="breakdown">
        {rows.map((row) => (
          <div
            className={reasons.includes(row) ? "breakdown-row top" : "breakdown-row"}
            key={row.key}
            title={`${row.label}: this site scores ${points(row.factor)} of 100 on it, and it counts for ${Math.round(row.share * 100)}% of the score`}
          >
            <span>{row.label}</span>
            <span className="breakdown-bar"><span style={{ width: `${points(row.factor)}%` }} /></span>
            <span className="breakdown-value">+{row.points}</span>
          </div>
        ))}
        <div className="total"><span>Score</span><span>{points(site.score)} / 100</span></div>
      </div>
      <dl>
        <dt>New 30-min access</dt><dd>{fmt(site.newly_accessible_population_30min ?? site.uncovered_population)} people</dd>
        <dt>Time saved</dt><dd>{minutesSaved != null ? oneDecimal(minutesSaved) : "—"} min average</dd>
        <dt>Nearest hospital</dt><dd>{nearestMiles != null ? oneDecimal(nearestMiles) : "—"} mi</dd>
        <dt>Shortage areas</dt><dd>{flags}</dd>
        <dt>Density</dt><dd>{fmt(site.density_per_sq_mi)} / sq mi{site.density_imputed ? " (estimated)" : ""}</dd>
        <dt>Tract population</dt><dd>{fmt(site.tract_population)}</dd>
      </dl>
      <p className="caveat">
        Each bar shows how this site rates on that factor; the number is the points it adds to the score.
        Select this site for services, access bands and configurations.
        The circle is a representative planning point for an area, not a surveyed building parcel.
      </p>
    </div>
  );
}
