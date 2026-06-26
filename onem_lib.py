"""
onem_lib.py — shared ingestion engine for the ONEM Tunisia energy store.

Implements the pieces every loader needs:
  * token normalization (decimal comma, thin/NBSP spaces, '-' -> NULL; value_raw kept)
  * Vocab: alias-based, normalized label -> controlled-vocabulary ID matching
  * GridPage: coordinate-based (x/y) table extraction from a PDF page
            (this is what defeats the discovery off-by-one / column-misalignment bug)
  * series_key / upsert_key construction (controlled IDs only -> stable across runs)
  * DB: idempotent UPSERT into observation, surpression-aware preferred recompute
  * manifest helpers

All loaders re-use this so the pipeline is uniform, idempotent, and re-runnable.
"""
import csv, re, unicodedata, json, datetime
import fitz  # PyMuPDF

# ---------------------------------------------------------------- normalization
NBSP = " "; THIN = " "; NARROW = " "

def strip_accents(s: str) -> str:
    return "".join(c for c in unicodedata.normalize("NFD", s)
                   if unicodedata.category(c) != "Mn")

def norm_label(s: str) -> str:
    """Normalize a label for alias matching: lowercase, no accents, collapse ws/punct."""
    if s is None:
        return ""
    s = s.replace(NBSP, " ").replace(THIN, " ").replace(NARROW, " ")
    s = strip_accents(s).lower()
    s = re.sub(r"[^\w\s]", " ", s)        # drop punctuation
    s = re.sub(r"\s+", " ", s).strip()
    return s

NUM_RE = re.compile(r"^-?\d[\d\s   ]*(?:[.,]\d+)?$")

def _looks_numeric(t):
    s = (t or "")
    for ch in [" ", " ", " ", " ", ",", ".", "%", "-", "+"]:
        s = s.replace(ch, "")
    return len(s) > 0 and s.isdigit()


def parse_number(tok: str):
    """Return float or None. Handles '4 702', '1 518', '0,3', '-291', '-', '_'.
    Keeps thousands grouping (space) and decimal comma. Returns None for '-'/'_'/''."""
    if tok is None:
        return None
    t = tok.strip()
    if t in ("-", "_", "", "—", "–"):
        return None
    t2 = t.replace(NBSP, "").replace(THIN, "").replace(NARROW, "").replace(" ", "")
    # decimal comma -> point (but only the LAST comma if multiple; ONEM uses comma decimals)
    if "," in t2 and "." not in t2:
        t2 = t2.replace(",", ".")
    elif "," in t2 and "." in t2:
        t2 = t2.replace(",", "")          # comma as thousands, point decimal (rare)
    try:
        return float(t2)
    except ValueError:
        return None

# ---------------------------------------------------------------- vocab matching
class Vocab:
    """Loads vocab_*.csv and builds normalized alias -> id maps per dimension."""
    def __init__(self, base="."):
        self.base = base
        self.maps = {}     # dim -> {norm_label: id}
        self.rows = {}     # dim -> [row dict]
        self._load("flow",    "vocab_flow.csv",    "flow_id",    ["label_fr","label_en","label_ar"])
        self._load("product", "vocab_product.csv", "product_id", ["label_fr","label_en","label_ar"])
        self._load("sector",  "vocab_sector.csv",  "sector_id",  ["label_fr","label_en"])
        self._load("region",  "vocab_region.csv",  "region_id",  ["label"])
        self._load("field",   "vocab_field.csv",   "field_id",   ["label"])
        self._load("level",   "vocab_level.csv",   "level_id",   ["label_fr","label_en"])

    def _load(self, dim, fname, idcol, labelcols):
        m = {}; rows = []
        with open(f"{self.base}/{fname}", encoding="utf-8") as f:
            for r in csv.DictReader(f):
                rows.append(r)
                _id = r[idcol]
                labels = [r.get(c, "") for c in labelcols]
                aliases = (r.get("aliases", "") or "").split("|")
                for lab in labels + aliases:
                    nl = norm_label(lab)
                    if nl:
                        m.setdefault(nl, _id)
        self.maps[dim] = m
        self.rows[dim] = rows

    def match(self, dim, label):
        """Exact/normalized match -> id, else None (caller quarantines)."""
        nl = norm_label(label)
        if not nl:
            return None
        m = self.maps[dim]
        if nl in m:
            return m[nl]
        # token-subset fallback: a label whose words are a superset of a known alias
        # (handles 'Gaz Com. de Sud *' footnote markers etc.)
        for known, _id in m.items():
            if known and (nl == known or nl.startswith(known + " ") or
                          (" " + known + " ") in (" " + nl + " ")):
                return _id
        return None

# ---------------------------------------------------------------- grid extraction
class GridPage:
    """Coordinate-based table reader for one PDF page.

    Words carry (x0,y0,x1,y1,text). We assign each numeric token to:
      - a COLUMN by nearest header x-anchor, and
      - a ROW by y-band.
    This is the alignment-safe primitive proven in 00_alignment_check.md.
    """
    def __init__(self, page):
        self.page = page
        self.words = page.get_text("words")  # list of (x0,y0,x1,y1, text, block, line, word)
        self.W = page.rect.width
        self.H = page.rect.height

    def find_word_centers(self, text, ymin=0, ymax=10**9):
        """x-centers of every occurrence of an exact word within a y-range."""
        return [ (w[0]+w[2])/2 for w in self.words
                 if w[4] == text and ymin <= w[1] <= ymax ]

    def header_anchor(self, words_list, ymin=0, ymax=10**9):
        """Average x-center for a header label spread over >=1 token."""
        xs = []
        for word in words_list:
            xs += self.find_word_centers(word, ymin, ymax)
        return sum(xs)/len(xs) if xs else None

    def row_tokens(self, ycenter, tol=5.0):
        """All tokens whose y0 is within tol of ycenter, sorted by x."""
        toks = [(w[0], w[2], w[4]) for w in self.words if abs(w[1]-ycenter) <= tol]
        return sorted(toks)

    def find_row_y(self, label_words, ymin=0, ymax=10**9):
        """y0 of the first token of a row identified by its leading label word(s)."""
        target = norm_label(" ".join(label_words))
        # build line groups
        lines = {}
        for w in self.words:
            if ymin <= w[1] <= ymax:
                lines.setdefault(round(w[1],1), []).append(w)
        for y in sorted(lines):
            txt = norm_label(" ".join(t[4] for t in sorted(lines[y], key=lambda z:z[0])))
            if txt.startswith(target) or target in txt:
                return y
        return None

    def read_rows(self, ymin, ymax, value_anchors, label_xmin=0.0, label_xmax=160.0,
                  tol=5.0, cell_xmin=0.0, cell_xmax=1e9):
        """Read a simple vertical table: each row has a text label in the band
        [label_xmin,label_xmax) and numeric values aligned under fixed x-anchors
        {colkey: x}. cell_xmin/xmax bound the value region so a side-by-side table
        on the same y-band is not mixed in. Returns list of (label, {colkey: token}, y)."""
        lines = {}
        for w in self.words:
            if ymin <= w[1] <= ymax:
                lines.setdefault(round(w[1], 1), []).append(w)
        out = []
        for y in sorted(lines):
            toks = sorted(lines[y], key=lambda z: z[0])
            label_parts = [t[4] for t in toks
                           if (label_xmin <= t[0] < label_xmax and not _looks_numeric(t[4]))]
            label = " ".join(label_parts).strip()
            cells = self.assign_cells(y, value_anchors, tol=tol,
                                      cell_xmin=cell_xmin, cell_xmax=cell_xmax)
            if label and any(v for v in cells.values()):
                out.append((label, cells, y))
        return out

    def assign_cells(self, ycenter, anchors, tol=5.0, xtol=9.0, cell_xmin=0.0, cell_xmax=1e9):
        """Given column anchors {colkey: x}, return {colkey: combined-number-token}.
        Adjacent numeric fragments (PyMuPDF splits '4 702' into '4','702') that map
        to the same column are concatenated in x-order before parsing.
        cell_xmin/xmax bound the value region (drops side-by-side neighbour tables)."""
        toks = [(x0, x1, t) for (x0, x1, t) in self.row_tokens(ycenter, tol)
                if cell_xmin <= (x0+x1)/2 <= cell_xmax]
        # group consecutive tokens by nearest anchor
        buckets = {}
        for x0, x1, t in toks:
            xc = (x0+x1)/2
            # nearest anchor within xtol
            best=None; bestd=1e9
            for k,ax in anchors.items():
                d=abs(xc-ax)
                if d<bestd:
                    bestd=d; best=k
            if best is not None and bestd <= xtol*3:  # generous; anchors are spaced
                buckets.setdefault(best, []).append((x0, t))
        out={}
        for k, items in buckets.items():
            items.sort()
            joined = "".join(t for _,t in items)
            out[k]=joined
        return out

# ---------------------------------------------------------------- keys
def series_key(indicator_id, flow_id, product_id, sector_id, region_id, field_id,
               level_id, calorific_basis, unit_id, period_type, redevance_included,
               scope=None, technology=None, regime=None, geography_scope=None,
               producer_id=None):
    parts = [indicator_id, flow_id, product_id, sector_id, region_id, field_id,
             level_id, producer_id, calorific_basis, unit_id, period_type,
             "" if redevance_included is None else str(redevance_included),
             scope, technology, regime, geography_scope]
    return "|".join("" if p is None else str(p) for p in parts)

def upsert_key(skey, period_start, period_end, source_id):
    return f"{skey}#{period_start}#{period_end}#{source_id}"

# ---------------------------------------------------------------- aggregation role
# Subtotal / alternative-breakdown markers shared by all loaders, so the role column is
# computed ONE way everywhere (gates + views + catalog read it from observation).
SUBTOTAL_PRODUCTS = {"prod.gasoil", "prod.gasoline", "prod.fuel_oil"}          # PP mid-level
# PP sub-variants (children of Gasoil/Essences) are flagged is_total to keep them out of
# the canonical leaf sum; as roles they are 'subtotal' (finer breakdown), never grand.
SUBCHILD_PRODUCTS = {"prod.gasoil_ordinaire", "prod.gasoil_ss", "prod.gasoil_premium",
                     "prod.gasoline_ssp", "prod.gasoline_super", "prod.gasoline_premium"}
SUBTOTAL_PRODUCERS = {"prod.steg"}                                            # elec producer subtotal

def aggregation_role(is_total, *, flow=None, product=None, producer=None, level=None,
                     scope=None, geography_scope=None, is_grand=None):
    """Classify a row's aggregation role from its is_total flag + dimensions.
      leaf                  -> not a total (safe to sum within its partition)
      grand_total           -> THE group total the canonical leaves sum to
      subtotal              -> intermediate aggregate (STEG over carriers; Gasoil over variants)
      alternative_breakdown -> a SECOND partition of the same total (gas-demand pressure HP/MBP)
    `is_grand` (when the caller knows) forces grand_total; otherwise inferred."""
    if not is_total:
        return "leaf"
    if is_grand:
        return "grand_total"
    # gas-demand pressure rows re-partition the demand total -> alternative
    if level and flow == "flow.demand":
        return "alternative_breakdown"
    # gas-demand non_power usage row is a subtotal (= HP+MBP) but still a usage leaf's
    # sibling; treat the non_power aggregate as subtotal, not grand_total
    if scope == "non_power" and flow == "flow.demand":
        return "subtotal"
    # electricity 'production pour/disponible pour marché local' are ALTERNATIVE
    # aggregates (production minus exports/échanges), not the national grand total.
    if scope in ("market_local", "available_local"):
        return "alternative_breakdown"
    # supply-balance lines (échanges/exportation/importation) below the production total
    # are not the national grand total either.
    if flow in ("flow.exchanges_transfers", "flow.export", "flow.import"):
        return "alternative_breakdown"
    # the exports-only sliver (Ventes externes) is an alternative slice, not a grand total;
    # the canonical elec-sales grand totals are geography_scope local + incl_exports.
    if geography_scope == "exports_only":
        return "alternative_breakdown"
    if product in SUBTOTAL_PRODUCTS or product in SUBCHILD_PRODUCTS:
        return "subtotal"
    if producer in SUBTOTAL_PRODUCERS:
        return "subtotal"
    return "grand_total"

# ---------------------------------------------------------------- DB helper
class DB:
    def __init__(self, con):
        self.con = con
        row = con.execute("SELECT COALESCE(MAX(observation_id),0) FROM observation").fetchone()
        self._oid = row[0]
        row = con.execute("SELECT COALESCE(MAX(id),0) FROM staging_unmapped").fetchone()
        self._uid = row[0]
        self.stats = {"insert":0, "noop":0, "unmapped":0, "collision":0}

    def next_oid(self):
        self._oid += 1; return self._oid

    def upsert_observation(self, **f):
        """Idempotent insert keyed on upsert_key. Returns observation_id or None (noop)."""
        skey = series_key(
            f["indicator_id"], f.get("flow_id"), f.get("product_id"), f.get("sector_id"),
            f.get("region_id"), f.get("field_id"), f.get("level_id"), f.get("calorific_basis"),
            f["unit_id"], f["period_type"], f.get("redevance_included"),
            scope=f.get("scope"), technology=f.get("technology"), regime=f.get("regime"),
            geography_scope=f.get("geography_scope"), producer_id=f.get("producer_id"))
        ukey = upsert_key(skey, f["period_start"], f["period_end"], f["source_id"])
        exists = self.con.execute(
            "SELECT observation_id, value, source_cell FROM observation WHERE upsert_key=?",
            [ukey]).fetchone()
        if exists:
            # Idempotent re-ingest is a no-op ONLY if the payload matches. A DIFFERENT
            # value arriving at the same upsert_key means two distinct source rows
            # collapsed to one series_key (e.g. a vocab alias collision like
            # 'Essence Super' + 'Essence Sans Pb' both -> prod.gasoline_ssp). That is a
            # silent overwrite -> quarantine the colliding cell instead of dropping it.
            new_val = f.get("value")
            if exists[1] is not None and new_val is not None and abs(exists[1] - new_val) > 0.01 \
               and exists[2] != f.get("source_cell"):
                self.quarantine(f["source_id"], f.get("source_ref"), "SERIES_KEY_COLLISION",
                                f"{f.get('source_cell')} value={new_val}",
                                context=f"collides with existing {exists[2]} value={exists[1]} "
                                        f"on series_key={skey}")
                self.stats["collision"] += 1
                return None
            self.stats["noop"] += 1
            return exists[0]
        # OQ-F2 (BLOCK-4): Barka(oil)/Baraka(gas, "Maâmoura et Baraka") are kept as
        # distinct field records pending ONEM confirmation — escalate them, mirroring
        # how OQ-R1 tags primary_broad gas. Done centrally here so EVERY loader's
        # observations on these fields fire the escalation cue (the field flows from
        # bilan/memento/conjoncture alike). Don't clobber a more specific escalation
        # a loader already set (e.g. OQ-R1).
        if f.get("field_id") in ("field.barka", "field.maamoura_baraka") \
           and not f.get("is_escalated"):
            f = dict(f)  # local copy; don't mutate caller's dict
            f["is_escalated"] = True
            f["escalation_ref"] = "OQ-F2"
            f.setdefault("footnotes", [])
            if "FN-OQ-F2-FIELD" not in f["footnotes"]:
                f["footnotes"] = list(f["footnotes"]) + ["FN-OQ-F2-FIELD"]

        oid = self.next_oid()
        cols = dict(
            observation_id=oid, series_key=skey, upsert_key=ukey,
            ingested_at=datetime.datetime(2026,6,25,0,0,0),
            value=f.get("value"), value_raw=f.get("value_raw"),
            indicator_id=f["indicator_id"], unit_id=f["unit_id"],
            calorific_basis=f.get("calorific_basis","NA"),
            basis_confidence=f.get("basis_confidence","stated"),
            period_type=f["period_type"], period_start=f["period_start"],
            period_end=f["period_end"], ytd_cutoff_month=f.get("ytd_cutoff_month"),
            ref_year=f.get("ref_year"), data_status=f["data_status"],
            source_id=f["source_id"], source_page=f.get("source_page"),
            source_ref=f.get("source_ref"), source_type=f.get("source_type","table"),
            template_version=f.get("template_version"),
            extraction_method=f.get("extraction_method","coordinate_map"),
            extraction_confidence=f.get("extraction_confidence","normal"),
            source_cell=f.get("source_cell"),
            flow_id=f.get("flow_id"), product_id=f.get("product_id"),
            sector_id=f.get("sector_id"), region_id=f.get("region_id"),
            field_id=f.get("field_id"), level_id=f.get("level_id"),
            producer_id=f.get("producer_id"),
            technology=f.get("technology"), regime=f.get("regime"),
            scope=f.get("scope"), geography_scope=f.get("geography_scope"),
            redevance_toggle_id=f.get("redevance_toggle_id"),
            redevance_included=f.get("redevance_included"),
            is_total=f.get("is_total", False),
            aggregation_role=f.get("aggregation_role") or aggregation_role(
                f.get("is_total", False), flow=f.get("flow_id"), product=f.get("product_id"),
                producer=f.get("producer_id"), level=f.get("level_id"), scope=f.get("scope"),
                geography_scope=f.get("geography_scope"), is_grand=f.get("is_grand")),
            is_derived=f.get("is_derived", False), derivation_note=f.get("derivation_note"),
            is_preferred=True, confidence=f.get("confidence","normal"),
            is_escalated=f.get("is_escalated", False), escalation_ref=f.get("escalation_ref"),
        )
        keys = list(cols.keys())
        self.con.execute(
            f"INSERT INTO observation ({','.join(keys)}) VALUES ({','.join('?'*len(keys))})",
            [cols[k] for k in keys])
        self.stats["insert"] += 1
        # attach footnotes
        for fn in f.get("footnotes", []) or []:
            self.con.execute(
                "INSERT OR IGNORE INTO observation_footnote VALUES (?,?)", [oid, fn])
        return oid

    def quarantine(self, source_id, source_ref, dimension, raw_label, context=""):
        self._uid += 1
        self.con.execute(
            "INSERT INTO staging_unmapped VALUES (?,?,?,?,?,?,?)",
            [self._uid, source_id, source_ref, dimension, raw_label, context,
             datetime.datetime(2026,6,25)])
        self.stats["unmapped"] += 1

# ---------------------------------------------------------------- manifest
MONTHS_FR = {"janvier":1,"fevrier":2,"février":2,"mars":3,"avril":4,"mai":5,"juin":6,
             "juillet":7,"aout":8,"août":8,"septembre":9,"octobre":10,"novembre":11,
             "decembre":12,"décembre":12}

def load_manifest(path="manifest.csv"):
    with open(path, encoding="utf-8") as f:
        return list(csv.DictReader(f))

FAM2TYPE = {"Bilan":"bilan","Memento":"memento","Conjoncture":"conjoncture",
            "Bulletin-COVID":"covid_bulletin","Rapport":"rapport"}

def derive_source_id(row):
    """Deterministic source_id from a manifest row: <type>_<period>[_<version>][_<lang>].
    AR editions get a _ar suffix so they can be registered but flagged non-canonical."""
    fam = row["report_family"]
    # Rapport Bilan files live under Bilan family but filename says Rapport
    path = row.get("local_path","")
    typ = FAM2TYPE.get(fam, "other")
    if fam == "Bilan" and "Rapport_Bilan" in path:
        typ = "rapport"
    period = (row.get("period") or "undated").replace("-","_")
    ver = (row.get("version") or "").strip()
    lang = (row.get("language") or "").strip()
    parts = [typ, period]
    if ver:
        parts.append(re.sub(r"[^A-Za-z0-9]+","",ver))
    sid = "_".join(parts)
    # disambiguate language for non-multi single-language editions
    if lang == "ar":
        sid += "_ar"
    elif lang == "en":
        sid += "_en"
    return sid, typ

def parse_cutoff_from_period(period):
    """Return cutoff month for a YYYY-MM period (Conjoncture), else None."""
    if period and re.match(r"^\d{4}-\d{2}$", period):
        return int(period.split("-")[1])
    return None
