#!/usr/bin/env python3
"""
Diagnostic: how would an Overture confidence threshold change coverage in one area?
Prints a table of counts per threshold, the source mix, and whether a few named places survive.

Usage: probe_confidence.py west,south,east,north [name1,name2,...]
"""
import sys, json, collections, os
import duckdb

bbox = [float(x) for x in sys.argv[1].split(",")]
names = [n.lower().replace(" ", "") for n in (sys.argv[2].split(",") if len(sys.argv) > 2 else [])]
release = os.environ.get("OVERTURE_RELEASE", "2026-09-23.1")
src = f"s3://overturemaps-us-west-2/release/{release}/theme=places/type=place/*"
con = duckdb.connect()
con.execute("INSTALL httpfs; LOAD httpfs; SET s3_region='us-west-2';")
q = f"""SELECT names.primary AS name, confidence, operating_status, taxonomy.primary AS cat,
               len(sources) AS nsrc, sources[1].dataset AS ds, websites[1] AS web, phones[1] AS phone,
               sources[1].update_time AS upd
        FROM read_parquet('{src}', hive_partitioning=1)
        WHERE bbox.xmin BETWEEN {bbox[0]} AND {bbox[2]} AND bbox.ymin BETWEEN {bbox[1]} AND {bbox[3]}
          AND regexp_matches(taxonomy.primary, 'restaurant|cafe|coffee|bar$|pub$|fast_food|pizza|burger|sandwich|deli$|bbq|grill$|steak|sushi|taco|chicken|wings|bagel|donut|ice_cream|diner|bistro', 'i')"""
rows = con.execute(q).fetchall()
print(f"{len(rows)} eating places in box")
for th in (0.5, 0.6, 0.7, 0.75, 0.8, 0.9, 0.95):
    print(f"  confidence >= {th:<5} keeps {sum(1 for r in rows if (r[1] or 0) >= th):5}")
print("sources per place:", collections.Counter(r[4] for r in rows).most_common(6))
print("first dataset:", collections.Counter(r[5] for r in rows).most_common(8))
print("operating_status:", collections.Counter(r[2] for r in rows).most_common(5))
# confidence distribution by source count
by = collections.defaultdict(list)
for r in rows:
    by[r[4]].append(r[1] or 0)
for k in sorted(by):
    v = sorted(by[k]); print(f"  {k} source(s): n={len(v)} median conf={v[len(v)//2]:.2f}")
for r in rows:
    if any(n in (r[0] or "").lower().replace(" ", "") for n in names):
        print("NAMED:", r)
json.dump([list(map(str, r)) for r in rows], open("probe.json", "w"))
