# Lucky Bite

Pull the handle, get a restaurant. Works anywhere on Earth, from one codebase, on Android, iPhone, Windows and Mac (as an installable web app).

## How it works

- `index.html` is the whole app. No build step, no framework.
- Restaurant data comes from OpenStreetMap merged with Overture Maps (Meta, Microsoft, Foursquare and other open listings),
  prebuilt into small map tiles (`data/tiles/*.json`) so the app loads instantly.
  Tiles are cached on the device after first use. If no tiles are available (for example when you open `index.html` straight from
  your disk), the app falls back to a live OpenStreetMap search, which is slower.
- `pipeline/` turns OpenStreetMap extracts (`extract.py`) and Overture's places dataset (`overture.py`) into those tiles
  (`tile.py`, which also drops Overture entries that duplicate an OSM place so OSM's richer tags win). `.github/workflows/build-data.yml` runs it for the whole world on
  GitHub's free runners, monthly, and publishes app + data to GitHub Pages.

## Set up hosting (one time, about 5 minutes)

1. Create a new GitHub repository (public is fine; Pages is free for public repos) and push this folder to its `main` branch.
2. In the repository: **Settings → Pages → Build and deployment → Source: GitHub Actions**.
3. **Actions** tab → **Build and deploy** → **Run workflow** (leave regions blank for the whole world).
   The first run extracts every continent in parallel; expect 1–3 hours. After that, it refreshes itself monthly.
   To test quickly first, run it with regions set to `north-america/us/kentucky` (a few minutes).
4. The workflow's summary shows your URL, like `https://<you>.github.io/<repo>/`. Open it on your phone and use
   "Add to Home Screen" (iPhone: Share menu; Android: Chrome menu) to install it as an app.

Pushing a change to `index.html` redeploys in a couple of minutes without rebuilding the data.

## Run the pipeline locally instead

```
brew install osmium-tool   # or: sudo apt install osmium-tool
pip install osmium
pipeline/build.sh north-america/us/kentucky work
pip install duckdb && python3 pipeline/overture.py work/places-overture.jsonl --bbox -89.6,36.5,-81.9,39.2   # optional, Kentucky box
python3 pipeline/tile.py data work/places-*.jsonl
python3 -m http.server 8000      # then open http://localhost:8000
```

## Data notes

- Price (`$`–`$$$$`) comes from OSM's `price_range` tag when present; otherwise it's estimated from the type and cuisine and
  shown with a `?`.
- "Chain" means OSM has a `brand` tag. Big chains are reliably tagged; small regional ones often aren't.
- Badges (vegan, vegetarian, pescatarian, halal, kosher, gluten-free, lactose-free, takeout, drive-through, outdoor seating, wheelchair) appear only when OSM says so; no badge means unknown, not no.
- "Delivers" relies on OSM's `delivery` tag, which is filled in for only a fraction of places.
- OSM coverage is excellent in North America, Europe, Japan, Australia; thinner in parts of Latin America, Africa and Asia.

## Next steps when it's time for the app stores

Wrap this same code with [Capacitor](https://capacitorjs.com) to produce native Android and iOS builds. Nothing in the app
needs to change.

Data © OpenStreetMap contributors (ODbL) and Overture Maps Foundation (CDLA Permissive 2.0 / Apache 2.0). Keep the
attribution line in the footer. Overture-only places have no hours, price or diet tags; the synopsis says so.

The Overture script defaults to the `2026-09-23.1` release; set `OVERTURE_RELEASE` (or `--release`) to use a newer one.
