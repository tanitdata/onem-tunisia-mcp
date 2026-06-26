#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
ONEM Tunisia energy-report acquisition agent (Phase 1 + Phase 2).

Acquire & catalog ONLY. Idempotent + revision-aware.
- Re-fetches the confirmed index pages, harvests PDF links (discovery).
- Downloads politely (descriptive UA, delay, retries+backoff, sequential).
- sha256 dedupe (idempotency) + same-URL hash drift => new version (supersedes).
- Writes manifest.csv + acquisition_log.csv (merged, not rewritten from scratch).

Run:  python acquire.py
"""
import csv, hashlib, html, json, os, re, sys, time, urllib.parse, urllib.request
from datetime import datetime, timezone

BASE = "https://www.energiemines.gov.tn"
UA = "ONEM-Archiver/1.0 (energy report acquisition; respectful crawl; contact: research)"
ROOT = os.path.dirname(os.path.abspath(__file__))
ARCHIVE = os.path.join(ROOT, "archive")
WORK = os.path.join(ROOT, "_work")
STATE = os.path.join(WORK, "state.json")          # url -> {sha256, file_id, local_path, version, period, ...}
MANIFEST = os.path.join(ROOT, "manifest.csv")
LOG = os.path.join(ROOT, "acquisition_log.csv")
DELAY = 1.2          # seconds between requests (politeness)
RETRIES = 3
TIMEOUT = 90

INDEX_PAGES = [
    "/fr/tc/publications/",
    "/ar/tc/%D8%A7%D9%84%D9%86%D8%B4%D8%B1%D9%8A%D8%A7%D8%AA/",
    "/fr/themes/energies-renouvelables/publications/",
    "/fr/themes/energie/efficacite-energetique/publications/",
    "/fr/accueil/",
    "/ar/",
]

# Out of scope: ministry staff mutual-association financial statements (administrative, not energy report)
OUT_OF_SCOPE = ["%D8%A7%D9%84%D9%82%D9%88%D8%A7%D8%A6%D9%85_%D8%A7%D9%84%D9%85%D8%A7%D9%84%D9%8A%D8%A9"]

MONTHS_FR = {
    'janvier':1,'fevrier':2,'février':2,'mars':3,'avril':4,'mai':5,'juin':6,
    'juillet':7,'aout':8,'août':8,'septembre':9,'octobre':10,'novembre':11,'decembre':12,'décembre':12,
}
MONTHS_AR = {
    'جانفي':1,'فيفري':2,'مارس':3,'أفريل':4,'افريل':4,'ماي':5,'جوان':6,'جويلية':7,
    'أوت':8,'اوت':8,'سبتمبر':9,'ىسبتمبر':9,'سيتمبر':9,'أكتوبر':10,'اكتوبر':10,'نوفمبر':11,'ديسمبر':12,
}

def now_iso():
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")

def fetch(url, binary=False):
    req = urllib.request.Request(url, headers={"User-Agent": UA, "Accept": "*/*"})
    last = None
    for attempt in range(1, RETRIES+1):
        try:
            with urllib.request.urlopen(req, timeout=TIMEOUT) as r:
                data = r.read()
                return r.getcode(), data, r.geturl()
        except Exception as e:
            last = e
            if attempt < RETRIES:
                time.sleep(DELAY * (2 ** attempt))   # exponential backoff
    return None, str(last).encode(), url

def harvest_pdf_paths():
    """Re-derive the PDF URL list from index pages (idempotent discovery)."""
    seen = {}   # path -> set(source index pages)
    for page in INDEX_PAGES:
        url = BASE + page
        code, data, _ = fetch(url)
        time.sleep(DELAY)
        if code != 200:
            print(f"  [warn] index {page} -> {code}", file=sys.stderr)
            continue
        text = data.decode("utf-8", "ignore")
        for m in re.finditer(r'href="([^"]+\.pdf)"', text, re.I):
            href = html.unescape(m.group(1))
            if "templates2" in href:        # CSS/asset noise
                continue
            path = href.lstrip("/")
            if not path.startswith("fileadmin"):
                continue
            seen.setdefault(path, set()).add(page)
    return seen

def decode_name(path):
    return urllib.parse.unquote(path).split("/")[-1]

def has_arabic(s):
    return any('؀' <= c <= 'ۿ' for c in s)

# Latin-named files can still be ARABIC editions, tagged by an "ar" token or "résumé"
# (the ONEM Arabic summaries ship as e.g. Conjoncture_..._Ar_resumé.pdf).
_AR_TOKEN = re.compile(r'(?:[_\-.]ar(?:[_\-.]|$)|r[eé]sum[eé]|arabe|arabic)', re.I)

def _latin_lang(low):
    if _AR_TOKEN.search(low):
        return "ar"
    return "fr"

def classify(path):
    d = urllib.parse.unquote(path)
    name = d.split("/")[-1]
    low = name.lower()
    if name.startswith("Conjoncture") or "conjoncture" in low or "conjonctre" in low:
        return "Conjoncture", _latin_lang(low)
    if "وضع_قطاع_الطاقة" in d:
        return "Conjoncture", "ar"
    if low.startswith("chiffres") or "chiffres_cl" in low or "chiffres_cles" in low:
        return "Memento", _latin_lang(low)
    if "مؤشرات_قطاع_الطاقة" in d:
        return "Memento", "ar"
    if name.startswith("Rapport_Bilan"):
        return "Bilan", "fr"
    if "Bilan_National" in name or "Bilan_national" in name:
        return "Bilan", "multi"      # trilingual FR/EN/AR poster
    if "Evolution_du_Bilan" in name:
        return "Bilan", "fr"
    if "Note_Methodologique" in name:
        return "Bilan", "fr"
    if name.startswith("Bulletin"):
        return "Bulletin-COVID", _latin_lang(low)
    # out of scope handled by caller; everything else = Other
    if has_arabic(name):
        return "Other", "ar"
    if "summary" in low or "_en" in low:
        return "Other", "en"
    return "Other", "fr"

def parse_period(path, family):
    """Return (period, note). period = YYYY-MM (monthly) or YYYY (annual) or '' ."""
    d = urllib.parse.unquote(path)
    name = d.split("/")[-1]
    low = name.lower()
    ymatch = re.search(r'(20\d\d)', name)
    year = ymatch.group(1) if ymatch else ""
    if not year:
        # legacy 2-digit year suffix, e.g. "...-fevrier-20.pdf" => 2020
        y2 = re.search(r'-(\d\d)(?:\.pdf)?$', low)
        if y2:
            year = "20" + y2.group(1)
    note = ""
    if family in ("Conjoncture", "Bulletin-COVID"):
        # year-end issues sometimes named "s:fin décembre" or AR "سنة YYYY"/"سنة YYYY محينة"
        if re.search(r'fin[_\s]+d[eé]cembre', low) or re.search(r'\bd[eé]cembre\b', low):
            return (f"{year}-12", note) if year else ("", "year missing")
        if 'سنة' in d:        # AR "year YYYY" => annual year-end recap, treat as YYYY-12
            return (f"{year}-12", "AR annual recap") if year else ("", "year missing")
        # find a month token
        mon = None
        if has_arabic(name):
            for tok, num in MONTHS_AR.items():
                if tok in d:
                    mon = num; break
        else:
            for tok, num in MONTHS_FR.items():
                if re.search(r'(?<![a-zéû])'+re.escape(tok)+r'(?![a-zéû])', low):
                    mon = num; break
        if mon and year:
            return f"{year}-{mon:02d}", note
        if year:
            return year, "month unresolved"
        return "", "period unresolved"
    else:
        # annual families: just the year (or year-range for Evolution doc)
        rng = re.search(r'(20\d\d)[-_]+(20\d\d)', name)
        if rng:
            return f"{rng.group(1)}-{rng.group(2)}", "range"
        return (year, note) if year else ("", "no year (reference doc)")

def parse_version(path):
    """Extract a *re-publication* version label from the filename (distinct editions of one period).

    Only genuine revision markers go here. Language/format markers (Ar, résumé, vf) are NOT versions —
    they are captured by `language` / notes. 'محينة' (Arabic: "updated/revised") and 'updated' ARE
    revision markers.
    """
    name = decode_name(path)
    stem = re.sub(r'\.pdf$', '', name, flags=re.I)
    labels = []
    # e.g. __VF-2024_, ___v_2024_, ___V_10-2023_, _updated, محينة (revised)
    for pat in [r'VF[-_]?\d{4}', r'v[_-]?\d{4}', r'V[_\s-]*\d{1,2}-?\d{3,4}', r'updated', r'محينة']:
        m = re.search(pat, stem, re.I)
        if m:
            labels.append(m.group(0).strip('_- '))
    return "|".join(dict.fromkeys(labels))   # dedupe preserve order

def sha256_bytes(b):
    h = hashlib.sha256(); h.update(b); return h.hexdigest()

def sha256_file(p):
    h = hashlib.sha256()
    with open(p, "rb") as f:
        for chunk in iter(lambda: f.read(1<<20), b""):
            h.update(chunk)
    return h.hexdigest()

def load_state():
    if os.path.exists(STATE):
        with open(STATE, encoding="utf-8") as f:
            return json.load(f)
    return {}

def save_state(s):
    with open(STATE, "w", encoding="utf-8") as f:
        json.dump(s, f, ensure_ascii=False, indent=1)

def safe_seg(s):
    return re.sub(r'[^0-9A-Za-z_.\-]', '_', s) if s else "na"

def main():
    os.makedirs(ARCHIVE, exist_ok=True)
    os.makedirs(WORK, exist_ok=True)
    state = load_state()                  # keyed by source_url
    # index existing archive by sha256 for cross-run idempotency
    existing_hashes = {rec["sha256"]: rec for rec in state.values() if "sha256" in rec}

    print("Harvesting index pages ...")
    found = harvest_pdf_paths()
    print(f"  discovered {len(found)} PDF URLs")

    manifest_rows = []
    log_rows = []

    paths = sorted(found.keys())
    for i, path in enumerate(paths, 1):
        url = BASE + "/" + path
        ts = now_iso()

        if any(tok in path for tok in OUT_OF_SCOPE):
            log_rows.append([url, "skipped-out-of-scope", "ministry mutual-association financial statement", ts])
            continue

        family, lang = classify(path)
        period, pnote = parse_period(path, family)
        version = parse_version(path)
        orig = decode_name(path)

        prev = state.get(url)
        print(f"[{i}/{len(paths)}] {family}/{period} {orig[:60]}")

        code, data, final = fetch(url, binary=True)
        time.sleep(DELAY)

        if code != 200 or not data[:5].startswith(b"%PDF"):
            reason = f"http={code}" + ("" if (data[:5].startswith(b"%PDF") or code!=200) else " not-pdf")
            if code == 200 and not data[:5].startswith(b"%PDF"):
                reason = "http=200 but not a PDF (magic mismatch)"
            log_rows.append([url, "failed", reason, ts])
            continue

        digest = sha256_bytes(data)
        fid = digest[:16]

        # idempotency: identical bytes already archived anywhere
        if digest in existing_hashes:
            rec = existing_hashes[digest]
            log_rows.append([url, "unchanged", f"sha256 match {fid}", ts])
            # ensure manifest reflects it
            manifest_rows.append(rec)
            continue

        supersedes = ""
        note = pnote
        if prev and prev.get("sha256") and prev["sha256"] != digest:
            # silent re-publication at same URL -> NEW version
            supersedes = prev["file_id"]
            if not version:
                version = "rev-" + ts[:10]
            note = (note + "; " if note else "") + f"silent re-publication at same URL (supersedes {supersedes})"

        # layout: archive/{family}/{period}/{version}/{orig}
        vdir = safe_seg(version) if version else "v1"
        dest_dir = os.path.join(ARCHIVE, safe_seg(family), safe_seg(period or "undated"), vdir)
        os.makedirs(dest_dir, exist_ok=True)
        dest = os.path.join(dest_dir, orig)
        # never overwrite a different artifact sharing a name
        if os.path.exists(dest) and sha256_file(dest) != digest:
            base, ext = os.path.splitext(orig)
            dest = os.path.join(dest_dir, f"{base}__{fid}{ext}")
        with open(dest, "wb") as f:
            f.write(data)

        rel = os.path.relpath(dest, ROOT).replace("\\", "/")
        rec = {
            "file_id": fid, "report_family": family, "period": period, "version": version,
            "language": lang, "source_url": url, "local_path": rel, "sha256": digest,
            "bytes": len(data), "http_status": code, "downloaded_at": ts,
            "supersedes": supersedes, "notes": note,
        }
        state[url] = rec
        existing_hashes[digest] = rec
        manifest_rows.append(rec)
        outcome = "new-version" if supersedes else "downloaded"
        log_rows.append([url, outcome, f"sha256 {fid}, {len(data)} bytes -> {rel}", ts])

    save_state(state)

    # ---- write manifest.csv (full current state = idempotent superset) ----
    cols = ["file_id","report_family","period","version","language","source_url",
            "local_path","sha256","bytes","http_status","downloaded_at","supersedes","notes"]
    # build from full state so re-runs keep prior rows
    all_recs = list(state.values())
    all_recs.sort(key=lambda r: (r["report_family"], r["period"], r["version"], r["source_url"]))
    with open(MANIFEST, "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=cols); w.writeheader()
        for r in all_recs:
            w.writerow({k: r.get(k, "") for k in cols})

    # ---- append to acquisition_log.csv (merge, not rewrite) ----
    log_exists = os.path.exists(LOG)
    with open(LOG, "a", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        if not log_exists:
            w.writerow(["source_url","outcome","reason","timestamp"])
        for row in log_rows:
            w.writerow(row)

    # summary counts
    from collections import Counter
    oc = Counter(r[1] for r in log_rows)
    print("\n=== run outcomes ===")
    for k, v in oc.items():
        print(f"  {k}: {v}")
    print(f"manifest rows (total in state): {len(all_recs)}")

if __name__ == "__main__":
    main()
