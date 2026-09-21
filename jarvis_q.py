
import sys, json, sqlite3, base64
db = sqlite3.connect(r'C:\Users\Starc\thc-social\thc_social.db')
db.row_factory = sqlite3.Row
cur = db.cursor()
mode = sys.argv[1]
if mode == 'q':
    qparams = json.loads(base64.b64decode(sys.argv[3]).decode('utf-8')) if len(sys.argv) > 3 else []
    cur.execute(sys.argv[2], qparams)
    print(json.dumps([dict(r) for r in cur.fetchall()], ensure_ascii=False))
elif mode == 'exec':
    params = json.loads(base64.b64decode(sys.argv[3]).decode('utf-8'))
    cur.execute(sys.argv[2], params)
    db.commit()
    print(json.dumps({"rows": cur.rowcount, "lastrowid": cur.lastrowid}))
