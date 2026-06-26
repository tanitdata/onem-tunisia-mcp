"""
build.py — orchestrate a full, idempotent (re-runnable) build of the reference set.
Phase A: seed + the three reference editions. Phase C/D extend this to the corpus.
"""
import duckdb
import seed as SEED
import onem_lib as L
import load_bilan, load_memento, load_conjoncture

def main():
    SEED.main()                       # fresh schema + dimensions
    con = duckdb.connect(SEED.DB_PATH)
    db = L.DB(con)
    v = L.Vocab(".")
    nb, _chk = load_bilan.load(db)
    nm = load_memento.load(db, v)
    nc, hdr = load_conjoncture.load(db, v)
    con.commit()
    print(f"\nReference build: Bilan={nb} Memento={nm} Conjoncture={nc}")
    print(f"DB stats: {db.stats}")
    total = con.execute("SELECT COUNT(*) FROM observation").fetchone()[0]
    print(f"observations total: {total}")
    con.close()

if __name__ == "__main__":
    main()
