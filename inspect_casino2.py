import sqlite3, os
os.chdir(r"C:\Users\Starc\thc-test")
con = sqlite3.connect("cannabet.db")
print("users:", list(con.execute("SELECT username, balance FROM users ORDER BY balance DESC LIMIT 20")))
con.close()
