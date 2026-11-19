// Where the frontend finds the API. Empty = same origin, which covers both
// the Python server (it serves the built site) and `npm run dev` (Vite
// proxies /api to the Python server). To use an API somewhere else, e.g. the
// /algorithm server, build with VITE_API_BASE=http://127.0.0.1:8001.
export const API_BASE = import.meta.env.VITE_API_BASE ?? "";

// Free OpenStreetMap basemaps, no API key needed. Both use the same map
// tiles and fonts; only the colours differ.
export const BASEMAP_STYLES = {
  light: "https://tiles.openfreemap.org/styles/positron",
  dark: "https://tiles.openfreemap.org/styles/dark",
};

// Satellite and aerial photos from the U.S. Geological Survey (public domain).
// Covers all 50 states; tiles go up to zoom 16 and are enlarged beyond that.
// Outside the U.S. the service returns blank tiles.
export const SATELLITE = {
  tiles: ["https://basemap.nationalmap.gov/arcgis/rest/services/USGSImageryOnly/MapServer/tile/{z}/{y}/{x}"],
  maxzoom: 16,
  attribution:
    'Imagery: <a href="https://www.usgs.gov/programs/national-geospatial-program/national-map" ' +
    'target="_blank" rel="noopener noreferrer">USGS The National Map</a>',
};

// This repository: the hackathon prototype plus later work. The team's original
// repository is linked from the README and the "Original Hackathon Prototype" release.
export const GITHUB_URL = "https://github.com/romanwambugu/medmap";
