import { GITHUB_URL } from "../config.js";

const TEAM = [
  {
    name: "Roman Wambugu",
    role: "Built the interactive map and other major frontend features, and led our pitch.",
    link: "https://github.com/romanwambugu",
  },
  {
    name: "Oliver Lemley",
    role: "Built the data pipeline, the API and the scoring algorithm.",
    link: "https://github.com/orlemley",
  },
  {
    name: "Colin Nichols",
    role: "Set up Docker so MedMap runs the same on any machine, and fixed the site-clustering bug the first time it hit.",
    link: "https://github.com/clnichols",
  },
];

const BUILT_WITH = [
  ["React + Vite", "Components, production bundling, and the interactive planning interface."],
  ["MapLibre GL JS", "Open-source map rendering in the browser."],
  ["OpenFreeMap", "Free OpenStreetMap basemap tiles."],
  ["FastAPI + DuckDB", "A typed API over the processed Parquet datasets and scoring model."],
  ["USGS National Map", "Public-domain satellite and aerial photos."],
  ["U.S. Census Geocoder + OpenStreetMap", "Layered, provenance-preserving hospital location matching."],
];

export default function AboutPage() {
  return (
    <>
      <title>About MedMap</title>
      <main className="page">
        <p className="eyebrow">About</p>
        <h1>The team</h1>
        <p className="lead">
          MedMap was built in 36 hours at TigerHacks 2026, where it won 1st place in the Developer Track.
        </p>

        <div className="card-grid">
          {TEAM.map((m) => (
            <div className="card" key={m.name}>
              <h3>{m.name}</h3>
              <p>{m.role}</p>
              {m.link && <p><a href={m.link}>{m.link.replace("https://", "")}</a></p>}
            </div>
          ))}
        </div>

        <h2>Source code</h2>
        <p>
          Everything is on GitHub: <a href={GITHUB_URL}>{GITHUB_URL.replace("https://", "")}</a>. The README explains
          how to run the map locally, what is in each dataset, and exactly how sites are scored.
        </p>

        <h2>Built with</h2>
        <div className="card-grid">
          {BUILT_WITH.map(([title, text]) => (
            <div className="card" key={title}>
              <h3>{title}</h3>
              <p>{text}</p>
            </div>
          ))}
        </div>
      </main>
      <footer className="site-footer">MedMap · TigerHacks26</footer>
    </>
  );
}
