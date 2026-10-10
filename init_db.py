"""Build actbayan.db from actbayan (1).sql (MySQL dump) -> SQLite.
Usage: python init_db.py
Creates fresh actbayan.db, loads schema + data, adds missing columns
(reactions_data, embedding), SQLite triggers, default roles + LGU/Admin accounts.
"""
import sqlite3
import re
import os
import bcrypt

SRC = "actbayan (1).sql"
DST = "actbayan.db"

AUTOINC_PK = {"al_id": "activity_logs", "ann_id": "announcements", "contact_id": "contacts",
              "cred_id": "credentials", "feedback_id": "feedbacks", "location_id": "location",
              "log_id": "login_logs", "pass_id": "password", "registration_id": "registration",
              "report_id": "reports", "reportupd_id": "report_update", "role_id": "role",
              "upvote_id": "upvotes", "user_id": "usercreds"}


def hash_password(pw: str) -> str:
    return bcrypt.hashpw(pw.encode(), bcrypt.gensalt()).decode()


def split_statements(text):
    # Remove -- comment lines and /*! */ blocks, then split on top-level semicolons
    text = re.sub(r"/\*.*?\*/", "", text, flags=re.S)
    lines = [ln for ln in text.splitlines() if not ln.strip().startswith("--")]
    buf = "\n".join(lines)
    # Remove trigger blocks (DELIMITER $$ ... $$ DELIMITER ;) but keep the rest;
    # we recreate equivalent SQLite triggers natively in main().
    buf = re.sub(r"DELIMITER\s+\$\$.*?DELIMITER\s*;", "", buf, flags=re.S | re.I)
    parts, cur, q, depth = [], "", None, 0
    i, n = 0, len(buf)
    while i < n:
        ch = buf[i]
        if q:
            cur += ch
            if ch == "\\" and i + 1 < n:
                cur += buf[i + 1]
                i += 1
            elif ch == q:
                # '' inside string is escaped quote
                if i + 1 < n and buf[i + 1] == q:
                    cur += q
                    i += 1
                else:
                    q = None
        else:
            if ch in ("'", '"', "`"):
                q = ch
                cur += ch
            elif ch == "(":
                depth += 1
                cur += ch
            elif ch == ")":
                depth -= 1
                cur += ch
            elif ch == ";" and depth == 0:
                cur += ch
                s = cur.strip()
                if s and s != ";":
                    parts.append(s)
                cur = ""
            else:
                cur += ch
        i += 1
    out = []
    for s in parts:
        u = s.strip().upper()
        if not u:
            continue
        if u.startswith("SET ") or u.startswith("START TRANSACTION") or u in ("COMMIT;", "COMMIT"):
            continue
        if u.startswith("ALTER TABLE") or u.startswith("ADD ") or u.startswith("MODIFY"):
            continue
        if u.startswith("CREATE TABLE") or u.startswith("INSERT INTO"):
            out.append(s)
    return out


def convert_create(stmt):
    s = stmt.replace("`", "")
    s = re.sub(r"\s*ENGINE\s*=\s*InnoDB.*", "", s, flags=re.I)
    s = re.sub(r"int\s*\(\d+\)", "INTEGER", s, flags=re.I)
    s = re.sub(r"varchar\s*\(\d+\)", "TEXT", s, flags=re.I)
    s = re.sub(r"decimal\s*\([^)]*\)", "REAL", s, flags=re.I)
    s = re.sub(r"\bchar\s*\(\d+\)", "TEXT", s, flags=re.I)
    # datatype defaults
    s = re.sub(r"timestamp NOT NULL DEFAULT current_timestamp\(\)", "TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP", s, flags=re.I)
    s = re.sub(r"timestamp NULL DEFAULT NULL", "TEXT DEFAULT NULL", s, flags=re.I)
    s = re.sub(r"date NOT NULL DEFAULT current_timestamp\(\)", "TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP", s, flags=re.I)
    m = re.search(r"CREATE TABLE\s+(\w+)\s*\((.*)\)\s*;?\s*$", s, flags=re.S | re.I)
    if not m:
        return None
    tbl, body = m.group(1), m.group(2)
    # split body lines on commas not inside parens
    cols, depth, cur = [], 0, ""
    for ch in body:
        if ch == "(":
            depth += 1
        elif ch == ")":
            depth -= 1
        if ch == "," and depth == 0:
            cols.append(cur.strip())
            cur = ""
        else:
            cur += ch
    if cur.strip():
        cols.append(cur.strip())
    pk_extra = {"accounts": "account_id"}
    out = []
    for c in cols:
        cu = c.upper()
        if cu.startswith("PRIMARY KEY") or cu.startswith("KEY ") or cu.startswith("UNIQUE") or cu.startswith("CONSTRAINT") or cu.startswith("FOREIGN KEY"):
            continue
        cm = re.match(r"(\w+)\s+(.*)", c, re.S)
        if not cm:
            continue
        name, rest = cm.group(1), cm.group(2).strip()
        rest = re.sub(r"PRIMARY KEY", "", rest, flags=re.I).strip()
        rest = re.sub(r"\s+", " ", rest)
        if AUTOINC_PK.get(name) == tbl:
            out.append(f"{name} INTEGER PRIMARY KEY AUTOINCREMENT")
        elif tbl in pk_extra and name == pk_extra[tbl]:
            out.append(f"{name} INTEGER PRIMARY KEY")
        else:
            out.append(f"{name} {rest}")
    return f"CREATE TABLE IF NOT EXISTS {tbl} (\n  " + ",\n  ".join(out) + "\n);", tbl


def convert_insert(stmt):
    s = stmt.replace("`", "")
    # MySQL \' escape -> SQLite '' escape
    s = s.replace("\\'", "''").replace('\\"', '""').replace("\\n", "\n")
    s = re.sub(r"\s+ON DUPLICATE KEY UPDATE.*", "", s, flags=re.I | re.S)
    return s


def main():
    if os.path.exists(DST):
        os.remove(DST)
        print(f"Removed old {DST}")
    raw = open(SRC, encoding="utf-8", errors="ignore").read()
    stmts = split_statements(raw)
    creates, inserts = [], []
    for st in stmts:
        u = st.strip().upper()
        if u.startswith("CREATE TABLE"):
            r = convert_create(st)
            if r:
                creates.append(r)
        elif u.startswith("INSERT INTO"):
            inserts.append(convert_insert(st))
    print(f"Parsed {len(creates)} tables, {len(inserts)} insert statements")
    con = sqlite3.connect(DST)
    cur = con.cursor()
    cur.execute("PRAGMA foreign_keys=OFF")
    for sql, tbl in creates:
        cur.execute(sql)
    print("Tables created")
    for ins in inserts:
        try:
            cur.executescript(ins)
        except Exception as e:
            print("INSERT warn:", str(e)[:160])
    con.commit()
    # --- SQLite extras: missing columns, indexes, triggers ---
    def has_col(tbl, col):
        return any(r[1] == col for r in cur.execute(f"PRAGMA table_info({tbl})").fetchall())
    if not has_col("feedbacks", "reactions_data"):
        cur.execute("ALTER TABLE feedbacks ADD COLUMN reactions_data TEXT DEFAULT NULL")
    if not has_col("reports", "embedding"):
        cur.execute("ALTER TABLE reports ADD COLUMN embedding TEXT DEFAULT NULL")
    if not has_col("feedbacks", "uploaded_at"):
        cur.execute("ALTER TABLE feedbacks ADD COLUMN uploaded_at TEXT DEFAULT NULL")
    cur.execute("CREATE INDEX IF NOT EXISTS idx_reports_status ON reports(status)")
    cur.execute("CREATE INDEX IF NOT EXISTS idx_reports_cat ON reports(category)")
    cur.execute("CREATE INDEX IF NOT EXISTS idx_reports_created ON reports(created_at)")
    cur.execute("DROP TRIGGER IF EXISTS passcontact")
    cur.execute("""
    CREATE TRIGGER passcontact AFTER INSERT ON contacts
    BEGIN
      INSERT INTO usercreds(contact_id, emorph) VALUES (NEW.contact_id, NEW.email);
      INSERT INTO usercreds(contact_id, emorph) VALUES (NEW.contact_id, NEW.phone_number);
    END;""")
    cur.execute("DROP TRIGGER IF EXISTS insertuser")
    cur.execute("""
    CREATE TRIGGER insertuser AFTER INSERT ON usercreds
    BEGIN
      INSERT INTO credentials(user_id, is_locked) VALUES (NEW.user_id, 'Y');
    END;""")
    con.commit()
    # --- default roles + LGU/Admin accounts ---
    n = cur.execute("SELECT COUNT(*) FROM role").fetchone()[0]
    if n == 0:
        cur.executemany("INSERT INTO role (role_id, role) VALUES (?, ?)",
                        [(1, "resident"), (2, "lgu official"), (3, "admin")])
        con.commit()
        print("Roles seeded")
    def ensure_account(fn, ln, email, phone, role_id, pw):
        row = cur.execute("SELECT contact_id FROM contacts WHERE email=? OR phone_number=?", (email, phone)).fetchone()
        if row:
            print(f"Exists: {email}")
            return
        aid = (cur.execute("SELECT COALESCE(MAX(account_id),0)+1 FROM accounts").fetchone()[0])
        cur.execute("INSERT INTO accounts (account_id, first_name, last_name, middle_name, dob, profile_photo, account_status) VALUES (?,?,?,?,?,?,?)",
                    (aid, fn, ln, "", "1990-01-01", "", "Verified"))
        cur.execute("INSERT INTO contacts (account_id, email, phone_number) VALUES (?,?,?)", (aid, email, phone))
        cid = cur.lastrowid
        uids = [r[0] for r in cur.execute("SELECT user_id FROM usercreds WHERE contact_id=?", (cid,)).fetchall()]
        cur.execute("INSERT INTO password (password) VALUES (?)", (hash_password(pw),))
        pid = cur.lastrowid
        for uid in uids:
            cur.execute("UPDATE credentials SET pass_id=?, is_locked='N' WHERE user_id=?", (pid, uid))
            if not cur.execute("SELECT 1 FROM user_role WHERE user_id=?", (uid,)).fetchone():
                cur.execute("INSERT INTO user_role (user_id, role_id) VALUES (?,?)", (uid, role_id))
        con.commit()
        print(f"Created {fn} {ln} ({email})")
    ensure_account("LGU", "Official", "lgu@actbayan.gov.ph", "09222222222", 2, "lgu12345")
    ensure_account("System", "Admin", "admin@actbayan.gov.ph", "09111111111", 3, "admin12345")
    nrep = cur.execute("SELECT COUNT(*) FROM reports").fetchone()[0]
    nacc = cur.execute("SELECT COUNT(*) FROM accounts").fetchone()[0]
    print(f"DONE: {nacc} accounts, {nrep} reports -> {DST}")
    con.close()


if __name__ == "__main__":
    main()

