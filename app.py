import os
import sqlite3
from functools import wraps

from flask import (Flask, flash, g, redirect, render_template, request,
                   session, url_for)
from werkzeug.security import check_password_hash, generate_password_hash

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DB_PATH = os.path.join(BASE_DIR, "warehouse.db")

app = Flask(__name__)
app.config["SECRET_KEY"] = os.environ.get("SECRET_KEY", "dev-only-change-me")


# ---------- database ----------
def get_db():
    if "db" not in g:
        g.db = sqlite3.connect(DB_PATH)
        g.db.row_factory = sqlite3.Row
        g.db.execute("PRAGMA foreign_keys = ON")
    return g.db


@app.teardown_appcontext
def close_db(_exc):
    db = g.pop("db", None)
    if db is not None:
        db.close()


def init_db():
    """Create tables and seed demo data on first run."""
    db = sqlite3.connect(DB_PATH)
    with open(os.path.join(BASE_DIR, "schema.sql"), encoding="utf-8") as f:
        schema_sql = f.read()
    tables_sql, views_sql = schema_sql.split("-- @views", 1)
    db.executescript(tables_sql)
    # older databases: add the cost snapshot column and backfill it from current product costs
    cols = [r[1] for r in db.execute("PRAGMA table_info(sale_items)")]
    ucols = [r[1] for r in db.execute("PRAGMA table_info(users)")]
    if "active" not in ucols:
        db.execute("ALTER TABLE users ADD COLUMN active INTEGER NOT NULL DEFAULT 1")
        db.commit()
    mcols = [r[1] for r in db.execute("PRAGMA table_info(stock_movements)")]
    if "note" not in mcols:
        db.execute("ALTER TABLE stock_movements ADD COLUMN note TEXT NOT NULL DEFAULT ''")
        db.commit()
    if "unit_cost" not in cols:
        db.execute("ALTER TABLE sale_items ADD COLUMN unit_cost REAL NOT NULL DEFAULT 0")
        db.execute("UPDATE sale_items SET unit_cost ="
                   " (SELECT cost FROM products WHERE products.id = sale_items.product_id)")
        db.commit()
    # older databases: stock_movements must accept the new 'return' reason (SQLite can't edit a CHECK, so rebuild)
    import re as _re
    sm_sql = db.execute("SELECT sql FROM sqlite_master WHERE type='table' AND name='stock_movements'").fetchone()[0]
    if "'purchase_cancel'" not in sm_sql:
        bak = os.path.join(BASE_DIR, "warehouse-backup-before-returns.db")
        if not os.path.exists(bak):  # keep a safety copy before touching an older database
            dest = sqlite3.connect(bak)
            db.backup(dest)
            dest.close()
        ddl = _re.search(r"CREATE TABLE IF NOT EXISTS stock_movements \(.*?\n\);", schema_sql, _re.S).group(0)
        db.executescript(
            "PRAGMA foreign_keys = OFF; BEGIN;"
            " ALTER TABLE stock_movements RENAME TO stock_movements_old;"
            + ddl.replace("IF NOT EXISTS ", "") +
            " INSERT INTO stock_movements (id, product_id, change, reason, ref_id, user_id, note, created_at)"
            " SELECT id, product_id, change, reason, ref_id, user_id, note, created_at FROM stock_movements_old;"
            " DROP TABLE stock_movements_old; COMMIT; PRAGMA foreign_keys = ON;")
    def has_col(table, col):
        return col in [r[1] for r in db.execute(f"PRAGMA table_info({table})")]
    if not has_col("sale_returns", "is_cancel"):
        db.execute("ALTER TABLE sale_returns ADD COLUMN is_cancel INTEGER NOT NULL DEFAULT 0")
    for col, ddl in (("cancelled_at", "TEXT"), ("cancelled_by", "INTEGER REFERENCES users(id)"),
                     ("cancel_reason", "TEXT NOT NULL DEFAULT ''")):
        if not has_col("purchases", col):
            db.execute(f"ALTER TABLE purchases ADD COLUMN {col} {ddl}")
    if not has_col("products", "low_alert_sent"):
        db.execute("ALTER TABLE products ADD COLUMN low_alert_sent INTEGER NOT NULL DEFAULT 0")
        # items that are already low at upgrade time count as "already seen", so no alert storm later
        db.execute("UPDATE products SET low_alert_sent = 1 WHERE quantity <= reorder_level")
    db.commit()
    db.executescript("DROP VIEW IF EXISTS sale_items_net; DROP VIEW IF EXISTS sales_net;" + views_sql)
    if db.execute("SELECT COUNT(*) FROM users").fetchone()[0] == 0:
        db.executemany(
            "INSERT INTO users (username, password_hash, full_name, role) VALUES (?,?,?,?)",
            [
                ("admin", generate_password_hash("admin123"), "Warehouse Admin", "admin"),
                ("keeper", generate_password_hash("keeper123"), "Store Keeper", "storekeeper"),
            ],
        )
        demo = [
            ("TS-BLK-M", "Basic T-Shirt", "T-Shirts", "M", "Black", 80, 150, 40, 10),
            ("TS-WHT-L", "Basic T-Shirt", "T-Shirts", "L", "White", 80, 150, 8, 10),
            ("JN-BLU-32", "Slim Jeans", "Jeans", "32", "Blue", 220, 400, 25, 8),
            ("HD-GRY-XL", "Zip Hoodie", "Hoodies", "XL", "Grey", 300, 550, 3, 5),
        ]
        for p in demo:
            cur = db.execute(
                "INSERT INTO products (sku,name,category,size,color,cost,price,quantity,reorder_level)"
                " VALUES (?,?,?,?,?,?,?,?,?)", p)
            db.execute(
                "INSERT INTO stock_movements (product_id, change, reason) VALUES (?,?, 'initial')",
                (cur.lastrowid, p[7]))
        db.commit()
    db.close()


# ---------- auth ----------
@app.before_request
def load_user():
    g.user = None
    uid = session.get("user_id")
    if uid is not None:
        g.user = get_db().execute("SELECT * FROM users WHERE id = ?", (uid,)).fetchone()
        if g.user is not None and not g.user["active"]:
            g.user = None
            session.clear()


def login_required(view):
    @wraps(view)
    def wrapped(*a, **kw):
        if g.user is None:
            return redirect(url_for("login", next=request.path))
        return view(*a, **kw)
    return wrapped


def admin_required(view):
    @wraps(view)
    def wrapped(*a, **kw):
        if g.user is None:
            return redirect(url_for("login", next=request.path))
        if g.user["role"] != "admin":
            flash("Only an admin can do that.", "error")
            return redirect(url_for("products"))
        return view(*a, **kw)
    return wrapped


@app.route("/login", methods=["GET", "POST"])
def login():
    if request.method == "POST":
        user = get_db().execute(
            "SELECT * FROM users WHERE username = ?",
            (request.form.get("username", "").strip().lower(),)).fetchone()
        if (user and user["active"]
                and check_password_hash(user["password_hash"], request.form.get("password", ""))):
            session.clear()
            session["user_id"] = user["id"]
            nxt = request.args.get("next", "")
            return redirect(nxt if nxt.startswith("/") and not nxt.startswith("//") else url_for("dashboard"))
        flash("Wrong username or password.", "error")
    return render_template("login.html")


@app.route("/logout", methods=["POST"])
def logout():
    session.clear()
    return redirect(url_for("login"))


# ---------- dashboard ----------
import math
from datetime import datetime, timedelta


def nice_max(v):
    """Round a chart maximum up to a tidy number (1, 2, 2.5, 5 times a power of ten)."""
    if v <= 0:
        return 1
    exp = 10 ** math.floor(math.log10(v))
    for m in (1, 2, 2.5, 5, 10):
        if v <= m * exp:
            return m * exp


def axis_label(n):
    return f"{n / 1000:g}k" if n >= 1000 else f"{n:g}"


@app.route("/")
@login_required
def dashboard():
    db = get_db()
    today = datetime.now().date()
    start = today - timedelta(days=29)
    rng = (start.isoformat(), today.isoformat())
    date_sql = "date(s.created_at,'localtime') BETWEEN ? AND ?"

    stats = db.execute(
        "SELECT COUNT(*) AS items, COALESCE(SUM(quantity),0) AS units,"
        " COALESCE(SUM(quantity*cost),0) AS value FROM products").fetchone()
    low = db.execute(
        "SELECT * FROM products WHERE quantity <= reorder_level ORDER BY quantity").fetchall()

    # sales per day for the last 30 days (days with no sales count as zero)
    sold = {r["d"]: r for r in db.execute(
        "SELECT date(s.created_at,'localtime') AS d, COUNT(*) AS n, SUM(s.net_total) AS t"
        " FROM sales_net s WHERE " + date_sql + " GROUP BY date(s.created_at,'localtime')", rng)}
    days = []
    for i in range(30):
        d = (start + timedelta(days=i)).isoformat()
        r = sold.get(d)
        days.append({"date": d, "n": r["n"] if r else 0, "total": r["t"] if r else 0.0})
    axis_max = nice_max(max(x["total"] for x in days))
    for x in days:
        x["pct"] = round(x["total"] / axis_max * 100, 1)
    month_n = sum(x["n"] for x in days)
    month_revenue = sum(x["total"] for x in days)

    profit = None
    if g.user["role"] == "admin":  # profit is for admins only, like the Reports page
        cogs = db.execute(
            "SELECT COALESCE(SUM(i.quantity*i.unit_cost),0) FROM sale_items_net i"
            " JOIN sales s ON s.id = i.sale_id WHERE " + date_sql, rng).fetchone()[0]
        profit = month_revenue - cogs

    top = [dict(r) for r in db.execute(TOP_SQL + " LIMIT 5", rng)]
    for r in top:
        r["pct"] = round(r["units"] / top[0]["units"] * 100, 1)
    cats = [dict(r) for r in db.execute(
        "SELECT COALESCE(NULLIF(category,''),'Uncategorized') AS category, SUM(quantity) AS units"
        " FROM products GROUP BY 1 ORDER BY units DESC")]
    cat_max = max([c["units"] for c in cats], default=0)
    for c in cats:
        c["pct"] = round(c["units"] / cat_max * 100, 1) if cat_max else 0

    # warn if the demo account still uses its demo password (only checked for those two names)
    default_pw = (g.user["username"] in ("admin", "keeper")
                  and check_password_hash(g.user["password_hash"], g.user["username"] + "123"))
    return render_template(
        "dashboard.html", stats=stats, low=low, default_pw=default_pw, days=days,
        axis_max=axis_label(axis_max), axis_mid=axis_label(axis_max / 2), today_sales=days[-1],
        month_n=month_n, month_revenue=month_revenue, profit=profit, top=top, cats=cats)


# ---------- products ----------
def read_product_form():
    f = request.form
    try:
        data = {
            "sku": f["sku"].strip().upper(),
            "name": f["name"].strip(),
            "category": f.get("category", "").strip(),
            "size": f.get("size", "").strip(),
            "color": f.get("color", "").strip(),
            "cost": float(f.get("cost") or 0),
            "price": float(f.get("price") or 0),
            "reorder_level": int(f.get("reorder_level") or 0),
        }
    except (KeyError, ValueError):
        return None, "Check the form: cost and price must be numbers, reorder level a whole number."
    if not data["sku"] or not data["name"]:
        return None, "SKU and name are required."
    if min(data["cost"], data["price"], data["reorder_level"]) < 0:
        return None, "Cost, price and reorder level can't be negative."
    return data, None


@app.route("/products")
@login_required
def products():
    q = request.args.get("q", "").strip()
    db = get_db()
    if q:
        like = f"%{q}%"
        rows = db.execute(
            "SELECT * FROM products WHERE sku LIKE ? OR name LIKE ? OR category LIKE ?"
            " OR color LIKE ? ORDER BY name, size", (like,) * 4).fetchall()
    else:
        rows = db.execute("SELECT * FROM products ORDER BY name, size").fetchall()
    return render_template("products.html", products=rows, q=q)


@app.route("/products/new", methods=["GET", "POST"])
@login_required
def product_new():
    if request.method == "POST":
        data, err = read_product_form()
        try:
            qty = int(request.form.get("quantity") or 0)
            if qty < 0:
                raise ValueError
        except ValueError:
            data, err = None, "Starting quantity must be a whole number, 0 or more."
        if err:
            flash(err, "error")
            return render_template("product_form.html", product=request.form, is_new=True)
        db = get_db()
        try:
            cur = db.execute(
                "INSERT INTO products (sku,name,category,size,color,cost,price,quantity,reorder_level)"
                " VALUES (:sku,:name,:category,:size,:color,:cost,:price,:qty,:reorder_level)",
                {**data, "qty": qty})
        except sqlite3.IntegrityError:
            flash(f"SKU {data['sku']} already exists.", "error")
            return render_template("product_form.html", product=request.form, is_new=True)
        if qty:
            db.execute(
                "INSERT INTO stock_movements (product_id, change, reason, user_id)"
                " VALUES (?,?, 'initial', ?)", (cur.lastrowid, qty, g.user["id"]))
        db.commit()
        flash("Product added.", "ok")
        return redirect(url_for("products"))
    return render_template("product_form.html", product={}, is_new=True)


@app.route("/products/<int:pid>/edit", methods=["GET", "POST"])
@login_required
def product_edit(pid):
    db = get_db()
    product = db.execute("SELECT * FROM products WHERE id = ?", (pid,)).fetchone()
    if product is None:
        flash("Product not found.", "error")
        return redirect(url_for("products"))
    if request.method == "POST":
        data, err = read_product_form()
        if err:
            flash(err, "error")
            return render_template("product_form.html", product={**product, **request.form}, is_new=False, pid=pid)
        try:
            db.execute(
                "UPDATE products SET sku=:sku, name=:name, category=:category, size=:size,"
                " color=:color, cost=:cost, price=:price, reorder_level=:reorder_level"
                " WHERE id=:id", {**data, "id": pid})
        except sqlite3.IntegrityError:
            flash(f"SKU {data['sku']} already exists.", "error")
            return render_template("product_form.html", product={**product, **request.form}, is_new=False, pid=pid)
        db.commit()
        flash("Product updated.", "ok")
        return redirect(url_for("products"))
    return render_template("product_form.html", product=product, is_new=False, pid=pid)


@app.route("/products/<int:pid>/delete", methods=["POST"])
@admin_required
def product_delete(pid):
    db = get_db()
    used = db.execute(
        "SELECT (SELECT COUNT(*) FROM purchase_items WHERE product_id=?)"
        " + (SELECT COUNT(*) FROM sale_items WHERE product_id=?)", (pid, pid)).fetchone()[0]
    if used:
        flash("This product appears in purchases or sales, so it can't be deleted.", "error")
    else:
        db.execute("DELETE FROM products WHERE id = ?", (pid,))
        db.commit()
        flash("Product deleted.", "ok")
    return redirect(url_for("products"))


# ---------- suppliers ----------
def read_contact_form():
    f = request.form
    name = f.get("name", "").strip()
    if not name:
        return None, "Name is required."
    return {"name": name, "phone": f.get("phone", "").strip(),
            "address": f.get("address", "").strip()}, None


@app.route("/suppliers")
@login_required
def suppliers():
    rows = get_db().execute(
        "SELECT s.*, COUNT(p.id) AS purchases, COALESCE(SUM(p.total),0) AS spent"
        " FROM suppliers s LEFT JOIN purchases p ON p.supplier_id = s.id AND p.cancelled_at IS NULL"
        " GROUP BY s.id ORDER BY s.name").fetchall()
    return render_template("suppliers.html", suppliers=rows)


@app.route("/suppliers/new", methods=["GET", "POST"])
@login_required
def supplier_new():
    if request.method == "POST":
        data, err = read_contact_form()
        if err:
            flash(err, "error")
            return render_template("supplier_form.html", s=request.form, is_new=True)
        db = get_db()
        db.execute("INSERT INTO suppliers (name, phone, address) VALUES (:name,:phone,:address)", data)
        db.commit()
        flash("Supplier added.", "ok")
        return redirect(url_for("suppliers"))
    return render_template("supplier_form.html", s={}, is_new=True)


@app.route("/suppliers/<int:sid>/edit", methods=["GET", "POST"])
@login_required
def supplier_edit(sid):
    db = get_db()
    s = db.execute("SELECT * FROM suppliers WHERE id = ?", (sid,)).fetchone()
    if s is None:
        flash("Supplier not found.", "error")
        return redirect(url_for("suppliers"))
    if request.method == "POST":
        data, err = read_contact_form()
        if err:
            flash(err, "error")
            return render_template("supplier_form.html", s={**s, **request.form}, is_new=False)
        db.execute("UPDATE suppliers SET name=:name, phone=:phone, address=:address WHERE id=:id",
                   {**data, "id": sid})
        db.commit()
        flash("Supplier updated.", "ok")
        return redirect(url_for("suppliers"))
    return render_template("supplier_form.html", s=s, is_new=False)


@app.route("/suppliers/<int:sid>/delete", methods=["POST"])
@admin_required
def supplier_delete(sid):
    db = get_db()
    if db.execute("SELECT COUNT(*) FROM purchases WHERE supplier_id = ?", (sid,)).fetchone()[0]:
        flash("This supplier has purchase invoices, so it can't be deleted.", "error")
    else:
        db.execute("DELETE FROM suppliers WHERE id = ?", (sid,))
        db.commit()
        flash("Supplier deleted.", "ok")
    return redirect(url_for("suppliers"))


# ---------- purchases ----------
@app.route("/purchases")
@login_required
def purchases():
    rows = get_db().execute(
        "SELECT p.*, s.name AS supplier, u.full_name AS user_name,"
        " (SELECT COUNT(*) FROM purchase_items i WHERE i.purchase_id = p.id) AS lines"
        " FROM purchases p JOIN suppliers s ON s.id = p.supplier_id"
        " JOIN users u ON u.id = p.user_id ORDER BY p.id DESC").fetchall()
    return render_template("purchases.html", purchases=rows)


def parse_purchase_rows(db):
    """Return (items, error). items = [(product_id, qty, unit_cost)]."""
    pids = request.form.getlist("product_id")
    qtys = request.form.getlist("quantity")
    costs = request.form.getlist("unit_cost")
    items = []
    for pid, qty, cost in zip(pids, qtys, costs):
        if not (pid or qty.strip() or cost.strip()):
            continue  # empty row
        try:
            pid_i, qty_i, cost_f = int(pid), int(qty), float(cost)
        except ValueError:
            return None, "Each line needs a product, a whole-number quantity and a unit cost."
        if qty_i <= 0 or cost_f < 0:
            return None, "Quantity must be at least 1 and unit cost can't be negative."
        if db.execute("SELECT 1 FROM products WHERE id = ?", (pid_i,)).fetchone() is None:
            return None, "One of the selected products doesn't exist."
        items.append((pid_i, qty_i, cost_f))
    if not items:
        return None, "Add at least one product line."
    return items, None


@app.route("/purchases/new", methods=["GET", "POST"])
@login_required
def purchase_new():
    db = get_db()
    sup = db.execute("SELECT * FROM suppliers ORDER BY name").fetchall()
    prods = db.execute("SELECT * FROM products ORDER BY name, size").fetchall()
    rows = [{"product_id": "", "quantity": "", "unit_cost": ""}]
    selected = ""
    if request.method == "POST":
        selected = request.form.get("supplier_id", "")
        rows = [{"product_id": a, "quantity": b, "unit_cost": c} for a, b, c in zip(
            request.form.getlist("product_id"), request.form.getlist("quantity"),
            request.form.getlist("unit_cost"))] or rows
        items, err = parse_purchase_rows(db)
        if not err and db.execute("SELECT 1 FROM suppliers WHERE id = ?", (selected,)).fetchone() is None:
            err = "Choose a supplier."
        if err:
            flash(err, "error")
        else:
            total = sum(q * c for _, q, c in items)
            cur = db.execute(
                "INSERT INTO purchases (supplier_id, user_id, total) VALUES (?,?,?)",
                (selected, g.user["id"], total))
            pid_inv = cur.lastrowid
            for product_id, qty, cost in items:
                db.execute(
                    "INSERT INTO purchase_items (purchase_id, product_id, quantity, unit_cost)"
                    " VALUES (?,?,?,?)", (pid_inv, product_id, qty, cost))
                # stock goes up and the product's cost follows the latest purchase price
                db.execute("UPDATE products SET quantity = quantity + ?, cost = ? WHERE id = ?",
                           (qty, cost, product_id))
                db.execute(
                    "INSERT INTO stock_movements (product_id, change, reason, ref_id, user_id)"
                    " VALUES (?,?, 'purchase', ?, ?)", (product_id, qty, pid_inv, g.user["id"]))
            db.commit()
            flash(f"Purchase #{pid_inv} saved. Stock updated.", "ok")
            return redirect(url_for("purchase_detail", pid=pid_inv))
    return render_template("purchase_form.html", suppliers=sup, products=prods,
                           rows=rows, selected=str(selected))


@app.route("/purchases/<int:pid>")
@login_required
def purchase_detail(pid):
    db = get_db()
    p = db.execute(
        "SELECT p.*, s.name AS supplier, s.phone, u.full_name AS user_name, cb.full_name AS cancelled_by_name FROM purchases p"
        " JOIN suppliers s ON s.id = p.supplier_id JOIN users u ON u.id = p.user_id"
        " LEFT JOIN users cb ON cb.id = p.cancelled_by WHERE p.id = ?", (pid,)).fetchone()
    if p is None:
        flash("Purchase not found.", "error")
        return redirect(url_for("purchases"))
    items = db.execute(
        "SELECT i.*, pr.sku, pr.name, pr.size, pr.color FROM purchase_items i"
        " JOIN products pr ON pr.id = i.product_id WHERE i.purchase_id = ?", (pid,)).fetchall()
    return render_template("purchase_detail.html", p=p, items=items)


# ---------- customers ----------
@app.route("/customers")
@login_required
def customers():
    rows = get_db().execute(
        "SELECT c.*, COUNT(s.id) AS sales, COALESCE(SUM(s.net_total),0) AS spent"
        " FROM customers c LEFT JOIN sales_net s ON s.customer_id = c.id"
        " GROUP BY c.id ORDER BY c.name").fetchall()
    return render_template("customers.html", customers=rows)


@app.route("/customers/new", methods=["GET", "POST"])
@login_required
def customer_new():
    if request.method == "POST":
        data, err = read_contact_form()
        if err:
            flash(err, "error")
            return render_template("customer_form.html", c=request.form, is_new=True)
        db = get_db()
        db.execute("INSERT INTO customers (name, phone, address) VALUES (:name,:phone,:address)", data)
        db.commit()
        flash("Customer added.", "ok")
        return redirect(url_for("customers"))
    return render_template("customer_form.html", c={}, is_new=True)


@app.route("/customers/<int:cid>/edit", methods=["GET", "POST"])
@login_required
def customer_edit(cid):
    db = get_db()
    c = db.execute("SELECT * FROM customers WHERE id = ?", (cid,)).fetchone()
    if c is None:
        flash("Customer not found.", "error")
        return redirect(url_for("customers"))
    if request.method == "POST":
        data, err = read_contact_form()
        if err:
            flash(err, "error")
            return render_template("customer_form.html", c={**c, **request.form}, is_new=False)
        db.execute("UPDATE customers SET name=:name, phone=:phone, address=:address WHERE id=:id",
                   {**data, "id": cid})
        db.commit()
        flash("Customer updated.", "ok")
        return redirect(url_for("customers"))
    return render_template("customer_form.html", c=c, is_new=False)


@app.route("/customers/<int:cid>/delete", methods=["POST"])
@admin_required
def customer_delete(cid):
    db = get_db()
    if db.execute("SELECT COUNT(*) FROM sales WHERE customer_id = ?", (cid,)).fetchone()[0]:
        flash("This customer has sales invoices, so they can't be deleted.", "error")
    else:
        db.execute("DELETE FROM customers WHERE id = ?", (cid,))
        db.commit()
        flash("Customer deleted.", "ok")
    return redirect(url_for("customers"))


# ---------- sales ----------
@app.route("/sales")
@login_required
def sales():
    rows = get_db().execute(
        "SELECT s.*, COALESCE(c.name, 'Walk-in customer') AS customer, u.full_name AS user_name,"
        " COALESCE((SELECT SUM(r.total) FROM sale_returns r WHERE r.sale_id = s.id),0) AS returned,"
        " EXISTS(SELECT 1 FROM sale_returns r WHERE r.sale_id = s.id AND r.is_cancel = 1) AS cancelled,"
        " (SELECT COUNT(*) FROM sale_items i WHERE i.sale_id = s.id) AS lines"
        " FROM sales s LEFT JOIN customers c ON c.id = s.customer_id"
        " JOIN users u ON u.id = s.user_id ORDER BY s.id DESC").fetchall()
    return render_template("sales.html", sales=rows)


def parse_sale_rows(db):
    """Return (items, error). items = [(product_id, qty, unit_price)], stock-checked."""
    pids = request.form.getlist("product_id")
    qtys = request.form.getlist("quantity")
    prices = request.form.getlist("unit_price")
    items = []
    for pid, qty, price in zip(pids, qtys, prices):
        if not (pid or qty.strip() or price.strip()):
            continue  # empty row
        try:
            pid_i, qty_i, price_f = int(pid), int(qty), float(price)
        except ValueError:
            return None, "Each line needs a product, a whole-number quantity and a unit price."
        if qty_i <= 0 or price_f < 0:
            return None, "Quantity must be at least 1 and unit price can't be negative."
        items.append((pid_i, qty_i, price_f))
    if not items:
        return None, "Add at least one product line."
    # stock check on the combined quantity per product (the same product may be on two lines)
    wanted = {}
    for pid_i, qty_i, _ in items:
        wanted[pid_i] = wanted.get(pid_i, 0) + qty_i
    for pid_i, qty_i in wanted.items():
        p = db.execute("SELECT sku, name, size, color, quantity FROM products WHERE id = ?",
                       (pid_i,)).fetchone()
        if p is None:
            return None, "One of the selected products doesn't exist."
        if qty_i > p["quantity"]:
            return None, (f"Not enough stock for {p['sku']} ({p['name']}, {p['size']}, {p['color']}): "
                          f"you asked for {qty_i}, only {p['quantity']} in stock.")
    return items, None


@app.route("/sales/new", methods=["GET", "POST"])
@login_required
def sale_new():
    db = get_db()
    custs = db.execute("SELECT * FROM customers ORDER BY name").fetchall()
    prods = db.execute("SELECT * FROM products WHERE quantity > 0 ORDER BY name, size").fetchall()
    rows = [{"product_id": "", "quantity": "", "unit_price": ""}]
    selected = ""
    if request.method == "POST":
        selected = request.form.get("customer_id", "")
        rows = [{"product_id": a, "quantity": b, "unit_price": c} for a, b, c in zip(
            request.form.getlist("product_id"), request.form.getlist("quantity"),
            request.form.getlist("unit_price"))] or rows
        items, err = parse_sale_rows(db)
        customer_id = None
        if not err and selected:
            if db.execute("SELECT 1 FROM customers WHERE id = ?", (selected,)).fetchone() is None:
                err = "Choose a customer from the list, or leave it as walk-in."
            else:
                customer_id = int(selected)
        if err:
            flash(err, "error")
        else:
            total = sum(q * p for _, q, p in items)
            try:
                cur = db.execute("INSERT INTO sales (customer_id, user_id, total) VALUES (?,?,?)",
                                 (customer_id, g.user["id"], total))
                sale_id = cur.lastrowid
                for product_id, qty, price in items:
                    cost = db.execute("SELECT cost FROM products WHERE id = ?",
                                      (product_id,)).fetchone()[0]
                    db.execute(
                        "INSERT INTO sale_items (sale_id, product_id, quantity, unit_price, unit_cost)"
                        " VALUES (?,?,?,?,?)", (sale_id, product_id, qty, price, cost))
                    # guarded update: never lets stock go below zero
                    res = db.execute(
                        "UPDATE products SET quantity = quantity - ? WHERE id = ? AND quantity >= ?",
                        (qty, product_id, qty))
                    if res.rowcount != 1:
                        raise ValueError("Stock changed while saving. Please try again.")
                    db.execute(
                        "INSERT INTO stock_movements (product_id, change, reason, ref_id, user_id)"
                        " VALUES (?,?, 'sale', ?, ?)", (product_id, -qty, sale_id, g.user["id"]))
                db.commit()
            except ValueError as e:
                db.rollback()
                flash(str(e), "error")
            else:
                flash(f"Sale #{sale_id} saved. Stock updated.", "ok")
                return redirect(url_for("sale_detail", sid=sale_id))
    return render_template("sale_form.html", customers=custs, products=prods,
                           rows=rows, selected=str(selected))


SALE_ITEMS_WITH_RETURNS = (
    "SELECT i.*, pr.sku, pr.name, pr.size, pr.color,"
    " COALESCE((SELECT SUM(ri.quantity) FROM sale_return_items ri WHERE ri.sale_item_id = i.id),0) AS returned"
    " FROM sale_items i JOIN products pr ON pr.id = i.product_id WHERE i.sale_id = ? ORDER BY i.id")


def load_sale(db, sid):
    return db.execute(
        "SELECT s.*, COALESCE(c.name, 'Walk-in customer') AS customer, c.phone, u.full_name AS user_name"
        " FROM sales s LEFT JOIN customers c ON c.id = s.customer_id JOIN users u ON u.id = s.user_id"
        " WHERE s.id = ?", (sid,)).fetchone()


@app.route("/sales/<int:sid>")
@login_required
def sale_detail(sid):
    db = get_db()
    s = load_sale(db, sid)
    if s is None:
        flash("Sale not found.", "error")
        return redirect(url_for("sales"))
    items = db.execute(SALE_ITEMS_WITH_RETURNS, (sid,)).fetchall()
    returns = db.execute(
        "SELECT r.*, u.full_name AS user_name FROM sale_returns r JOIN users u ON u.id = r.user_id"
        " WHERE r.sale_id = ? ORDER BY r.id", (sid,)).fetchall()
    ritems = {}
    for r in db.execute(
            "SELECT ri.*, pr.sku, pr.name, pr.size, pr.color FROM sale_return_items ri"
            " JOIN sale_returns rt ON rt.id = ri.return_id JOIN products pr ON pr.id = ri.product_id"
            " WHERE rt.sale_id = ? ORDER BY ri.id", (sid,)):
        ritems.setdefault(r["return_id"], []).append(r)
    returned_total = sum(r["total"] for r in returns)
    cancel = next((r for r in returns if r["is_cancel"]), None)
    can_return = cancel is None and any(i["quantity"] - i["returned"] > 0 for i in items)
    return render_template("sale_detail.html", s=s, items=items, returns=returns, ritems=ritems,
                           returned_total=returned_total, can_return=can_return, cancel=cancel)


@app.route("/sales/<int:sid>/return", methods=["GET", "POST"])
@login_required
def sale_return(sid):
    db = get_db()
    s = load_sale(db, sid)
    if s is None:
        flash("Sale not found.", "error")
        return redirect(url_for("sales"))
    items = db.execute(SALE_ITEMS_WITH_RETURNS, (sid,)).fetchall()
    if db.execute("SELECT 1 FROM sale_returns WHERE sale_id = ? AND is_cancel = 1", (sid,)).fetchone():
        flash("This sale was cancelled, so there is nothing left to return.", "error")
        return redirect(url_for("sale_detail", sid=sid))
    if not any(i["quantity"] - i["returned"] > 0 for i in items):
        flash("Everything on this sale has already been returned.", "error")
        return redirect(url_for("sale_detail", sid=sid))
    if request.method == "POST":
        f = request.form
        note = f.get("note", "").strip()[:200]
        picked, err = [], None
        for i in items:
            raw = f.get(f"qty_{i['id']}", "0").strip() or "0"
            try:
                q = int(raw)
            except ValueError:
                err = "Quantities must be whole numbers."
                break
            left = i["quantity"] - i["returned"]
            if q < 0 or q > left:
                err = f"You can return at most {left} of {i['sku']}."
                break
            if q:
                picked.append((i, q))
        if not err and not picked:
            err = "Enter a quantity to return for at least one line."
        if err:
            flash(err, "error")
        else:
            total = sum(q * i["unit_price"] for i, q in picked)
            rid = db.execute(
                "INSERT INTO sale_returns (sale_id, user_id, total, note) VALUES (?,?,?,?)",
                (sid, g.user["id"], total, note)).lastrowid
            for i, q in picked:
                db.execute(
                    "INSERT INTO sale_return_items (return_id, sale_item_id, product_id, quantity, unit_price, unit_cost)"
                    " VALUES (?,?,?,?,?,?)", (rid, i["id"], i["product_id"], q, i["unit_price"], i["unit_cost"]))
                db.execute("UPDATE products SET quantity = quantity + ? WHERE id = ?", (q, i["product_id"]))
                db.execute(
                    "INSERT INTO stock_movements (product_id, change, reason, ref_id, user_id, note)"
                    " VALUES (?,?, 'return', ?, ?, ?)",
                    (i["product_id"], q, rid, g.user["id"],
                     f"Return of sale #{sid}" + (f": {note}" if note else "")))
            db.commit()
            flash(f"Return #{rid} saved: {total:,.2f} EGP refunded and the items are back in stock.", "ok")
            return redirect(url_for("sale_detail", sid=sid))
    return render_template("sale_return.html", s=s, items=items, form=request.form)


@app.route("/sales/<int:sid>/cancel", methods=["POST"])
@admin_required
def sale_cancel(sid):
    """Cancel a whole sale: everything still sold goes back to stock and the sale leaves the reports."""
    db = get_db()
    s = load_sale(db, sid)
    if s is None:
        flash("Sale not found.", "error")
        return redirect(url_for("sales"))
    reason = request.form.get("reason", "").strip()[:200]
    if not reason:
        flash("Write a reason to cancel the sale.", "error")
        return redirect(url_for("sale_detail", sid=sid))
    if db.execute("SELECT 1 FROM sale_returns WHERE sale_id = ? AND is_cancel = 1", (sid,)).fetchone():
        flash("This sale is already cancelled.", "error")
        return redirect(url_for("sale_detail", sid=sid))
    items = db.execute(SALE_ITEMS_WITH_RETURNS, (sid,)).fetchall()
    left = [(i, i["quantity"] - i["returned"]) for i in items if i["quantity"] - i["returned"] > 0]
    total = sum(q * i["unit_price"] for i, q in left)
    rid = db.execute(
        "INSERT INTO sale_returns (sale_id, user_id, total, note, is_cancel) VALUES (?,?,?,?,1)",
        (sid, g.user["id"], total, f"Sale cancelled: {reason}")).lastrowid
    for i, q in left:
        db.execute(
            "INSERT INTO sale_return_items (return_id, sale_item_id, product_id, quantity, unit_price, unit_cost)"
            " VALUES (?,?,?,?,?,?)", (rid, i["id"], i["product_id"], q, i["unit_price"], i["unit_cost"]))
        db.execute("UPDATE products SET quantity = quantity + ? WHERE id = ?", (q, i["product_id"]))
        db.execute(
            "INSERT INTO stock_movements (product_id, change, reason, ref_id, user_id, note)"
            " VALUES (?,?, 'return', ?, ?, ?)",
            (i["product_id"], q, rid, g.user["id"], f"Sale #{sid} cancelled: {reason}"))
    db.commit()
    units = sum(q for _, q in left)
    flash(f"Sale #{sid} cancelled. {units} item{'' if units == 1 else 's'} back in stock.", "ok")
    return redirect(url_for("sale_detail", sid=sid))


@app.route("/purchases/<int:pid>/cancel", methods=["POST"])
@admin_required
def purchase_cancel(pid):
    """Cancel a purchase entered by mistake: its quantities leave stock again."""
    db = get_db()
    p = db.execute("SELECT * FROM purchases WHERE id = ?", (pid,)).fetchone()
    if p is None:
        flash("Purchase not found.", "error")
        return redirect(url_for("purchases"))
    reason = request.form.get("reason", "").strip()[:200]
    if not reason:
        flash("Write a reason to cancel the purchase.", "error")
        return redirect(url_for("purchase_detail", pid=pid))
    if p["cancelled_at"]:
        flash("This purchase is already cancelled.", "error")
        return redirect(url_for("purchase_detail", pid=pid))
    lines = db.execute(
        "SELECT i.product_id, SUM(i.quantity) AS qty, pr.sku, pr.quantity AS in_stock"
        " FROM purchase_items i JOIN products pr ON pr.id = i.product_id"
        " WHERE i.purchase_id = ? GROUP BY i.product_id", (pid,)).fetchall()
    short = [f"{l['sku']} (bought {l['qty']}, only {l['in_stock']} in stock)" for l in lines if l["in_stock"] < l["qty"]]
    if short:
        flash("Can't cancel this purchase because some of that stock is already gone: "
              + ", ".join(short) + ". Cancel or return the sales that used it first.", "error")
        return redirect(url_for("purchase_detail", pid=pid))
    for l in lines:
        res = db.execute("UPDATE products SET quantity = quantity - ? WHERE id = ? AND quantity >= ?",
                         (l["qty"], l["product_id"], l["qty"]))
        if res.rowcount != 1:
            db.rollback()
            flash("Stock changed while saving. Please try again.", "error")
            return redirect(url_for("purchase_detail", pid=pid))
        db.execute(
            "INSERT INTO stock_movements (product_id, change, reason, ref_id, user_id, note)"
            " VALUES (?,?, 'purchase_cancel', ?, ?, ?)",
            (l["product_id"], -l["qty"], pid, g.user["id"], f"Purchase #{pid} cancelled: {reason}"))
        # the cost goes back to the latest price from the other purchases, if there is one
        prev = db.execute(
            "SELECT i.unit_cost FROM purchase_items i JOIN purchases q ON q.id = i.purchase_id"
            " WHERE i.product_id = ? AND q.id != ? AND q.cancelled_at IS NULL"
            " ORDER BY q.id DESC, i.id DESC LIMIT 1", (l["product_id"], pid)).fetchone()
        if prev:
            db.execute("UPDATE products SET cost = ? WHERE id = ?", (prev["unit_cost"], l["product_id"]))
    db.execute("UPDATE purchases SET cancelled_at = CURRENT_TIMESTAMP, cancelled_by = ?, cancel_reason = ?"
               " WHERE id = ?", (g.user["id"], reason, pid))
    db.commit()
    flash(f"Purchase #{pid} cancelled and its stock taken out.", "ok")
    return redirect(url_for("purchase_detail", pid=pid))


# ---------- reports ----------
import csv
import io
from datetime import datetime, timezone

from flask import Response


@app.template_filter("local")
def local_time(value):
    """Show stored UTC timestamps in the computer's own time zone."""
    try:
        dt = datetime.strptime(value, "%Y-%m-%d %H:%M:%S").replace(tzinfo=timezone.utc)
        return dt.astimezone().strftime("%Y-%m-%d %H:%M")
    except (TypeError, ValueError):
        return value


REASON_LABELS = {"initial": "Starting stock", "purchase": "Purchase", "return": "Customer return", "purchase_cancel": "Purchase cancelled",
                 "sale": "Sale", "adjustment": "Adjustment"}


def date_range():
    """Read ?from= and ?to= (YYYY-MM-DD). Blank or invalid means no limit."""
    out = []
    for key in ("from", "to"):
        v = request.args.get(key, "").strip()
        try:
            datetime.strptime(v, "%Y-%m-%d")
        except ValueError:
            v = ""
        out.append(v)
    return out[0], out[1]


def bounds(d1, d2):
    return d1 or "0000-01-01", d2 or "9999-12-31"


def csv_response(filename, header, rows):
    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerow(header)
    for r in rows:
        w.writerow([
            ("'" + c) if isinstance(c, str) and c[:1] in ("=", "+", "-", "@") else
            (round(c, 2) if isinstance(c, float) else c)
            for c in r])
    # BOM so Excel opens it with the right encoding
    return Response("\ufeff" + buf.getvalue(), mimetype="text/csv",
                    headers={"Content-Disposition": f"attachment; filename={filename}"})


SALE_DATE = "date(s.created_at,'localtime') BETWEEN ? AND ?"
TOP_SQL = (
    "SELECT pr.sku, pr.name, pr.size, pr.color, SUM(i.quantity) AS units,"
    " SUM(i.quantity*i.unit_price) AS revenue,"
    " SUM(i.quantity*(i.unit_price-i.unit_cost)) AS profit"
    " FROM sale_items_net i JOIN sales s ON s.id = i.sale_id JOIN products pr ON pr.id = i.product_id"
    " WHERE " + SALE_DATE + " GROUP BY pr.id HAVING SUM(i.quantity) > 0 ORDER BY units DESC, revenue DESC")


@app.route("/reports")
@admin_required
def reports():
    db = get_db()
    d1, d2 = date_range()
    rng = bounds(d1, d2)
    sales_row = db.execute(
        "SELECT COUNT(*) AS n, COALESCE(SUM(s.total),0) AS gross, COALESCE(SUM(s.net_total),0) AS total"
        " FROM sales_net s WHERE " + SALE_DATE,
        rng).fetchone()
    lines_row = db.execute(
        "SELECT COALESCE(SUM(i.quantity),0) AS units, COALESCE(SUM(i.quantity*i.unit_cost),0) AS cogs"
        " FROM sale_items_net i JOIN sales s ON s.id = i.sale_id WHERE " + SALE_DATE, rng).fetchone()
    purch_row = db.execute(
        "SELECT COUNT(*) AS n, COALESCE(SUM(p.total),0) AS total FROM purchases p"
        " WHERE p.cancelled_at IS NULL AND date(p.created_at,'localtime') BETWEEN ? AND ?", rng).fetchone()
    revenue, cogs = sales_row["total"], lines_row["cogs"]
    returned = sales_row["gross"] - revenue
    profit = revenue - cogs
    margin = (profit / revenue * 100) if revenue else 0
    top = db.execute(TOP_SQL + " LIMIT 10", rng).fetchall()
    daily = db.execute(
        "SELECT date(s.created_at,'localtime') AS day, COUNT(*) AS n, SUM(s.net_total) AS total"
        " FROM sales_net s WHERE " + SALE_DATE + " GROUP BY day ORDER BY day DESC", rng).fetchall()
    return render_template("reports.html", d1=d1, d2=d2, sales=sales_row, lines=lines_row,
                           purch=purch_row, revenue=revenue, returned=returned, cogs=cogs, profit=profit,
                           margin=margin, top=top, daily=daily)


@app.route("/reports/top-products.csv")
@admin_required
def reports_top_csv():
    d1, d2 = date_range()
    rows = get_db().execute(TOP_SQL, bounds(d1, d2)).fetchall()
    return csv_response("top-products.csv",
                        ["SKU", "Product", "Size", "Color", "Units sold", "Revenue", "Profit"], rows)


@app.route("/reports/sales.csv")
@admin_required
def reports_sales_csv():
    d1, d2 = date_range()
    rows = get_db().execute(
        "SELECT datetime(s.created_at,'localtime'), s.id, COALESCE(c.name,'Walk-in customer'),"
        " pr.sku, pr.name, pr.size, pr.color, i.sold_quantity, i.sold_quantity - i.quantity,"
        " i.unit_price, i.unit_cost,"
        " i.quantity*i.unit_price, i.quantity*(i.unit_price-i.unit_cost)"
        " FROM sale_items_net i JOIN sales s ON s.id = i.sale_id"
        " LEFT JOIN customers c ON c.id = s.customer_id JOIN products pr ON pr.id = i.product_id"
        " WHERE " + SALE_DATE + " ORDER BY s.id, i.id", bounds(d1, d2)).fetchall()
    return csv_response("sales-lines.csv",
                        ["Date", "Invoice", "Customer", "SKU", "Product", "Size", "Color",
                         "Quantity sold", "Quantity returned", "Unit price", "Unit cost",
                         "Net line total", "Net profit"], rows)


def movement_query(limit=None):
    d1, d2 = date_range()
    lo, hi = bounds(d1, d2)
    product = request.args.get("product", "").strip()
    reason = request.args.get("reason", "").strip()
    sql = ("SELECT m.id, m.change, m.reason, m.ref_id, m.note, datetime(m.created_at,'localtime') AS local_at,"
           " pr.sku, pr.name, pr.size, pr.color, u.full_name AS user_name, rt.sale_id AS return_sale_id"
           " FROM stock_movements m JOIN products pr ON pr.id = m.product_id"
           " LEFT JOIN users u ON u.id = m.user_id"
           " LEFT JOIN sale_returns rt ON m.reason = 'return' AND rt.id = m.ref_id"
           " WHERE date(m.created_at,'localtime') BETWEEN ? AND ?")
    params = [lo, hi]
    if product.isdigit():
        sql += " AND m.product_id = ?"
        params.append(int(product))
    else:
        product = ""
    if reason in REASON_LABELS:
        sql += " AND m.reason = ?"
        params.append(reason)
    else:
        reason = ""
    sql += " ORDER BY m.id DESC"
    if limit:
        sql += f" LIMIT {int(limit)}"
    return get_db().execute(sql, params).fetchall(), d1, d2, product, reason


@app.route("/reports/movements")
@login_required
def movements():
    rows, d1, d2, product, reason = movement_query(limit=301)
    truncated = len(rows) > 300
    products_all = get_db().execute("SELECT id, sku, name, size, color FROM products ORDER BY name, size").fetchall()
    return render_template("movements.html", rows=rows[:300], truncated=truncated, d1=d1, d2=d2,
                           product=product, reason=reason, products=products_all,
                           labels=REASON_LABELS)


@app.route("/reports/movements.csv")
@login_required
def movements_csv():
    rows, *_ = movement_query()
    data = [(r["local_at"], r["sku"], r["name"], r["size"], r["color"], r["change"],
             REASON_LABELS[r["reason"]], (r["return_sale_id"] if r["reason"] == "return" else r["ref_id"]) or "", r["note"],
             r["user_name"] or "")
            for r in rows]
    return csv_response("stock-movements.csv",
                        ["Date", "SKU", "Product", "Size", "Color", "Change", "Reason",
                         "Invoice", "Note", "User"], data)


# ---------- manual stock adjustments ----------
ADJUST_TYPES = {
    "count": ("Stocktake count", "set"),
    "damaged": ("Damaged", "remove"),
    "lost": ("Lost or stolen", "remove"),
    "found": ("Found or other addition", "add"),
}


@app.route("/stock/adjust", methods=["GET", "POST"])
@login_required
def stock_adjust():
    db = get_db()
    if request.method == "POST":
        f = request.form
        kind = f.get("type", "")
        details = f.get("note", "").strip()[:200]
        err, p, delta = None, None, 0
        try:
            pid = int(f.get("product_id", ""))
            qty = int(f.get("quantity", ""))
        except ValueError:
            err = "Choose a product and enter a whole-number quantity."
        if not err and kind not in ADJUST_TYPES:
            err = "Choose an adjustment type."
        if not err:
            label, mode = ADJUST_TYPES[kind]
            p = db.execute("SELECT * FROM products WHERE id = ?", (pid,)).fetchone()
            if p is None:
                err = "That product doesn't exist."
            elif mode == "set":
                if qty < 0:
                    err = "The counted quantity can't be negative."
                else:
                    delta = qty - p["quantity"]
            elif qty < 1:
                err = "Quantity must be at least 1."
            elif mode == "remove":
                if qty > p["quantity"]:
                    err = f"Only {p['quantity']} in stock, so you can't remove {qty}."
                delta = -qty
            else:
                delta = qty
            if not err and delta == 0:
                err = "The counted quantity matches the current stock, so nothing changed."
        if err:
            flash(err, "error")
        else:
            note = label + (": " + details if details else "")
            res = db.execute(
                "UPDATE products SET quantity = quantity + ? WHERE id = ? AND quantity + ? >= 0",
                (delta, pid, delta))
            if res.rowcount != 1:
                db.rollback()
                flash("Stock changed while saving. Please try again.", "error")
            else:
                db.execute(
                    "INSERT INTO stock_movements (product_id, change, reason, user_id, note)"
                    " VALUES (?,?, 'adjustment', ?, ?)", (pid, delta, g.user["id"], note))
                db.commit()
                flash(f"{p['sku']}: stock went from {p['quantity']} to {p['quantity'] + delta}.", "ok")
                return redirect(url_for("stock_adjust"))
    products_all = db.execute("SELECT * FROM products ORDER BY name, size").fetchall()
    recent = db.execute(
        "SELECT m.change, m.note, datetime(m.created_at,'localtime') AS local_at,"
        " pr.sku, pr.name, pr.size, pr.color, u.full_name AS user_name"
        " FROM stock_movements m JOIN products pr ON pr.id = m.product_id"
        " LEFT JOIN users u ON u.id = m.user_id WHERE m.reason = 'adjustment'"
        " ORDER BY m.id DESC LIMIT 10").fetchall()
    return render_template("stock_adjust.html", products=products_all, recent=recent,
                           types=ADJUST_TYPES, modes={k: v[1] for k, v in ADJUST_TYPES.items()},
                           form=request.form)


# ---------- users and passwords ----------
import re

USERNAME_RE = re.compile(r"^[a-z0-9._-]{3,30}$")
MIN_PASSWORD = 8


def other_active_admins(db, user_id):
    return db.execute(
        "SELECT COUNT(*) FROM users WHERE role = 'admin' AND active = 1 AND id != ?",
        (user_id,)).fetchone()[0]


@app.route("/users")
@admin_required
def users():
    rows = get_db().execute(
        "SELECT u.*, (SELECT COUNT(*) FROM sales WHERE user_id = u.id)"
        " + (SELECT COUNT(*) FROM purchases WHERE user_id = u.id) AS invoices"
        " FROM users u ORDER BY u.active DESC, u.role, u.username").fetchall()
    return render_template("users.html", users=rows)


@app.route("/users/new", methods=["GET", "POST"])
@admin_required
def user_new():
    if request.method == "POST":
        f = request.form
        username = f.get("username", "").strip().lower()
        full_name = f.get("full_name", "").strip()
        role = f.get("role", "")
        password = f.get("password", "")
        err = None
        if not USERNAME_RE.match(username):
            err = "Username must be 3-30 characters: letters, numbers, dot, dash or underscore."
        elif not full_name:
            err = "Full name is required."
        elif role not in ("admin", "storekeeper"):
            err = "Choose a role."
        elif len(password) < MIN_PASSWORD:
            err = f"Password must be at least {MIN_PASSWORD} characters."
        if not err:
            db = get_db()
            try:
                db.execute(
                    "INSERT INTO users (username, password_hash, full_name, role) VALUES (?,?,?,?)",
                    (username, generate_password_hash(password), full_name, role))
                db.commit()
            except sqlite3.IntegrityError:
                err = f"The username {username} is already taken."
            else:
                flash(f"User {username} created.", "ok")
                return redirect(url_for("users"))
        flash(err, "error")
        return render_template("user_form.html", u=f, is_new=True)
    return render_template("user_form.html", u={"role": "storekeeper"}, is_new=True)


@app.route("/users/<int:uid>/edit", methods=["GET", "POST"])
@admin_required
def user_edit(uid):
    db = get_db()
    u = db.execute("SELECT * FROM users WHERE id = ?", (uid,)).fetchone()
    if u is None:
        flash("User not found.", "error")
        return redirect(url_for("users"))
    if request.method == "POST":
        f = request.form
        full_name = f.get("full_name", "").strip()
        role = f.get("role", "")
        active = 1 if f.get("active") else 0
        password = f.get("password", "")
        err = None
        if not full_name:
            err = "Full name is required."
        elif role not in ("admin", "storekeeper"):
            err = "Choose a role."
        elif password and len(password) < MIN_PASSWORD:
            err = f"Password must be at least {MIN_PASSWORD} characters."
        elif u["id"] == g.user["id"] and not active:
            err = "You can't deactivate your own account."
        elif (u["role"] == "admin" and u["active"] and (role != "admin" or not active)
              and other_active_admins(db, u["id"]) == 0):
            err = "There must be at least one active admin."
        if err:
            flash(err, "error")
            return render_template("user_form.html", u={**u, **f, "active": active}, is_new=False)
        db.execute("UPDATE users SET full_name = ?, role = ?, active = ? WHERE id = ?",
                   (full_name, role, active, uid))
        if password:
            db.execute("UPDATE users SET password_hash = ? WHERE id = ?",
                       (generate_password_hash(password), uid))
        db.commit()
        flash("User updated." + (" Password changed." if password else ""), "ok")
        return redirect(url_for("users"))
    return render_template("user_form.html", u=u, is_new=False)


@app.route("/account", methods=["GET", "POST"])
@login_required
def account():
    if request.method == "POST":
        f = request.form
        current, new, confirm = f.get("current", ""), f.get("new", ""), f.get("confirm", "")
        if not check_password_hash(g.user["password_hash"], current):
            flash("Your current password is wrong.", "error")
        elif len(new) < MIN_PASSWORD:
            flash(f"The new password must be at least {MIN_PASSWORD} characters.", "error")
        elif new != confirm:
            flash("The two new passwords don't match.", "error")
        elif new == current:
            flash("Choose a password different from the current one.", "error")
        else:
            db = get_db()
            db.execute("UPDATE users SET password_hash = ? WHERE id = ?",
                       (generate_password_hash(new), g.user["id"]))
            db.commit()
            flash("Password changed.", "ok")
            return redirect(url_for("dashboard"))
    return render_template("account.html")


# ---------- low-stock alerts (webhook, built for n8n) ----------
import json
import threading
import urllib.error
import urllib.parse
import urllib.request

# routes that can change stock levels or reorder levels
STOCK_ENDPOINTS = {"sale_new", "sale_return", "sale_cancel", "purchase_new", "purchase_cancel",
                   "stock_adjust", "product_new", "product_edit", "product_delete"}
last_delivery = None  # the newest background sender (the tests wait for it)


def get_setting(db, key, default=""):
    r = db.execute("SELECT value FROM settings WHERE key = ?", (key,)).fetchone()
    return r["value"] if r else default


def set_setting(db, key, value):
    db.execute("INSERT INTO settings (key, value) VALUES (?, ?)"
               " ON CONFLICT(key) DO UPDATE SET value = excluded.value", (key, value))


def alert_item(r):
    return {"sku": r["sku"], "name": r["name"], "size": r["size"], "color": r["color"],
            "category": r["category"], "quantity": r["quantity"], "reorder_level": r["reorder_level"],
            "status": "out" if r["quantity"] == 0 else "low"}


def alert_message(items):
    lines = ["\u26a0\ufe0f Low stock alert - Clothing Warehouse"]
    for i in items:
        label = f"{i['sku']} - {i['name']} ({i['size']}, {i['color']})"
        lines.append(f"\u2022 {label}: " + ("OUT OF STOCK" if i["status"] == "out"
                                             else f"{i['quantity']} left (reorder at {i['reorder_level']})"))
    return "\n".join(lines)


def send_alert(db, kind, items, root):
    """Write a log row and deliver the webhook in the background, so a slow n8n never slows the app."""
    global last_delivery
    url, secret = get_setting(db, "alert_url"), get_setting(db, "alert_secret")
    event = "test" if kind == "test" else "low_stock"
    message = ("\u2705 Test alert from Clothing Warehouse. If you can read this, the connection works."
               if kind == "test" else alert_message(items))
    payload = {"event": event, "store": "Clothing Warehouse", "sent_at": datetime.now(timezone.utc).isoformat(),
               "dashboard_url": root, "count": len(items), "products": items, "message": message}
    summary = ", ".join(f"{i['sku']} ({i['quantity']})" for i in items)[:300]
    log_id = db.execute("INSERT INTO alert_log (kind, summary) VALUES (?, ?)", (kind, summary)).lastrowid
    db.execute("DELETE FROM alert_log WHERE id NOT IN (SELECT id FROM alert_log ORDER BY id DESC LIMIT 200)")
    db.commit()

    def deliver():
        ok, detail = 0, ""
        try:
            headers = {"Content-Type": "application/json", "User-Agent": "clothing-warehouse/1.0"}
            if secret:
                headers["X-Warehouse-Secret"] = secret
            req = urllib.request.Request(url, data=json.dumps(payload).encode("utf-8"),
                                         headers=headers, method="POST")
            with urllib.request.urlopen(req, timeout=6) as resp:
                ok, detail = (1 if 200 <= resp.status < 300 else 0), f"HTTP {resp.status}"
        except urllib.error.HTTPError as e:
            detail = f"HTTP {e.code}"
        except Exception as e:  # connection refused, timeout, bad address...
            detail = f"{type(e).__name__}: {e}"[:200]
        c = sqlite3.connect(DB_PATH)
        c.execute("UPDATE alert_log SET ok = ?, detail = ? WHERE id = ?", (ok, detail, log_id))
        c.commit()
        c.close()

    last_delivery = threading.Thread(target=deliver, daemon=True)
    last_delivery.start()
    return log_id


def check_low_stock(db, root):
    """Alert once when an item drops to its reorder level; re-arm it when stock is back above it.
    The flags are kept up to date even while alerts are off, so switching them on never floods n8n."""
    rows = db.execute("SELECT * FROM products").fetchall()
    newly = [r for r in rows if r["quantity"] <= r["reorder_level"] and not r["low_alert_sent"]]
    recovered = [r["id"] for r in rows if r["quantity"] > r["reorder_level"] and r["low_alert_sent"]]
    if not newly and not recovered:
        return
    db.executemany("UPDATE products SET low_alert_sent = 1 WHERE id = ?", [(r["id"],) for r in newly])
    db.executemany("UPDATE products SET low_alert_sent = 0 WHERE id = ?", [(i,) for i in recovered])
    db.commit()
    if newly and get_setting(db, "alert_enabled") == "1" and get_setting(db, "alert_url"):
        send_alert(db, "low_stock", [alert_item(r) for r in newly], root)


@app.after_request
def low_stock_hook(resp):
    if request.method == "POST" and resp.status_code == 302 and request.endpoint in STOCK_ENDPOINTS:
        try:
            check_low_stock(get_db(), request.url_root)
        except Exception:  # an alert problem must never break a sale
            app.logger.exception("low-stock check failed")
    return resp


def valid_webhook_url(u):
    p = urllib.parse.urlparse(u)
    return p.scheme in ("http", "https") and bool(p.netloc) and len(u) <= 500


@app.route("/settings/alerts", methods=["GET", "POST"])
@admin_required
def alert_settings():
    db = get_db()
    if request.method == "POST":
        f = request.form
        enabled = "1" if f.get("enabled") else "0"
        url = f.get("url", "").strip()
        new_secret = f.get("secret", "").strip()[:100]
        err = None
        if url and not valid_webhook_url(url):
            err = "The webhook address must start with http:// or https://"
        elif enabled == "1" and not url:
            err = "Enter the webhook address before turning alerts on."
        if err:
            flash(err, "error")
        else:
            if enabled == "1" and get_setting(db, "alert_enabled") != "1":
                # switching alerts on: whatever is low right now counts as already known
                db.execute("UPDATE products SET low_alert_sent = CASE WHEN quantity <= reorder_level THEN 1 ELSE 0 END")
            set_setting(db, "alert_enabled", enabled)
            set_setting(db, "alert_url", url)
            if f.get("clear_secret"):
                set_setting(db, "alert_secret", "")
            elif new_secret:
                set_setting(db, "alert_secret", new_secret)
            db.commit()
            flash("Alert settings saved." + (" Alerts are on." if enabled == "1" else " Alerts are off."), "ok")
            return redirect(url_for("alert_settings"))
    cfg = {"enabled": get_setting(db, "alert_enabled") == "1", "url": get_setting(db, "alert_url"),
           "has_secret": bool(get_setting(db, "alert_secret"))}
    if request.method == "POST":  # show what was typed after an error
        cfg.update(enabled=request.form.get("enabled") == "1", url=request.form.get("url", ""))
    log = db.execute("SELECT * FROM alert_log ORDER BY id DESC LIMIT 15").fetchall()
    low = db.execute("SELECT COUNT(*) FROM products WHERE quantity <= reorder_level").fetchone()[0]
    return render_template("alert_settings.html", cfg=cfg, log=log, low=low)


@app.route("/settings/alerts/test", methods=["POST"])
@admin_required
def alert_test():
    db = get_db()
    if not get_setting(db, "alert_url"):
        flash("Save a webhook address first.", "error")
    else:
        sample = [{"sku": "TEST-001", "name": "Test product", "size": "M", "color": "Blue", "category": "Test",
                   "quantity": 2, "reorder_level": 5, "status": "low"}]
        send_alert(db, "test", sample, request.url_root)
        flash("Test alert sent. Check the result in the table below in a few seconds.", "ok")
    return redirect(url_for("alert_settings"))


@app.route("/settings/alerts/send-now", methods=["POST"])
@admin_required
def alert_send_now():
    db = get_db()
    rows = db.execute("SELECT * FROM products WHERE quantity <= reorder_level ORDER BY quantity, sku").fetchall()
    if not get_setting(db, "alert_url"):
        flash("Save a webhook address first.", "error")
    elif not rows:
        flash("Nothing is below its reorder level right now.", "error")
    else:
        send_alert(db, "manual", [alert_item(r) for r in rows], request.url_root)
        flash(f"Sent the current low-stock list ({len(rows)} item{'' if len(rows) == 1 else 's'}).", "ok")
    return redirect(url_for("alert_settings"))


init_db()

if __name__ == "__main__":
    app.run(debug=True)
