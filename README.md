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
Login + roles, dashboard with low-stock list, products CRUD with search, suppliers CRUD, purchase invoices (multi-line, raises stock, logs stock_movements), customers CRUD, sales invoices (multi-line, stock-checked, lowers stock, logs stock_movements), reports (revenue, cost, profit, top products, daily sales, stock movements, CSV export; profit pages are admin-only), manual stock adjustments (stocktake, damaged, lost, found) with a note, logged in stock_movements, user management (add users, roles, deactivate, reset passwords, change own password).

## Next modules
None planned. See ideas below.
Rule: `products.quantity` changes only through purchases/sales/adjustments, and each change writes a row to `stock_movements`.

## Users
Admins manage users from the Users page. Deactivated users can't log in but their invoices stay. The system always keeps at least one active admin. Change the demo passwords before the presentation.

## Demo data
`python seed_demo.py` fills an empty database with suppliers, customers, 20 products and 30 days of purchases, sales and adjustments (see the notes at the top of that file). It refuses to run if invoices already exist.

## Git
`.gitignore` keeps `warehouse.db` out of the repository, so each person gets their own local database.

## Returns and cancellations
- **Return items** (sale page): choose how many of each line came back. The items go back into stock, a `sale_returns` row keeps the history, and the refund is taken off revenue and profit. The invoice itself is never edited. Returns are counted on the date of the original sale.
- **Cancel this whole sale** (admin): puts everything that is still sold back into stock and removes the sale from the reports. A reason is required.
- **Cancel this purchase** (admin): for a purchase entered by mistake. Only possible while the stock from it is still in the warehouse (otherwise cancel or return the sales that used it first). The product cost goes back to the latest price from the other purchases.
- Every step is recorded in Stock movements with who did it and why.
- The first start after upgrading from an older version makes a safety copy named `warehouse-backup-before-returns.db`.
- Not built: returning goods to a supplier (partial purchase returns).

## Low-stock alerts to n8n
The app can message you (for example on Telegram) when an item drops to its reorder level.
1. In n8n: Import from file `n8n/low-stock-alert.workflow.json`.
2. Webhook node: create a Header Auth credential (name `X-Warehouse-Secret`, value = any secret you choose). Telegram node: choose your Telegram credential and put your chat id in place of `YOUR_CHAT_ID`.
3. Activate the workflow and copy the **Production URL** of the Webhook node (it looks like `http://localhost:5678/webhook/warehouse-low-stock`).
4. In the app, log in as admin, open **Alerts**, paste the URL, type the same secret, tick "Send alerts to n8n", Save, then press **Send a test alert**.

How it behaves
- One alert per item when it reaches its reorder level. It alerts again only after the item was restocked above the level and runs low again. Several items that cross in one action arrive in one message.
- Turning alerts on never sends the items that are already low. Use "Send the current low-stock list now" for that.
- Sending happens in the background with a 6 second timeout. If n8n is down the sale is still saved and the Alerts page shows the failure.
- While testing in n8n, the "Test URL" only works after you press "Listen for test event". Use the Production URL for real alerts.
- The app posts JSON: `event`, `message`, `count`, `products[]` (sku, name, size, color, category, quantity, reorder_level, status), `dashboard_url`. You can swap the Telegram node for WhatsApp, email or Google Sheets.

## Look and feel
The whole app uses a dark blue theme by default, with a light theme you can switch to using the sun/moon button at the bottom of the sidebar (the choice is remembered in the browser). Colors are CSS variables at the top of `static/style.css`, so a new theme only needs a new block of variables. Sidebar icons live in `templates/_icons.html`.

The dashboard shows: greeting and date, four KPI cards (products, stock value, low-stock items, today's sales with a comparison to yesterday), units by category, stock status (in stock / low / out), sales per day for 30 days, top sellers, the last 8 stock movements, and quick actions. Profit appears only for admins.
