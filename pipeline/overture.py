#!/usr/bin/env python3
"""
Second data source: Overture Maps places (Meta, Microsoft, Foursquare and others; open license).
Pulls every eating place in the world straight from Overture's public S3 bucket with DuckDB,
writing the same places.jsonl format as extract.py. tile.py then merges it with OpenStreetMap,
dropping Overture entries that duplicate an OSM place (same name, same spot) so OSM's richer
tags win when both know a restaurant.

Overture gives us: name, location, category (-> type and cuisine), brand (-> chain), address, website.
It does not have hours, prices or diet tags, so those stay empty for Overture-only places.

Usage: overture.py OUTPUT.jsonl [--release 2026-09-23.1] [--bbox west,south,east,north]
Env:   OVERTURE_RELEASE overrides the default release.
Needs: pip install duckdb
"""
import argparse, json, os, re, sys, time
import duckdb

DEFAULT_RELEASE = os.environ.get("OVERTURE_RELEASE", "2026-09-23.1")

# Overture category -> our kind. Anything matching EAT that isn't listed falls back to "restaurant".
EAT = re.compile(r"(restaurant|cafe|coffee|tea_room|teahouse|bar$|_bar$|pub$|brewpub|gastropub|brewery|taproom|"
                 r"fast_food|food_court|food_truck|diner|bistro|pizzeria|pizza|taqueria|steakhouse|ramen|sushi|"
                 r"noodle|burger|sandwich|\bdeli$|_deli$|^deli_|bbq|barbecue|\bgrill$|_grill$|cafeteria|ice_cream|dessert|donut|bagel|"
                 r"juice|smoothie|bubble_tea|wine_bar|cocktail|lounge|izakaya|bakery_cafe)", re.I)
KIND_RULES = [
    (re.compile(r"cafe|coffee|tea|ice_cream|dessert|donut|bagel|juice|smoothie|bubble_tea|bakery_cafe", re.I), "cafe"),
    (re.compile(r"bar|pub|brew|taproom|cocktail|lounge|izakaya|wine", re.I), "bar"),
    (re.compile(r"fast_food|food_court|food_truck|pizza|burger|sandwich|taqueria|donut|bagel|deli", re.I), "fast_food"),
]
CHEAP = re.compile(r"pizza|burger|sandwich|taco|hot ?dog|chicken|donut|doughnut|bagel|ice_cream|coffee|fried|wings|sub|deli|diner|noodle|bubble_tea|juice|smoothie|kebab|falafel|shawarma|fish_and_chips|dumpling|ramen", re.I)
PRICEY = re.compile(r"steak|fine|french|sushi|seafood|oyster|tapas|omakase|wine|fine_dining", re.I)
JUNK = re.compile(r"(?<!pizza_)delivery|courier|catering|supplier|supplies|wholesale|equipment|remodel|contractor|rental|manufactur", re.I)
STRIP = re.compile(r"_(restaurant|bar|cafe|shop|place|house|joint|spot|stand|truck|parlor|parlour)$")


def kind_for(cat):
    for rx, k in KIND_RULES:
        if rx.search(cat):
            return k
    return "restaurant"


def cuisine_for(cat):
    c = STRIP.sub("", cat.replace("_delivery_service", ""))
    if c in ("restaurant", "cafe", "bar", "pub", "fast_food", "food_court", "coffee", "diner", "bistro", "eat_and_drink"):
        return ""
    return c.replace("_", " ")[:30]


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("out")
    ap.add_argument("--release", default=DEFAULT_RELEASE)
    ap.add_argument("--bbox", help="west,south,east,north (testing)")
    ap.add_argument("--src", help="override the parquet path/glob (testing)")
    a = ap.parse_args()

    src = a.src or f"s3://overturemaps-us-west-2/release/{a.release}/theme=places/type=place/*"
    con = duckdb.connect()
    con.execute("INSTALL httpfs; LOAD httpfs; SET s3_region='us-west-2';")
    con.execute("SET enable_object_cache=true; SET threads=4;")

    # The category column was renamed in 2026 (categories -> taxonomy); handle both.
    desc = con.execute(f"DESCRIBE SELECT * FROM read_parquet('{src}', hive_partitioning=1) LIMIT 0").fetchall()
    cols = {r[0]: str(r[1]) for r in desc}
    if "taxonomy" in cols and "primary" in cols["taxonomy"]:
        cat = "taxonomy.primary"
    elif "categories" in cols:
        cat = "categories.primary"
    elif "basic_category" in cols:
        cat = "basic_category"
    else:
        sys.exit("Can't find a category column in this Overture release; columns: " + ", ".join(sorted(cols)))
    print(f"Using category column {cat}", file=sys.stderr)
    status = "operating_status" if "operating_status" in cols else "NULL"
    brand_e = "brand.names.primary" if "names" in cols.get("brand", "") else "NULL"
    phone_e = "phones[1]" if "[]" in cols.get("phones", "") else "NULL"
    web_e = "websites[1]" if "[]" in cols.get("websites", "") else "NULL"
    addr_e = "addresses[1].freeform" if "freeform" in cols.get("addresses", "") else "NULL"
    where = [f"regexp_matches({cat}, '{EAT.pattern}', 'i')", f"NOT regexp_matches({cat}, 'courier|catering|supplier|supplies|wholesale|equipment|remodel|contractor|rental|manufactur', 'i')",
             "names.primary IS NOT NULL", "confidence >= 0.5"]
    if a.bbox:
        w, s, e, n = map(float, a.bbox.split(","))
        where.append(f"bbox.xmin >= {w} AND bbox.xmax <= {e} AND bbox.ymin >= {s} AND bbox.ymax <= {n}")
    q = f"""
      SELECT id, names.primary AS name, (bbox.ymin + bbox.ymax)/2 AS lat, (bbox.xmin + bbox.xmax)/2 AS lon,
             {cat} AS cat, {brand_e} AS brand, {web_e} AS website,
             {addr_e} AS addr, {status} AS status, {phone_e} AS phone
      FROM read_parquet('{src}', hive_partitioning=1)
      WHERE {" AND ".join(where)}
    """
    print(f"Overture {a.release}: querying… (this streams a few GB from S3)", file=sys.stderr, flush=True)
    t0 = time.time()
    n = 0
    with open(a.out, "w", encoding="utf-8") as out:
        cur = con.execute(q)
        while True:
            rows = cur.fetchmany(50000)
            if not rows:
                break
            for oid, name, lat, lon, cat_v, brand, website, addr, status_v, phone in rows:
                if status_v and status_v != "open":
                    continue
                if lat is None or lon is None or not name:
                    continue
                cat_v = cat_v or ""
                if JUNK.search(cat_v):   # delivery/courier services, suppliers, remodelers that mention food words
                    continue
                kind = kind_for(cat_v)
                cuisine = cuisine_for(cat_v)
                chain = 1 if brand else 0
                if kind in ("fast_food", "cafe"):
                    price = 1
                elif PRICEY.search(cat_v):
                    price = 3
                elif CHEAP.search(cat_v):
                    price = 1 if chain else 2
                else:
                    price = 2
                website = website or ""
                if len(website) > 120 or not website.startswith("http"):
                    website = ""
                # Overture ids are 32-hex GERS ids; keep them short but unique enough.
                rec = [f"o{oid[:16]}", name[:80], round(lat, 5), round(lon, 5), kind, chain, price, 1,
                       cuisine, "", "", (addr or "")[:60], website, "", "", (phone or "")[:24]]
                out.write(json.dumps(rec, ensure_ascii=False, separators=(",", ":")) + "\n")
                n += 1
                if n % 100000 == 0:
                    print(f"  {n:,} places", file=sys.stderr, flush=True)
    print(f"{n:,} Overture places in {time.time() - t0:.0f}s -> {a.out}", file=sys.stderr)


if __name__ == "__main__":
    main()
