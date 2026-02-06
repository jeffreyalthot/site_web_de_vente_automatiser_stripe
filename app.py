import os
import sqlite3
from datetime import datetime, timedelta
from functools import wraps
from configparser import ConfigParser

from flask import Flask, render_template, request, redirect, url_for, session, flash
from werkzeug.security import generate_password_hash, check_password_hash

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DB_PATH = os.path.join(BASE_DIR, "store.db")
CONFIG_PATH = os.path.join(BASE_DIR, "config.conf")

app = Flask(__name__)
app.secret_key = "dev-secret-change-me"


def load_config():
    parser = ConfigParser()
    parser.read(CONFIG_PATH)
    return parser


def init_db():
    with sqlite3.connect(DB_PATH) as conn:
        cur = conn.cursor()
        cur.execute(
            """
            CREATE TABLE IF NOT EXISTS users (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                username TEXT UNIQUE NOT NULL,
                password_hash TEXT NOT NULL
            )
            """
        )
        cur.execute(
            """
            CREATE TABLE IF NOT EXISTS products (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                name TEXT NOT NULL,
                category TEXT NOT NULL,
                description TEXT,
                price REAL NOT NULL,
                stock INTEGER NOT NULL,
                image_url TEXT
            )
            """
        )
        cur.execute(
            """
            CREATE TABLE IF NOT EXISTS orders (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                customer_name TEXT NOT NULL,
                customer_address TEXT NOT NULL,
                stripe_payment_id TEXT,
                total REAL NOT NULL,
                paid INTEGER NOT NULL,
                created_at TEXT NOT NULL
            )
            """
        )
        cur.execute(
            """
            CREATE TABLE IF NOT EXISTS order_items (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                order_id INTEGER NOT NULL,
                product_id INTEGER NOT NULL,
                quantity INTEGER NOT NULL,
                price REAL NOT NULL,
                FOREIGN KEY(order_id) REFERENCES orders(id),
                FOREIGN KEY(product_id) REFERENCES products(id)
            )
            """
        )
        conn.commit()


def get_db_connection():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def login_required(view):
    @wraps(view)
    def wrapper(*args, **kwargs):
        if "user_id" not in session:
            return redirect(url_for("login"))
        return view(*args, **kwargs)

    return wrapper


def admin_required(view):
    @wraps(view)
    def wrapper(*args, **kwargs):
        if not session.get("is_admin"):
            return redirect(url_for("admin_login"))
        return view(*args, **kwargs)

    return wrapper


@app.context_processor
def inject_cart():
    cart = session.get("cart", {})
    return {"cart_count": sum(item["quantity"] for item in cart.values())}


@app.route("/")
def index():
    conn = get_db_connection()
    products = conn.execute("SELECT * FROM products ORDER BY id DESC").fetchall()
    conn.close()
    categories = sorted({product["category"] for product in products})
    grouped = {category: [] for category in categories}
    for product in products:
        grouped[product["category"]].append(product)
    return render_template("index.html", grouped=grouped)


@app.route("/product/<int:product_id>")
def product_detail(product_id):
    conn = get_db_connection()
    product = conn.execute("SELECT * FROM products WHERE id = ?", (product_id,)).fetchone()
    conn.close()
    if not product:
        flash("Produit introuvable", "error")
        return redirect(url_for("index"))
    return render_template("product.html", product=product)


@app.route("/cart")
def cart():
    cart_items = session.get("cart", {})
    products = []
    subtotal = 0
    if cart_items:
        conn = get_db_connection()
        ids = tuple(int(pid) for pid in cart_items.keys())
        placeholders = ",".join(["?"] * len(ids))
        rows = conn.execute(f"SELECT * FROM products WHERE id IN ({placeholders})", ids).fetchall()
        conn.close()
        for row in rows:
            quantity = cart_items[str(row["id"])]
            line_total = row["price"] * quantity
            subtotal += line_total
            products.append({"product": row, "quantity": quantity, "line_total": line_total})
    shipping = 0 if subtotal >= 100 else 12.50 if subtotal > 0 else 0
    total = subtotal + shipping
    return render_template(
        "cart.html",
        products=products,
        subtotal=subtotal,
        shipping=shipping,
        total=total,
    )


@app.route("/add-to-cart/<int:product_id>", methods=["POST"])
def add_to_cart(product_id):
    cart_items = session.get("cart", {})
    current_qty = cart_items.get(str(product_id), 0)
    cart_items[str(product_id)] = current_qty + 1
    session["cart"] = cart_items
    flash("Article ajouté au panier.", "success")
    return redirect(url_for("cart"))


@app.route("/remove-from-cart/<int:product_id>", methods=["POST"])
def remove_from_cart(product_id):
    cart_items = session.get("cart", {})
    cart_items.pop(str(product_id), None)
    session["cart"] = cart_items
    return redirect(url_for("cart"))


@app.route("/checkout", methods=["POST"])
@login_required
def checkout():
    cart_items = session.get("cart", {})
    if not cart_items:
        flash("Votre panier est vide.", "error")
        return redirect(url_for("cart"))

    customer_name = request.form.get("customer_name")
    customer_address = request.form.get("customer_address")
    payment_ref = request.form.get("payment_ref")

    if not customer_name or not customer_address or not payment_ref:
        flash("Merci de compléter toutes les informations de paiement.", "error")
        return redirect(url_for("cart"))

    conn = get_db_connection()
    ids = tuple(int(pid) for pid in cart_items.keys())
    placeholders = ",".join(["?"] * len(ids))
    rows = conn.execute(f"SELECT * FROM products WHERE id IN ({placeholders})", ids).fetchall()

    subtotal = 0
    order_items = []
    for row in rows:
        quantity = cart_items[str(row["id"])]
        line_total = row["price"] * quantity
        subtotal += line_total
        order_items.append((row["id"], quantity, row["price"]))

    shipping = 0 if subtotal >= 100 else 12.50
    total = subtotal + shipping

    now = datetime.utcnow().isoformat()
    cursor = conn.cursor()
    cursor.execute(
        """
        INSERT INTO orders (customer_name, customer_address, stripe_payment_id, total, paid, created_at)
        VALUES (?, ?, ?, ?, ?, ?)
        """,
        (customer_name, customer_address, payment_ref, total, 1, now),
    )
    order_id = cursor.lastrowid

    for product_id, quantity, price in order_items:
        cursor.execute(
            """
            INSERT INTO order_items (order_id, product_id, quantity, price)
            VALUES (?, ?, ?, ?)
            """,
            (order_id, product_id, quantity, price),
        )
        cursor.execute(
            "UPDATE products SET stock = MAX(stock - ?, 0) WHERE id = ?", (quantity, product_id)
        )

    conn.commit()
    conn.close()

    session["cart"] = {}
    flash("Paiement validé. Merci pour votre commande !", "success")
    return redirect(url_for("index"))


@app.route("/register", methods=["GET", "POST"])
def register():
    if request.method == "POST":
        username = request.form.get("username")
        password = request.form.get("password")
        if not username or not password:
            flash("Merci de remplir tous les champs.", "error")
        else:
            conn = get_db_connection()
            try:
                conn.execute(
                    "INSERT INTO users (username, password_hash) VALUES (?, ?)",
                    (username, generate_password_hash(password)),
                )
                conn.commit()
                conn.close()
                flash("Compte créé. Vous pouvez vous connecter.", "success")
                return redirect(url_for("login"))
            except sqlite3.IntegrityError:
                conn.close()
                flash("Ce nom d'utilisateur existe déjà.", "error")
    return render_template("register.html")


@app.route("/login", methods=["GET", "POST"])
def login():
    if request.method == "POST":
        username = request.form.get("username")
        password = request.form.get("password")
        conn = get_db_connection()
        user = conn.execute("SELECT * FROM users WHERE username = ?", (username,)).fetchone()
        conn.close()
        if user and check_password_hash(user["password_hash"], password):
            session["user_id"] = user["id"]
            session["is_admin"] = False
            flash("Bienvenue !", "success")
            return redirect(url_for("index"))
        flash("Identifiants invalides.", "error")
    return render_template("login.html")


@app.route("/logout")
def logout():
    session.clear()
    return redirect(url_for("index"))


@app.route("/admin/login", methods=["GET", "POST"])
def admin_login():
    config = load_config()
    admin_user = config.get("admin", "username", fallback="admin")
    admin_password = config.get("admin", "password", fallback="admin")

    if request.method == "POST":
        username = request.form.get("username")
        password = request.form.get("password")
        if username == admin_user and password == admin_password:
            session["is_admin"] = True
            flash("Connexion administrateur réussie.", "success")
            return redirect(url_for("admin_dashboard"))
        flash("Identifiants administrateur invalides.", "error")
    return render_template("admin_login.html")


@app.route("/admin/dashboard")
@admin_required
def admin_dashboard():
    conn = get_db_connection()
    products = conn.execute("SELECT * FROM products ORDER BY id DESC").fetchall()
    orders = conn.execute(
        "SELECT * FROM orders WHERE paid = 1 ORDER BY created_at DESC"
    ).fetchall()

    now = datetime.utcnow()
    start_week = now - timedelta(days=7)
    start_month = now - timedelta(days=30)
    start_year = now - timedelta(days=365)

    weekly_total = conn.execute(
        "SELECT COALESCE(SUM(total), 0) FROM orders WHERE paid = 1 AND created_at >= ?",
        (start_week.isoformat(),),
    ).fetchone()[0]
    monthly_total = conn.execute(
        "SELECT COALESCE(SUM(total), 0) FROM orders WHERE paid = 1 AND created_at >= ?",
        (start_month.isoformat(),),
    ).fetchone()[0]
    yearly_total = conn.execute(
        "SELECT COALESCE(SUM(total), 0) FROM orders WHERE paid = 1 AND created_at >= ?",
        (start_year.isoformat(),),
    ).fetchone()[0]
    avg_daily = monthly_total / 30 if monthly_total else 0

    conn.close()
    return render_template(
        "admin_dashboard.html",
        products=products,
        orders=orders,
        weekly_total=weekly_total,
        monthly_total=monthly_total,
        yearly_total=yearly_total,
        avg_daily=avg_daily,
    )


@app.route("/admin/products", methods=["POST"])
@admin_required
def admin_add_product():
    name = request.form.get("name")
    category = request.form.get("category")
    description = request.form.get("description")
    price = request.form.get("price")
    stock = request.form.get("stock")
    image_url = request.form.get("image_url")

    if not all([name, category, price, stock]):
        flash("Merci de remplir les champs obligatoires.", "error")
        return redirect(url_for("admin_dashboard"))

    conn = get_db_connection()
    conn.execute(
        """
        INSERT INTO products (name, category, description, price, stock, image_url)
        VALUES (?, ?, ?, ?, ?, ?)
        """,
        (name, category, description, float(price), int(stock), image_url),
    )
    conn.commit()
    conn.close()

    flash("Article ajouté.", "success")
    return redirect(url_for("admin_dashboard"))


@app.route("/admin/products/<int:product_id>/stock", methods=["POST"])
@admin_required
def admin_update_stock(product_id):
    stock = request.form.get("stock")
    price = request.form.get("price")
    conn = get_db_connection()
    conn.execute(
        "UPDATE products SET stock = ?, price = ? WHERE id = ?",
        (int(stock), float(price), product_id),
    )
    conn.commit()
    conn.close()
    flash("Stock et prix mis à jour.", "success")
    return redirect(url_for("admin_dashboard"))


if __name__ == "__main__":
    init_db()
    app.run(host="0.0.0.0", port=5000, debug=True)
