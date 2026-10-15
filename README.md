# Clothing Warehouse – Information Systems project

Flask + SQLite. Software only, no hardware.

## Run
    pip install -r requirements.txt
    python app.py
Open http://127.0.0.1:5000

The database (`warehouse.db`) is created automatically on first run with demo data.
To reset it, stop the app and delete `warehouse.db`.

Demo logins: `admin / admin123` (can delete products), `keeper / keeper123`.

## Structure
- `app.py`        routes, auth, DB helpers
- `schema.sql`    all 9 tables (users, products, suppliers, customers, purchases, purchase_items, sales, sale_items, stock_movements)
- `templates/`    Jinja pages
- `static/`       CSS

## Done
Login + roles, dashboard with low-stock list, products CRUD with search, suppliers CRUD, purchase invoices (multi-line, raises stock, logs stock_movements), customers CRUD, sales invoices (multi-line, stock-checked, lowers stock, logs stock_movements), reports (revenue, cost, profit, top products, daily sales, stock movements, CSV export; profit pages are admin-only), manual stock adjustments (stocktake, damaged, lost, customer return) with a note, logged in stock_movements.

## Next modules
User management.
Rule: `products.quantity` changes only through purchases/sales/adjustments, and each change writes a row to `stock_movements`.
