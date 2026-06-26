"""
eval/specimen_tn_dz_royalty.py — Level-1 (no-model) probes for the TN-DZ gas royalty specimen.

STAGED, not yet in the standing suite: the specimen is promoted to Layer 2 only once these
Level-1 probes pass (the decisive regularization footnote must be RETRIEVABLE, and the transit
query must read out-of-scope — otherwise no model can answer soundly and a Level-2 pass is luck).

Read-only. Re-run after the S-1 (footnote link) / S-2 (transit out-of-scope) fixes and a server
restart. `python -m eval.specimen_tn_dz_royalty`.
"""
from __future__ import annotations

import json

from eval.harness import call_tool

REDEVANCE_PCI_YTD = "redevance|flow.royalty|prod.natural_gas||||||PCI|ktep-pci|ytd_cumulative|||||"


def _probes():
    out = []

    # P1 (S-1, BLOCK): the 240 Mm³ regularization footnote must be reachable via describe_series.
    d = call_tool("describe_series", {"series_id": REDEVANCE_PCI_YTD})
    blob = json.dumps(d, ensure_ascii=False).lower()
    has_reg = any(w in blob for w in ("240", "gularis", "dépassement", "depassement",
                                      "dépassement", "overdraw", "régularis"))
    out.append(("P1_footnote_retrievable", has_reg,
                "describe_series(redevance) surfaces the 240 Mm³ regularization footnote"))

    # P2 (S-3, ADVISE): redevance resolves from FR/EN (and ideally AR-dialect).
    fr = call_tool("search_series", {"query": "redevance / forfait fiscal gaz algérien", "limit": 3})
    fr_hit = any("redevance" in (s.get("series_id", "")) for s in (fr.get("series") or []))
    out.append(("P2_redevance_resolves_fr", fr_hit, "search_series resolves redevance from FR terms"))
    ar = call_tool("search_series", {"query": "ريع جبائي على الغاز الجزائري", "limit": 3})
    ar_hit = any("redevance" in (s.get("series_id", "")) for s in (ar.get("series") or []))
    out.append(("P2b_redevance_resolves_ar_dialect", ar_hit,
                "search_series resolves redevance from AR/dialect (S-3, ADVISE — may fail)"))

    # P3: core à-fin-avril (cutoff=4) values present. The ytd series carries 12 monthly
    # cumulative points per year (cutoff 1..12) — correctly distinguished by the server
    # (CLAUDE.md #3) — so we must select cutoff_month=4, not just the year.
    gs = call_tool("get_series", {"series_id": REDEVANCE_PCI_YTD})
    pts = {(p.get("ref_year"), p.get("ytd_cutoff_month")): p.get("value")
           for p in (gs.get("observations") or [])}
    for yr, want in [(2026, 182.0), (2025, 267.0)]:
        val = pts.get((yr, 4))
        out.append((f"P3_value_{yr}_apr", val == want,
                    f"redevance ytd à-fin-avril {yr} = {want} (got {val})"))

    # P4 (S-2, BLOCK): transit-volume free-text must read out-of-scope, not return unrelated series.
    t = call_tool("search_series", {"query": "Transmed throughput Algeria to Italy", "limit": 3})
    tblob = json.dumps(t, ensure_ascii=False).lower()
    oos = any(w in tblob for w in ("out_of_scope", "out of scope", "not ingested",
                                   "also_note_out_of_scope"))
    out.append(("P4_transit_out_of_scope", oos,
                "transit-volume query carries an out-of-scope signal (S-2 — currently fails)"))

    return out


def run() -> list[dict]:
    return [{"probe": pid, "passed": bool(ok), "detail": detail} for pid, ok, detail in _probes()]


if __name__ == "__main__":
    res = run()
    for r in res:
        print(f"[{'PASS' if r['passed'] else 'FAIL'}] {r['probe']:36s} {r['detail']}")
    n = sum(r["passed"] for r in res)
    print("-" * 90)
    print(f"Specimen Level-1: {n}/{len(res)} probes pass. "
          f"{'READY to promote to Layer 2.' if n == len(res) else 'NOT ready — see specimen_tn_dz_royalty.md.'}")
