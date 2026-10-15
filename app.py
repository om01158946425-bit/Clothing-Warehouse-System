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
        db.executescript(f.read())
    # older databases: add the cost snapshot column and backfill it from current product costs
    cols = [r[1] for r in db.execute("PRAGMA table_info(sale_items)")]
    mcols = [r[1] for r in db.execute("PRAGMA table_info(stock_movements)")]
    if "note" not in mcols:
        db.execute("ALTER TABLE stock_movements ADD COLUMN note TEXT NOT NULL DEFAULT ''")
        db.commit()
    if "unit_cost" not in cols:
        db.execute("ALTER TABLE sale_items ADD COLUMN unit_cost REAL NOT NULL DEFAULT 0")
        db.execute("UPDATE sale_items SET unit_cost ="
                   " (SELECT cost FROM products WHERE products.id = sale_items.product_id)")
        db.commit()
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
            (request.form.get("username", "").strip(),)).fetchone()
        if user and check_password_hash(user["password_hash"], request.form.get("password", "")):
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
@app.route("/")
@login_required
def dashboard():
    db = get_db()
    stats = db.execute(
        "SELECT COUNT(*) AS items, COALESCE(SUM(quantity),0) AS units,"
        " COALESCE(SUM(quantity*cost),0) AS value FROM products").fetchone()
    low = db.execute(
        "SELECT * FROM products WHERE quantity <= reorder_level ORDER BY quantity").fetchall()
    return render_template("dashboard.html", stats=stats, low=low)


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
        " FROM suppliers s LEFT JOIN purchases p ON p.supplier_id = s.id"
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
        "SELECT p.*, s.name AS supplier, s.phone, u.full_name AS user_name FROM purchases p"
        " JOIN suppliers s ON s.id = p.supplier_id JOIN users u ON u.id = p.user_id"
        " WHERE p.id = ?", (pid,)).fetchone()
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
        "SELECT c.*, COUNT(s.id) AS sales, COALESCE(SUM(s.total),0) AS spent"
        " FROM customers c LEFT JOIN sales s ON s.customer_id = c.id"
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


@app.route("/sales/<int:sid>")
@login_required
def sale_detail(sid):
    db = get_db()
    s = db.execute(
        "SELECT s.*, COALESCE(c.name, 'Walk-in customer') AS customer, c.phone, u.full_name AS user_name"
        " FROM sales s LEFT JOIN customers c ON c.id = s.customer_id JOIN users u ON u.id = s.user_id"
        " WHERE s.id = ?", (sid,)).fetchone()
    if s is None:
        flash("Sale not found.", "error")
        return redirect(url_for("sales"))
    items = db.execute(
        "SELECT i.*, pr.sku, pr.name, pr.size, pr.color FROM sale_items i"
        " JOIN products pr ON pr.id = i.product_id WHERE i.sale_id = ?", (sid,)).fetchall()
    return render_template("sale_detail.html", s=s, items=items)


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


REASON_LABELS = {"initial": "Starting stock", "purchase": "Purchase",
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
    " FROM sale_items i JOIN sales s ON s.id = i.sale_id JOIN products pr ON pr.id = i.product_id"
    " WHERE " + SALE_DATE + " GROUP BY pr.id ORDER BY units DESC, revenue DESC")


@app.route("/reports")
@admin_required
def reports():
    db = get_db()
    d1, d2 = date_range()
    rng = bounds(d1, d2)
    sales_row = db.execute(
        "SELECT COUNT(*) AS n, COALESCE(SUM(s.total),0) AS total FROM sales s WHERE " + SALE_DATE,
        rng).fetchone()
    lines_row = db.execute(
        "SELECT COALESCE(SUM(i.quantity),0) AS units, COALESCE(SUM(i.quantity*i.unit_cost),0) AS cogs"
        " FROM sale_items i JOIN sales s ON s.id = i.sale_id WHERE " + SALE_DATE, rng).fetchone()
    purch_row = db.execute(
        "SELECT COUNT(*) AS n, COALESCE(SUM(p.total),0) AS total FROM purchases p"
        " WHERE date(p.created_at,'localtime') BETWEEN ? AND ?", rng).fetchone()
    revenue, cogs = sales_row["total"], lines_row["cogs"]
    profit = revenue - cogs
    margin = (profit / revenue * 100) if revenue else 0
    top = db.execute(TOP_SQL + " LIMIT 10", rng).fetchall()
    daily = db.execute(
        "SELECT date(s.created_at,'localtime') AS day, COUNT(*) AS n, SUM(s.total) AS total"
        " FROM sales s WHERE " + SALE_DATE + " GROUP BY day ORDER BY day DESC", rng).fetchall()
    return render_template("reports.html", d1=d1, d2=d2, sales=sales_row, lines=lines_row,
                           purch=purch_row, revenue=revenue, cogs=cogs, profit=profit,
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
        " pr.sku, pr.name, pr.size, pr.color, i.quantity, i.unit_price, i.unit_cost,"
        " i.quantity*i.unit_price, i.quantity*(i.unit_price-i.unit_cost)"
        " FROM sale_items i JOIN sales s ON s.id = i.sale_id"
        " LEFT JOIN customers c ON c.id = s.customer_id JOIN products pr ON pr.id = i.product_id"
        " WHERE " + SALE_DATE + " ORDER BY s.id, i.id", bounds(d1, d2)).fetchall()
    return csv_response("sales-lines.csv",
                        ["Date", "Invoice", "Customer", "SKU", "Product", "Size", "Color",
                         "Quantity", "Unit price", "Unit cost", "Line total", "Profit"], rows)


def movement_query(limit=None):
    d1, d2 = date_range()
    lo, hi = bounds(d1, d2)
    product = request.args.get("product", "").strip()
    reason = request.args.get("reason", "").strip()
    sql = ("SELECT m.id, m.change, m.reason, m.ref_id, m.note, datetime(m.created_at,'localtime') AS local_at,"
           " pr.sku, pr.name, pr.size, pr.color, u.full_name AS user_name"
           " FROM stock_movements m JOIN products pr ON pr.id = m.product_id"
           " LEFT JOIN users u ON u.id = m.user_id"
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
             REASON_LABELS[r["reason"]], r["ref_id"] if r["ref_id"] else "", r["note"],
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
    "return": ("Customer return", "add"),
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


init_db()

if __name__ == "__main__":
    app.run(debug=True)
