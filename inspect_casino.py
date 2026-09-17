import sqlite3, os
os.chdir(r"C:\Users\Starc\thc-test")
con = sqlite3.connect("cannabet.db")
tables = [r[0] for r in con.execute("SELECT name FROM sqlite_master WHERE type='table'")]
print("tables:", tables)
for t in tables:
    cols = [c[1] for c in con.execute(f"PRAGMA table_info({t})")]
    n = con.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0]
    print(t, "rows=", n, "cols=", cols[:10])
con.close()
