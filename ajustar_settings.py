import os, psycopg2

INSTANCE_ID = os.environ.get("EV_INSTANCE_ID")  # ex.: 4a1e5121-6a83-48c0-bf31-99e5d8e9b342
DATABASE_URL = os.environ.get("DATABASE_URL")
if not (INSTANCE_ID and DATABASE_URL):
    raise SystemExit("Defina EV_INSTANCE_ID e DATABASE_URL")

SQL = """
UPDATE "Setting"
   SET "readMessages" = FALSE,
       "readStatus"   = FALSE
 WHERE "instanceId"  = %s;
"""

with psycopg2.connect(DATABASE_URL) as conn:
    with conn.cursor() as cur:
        cur.execute(SQL, (INSTANCE_ID,))
        conn.commit()
        print("✅ readMessages/readStatus desativados para a instância:", INSTANCE_ID)
