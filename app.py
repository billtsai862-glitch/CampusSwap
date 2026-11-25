import os
import sqlite3
from pathlib import Path
from functools import wraps

from flask import Flask, render_template, request, redirect, url_for, session, flash, abort
from werkzeug.security import generate_password_hash, check_password_hash

BASE_DIR = Path(__file__).resolve().parent
DB_PATH = Path(os.environ.get("DATABASE_PATH", str(BASE_DIR / "campusswap.db")))

app = Flask(__name__)
app.config["SECRET_KEY"] = os.environ.get("SECRET_KEY", "dev-only-change-this-before-public-use")


def get_db():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def init_db():
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    with get_db() as conn:
        conn.execute("""
            CREATE TABLE IF NOT EXISTS users (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                username TEXT NOT NULL UNIQUE,
                password_hash TEXT NOT NULL,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        """)
        conn.execute("""
            CREATE TABLE IF NOT EXISTS products (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER NOT NULL,
                title TEXT NOT NULL,
                category TEXT NOT NULL,
                price INTEGER NOT NULL CHECK(price >= 0),
                condition_text TEXT NOT NULL,
                description TEXT,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                FOREIGN KEY(user_id) REFERENCES users(id)
            )
        """)


def login_required(view):
    @wraps(view)
    def wrapped(*args, **kwargs):
        if "user_id" not in session:
            flash("請先登入才能使用這項功能。")
            return redirect(url_for("login"))
        return view(*args, **kwargs)
    return wrapped


@app.route("/")
def index():
    with get_db() as conn:
        products = conn.execute("""
            SELECT products.*, users.username
            FROM products JOIN users ON products.user_id = users.id
            ORDER BY products.id DESC
        """).fetchall()
    return render_template("index.html", products=products)


@app.route("/register", methods=["GET", "POST"])
def register():
    if request.method == "POST":
        username = request.form.get("username", "").strip()
        password = request.form.get("password", "")
        if len(username) < 2 or len(password) < 6:
            flash("帳號至少 2 個字元，密碼至少 6 個字元。")
            return render_template("register.html")
        try:
            with get_db() as conn:
                conn.execute(
                    "INSERT INTO users (username, password_hash) VALUES (?, ?)",
                    (username, generate_password_hash(password))
                )
            flash("註冊成功，請登入。")
            return redirect(url_for("login"))
        except sqlite3.IntegrityError:
            flash("這個帳號已經有人使用。")
    return render_template("register.html")


@app.route("/login", methods=["GET", "POST"])
def login():
    if request.method == "POST":
        username = request.form.get("username", "").strip()
        password = request.form.get("password", "")
        with get_db() as conn:
            user = conn.execute(
                "SELECT * FROM users WHERE username = ?", (username,)
            ).fetchone()
        if user and check_password_hash(user["password_hash"], password):
            session.clear()
            session["user_id"] = user["id"]
            session["username"] = user["username"]
            return redirect(url_for("index"))
        flash("帳號或密碼錯誤。")
    return render_template("login.html")


@app.route("/logout")
def logout():
    session.clear()
    flash("已登出。")
    return redirect(url_for("index"))


@app.route("/product/new", methods=["GET", "POST"])
@login_required
def new_product():
    if request.method == "POST":
        title = request.form.get("title", "").strip()
        category = request.form.get("category", "").strip()
        condition_text = request.form.get("condition_text", "").strip()
        description = request.form.get("description", "").strip()
        try:
            price = int(request.form.get("price", ""))
            if price < 0:
                raise ValueError
        except ValueError:
            flash("價格必須是 0 或正整數。")
            return render_template("new_product.html")
        if not title or not category or not condition_text:
            flash("商品名稱、分類及商品狀況不可空白。")
            return render_template("new_product.html")
        with get_db() as conn:
            conn.execute("""
                INSERT INTO products (user_id, title, category, price, condition_text, description)
                VALUES (?, ?, ?, ?, ?, ?)
            """, (session["user_id"], title, category, price, condition_text, description))
        flash("商品刊登成功！")
        return redirect(url_for("index"))
    return render_template("new_product.html")


@app.route("/product/<int:product_id>")
def product_detail(product_id):
    with get_db() as conn:
        product = conn.execute("""
            SELECT products.*, users.username
            FROM products JOIN users ON products.user_id = users.id
            WHERE products.id = ?
        """, (product_id,)).fetchone()
    if product is None:
        abort(404)
    return render_template("product_detail.html", product=product)


@app.route("/search")
def search():
    keyword = request.args.get("q", "").strip()
    with get_db() as conn:
        products = conn.execute("""
            SELECT products.*, users.username
            FROM products JOIN users ON products.user_id = users.id
            WHERE products.title LIKE ? OR products.category LIKE ?
            ORDER BY products.id DESC
        """, (f"%{keyword}%", f"%{keyword}%")).fetchall()
    return render_template("index.html", products=products, keyword=keyword)


# Gunicorn imports this module directly, so initialize the database here too.
init_db()


if __name__ == "__main__":
    app.run(
        host="0.0.0.0",
        port=int(os.environ.get("PORT", "5000")),
        debug=False
    )
