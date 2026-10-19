#!/usr/bin/env python3
"""
Step 1 of the Lucky Bite data pipeline: OSM PBF -> places.jsonl (one JSON array per line).

Works on a PBF that osmium tags-filter has already reduced to eating places (fast), or on a
raw extract (slow but correct). Run it once per continent; tile.py merges the outputs.

Usage: extract.py INPUT.osm.pbf OUTPUT.jsonl

Record (array; positions matter, the app reads them by index):
  [osmId, name, lat, lon, kind, chain, price, priceEstimated, cuisine, hours, delivery, address, website]
    kind      "restaurant" | "fast_food" | "cafe" | "bar"
    chain     0 | 1          (OSM brand tag present)
    price     1..4           ($..$$$$)
    priceEst  0 from OSM price_range | 1 guessed from type and cuisine
    delivery  "" | "yes" | "no" | "only"
    badges    comma-separated codes, only when OSM says so (see BADGES); "!" suffix = "only"
              e.g. "vg!,gf,tk" = vegan-only, gluten-free options, takeout
    menu      URL of the menu when OSM has one (website:menu), else ""
    phone     phone number as written in OSM, else ""
    locality  "City, ST 12345" from addr:city / addr:state / addr:postcode, as much as is known
"""
import json, re, sys, time
import osmium

KINDS = {"restaurant": "restaurant", "fast_food": "fast_food", "cafe": "cafe", "bar": "bar", "pub": "bar",
         "food_court": "fast_food", "biergarten": "bar", "ice_cream": "cafe"}
CHEAP = re.compile(r"pizza|burger|sandwich|taco|hot ?dog|chicken|donut|doughnut|bagel|ice_cream|coffee|fried|wings|sub|deli|diner|noodle|bubble_tea|juice|smoothie|kebab|falafel|shawarma|fish_and_chips|dumpling|ramen", re.I)
PRICEY = re.compile(r"steak|fine|french|sushi|seafood|oyster|tapas|omakase|wine|fine_dining", re.I)
# (tag, code). Diet tags take yes/only; the rest take yes. Shown on the result card only when present.
BADGES = [("diet:vegan", "vg"), ("diet:vegetarian", "ve"), ("diet:pescetarian", "pe"), ("diet:halal", "ha"),
          ("diet:kosher", "ko"), ("diet:gluten_free", "gf"), ("diet:lactose_free", "lf"),
          ("takeaway", "tk"), ("drive_through", "dt"), ("outdoor_seating", "os"), ("wheelchair", "wc")]


KID_TAGS = ("kids_area", "highchair", "changing_table", "kids_menu", "playground", "kids")
DRINK_TAGS = ("drink:beer", "drink:wine", "drink:cocktail", "drink:liquor", "drink:spirits", "bar", "alcohol")


def badges_for(tags):
    out = []
    for tag, code in BADGES:
        v = tags.get(tag, "")
        if v == "only" and tag.startswith("diet:"):
            out.append(code + "!")
        elif v == "yes":
            out.append(code)
    if any(tags.get(t) == "yes" for t in KID_TAGS):
        out.append("kf")
    amenity = tags.get("amenity", "")
    if amenity in ("bar", "pub", "biergarten") or tags.get("microbrewery") == "yes" or tags.get("brewery") not in (None, "", "no") \
            or any(tags.get(t) in ("yes", "served") for t in DRINK_TAGS):
        out.append("al")
    if tags.get("reservation") in ("yes", "recommended", "required"):
        out.append("rv" + ("!" if tags.get("reservation") == "required" else ""))
    if tags.get("takeaway") == "only":
        out = [b for b in out if b != "tk"] + ["tk!"]
    return ",".join(out)


def normalize(osm_id, tags, lat, lon):
    name = tags.get("name") or tags.get("name:en") or tags.get("brand")
    if not name:
        return None
    kind = KINDS.get(tags.get("amenity", ""))
    if not kind:
        return None
    chain = 1 if (tags.get("brand") or tags.get("brand:wikidata")) else 0
    pr = re.search(r"\$+", tags.get("price_range", ""))
    cuisine = tags.get("cuisine", "")
    if pr:
        price, est = min(4, len(pr.group(0))), 0
    else:
        est = 1
        if kind in ("fast_food", "cafe"):
            price = 1
        elif PRICEY.search(cuisine):
            price = 3
        elif CHEAP.search(cuisine):
            price = 1 if chain else 2
        else:
            price = 2
    addr = " ".join(x for x in (tags.get("addr:housenumber"), tags.get("addr:street")) if x)
    city = tags.get("addr:city", ""); state = tags.get("addr:state", ""); zipc = tags.get("addr:postcode", "")
    locality = ", ".join(x for x in (city, " ".join(y for y in (state, zipc) if y)) if x)[:60]
    website = tags.get("website") or tags.get("contact:website") or ""
    if len(website) > 120 or not website.startswith("http"):
        website = ""
    delivery = tags.get("delivery", "")
    phone = (tags.get("phone") or tags.get("contact:phone") or tags.get("contact:mobile") or "").split(";")[0].strip()[:24]
    menu = tags.get("website:menu") or tags.get("contact:menu") or tags.get("menu:url") or ""
    if len(menu) > 160 or not menu.startswith("http"):
        menu = ""
    return [
        osm_id, name[:80], round(lat, 5), round(lon, 5), kind, chain, price, est,
        cuisine.split(";")[0].replace("_", " ")[:30],
        tags.get("opening_hours", "")[:160],
        delivery if delivery in ("yes", "no", "only") else "",
        addr[:60], website, badges_for(tags), menu, phone, locality,
    ]


class Extractor(osmium.SimpleHandler):
    def __init__(self, out):
        super().__init__()
        self.out, self.n = out, 0

    def emit(self, osm_id, tags, lat, lon):
        rec = normalize(osm_id, dict(tags), lat, lon)
        if rec:
            self.out.write(json.dumps(rec, ensure_ascii=False, separators=(",", ":")) + "\n")
            self.n += 1
            if self.n % 100000 == 0:
                print(f"  {self.n:,} places", file=sys.stderr, flush=True)

    def node(self, n):
        if "amenity" in n.tags and n.location.valid():
            self.emit(f"n{n.id}", n.tags, n.location.lat, n.location.lon)

    def area(self, a):  # restaurants mapped as buildings or multipolygons
        if "amenity" not in a.tags:
            return
        try:
            pts = [(p.lat, p.lon) for p in next(a.outer_rings()) if p.location.valid()]
        except Exception:
            return
        if not pts:
            return
        lat = sum(p[0] for p in pts) / len(pts)
        lon = sum(p[1] for p in pts) / len(pts)
        self.emit(("w" if a.from_way() else "r") + str(a.orig_id()), a.tags, lat, lon)


def main():
    if len(sys.argv) != 3:
        print(__doc__); sys.exit(1)
    src, dst = sys.argv[1], sys.argv[2]
    t0 = time.time()
    with open(dst, "w", encoding="utf-8") as out:
        h = Extractor(out)
        h.apply_file(src, locations=True, idx="flex_mem")
    print(f"{h.n:,} places from {src} in {time.time() - t0:.0f}s", file=sys.stderr)


if __name__ == "__main__":
    main()
