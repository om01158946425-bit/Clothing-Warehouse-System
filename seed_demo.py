"""Fill the warehouse database with realistic demo data.

Run:  python seed_demo.py

It creates suppliers, customers, about 20 clothing products, 30 days of purchases and
sales, a cancelled sale, a cancelled purchase, a partial return and a few stock adjustments. Everything goes through the real app code, so stock,
costs and reports stay consistent. Dates are spread over the last 30 days.

It refuses to run if the database already has purchase or sales invoices, so it can't
mix demo data into real data. To start clean: stop the app, delete warehouse.db, run
python app.py once, stop it, then run this script.
"""
import random
import re
import sqlite3
import sys
from datetime import datetime, timedelta, timezone

import app as A

DAYS = 30
SUPPLIERS = {
    "Nile Textiles": ("0100 000 0001", "Cairo"),
    "Delta Garments": ("0100 000 0002", "Mahalla"),
    "Cairo Wholesale Fashion": ("0100 000 0003", "Downtown Cairo"),
}
SUPPLIER_CATEGORIES = {
    "Nile Textiles": {"T-Shirts", "Accessories", "Pants"},
    "Delta Garments": {"Jeans", "Shorts", "Skirts"},
    "Cairo Wholesale Fashion": {"Hoodies", "Jackets", "Dresses"},
}
CUSTOMERS = [
    ("Mona Ali", "0111 000 0001"), ("Karim Hassan", "0111 000 0002"),
    ("Salma Mahmoud", "0111 000 0003"), ("Youssef Adel", "0111 000 0004"),
    ("Nour Ibrahim", "0111 000 0005"), ("Hana Samir", "0111 000 0006"),
]
# sku, name, category, size, color, cost, price, reorder level
PRODUCTS = [
    ("TS-BLK-L", "Basic T-Shirt", "T-Shirts", "L", "Black", 80, 150, 10),
    ("TS-WHT-M", "Basic T-Shirt", "T-Shirts", "M", "White", 80, 150, 10),
    ("TS-NVY-XL", "Polo Shirt", "T-Shirts", "XL", "Navy", 140, 260, 6),
    ("JN-BLK-34", "Slim Jeans", "Jeans", "34", "Black", 230, 420, 8),
    ("JN-BLU-30", "Slim Jeans", "Jeans", "30", "Blue", 220, 400, 8),
    ("HD-BLK-L", "Zip Hoodie", "Hoodies", "L", "Black", 300, 550, 5),
    ("HD-NVY-M", "Pullover Hoodie", "Hoodies", "M", "Navy", 280, 500, 5),
    ("SH-KHK-32", "Chino Shorts", "Shorts", "32", "Khaki", 150, 280, 6),
    ("SH-BLK-30", "Chino Shorts", "Shorts", "30", "Black", 150, 280, 6),
    ("JK-BLK-L", "Bomber Jacket", "Jackets", "L", "Black", 420, 750, 4),
    ("JK-OLV-M", "Bomber Jacket", "Jackets", "M", "Olive", 420, 750, 4),
    ("DR-FLR-M", "Summer Dress", "Dresses", "M", "Floral", 190, 350, 5),
    ("DR-BLK-S", "Summer Dress", "Dresses", "S", "Black", 190, 350, 5),
    ("SK-DNM-M", "Denim Skirt", "Skirts", "M", "Blue", 160, 300, 5),
    ("SW-GRY-L", "Sweatpants", "Pants", "L", "Grey", 170, 320, 6),
    ("SC-WHT-OS", "Cotton Socks (3 pack)", "Accessories", "One size", "White", 35, 70, 15),
]
POPULARITY = {"T-Shirts": 3, "Jeans": 3, "Hoodies": 2, "Accessories": 2}  # others = 1

rnd = random.Random(2026)
db = sqlite3.connect(A.DB_PATH)
db.row_factory = sqlite3.Row
client = A.app.test_client()


def fail(msg):
    sys.exit("Stopped: " + msg)


def post(url, data):
    r = client.post(url, data=data)
    if r.status_code != 302:
        m = re.search(r'class="flash error">(.*?)</div>', r.get_data(as_text=True), re.S)
        fail(f"{url} was refused: {m.group(1).strip() if m else r.status_code}")


def utc_stamp(offset, minute):
    """UTC timestamp for `offset` days ago at `minute` minutes after local midnight."""
    midnight = datetime.now().astimezone().replace(hour=0, minute=0, second=0, microsecond=0)
    local = midnight - timedelta(days=offset) + timedelta(minutes=minute)
    return local.astimezone(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")


def inventory():
    return [dict(r) for r in db.execute("SELECT * FROM products ORDER BY id")]


def last_id(table):
    return db.execute(f"SELECT MAX(id) FROM {table}").fetchone()[0]


def purchase(supplier_id, lines, offset, minute):
    post("/purchases/new", {
        "supplier_id": supplier_id,
        "product_id": [p["id"] for p, _, _ in lines],
        "quantity": [q for _, q, _ in lines],
        "unit_cost": [c for _, _, c in lines]})
    pid, stamp = last_id("purchases"), utc_stamp(offset, minute)
    db.execute("UPDATE purchases SET created_at = ? WHERE id = ?", (stamp, pid))
    db.execute("UPDATE stock_movements SET created_at = ? WHERE reason = 'purchase' AND ref_id = ?", (stamp, pid))
    db.commit()


def sale(offset, minute, customer_ids):
    pool = [p for p in inventory() if p["quantity"] > 0]
    if not pool:
        return
    lines = []
    for _ in range(min(rnd.choice([1, 1, 2, 2, 3]), len(pool))):
        p = rnd.choices(pool, [POPULARITY.get(x["category"], 1) for x in pool])[0]
        pool.remove(p)
        qty = min(rnd.randint(1, 4 if p["category"] == "Accessories" else 3), p["quantity"])
        price = p["price"] * (0.9 if rnd.random() < 0.15 else 1)  # occasional 10% discount
        lines.append((p, qty, round(price, 2)))
    walk_in = rnd.random() < 0.4
    post("/sales/new", {
        "customer_id": "" if walk_in else rnd.choice(customer_ids),
        "product_id": [p["id"] for p, _, _ in lines],
        "quantity": [q for _, q, _ in lines],
        "unit_price": [pr for _, _, pr in lines]})
    sid, stamp = last_id("sales"), utc_stamp(offset, minute)
    db.execute("UPDATE sales SET created_at = ? WHERE id = ?", (stamp, sid))
    db.execute("UPDATE stock_movements SET created_at = ? WHERE reason = 'sale' AND ref_id = ?", (stamp, sid))
    db.commit()


def clearance_sale(offset, minute, customer_id, targets):
    """One large order that brings chosen products down to a target stock level."""
    lines = []
    for sku, target in targets:
        p = db.execute("SELECT * FROM products WHERE sku = ?", (sku,)).fetchone()
        if p["quantity"] > target:
            lines.append((p, p["quantity"] - target, p["price"]))
    if not lines:
        return
    post("/sales/new", {
        "customer_id": customer_id,
        "product_id": [p["id"] for p, _, _ in lines],
        "quantity": [q for _, q, _ in lines],
        "unit_price": [pr for _, _, pr in lines]})
    sid, stamp = last_id("sales"), utc_stamp(offset, minute)
    db.execute("UPDATE sales SET created_at = ? WHERE id = ?", (stamp, sid))
    db.execute("UPDATE stock_movements SET created_at = ? WHERE reason = 'sale' AND ref_id = ?", (stamp, sid))
    db.commit()


def adjust(sku, kind, qty, note, offset, minute):
    p = db.execute("SELECT id FROM products WHERE sku = ?", (sku,)).fetchone()
    post("/stock/adjust", {"product_id": p["id"], "type": kind, "quantity": qty, "note": note})
    db.execute("UPDATE stock_movements SET created_at = ? WHERE id = ?",
               (utc_stamp(offset, minute), last_id("stock_movements")))
    db.commit()


def finish_return(offset, minute):
    rid, stamp = last_id("sale_returns"), utc_stamp(offset, minute)
    db.execute("UPDATE sale_returns SET created_at = ? WHERE id = ?", (stamp, rid))
    db.execute("UPDATE stock_movements SET created_at = ? WHERE reason = 'return' AND ref_id = ?", (stamp, rid))
    db.commit()


def partial_return(offset, minute):
    """A customer brings back one pair of jeans (wrong size)."""
    row = db.execute(
        "SELECT i.id, i.sale_id FROM sale_items i JOIN sales s ON s.id = i.sale_id"
        " JOIN products p ON p.id = i.product_id WHERE p.category = 'Jeans' AND s.created_at < ?"
        " ORDER BY s.id DESC LIMIT 1", (utc_stamp(offset, minute),)).fetchone()
    if row is None:
        return
    post(f"/sales/{row['sale_id']}/return", {f"qty_{row['id']}": 1, "note": "wrong size"})
    finish_return(offset, minute)


def cancel_sale(offset, minute):
    """An admin cancels a whole sale: every item goes back to stock."""
    sale = db.execute("SELECT id FROM sales WHERE created_at < ? ORDER BY id DESC LIMIT 1 OFFSET 2",
                      (utc_stamp(offset, minute),)).fetchone()
    if sale is None:
        return
    post(f"/sales/{sale['id']}/cancel", {"reason": "customer changed their mind"})
    finish_return(offset, minute)


def mistaken_purchase(supplier_id, offset, minute):
    """A purchase typed in with an extra zero in the cost, then cancelled."""
    p = db.execute("SELECT id, cost FROM products WHERE sku = 'SW-GRY-L'").fetchone()
    post("/purchases/new", {"supplier_id": supplier_id, "product_id": [p["id"]], "quantity": [10],
                            "unit_cost": [p["cost"] * 10]})
    pid = last_id("purchases")
    post(f"/purchases/{pid}/cancel", {"reason": "entered by mistake, the cost had an extra zero"})
    t1, t2 = utc_stamp(offset, minute), utc_stamp(offset, minute + 8)
    db.execute("UPDATE purchases SET created_at = ?, cancelled_at = ? WHERE id = ?", (t1, t2, pid))
    db.execute("UPDATE stock_movements SET created_at = ? WHERE reason = 'purchase' AND ref_id = ?", (t1, pid))
    db.execute("UPDATE stock_movements SET created_at = ? WHERE reason = 'purchase_cancel' AND ref_id = ?", (t2, pid))
    db.commit()


def stock_of(sku):
    return db.execute("SELECT quantity FROM products WHERE sku = ?", (sku,)).fetchone()[0]


def main():
    if db.execute("SELECT (SELECT COUNT(*) FROM sales) + (SELECT COUNT(*) FROM purchases)").fetchone()[0]:
        fail("this database already has invoices, so demo data would mix with real data.\n"
             "To start clean: stop the app, delete warehouse.db, run python app.py once, "
             "stop it, then run this script again.")
    admin = db.execute("SELECT id FROM users WHERE role = 'admin' AND active = 1 ORDER BY id").fetchone()
    if admin is None:
        fail("no active admin user found.")
    with client.session_transaction() as s:
        s["user_id"] = admin["id"]

    # suppliers, customers, products (reuse anything that already exists)
    for name, (phone, address) in SUPPLIERS.items():
        if not db.execute("SELECT 1 FROM suppliers WHERE name = ?", (name,)).fetchone():
            post("/suppliers/new", {"name": name, "phone": phone, "address": address})
    for name, phone in CUSTOMERS:
        if not db.execute("SELECT 1 FROM customers WHERE name = ?", (name,)).fetchone():
            post("/customers/new", {"name": name, "phone": phone, "address": "Cairo"})
    for sku, name, cat, size, color, cost, price, reorder in PRODUCTS:
        if not db.execute("SELECT 1 FROM products WHERE sku = ?", (sku,)).fetchone():
            post("/products/new", {"sku": sku, "name": name, "category": cat, "size": size,
                                   "color": color, "cost": cost, "price": price,
                                   "reorder_level": reorder, "quantity": 0})
    supplier_ids = {r["name"]: r["id"] for r in db.execute("SELECT id, name FROM suppliers")}
    customer_ids = [r["id"] for r in db.execute(
        "SELECT id FROM customers WHERE name IN (%s)" % ",".join("?" * len(CUSTOMERS)),
        [n for n, _ in CUSTOMERS])]
    # the built-in starter stock counts as opening stock from the start of the period
    db.execute("UPDATE stock_movements SET created_at = ? WHERE reason = 'initial'", (utc_stamp(DAYS, 8 * 60),))
    db.commit()

    def restock(offset, everything):
        for i, (sname, cats) in enumerate(SUPPLIER_CATEGORIES.items()):
            lines = []
            for p in inventory():
                if p["category"] not in cats:
                    continue
                if not everything and p["quantity"] > p["reorder_level"] * 3:
                    continue
                qty = rnd.randint(60, 100) if p["category"] == "Accessories" else rnd.randint(25, 50)
                lines.append((p, qty, max(1, round(p["cost"] * rnd.uniform(0.97, 1.05)))))
            if lines:
                purchase(supplier_ids[sname], lines, offset, 8 * 60 + i * 15)

    restock(DAYS - 1, everything=True)  # first delivery fills every product
    for offset in range(DAYS - 1, -1, -1):
        if offset in (28, 21, 14, 7):
            restock(offset, everything=False)
        now_min = datetime.now().hour * 60 + datetime.now().minute
        if offset == 0:
            minutes = sorted(rnd.randint(5, max(6, now_min - 1)) for _ in range(rnd.randint(1, 3)))
        else:
            minutes = sorted(rnd.randint(9 * 60, 20 * 60 + 59) for _ in range(rnd.randint(2, 5)))
        for m in minutes:
            sale(offset, m, customer_ids)
        if offset == 20 and stock_of("HD-BLK-L") > 1:
            adjust("HD-BLK-L", "damaged", 1, "torn zipper", offset, 21 * 60 + 30)
        if offset == 14 and stock_of("TS-BLK-M") > 1:
            adjust("TS-BLK-M", "count", stock_of("TS-BLK-M") - 1, "monthly stock count", offset, 21 * 60 + 40)
        if offset == 9:
            cancel_sale(offset, 21 * 60 + 45)
        if offset == 12:
            mistaken_purchase(supplier_ids["Nile Textiles"], offset, 21 * 60 + 50)
        if offset == 6:
            partial_return(offset, 21 * 60 + 20)
        if offset == 1:  # a big order leaves two products low or out of stock for the dashboard
            clearance_sale(offset, 21 * 60 + 30, customer_ids[0], [("JK-BLK-L", 2), ("DR-BLK-S", 0)])

    q = lambda sql: db.execute(sql).fetchone()[0]
    print("Demo data loaded.")
    print(f"  products:    {q('SELECT COUNT(*) FROM products')}")
    print(f"  suppliers:   {q('SELECT COUNT(*) FROM suppliers')}   customers: {q('SELECT COUNT(*) FROM customers')}")
    print(f"  purchases:   {q('SELECT COUNT(*) FROM purchases')}   sales: {q('SELECT COUNT(*) FROM sales')}   (each includes one cancelled)")
    print(f"  revenue:     {q('SELECT COALESCE(SUM(net_total),0) FROM sales_net'):,.2f} EGP net revenue over the last {DAYS} days")
    print("Open the Dashboard and Reports pages to see it. Log in with your existing accounts.")


main()
