#!/usr/bin/env python3
"""Rebuild the site's model data files from the live Google Sheet.

Reads the sheet over the credential-free gviz JSON endpoint (the sheet is
shared anyone-with-link VIEWER), cross-checks every row against the sheet's
own model score, re-matches Sleeper headshot IDs, and rewrites the three
split data files plus 0-model-config.js in place. Also bumps the ?v= asset
version in index.html so returning phones pick up the new data.

Guards: header equality checks, a minimum row count, a per-row model-score
cross-check, and a 90% headshot-match floor. Any failure exits non-zero so
the Action fails loudly and commits nothing.

Coverage fields: scope ("Completed W1-N") is recomputed from the sheet's
weekly XO data. salaryWeeks/propWeeks are carried forward from the existing
repo data files (the new sheet's weekly tab lacks prop lines for many
players, so recomputing would zero them out); rows new to the sheet fall
back to counts from the XO tab.

Env:
  SHEET_ID    (required) Google Sheet id, supplied via repo variable
              MODEL_SCORES_SHEET_ID. Never printed.
  CHECK_ONLY  when "1", run all fetches, checks and the acceptance diff
              without writing files.
"""

import csv
import datetime
import json
import os
import math
import re
import sys
import urllib.parse
import urllib.request
from collections import defaultdict

MIN_ROWS = 1900
MIN_HEADSHOT_RATIO = 0.9
# Site weights: actual PPG 50%, preseason 25%, DFS salary 15%, weekly 5%, prop 5%.
# The sheet's model score must equal this mix of its component Zs for every row.
WEIGHT_TOL = 1e-6

# XO data tab block 1: A=pid, B=first, C=last, D=pos group,
# K=season_projected_points_0, L=week_projected_points_0, M=dfs_salary_0,
# AL=fantasy_points_from_plays_0, AM=year, AN=week, AO=pos group,
# AP=implied_prop_pts, AQ=main_slate, AR=week_completed. (Columns AT+ are a
# separate game-info join and are not used.)
XO_TQ = "select A,B,C,D,K,L,M,AL,AM,AN,AO,AP,AQ,AR"

UA = {"User-Agent": "redraft-score-updater (GitHub Actions)"}

REPO_FILES = [
    ("2-model-data.js", "CPLANT_DATA"),
    ("3-model-data.js", "CPLANT_DATA2"),
    ("4-model-data.js", "CPLANT_DATA3"),
]


def fetch_gviz(sheet_id, sheet_name, tq=None):
    params = {"tqx": "out:json", "sheet": sheet_name}
    if tq:
        params["tq"] = tq
    url = (
        "https://docs.google.com/spreadsheets/d/"
        + sheet_id
        + "/gviz/tq?"
        + urllib.parse.urlencode(params)
    )
    req = urllib.request.Request(url, headers=UA)
    try:
        with urllib.request.urlopen(req, timeout=180) as resp:
            text = resp.read().decode("utf-8")
    except Exception as exc:
        # Never leak the sheet URL/ID into public Actions logs.
        raise RuntimeError(
            "gviz fetch failed for sheet tab %r (%s). Is the sheet still link-shared?"
            % (sheet_name, type(exc).__name__)
        ) from None
    start = text.find("setResponse(")
    if start == -1:
        raise RuntimeError("Unexpected gviz response for %r." % sheet_name)
    start += len("setResponse(")
    payload = json.loads(text[start : text.rfind(")")])
    if payload.get("status") == "error":
        raise RuntimeError("gviz error for %r: %r" % (sheet_name, payload.get("errors")))
    table = payload["table"]
    labels = [str(c.get("label") or "") for c in table["cols"]]
    rows = table_rows(table)
    # gviz header handling is inconsistent across tabs. Normalize: when the
    # first data row is itself the header row (first cell 'pid'), surface it
    # as labels and drop it from the data.
    if rows and str(rows[0][0] or "") == "pid":
        labels = [str(x) if x is not None else "" for x in rows[0]]
        rows = rows[1:]
    return labels, rows


def table_rows(table):
    ncols = len(table["cols"])
    rows = []
    for r in table["rows"]:
        cells = r.get("c") or []
        row = []
        for i in range(ncols):
            cell = cells[i] if i < len(cells) else None
            row.append(None if cell is None else cell.get("v"))
        rows.append(row)
    return rows


def num(v):
    return float(v) if isinstance(v, (int, float)) else None


def int_or_none(v):
    return int(v) if isinstance(v, (int, float)) else None


def r4(v):
    return None if v is None else round(v, 4)


def norm_name(n):
    n = str(n).lower()
    n = re.sub(r"\b(jr|sr|ii|iii|iv|v)\b\.?", "", n)
    return re.sub(r"[^a-z]", "", n)


def fetch_headshot_candidates():
    url = "https://api.sleeper.app/v1/players/nfl"
    req = urllib.request.Request(url, headers=UA)
    with urllib.request.urlopen(req, timeout=180) as resp:
        players = json.load(resp)
    cand = defaultdict(list)
    for pid, p in players.items():
        pos = p.get("position")
        fn = p.get("full_name") or ""
        if not pos or not fn:
            continue
        cand[(norm_name(fn), pos)].append((pid, p))
    return cand


def resolve_headshot(cand, key):
    c = cand.get(key, [])
    if len(c) == 1:
        return c[0][0]
    active = [x for x in c if x[1].get("team") and x[1].get("status") == "Active"]
    if len(active) == 1:
        return active[0][0]
    return None


def load_existing_rows():
    """Read the current data files from the repo checkout, keyed by row id."""
    rows = {}
    for path, var in REPO_FILES:
        if not os.path.exists(path):
            continue
        text = open(path, encoding="utf-8").read()
        m = re.search(re.escape("window." + var + "=") + r"(\[.*\]);", text, re.S)
        if not m:
            continue
        for row in json.loads(m.group(1)):
            rows[row["id"]] = row
    return rows


def main():
    sheet_id = os.environ.get("SHEET_ID", "").strip()
    if not sheet_id:
        sys.exit("SHEET_ID env var is required")
    check_only = os.environ.get("CHECK_ONLY", "") == "1"

    # --- Scores tab: identity + actuals -------------------------------------
    # No tq for Scores/Z-scores: gviz then returns the sheet's own header row
    # as complete column labels (with tq it returns partial labels on some
    # tabs). Extra right-hand columns (reference junk) are sliced off by index.
    header, scores = fetch_gviz(sheet_id, "Scores")
    if header[:7] != ["pid", "first", "last", "pos", "year", "actual_fp", "games_played"]:
        sys.exit("Scores header changed; refusing to rebuild. Got: %r" % (header[:7],))
    info = {}  # (pid, year) -> dict
    weights = None
    for r in [x[:18] for x in scores]:
        pid, first, last, pos = (str(x).strip() if x else "" for x in r[:4])
        year = int_or_none(r[4])
        if not pid or not pos or year is None:
            continue
        if weights is None:
            w = [num(x) for x in r[13:18]]
            if None not in w:
                weights = w
        info[(pid, year)] = {
            "pid": pid,
            "name": ("%s %s" % (first, last)).strip(),
            "pos": pos,
            "year": year,
            "act": num(r[5]),
            "weeks": int_or_none(r[6]),
        }
    if weights != [0.25, 0.05, 0.5, 0.15, 0.05]:
        sys.exit("Sheet weights changed; refusing to rebuild. Got: %r" % (weights,))

    # --- Z-scores tab: component Zs + sheet model score ----------------------
    header, zs = fetch_gviz(sheet_id, "Z-scores")
    # gviz nulls out header cells on numeric-inferred columns, so only A-D
    # labels survive. Guard A-D by name; E-L semantics are guarded per row by
    # the model-score cross-check, which fails loudly if any column shifts.
    if header[:4] != ["pid", "first", "last", "pos"]:
        sys.exit("Z-scores header changed; refusing to rebuild. Got: %r" % (header[:4],))

    out_rows = []
    for r in [x[:14] for x in zs]:
        pid = str(r[0]).strip() if r[0] else ""
        year = int_or_none(r[4])
        if not pid or year is None:
            continue
        base = info.get((pid, year))
        if base is None:
            sys.exit("Z-scores row missing from Scores tab: %r" % ((pid, year),))
        pre, weekly = num(r[6]), num(r[7])
        zact, zsalary, zprop = num(r[8]), num(r[9]), num(r[10])
        sheet_model = num(r[11])
        tgt_ppg, tgt_pre = num(r[12]), num(r[13])
        if sheet_model is not None and None not in (pre, weekly, zact, zsalary, zprop):
            mixed = 0.25 * pre + 0.05 * weekly + 0.5 * zact + 0.15 * zsalary + 0.05 * zprop
            if abs(mixed - sheet_model) > WEIGHT_TOL:
                sys.exit(
                    "Model-score cross-check failed for %r: sheet %r vs computed %r"
                    % ((pid, year), sheet_model, mixed)
                )
        row = {
            "id": "%s-%s-%d" % (pid, base["pos"], year),
            "player": pid,
            "name": base["name"],
            "pos": base["pos"],
            "season": year,
            "pre": r4(pre),
            "weekly": r4(weekly),
            "salary": r4(zsalary),
            "prop": r4(zprop),
            "tgtPPG": r4(tgt_ppg),
            "tgtPre": r4(tgt_pre),
            "weeks": base["weeks"],
        }
        if base["act"] is not None:
            row["_act"] = round(base["act"], 1)
        out_rows.append(row)

    if len(out_rows) < MIN_ROWS:
        sys.exit("Only %d rows built (min %d); refusing." % (len(out_rows), MIN_ROWS))

    # --- XO data: scope + fallback coverage ----------------------------------
    _xo_header, xo = fetch_gviz(sheet_id, "XO data", tq=XO_TQ)
    sal_weeks = defaultdict(set)
    prop_weeks = defaultdict(set)
    completed_max = defaultdict(int)
    weekly_rows = []
    for r in xo:
        pid = str(r[0]).strip() if r[0] else ""
        if not pid:
            continue
        name = ("%s %s" % (str(r[1] or "").strip(), str(r[2] or "").strip())).strip()
        pos = str(r[10] or r[3] or "").strip()
        season_proj, week_proj = num(r[4]), num(r[5])
        has_salary = num(r[6]) is not None
        pts = num(r[7])
        y, w = int_or_none(r[8]), int_or_none(r[9])
        prop_pts = num(r[11])
        main = int_or_none(r[12])
        comp = int_or_none(r[13])
        if y is None or w is None:
            continue
        weekly_rows.append([
            pid, name, pos, y, w, r4(pts), r4(week_proj), r4(season_proj),
            num(r[6]), r4(prop_pts), main, comp,
        ])
        if prop_pts is not None and comp == 1:
            prop_weeks[(pid, y)].add(w)
        if has_salary and main == 1 and comp == 1:
            sal_weeks[(pid, y)].add(w)
        if comp == 1:
            completed_max[y] = max(completed_max[y], w)

    # --- Coverage: carry salaryWeeks/propWeeks forward, recompute scope ------
    existing = load_existing_rows()
    for row in out_rows:
        key = (row["player"], row["season"])
        old = existing.get(row["id"])
        if old is not None and "salaryWeeks" in old:
            row["salaryWeeks"] = old["salaryWeeks"]
        else:
            row["salaryWeeks"] = len(sal_weeks.get(key, ()))
        if old is not None and "propWeeks" in old:
            row["propWeeks"] = old["propWeeks"]
        else:
            row["propWeeks"] = len(prop_weeks.get(key, ()))
        cm = completed_max.get(row["season"])
        row["scope"] = "Completed W1-%d" % cm if cm else None
        # act must stay the last key, matching the existing file layout.
        if "_act" in row:
            row["act"] = row.pop("_act")

    # --- Headshots ------------------------------------------------------------
    cand = fetch_headshot_candidates()
    headshots = {}
    for row in out_rows:
        hid = resolve_headshot(cand, (norm_name(row["name"]), row["pos"]))
        if hid:
            headshots[row["player"]] = str(hid)
    if len(headshots) < MIN_HEADSHOT_RATIO * len({r["player"] for r in out_rows}):
        sys.exit(
            "Only %d headshots matched; refusing." % len(headshots)
        )

    # --- Assemble outputs ------------------------------------------------------
    today_et = datetime.datetime.now(
        datetime.timezone(datetime.timedelta(hours=-5))
    ).date().isoformat()
    config = {
        "scoreName": "Redraft Score",
        "weights": {
            "pre": 0.25,
            "weekly": 0.05,
            "actual": 0.5,
            "salary": 0.15,
            "prop": 0.05,
        },
        "updated": today_et,
        "rows": len(out_rows),
        "headshotsMatched": len(headshots),
    }
    parts = [
        "window.CPLANT_CONFIG="
        + json.dumps(config, separators=(",", ":"))
        + ";\nwindow.CPLANT_HEADSHOTS="
        + json.dumps(headshots, separators=(",", ":"), sort_keys=True)
        + ";\n"
    ]
    n = len(out_rows)
    third = (n + 2) // 3
    for idx, (path, var) in enumerate(REPO_FILES):
        chunk = out_rows[idx * third : (idx + 1) * third]
        parts.append(
            (path, "window." + var + "=" + json.dumps(chunk, separators=(",", ":")) + ";\n")
        )

    if check_only:
        print("CHECK_ONLY ok: %d rows, %d headshots" % (n, len(headshots)))
        return

    for path, content in parts[1:]:
        with open(path, "w", encoding="utf-8") as f:
            f.write(content)
    with open("0-model-config.js", "w", encoding="utf-8") as f:
        f.write(parts[0])

    # --- CSV exports ---------------------------------------------------------
    # Actual-PPG Zs are computed exactly as the site's runtime does: cohort is
    # players with >=4 games in the same position and season, PPG = act/weeks,
    # population standard deviation.
    ACTMIN = 4
    cohort = defaultdict(list)
    for row in out_rows:
        act, weeks = row.get("act"), row.get("weeks")
        if act is not None and weeks is not None and weeks >= ACTMIN:
            cohort[(row["pos"], row["season"])].append(act / weeks)
    cohort_stats = {}
    for k, xs in cohort.items():
        m = sum(xs) / len(xs)
        sd = math.sqrt(sum((x - m) ** 2 for x in xs) / len(xs)) or 1.0
        cohort_stats[k] = (m, sd)
    runtime_zact = {}
    for row in out_rows:
        act, weeks = row.get("act"), row.get("weeks")
        st = cohort_stats.get((row["pos"], row["season"]))
        if st and act is not None and weeks is not None and weeks >= ACTMIN:
            runtime_zact[row["id"]] = (act / weeks - st[0]) / st[1]

    def phi(z):
        return 0.5 * (1.0 + math.erf(z / math.sqrt(2.0)))

    def model_of(row):
        # Site semantics: actual PPG counts from 4 games; missing components
        # are dropped and the remaining weights rescale (nothing zero-filled).
        comps = []
        if row.get("pre") is not None:
            comps.append((0.25, row["pre"]))
        if row.get("weekly") is not None:
            comps.append((0.05, row["weekly"]))
        zact = runtime_zact.get(row["id"])
        if zact is not None:
            comps.append((0.5, zact))
        if row.get("salary") is not None:
            comps.append((0.15, row["salary"]))
        if row.get("prop") is not None:
            comps.append((0.05, row["prop"]))
        tw = sum(w for w, _ in comps)
        if tw == 0:
            return None
        return sum(w * v for w, v in comps) / tw

    with open("data.csv", "w", encoding="utf-8", newline="") as f:
        wcsv = csv.writer(f)
        wcsv.writerow([
            "id", "player_id", "name", "pos", "season", "model_z", "score_0_100",
            "z_preseason", "z_weekly_proj", "z_actual_ppg", "z_salary", "z_prop",
            "actual_pts", "actual_ppg", "games_played", "salary_weeks",
            "prop_weeks", "scope", "target_n1_actual_ppg",
            "target_n1_preseason_pts", "headshot_url",
        ])
        for row in out_rows:
            mz = model_of(row)
            act = row.get("act")
            weeks = row.get("weeks")
            ppg = round(act / weeks, 3) if act is not None and weeks else None
            hid = headshots.get(row["player"])
            wcsv.writerow([
                row["id"], row["player"], row["name"], row["pos"], row["season"],
                None if mz is None else round(mz, 4),
                None if mz is None else round(100 * phi(mz), 1),
                row.get("pre"), row.get("weekly"),
                None if runtime_zact.get(row["id"]) is None else round(runtime_zact[row["id"]], 4),
                row.get("salary"), row.get("prop"), act, ppg, weeks,
                row.get("salaryWeeks"), row.get("propWeeks"), row.get("scope"),
                row.get("tgtPPG"), row.get("tgtPre"),
                ("https://sleepercdn.com/content/nfl/players/%s.jpg" % hid) if hid else None,
            ])

    with open("weekly.csv", "w", encoding="utf-8", newline="") as f:
        wcsv = csv.writer(f)
        wcsv.writerow([
            "player_id", "name", "pos", "season", "week", "fantasy_points",
            "weekly_projection", "season_projection", "dfs_salary",
            "implied_prop_pts", "main_slate", "week_completed",
        ])
        for wr in weekly_rows:
            wcsv.writerow(wr)

    # Bump the asset version so returning phones pick up the new files.
    version = datetime.datetime.utcnow().strftime("%Y%m%d-%H%M")
    with open("index.html", encoding="utf-8") as f:
        html = f.read()
    html, n_sub = re.subn(r"\?v=\d{8}-\d+", "?v=" + version, html)
    if n_sub < 4:
        sys.exit("Expected at least 4 asset version strings in index.html, found %d" % n_sub)
    with open("index.html", "w", encoding="utf-8") as f:
        f.write(html)

    print("wrote %d rows across 3 files; %d headshots; version %s" % (n, len(headshots), version))


if __name__ == "__main__":
    main()
