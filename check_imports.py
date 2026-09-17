import io, re

def read(p):
    with io.open(p, encoding="utf-8") as f:
        return f.read()

m = read(r"C:\Users\Starc\thc-social\app\main.py")
print("timedelta imported:", bool(re.search(r"from datetime import.*timedelta|import.*timedelta", m)))
print("twitch_oauth module imported:", bool(re.search(r"^import.*twitch_oauth|^from \. import.*twitch_oauth", m, re.M)))
print("httpx imported:", "import httpx" in m)
# show twitch_oauth import line
for mm in re.finditer(r".*twitch_oauth.*", m):
    line = mm.group(0).strip()
    if "import" in line:
        print("tw import:", line[:120])
        break
print("timezone imported:", "timezone" in m[:2000])
