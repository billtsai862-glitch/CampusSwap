import os
import uuid
import urllib.request
import urllib.error
from pathlib import Path
from functools import wraps
import psycopg
from psycopg.rows import dict_row

from flask import Flask, render_template, render_template_string, request, redirect, url_for, session, flash, abort
from werkzeug.security import generate_password_hash, check_password_hash
from werkzeug.utils import secure_filename

BASE_DIR = Path(__file__).resolve().parent

app = Flask(__name__)
app.config["SECRET_KEY"] = os.environ.get("SECRET_KEY", "dev-only-change-this-before-public-use")
app.config["MAX_CONTENT_LENGTH"] = 5 * 1024 * 1024
app.config["UPLOAD_FOLDER"] = os.environ.get("UPLOAD_FOLDER", str(BASE_DIR / "static" / "uploads"))
ALLOWED_IMAGE_EXTENSIONS = {".jpg", ".jpeg", ".png", ".webp", ".gif"}


DATABASE_URL = os.environ.get("DATABASE_URL", "")

class HybridRow(dict):
    def __getitem__(self, key):
        if isinstance(key, int):
            return list(self.values())[key]
        return super().__getitem__(key)

def hybrid_row(cursor):
    values = cursor.fetchone()
    return values

def get_db():
    if not DATABASE_URL:
        raise RuntimeError("DATABASE_URL 尚未設定，請在 Render 設定 Supabase PostgreSQL 連線字串。")
    conn = psycopg.connect(DATABASE_URL, row_factory=dict_row)
    return PgConnection(conn)

def init_db():
    # Supabase tables are managed by database migrations.
    return None


class PgCursor:
    def __init__(self, cursor):
        self._cursor = cursor
    def execute(self, query, params=None):
        query = query.replace("?", "%s")
        self._cursor.execute(query, params or ())
        return self
    def fetchone(self):
        row = self._cursor.fetchone()
        return HybridRow(row) if row is not None else None
    def fetchall(self):
        return [HybridRow(row) for row in self._cursor.fetchall()]
    def __iter__(self):
        return iter(self.fetchall())
    def __getattr__(self, name):
        return getattr(self._cursor, name)

class PgConnection:
    def __init__(self, connection):
        self._connection = connection
    def execute(self, query, params=None):
        return PgCursor(self._connection.cursor()).execute(query, params)
    def __enter__(self):
        return self
    def __exit__(self, exc_type, exc, tb):
        try:
            if exc_type:
                self._connection.rollback()
            else:
                self._connection.commit()
        finally:
            self._connection.close()
    def close(self):
        self._connection.close()

def save_uploaded_image():
    uploaded = request.files.get("image")
    if not uploaded or not uploaded.filename:
        return None
    extension = Path(secure_filename(uploaded.filename)).suffix.lower()
    if extension not in ALLOWED_IMAGE_EXTENSIONS:
        raise ValueError("圖片格式僅支援 JPG、PNG、WEBP 或 GIF。")
    expected_mime = {
        ".jpg": "image/jpeg", ".jpeg": "image/jpeg",
        ".png": "image/png", ".webp": "image/webp", ".gif": "image/gif"
    }
    if uploaded.mimetype != expected_mime[extension]:
        raise ValueError("圖片檔案格式不正確，請重新選擇照片。")
    supabase_url = os.environ.get("SUPABASE_URL", "").rstrip("/")
    storage_key = os.environ.get("SUPABASE_SERVICE_ROLE_KEY", "")
    if not supabase_url or not storage_key:
        raise ValueError("圖片儲存尚未設定，請在 Render 設定 Supabase Storage 金鑰。")
    filename = f"{uuid.uuid4().hex}{extension}"
    endpoint = f"{supabase_url}/storage/v1/object/product-images/{filename}"
    data = uploaded.read()
    req = urllib.request.Request(
        endpoint,
        data=data,
        headers={
            "Authorization": f"Bearer {storage_key}",
            "apikey": storage_key,
            "Content-Type": expected_mime[extension],
            "x-upsert": "false"
        },
        method="POST"
    )
    try:
        with urllib.request.urlopen(req, timeout=30) as response:
            if response.status not in (200, 201):
                raise ValueError("圖片上傳失敗，請稍後再試。")
    except urllib.error.HTTPError as error:
        raise ValueError("圖片上傳失敗，請確認 Supabase Storage 設定。") from error
    return f"{supabase_url}/storage/v1/object/public/product-images/{filename}"


def login_required(view):
    @wraps(view)
    def wrapped(*args, **kwargs):
        if "user_id" not in session:
            flash("請先登入才能使用這項功能。", "warning")
            return redirect(url_for("login", next=request.path))
        return view(*args, **kwargs)
    return wrapped


def get_product_or_404(product_id):
    with get_db() as conn:
        product = conn.execute("""
            SELECT p.*, u.username
            FROM products p JOIN users u ON p.user_id = u.id
            WHERE p.id = ?
        """, (product_id,)).fetchone()
    if product is None:
        abort(404)
    return product


def validate_product_form():
    title = request.form.get("title", "").strip()
    category = request.form.get("category", "").strip()
    condition_text = request.form.get("condition_text", "").strip()
    description = request.form.get("description", "").strip()
    image_url = request.form.get("image_url", "").strip()
    try:
        price = int(request.form.get("price", ""))
        if price < 0:
            raise ValueError
    except ValueError:
        raise ValueError("價格必須是 0 或正整數。")
    if not title or not category or not condition_text:
        raise ValueError("商品名稱、分類及商品狀況不可空白。")
    if len(title) > 80 or len(description) > 1000:
        raise ValueError("商品名稱最多 80 字，商品描述最多 1000 字。")
    if image_url and not (image_url.startswith("https://") or image_url.startswith("http://")):
        raise ValueError("圖片網址必須以 http:// 或 https:// 開頭。")
    return title, category, price, condition_text, description, image_url


@app.route("/")
def index():
    keyword = request.args.get("q", "").strip()
    category = request.args.get("category", "").strip()
    sort = request.args.get("sort", "latest")
    order_by = {
        "latest": "p.id DESC",
        "price_asc": "p.price ASC, p.id DESC",
        "price_desc": "p.price DESC, p.id DESC"
    }.get(sort, "p.id DESC")
    sql = """
        SELECT p.*, u.username,
               (SELECT COUNT(*) FROM favorites f WHERE f.product_id = p.id) AS favorite_count
        FROM products p JOIN users u ON p.user_id = u.id
        WHERE p.is_active IS TRUE
    """
    params = []
    if keyword:
        sql += " AND (p.title LIKE ? OR p.category LIKE ? OR p.description LIKE ?)"
        params.extend([f"%{keyword}%", f"%{keyword}%", f"%{keyword}%"])
    if category:
        sql += " AND p.category = ?"
        params.append(category)
    sql += f" ORDER BY {order_by} LIMIT 60"
    with get_db() as conn:
        products = conn.execute(sql, params).fetchall()
        categories = conn.execute(
            "SELECT DISTINCT category FROM products WHERE is_active IS TRUE ORDER BY category"
        ).fetchall()
        stats = conn.execute("""
            SELECT COUNT(*) AS product_count,
                   (SELECT COUNT(*) FROM users) AS member_count
            FROM products WHERE is_active IS TRUE
        """).fetchone()
    return render_template("index.html", products=products, categories=categories,
                           keyword=keyword, selected_category=category, sort=sort, stats=stats)


@app.route("/register", methods=["GET", "POST"])
def register():
    if request.method == "POST":
        username = request.form.get("username", "").strip()
        password = request.form.get("password", "")
        if len(username) < 2 or len(username) > 24 or len(password) < 6 or len(password) > 128:
            flash("帳號需為 2 至 24 個字元，密碼需為 6 至 128 個字元。", "warning")
            return render_template("register.html")
        try:
            with get_db() as conn:
                conn.execute(
                    "INSERT INTO users (username, password_hash) VALUES (?, ?)",
                    (username, generate_password_hash(password))
                )
            flash("註冊成功，歡迎加入 CampusSwap！", "success")
            return redirect(url_for("login"))
        except psycopg.IntegrityError:
            flash("這個帳號已經有人使用，請更換帳號。", "warning")
    return render_template("register.html")


@app.route("/login", methods=["GET", "POST"])
def login():
    if request.method == "POST":
        username = request.form.get("username", "").strip()
        password = request.form.get("password", "")
        if len(password) < 6 or len(password) > 128:
            flash("密碼需為 6 至 128 個字元。", "warning")
            return render_template("login.html")
        with get_db() as conn:
            user = conn.execute(
                "SELECT * FROM users WHERE username = ?", (username,)
            ).fetchone()
        if user and check_password_hash(user["password_hash"], password):
            session.clear()
            session["user_id"] = user["id"]
            session["username"] = user["username"]
            next_url = request.args.get("next", "")
            if next_url.startswith("/") and not next_url.startswith("//"):
                return redirect(next_url)
            return redirect(url_for("index"))
        flash("帳號或密碼錯誤，請再試一次。", "danger")
    return render_template("login.html")


@app.route("/logout")
def logout():
    session.clear()
    flash("已安全登出。", "success")
    return redirect(url_for("index"))


@app.route("/product/new", methods=["GET", "POST"])
@login_required
def new_product():
    if request.method == "POST":
        try:
            values = validate_product_form()
            uploaded_image = save_uploaded_image()
            if uploaded_image:
                values = values[:-1] + (uploaded_image,)
        except ValueError as error:
            flash(str(error), "warning")
            return render_template("new_product.html")
        with get_db() as conn:
            conn.execute("""
                INSERT INTO products
                (user_id, title, category, price, condition_text, description, image_url)
                VALUES (?, ?, ?, ?, ?, ?, ?)
            """, (session["user_id"], *values))
        flash("商品已成功刊登！", "success")
        return redirect(url_for("dashboard"))
    return render_template("new_product.html")


@app.route("/product/<int:product_id>")
def product_detail(product_id):
    product = get_product_or_404(product_id)
    if not product["is_active"] and session.get("user_id") != product["user_id"]:
        abort(404)
    is_favorite = False
    own_product = session.get("user_id") == product["user_id"]
    my_request = None
    if session.get("user_id"):
        with get_db() as conn:
            is_favorite = conn.execute(
                "SELECT 1 FROM favorites WHERE user_id = ? AND product_id = ?",
                (session["user_id"], product_id)
            ).fetchone() is not None
            my_request = conn.execute(
                "SELECT * FROM purchase_requests WHERE product_id = ? AND buyer_id = ?",
                (product_id, session["user_id"])
            ).fetchone()
    with get_db() as conn:
        favorite_count = conn.execute(
            "SELECT COUNT(*) FROM favorites WHERE product_id = ?", (product_id,)
        ).fetchone()[0]
    return render_template("product_detail.html", product=product, is_favorite=is_favorite,
                           own_product=own_product, my_request=my_request,
                           favorite_count=favorite_count)


@app.route("/product/<int:product_id>/edit", methods=["GET", "POST"])
@login_required
def edit_product(product_id):
    product = get_product_or_404(product_id)
    if product["user_id"] != session["user_id"]:
        abort(403)
    if request.method == "POST":
        try:
            values = validate_product_form()
            uploaded_image = save_uploaded_image()
            if uploaded_image:
                values = values[:-1] + (uploaded_image,)
            else:
                values = values[:-1] + (product["image_url"],)
        except ValueError as error:
            flash(str(error), "warning")
            return render_template("edit_product.html", product=product)
        with get_db() as conn:
            conn.execute("""
                UPDATE products
                SET title=?, category=?, price=?, condition_text=?, description=?, image_url=?
                WHERE id=? AND user_id=?
            """, (*values, product_id, session["user_id"]))
        flash("商品資訊已更新。", "success")
        return redirect(url_for("product_detail", product_id=product_id))
    return render_template("edit_product.html", product=product)


@app.route("/product/<int:product_id>/delete", methods=["POST"])
@login_required
def delete_product(product_id):
    with get_db() as conn:
        product = conn.execute(
            "SELECT * FROM products WHERE id = ? AND user_id = ?",
            (product_id, session["user_id"])
        ).fetchone()
        if product is None:
            abort(404)
        conn.execute("DELETE FROM favorites WHERE product_id = ?", (product_id,))
        conn.execute("DELETE FROM purchase_requests WHERE product_id = ?", (product_id,))
        conn.execute("DELETE FROM products WHERE id = ?", (product_id,))
    flash("商品已刪除。", "success")
    return redirect(url_for("dashboard"))


@app.route("/favorite/<int:product_id>", methods=["POST"])
@login_required
def toggle_favorite(product_id):
    product = get_product_or_404(product_id)
    if product["user_id"] == session["user_id"]:
        flash("不能收藏自己刊登的商品。", "warning")
        return redirect(url_for("product_detail", product_id=product_id))
    with get_db() as conn:
        existing = conn.execute(
            "SELECT id FROM favorites WHERE user_id=? AND product_id=?",
            (session["user_id"], product_id)
        ).fetchone()
        if existing:
            conn.execute("DELETE FROM favorites WHERE id=?", (existing["id"],))
            flash("已從收藏清單移除。", "success")
        else:
            conn.execute(
                "INSERT INTO favorites (user_id, product_id) VALUES (?, ?) ON CONFLICT DO NOTHING",
                (session["user_id"], product_id)
            )
            flash("已加入收藏！", "success")
    return redirect(request.referrer or url_for("product_detail", product_id=product_id))


@app.route("/favorites")
@login_required
def favorites():
    with get_db() as conn:
        products = conn.execute("""
            SELECT p.*, u.username FROM favorites f
            JOIN products p ON p.id=f.product_id
            JOIN users u ON u.id=p.user_id
            WHERE f.user_id=? AND p.is_active IS TRUE
            ORDER BY f.created_at DESC
        """, (session["user_id"],)).fetchall()
    return render_template("favorites.html", products=products)


@app.route("/request/<int:product_id>", methods=["POST"])
@login_required
def create_purchase_request(product_id):
    product = get_product_or_404(product_id)
    if product["user_id"] == session["user_id"]:
        flash("不能對自己的商品提出購買申請。", "warning")
        return redirect(url_for("product_detail", product_id=product_id))
    if not product["is_active"]:
        flash("此商品目前已下架。", "warning")
        return redirect(url_for("index"))
    message = request.form.get("message", "").strip()[:500]
    with get_db() as conn:
        try:
            conn.execute("""
                INSERT INTO purchase_requests (product_id, buyer_id, message)
                VALUES (?, ?, ?)
            """, (product_id, session["user_id"], message))
            flash("購買申請已送出，請至個人中心查看進度。", "success")
        except psycopg.IntegrityError:
            flash("你已經對這項商品提出過申請。", "warning")
    return redirect(url_for("product_detail", product_id=product_id))


@app.route("/request/<int:request_id>/<action>", methods=["POST"])
@login_required
def update_purchase_request(request_id, action):
    if action not in ("accept", "reject", "complete", "cancel"):
        abort(404)
    with get_db() as conn:
        item = conn.execute("""
            SELECT r.*, p.user_id AS seller_id, p.id AS item_id, p.is_active
            FROM purchase_requests r JOIN products p ON p.id=r.product_id
            WHERE r.id=?
        """, (request_id,)).fetchone()
        if item is None:
            abort(404)
        if action in ("accept", "reject", "complete") and item["seller_id"] != session["user_id"]:
            abort(403)
        if action == "cancel" and item["buyer_id"] != session["user_id"]:
            abort(403)
        if action == "accept":
            if item["status"] != "pending" or not item["is_active"]:
                flash("這筆申請目前無法接受。", "warning")
            else:
                conn.execute("""
                    UPDATE purchase_requests SET status='rejected', updated_at=CURRENT_TIMESTAMP
                    WHERE product_id=? AND id<>? AND status='pending'
                """, (item["product_id"], request_id))
                conn.execute("""
                    UPDATE purchase_requests SET status='accepted', updated_at=CURRENT_TIMESTAMP
                    WHERE id=?
                """, (request_id,))
                flash("已接受購買申請，請與買家聯絡安排面交。", "success")
        elif action == "reject":
            if item["status"] == "pending":
                conn.execute("UPDATE purchase_requests SET status='rejected', updated_at=CURRENT_TIMESTAMP WHERE id=?", (request_id,))
                flash("已婉拒這筆申請。", "success")
        elif action == "complete":
            if item["status"] != "accepted":
                flash("只有已接受的申請可以標記為完成。", "warning")
            else:
                conn.execute("UPDATE purchase_requests SET status='completed', updated_at=CURRENT_TIMESTAMP WHERE id=?", (request_id,))
                conn.execute("UPDATE products SET is_active=FALSE WHERE id=?", (item["product_id"],))
                flash("交易已完成，商品已自動下架。", "success")
        elif action == "cancel":
            if item["status"] == "pending":
                conn.execute("UPDATE purchase_requests SET status='cancelled', updated_at=CURRENT_TIMESTAMP WHERE id=?", (request_id,))
                flash("已取消購買申請。", "success")
    return redirect(url_for("dashboard"))


@app.route("/dashboard")
@login_required
def dashboard():
    user_id = session["user_id"]
    with get_db() as conn:
        user = conn.execute("SELECT * FROM users WHERE id=?", (user_id,)).fetchone()
        listings = conn.execute("""
            SELECT p.*,
              (SELECT COUNT(*) FROM favorites f WHERE f.product_id=p.id) AS favorite_count,
              (SELECT COUNT(*) FROM purchase_requests r WHERE r.product_id=p.id AND r.status='pending') AS pending_count
            FROM products p WHERE p.user_id=? ORDER BY p.id DESC
        """, (user_id,)).fetchall()
        received = conn.execute("""
            SELECT r.*, p.title, p.price, p.id AS item_id, b.username AS buyer_name
            FROM purchase_requests r
            JOIN products p ON p.id=r.product_id
            JOIN users b ON b.id=r.buyer_id
            WHERE p.user_id=? ORDER BY r.id DESC
        """, (user_id,)).fetchall()
        sent = conn.execute("""
            SELECT r.*, p.title, p.price, p.id AS item_id, s.username AS seller_name
            FROM purchase_requests r
            JOIN products p ON p.id=r.product_id
            JOIN users s ON s.id=p.user_id
            WHERE r.buyer_id=? ORDER BY r.id DESC
        """, (user_id,)).fetchall()
        counts = conn.execute("""
            SELECT
              (SELECT COUNT(*) FROM products WHERE user_id=?) AS listing_count,
              (SELECT COUNT(*) FROM favorites WHERE user_id=?) AS favorite_count,
              (SELECT COUNT(*) FROM purchase_requests r JOIN products p ON p.id=r.product_id WHERE p.user_id=? AND r.status='pending') AS pending_count
        """, (user_id, user_id, user_id)).fetchone()
    return render_template("dashboard.html", user=user, listings=listings,
                           received=received, sent=sent, counts=counts)


@app.route("/messages")
@login_required
def messages_inbox():
    user_id = session["user_id"]
    with get_db() as conn:
        conversations = conn.execute("""
            SELECT c.id, c.product_id, c.updated_at, p.title, p.image_url,
                   CASE WHEN c.buyer_id = ? THEN seller.username ELSE buyer.username END AS other_username,
                   (SELECT body FROM messages m WHERE m.conversation_id=c.id ORDER BY m.id DESC LIMIT 1) AS last_message,
                   (SELECT COUNT(*) FROM messages m WHERE m.conversation_id=c.id
                    AND m.sender_id<>? AND m.read_at IS NULL) AS unread_count
            FROM conversations c
            JOIN products p ON p.id=c.product_id
            JOIN users buyer ON buyer.id=c.buyer_id
            JOIN users seller ON seller.id=c.seller_id
            WHERE c.buyer_id=? OR c.seller_id=?
            ORDER BY c.updated_at DESC, c.id DESC
        """, (user_id, user_id, user_id, user_id)).fetchall()
    html = """
    {% extends 'base.html' %}{% block title %}站內訊息｜CampusSwap{% endblock %}
    {% block content %}
    <div class="dashboard-head"><div><span class="eyebrow">YOUR MESSAGES</span><h1>站內訊息</h1><p>與買家、賣家討論商品細節及面交方式。</p></div></div>
    <section class="message-inbox">
    {% for c in conversations %}
      <a class="conversation-row" href="{{ url_for('chat_room', conversation_id=c.id) }}">
        {% if c.image_url %}<img src="{{ c.image_url }}" alt="商品照片">{% else %}<div class="conversation-placeholder">CS</div>{% endif %}
        <div class="conversation-copy"><strong>{{ c.other_username }} <span>· {{ c.title }}</span></strong><p>{{ c.last_message or '尚無訊息' }}</p><small>{{ c.updated_at }}</small></div>
        {% if c.unread_count %}<span class="unread-pill">{{ c.unread_count }} 則未讀</span>{% endif %}
      </a>
    {% else %}<div class="empty-inline">目前還沒有對話。可以先到商品頁傳訊息給賣家。</div>{% endfor %}
    </section>{% endblock %}
    """
    return render_template_string(html, conversations=conversations)


@app.route("/chat/start/<int:product_id>", methods=["POST"])
@login_required
def start_chat(product_id):
    product = get_product_or_404(product_id)
    if product["user_id"] == session["user_id"]:
        flash("不能傳訊息給自己。", "warning")
        return redirect(url_for("product_detail", product_id=product_id))
    if not product["is_active"]:
        flash("此商品已下架，無法開啟新對話。", "warning")
        return redirect(url_for("product_detail", product_id=product_id))
    body = request.form.get("body", "").strip()
    if not body or len(body) > 1000:
        flash("訊息不可空白，且最多 1000 字。", "warning")
        return redirect(url_for("product_detail", product_id=product_id))
    with get_db() as conn:
        conn.execute("INSERT INTO conversations (product_id,buyer_id,seller_id) VALUES (?,?,?) ON CONFLICT DO NOTHING",
                     (product_id, session["user_id"], product["user_id"]))
        conv = conn.execute("SELECT id FROM conversations WHERE product_id=? AND buyer_id=? AND seller_id=?",
                            (product_id, session["user_id"], product["user_id"])).fetchone()
        conn.execute("INSERT INTO messages (conversation_id,sender_id,body) VALUES (?,?,?)",
                     (conv["id"], session["user_id"], body))
        conn.execute("UPDATE conversations SET updated_at=CURRENT_TIMESTAMP WHERE id=?", (conv["id"],))
    return redirect(url_for("chat_room", conversation_id=conv["id"]))


@app.route("/chat/<int:conversation_id>", methods=["GET", "POST"])
@login_required
def chat_room(conversation_id):
    user_id = session["user_id"]
    with get_db() as conn:
        conv = conn.execute("""
            SELECT c.*, p.title, p.id AS item_id,
                   b.username AS buyer_name, s.username AS seller_name
            FROM conversations c JOIN products p ON p.id=c.product_id
            JOIN users b ON b.id=c.buyer_id JOIN users s ON s.id=c.seller_id
            WHERE c.id=?
        """, (conversation_id,)).fetchone()
        if conv is None:
            abort(404)
        if user_id not in (conv["buyer_id"], conv["seller_id"]):
            abort(403)
        if request.method == "POST":
            body = request.form.get("body", "").strip()
            if not body or len(body) > 1000:
                flash("訊息不可空白，且最多 1000 字。", "warning")
            else:
                conn.execute("INSERT INTO messages (conversation_id,sender_id,body) VALUES (?,?,?)",
                             (conversation_id, user_id, body))
                conn.execute("UPDATE conversations SET updated_at=CURRENT_TIMESTAMP WHERE id=?", (conversation_id,))
                return redirect(url_for("chat_room", conversation_id=conversation_id))
        conn.execute("UPDATE messages SET read_at=CURRENT_TIMESTAMP WHERE conversation_id=? AND sender_id<>? AND read_at IS NULL",
                     (conversation_id, user_id))
        msgs = conn.execute("SELECT m.*,u.username FROM messages m JOIN users u ON u.id=m.sender_id WHERE m.conversation_id=? ORDER BY m.id",
                            (conversation_id,)).fetchall()
    other = conv["seller_name"] if user_id == conv["buyer_id"] else conv["buyer_name"]
    html = """
    {% extends 'base.html' %}{% block title %}與 {{ other }} 聊天｜CampusSwap{% endblock %}
    {% block content %}
    <div class="chat-page">
      <div class="chat-top"><a href="{{ url_for('messages_inbox') }}">← 所有訊息</a><div><strong>{{ other }}</strong><small>商品：{{ conv.title }}</small></div><a href="{{ url_for('product_detail', product_id=conv.product_id) }}">查看商品</a></div>
      <section class="chat-messages" id="chat-messages">
        {% for m in msgs %}<div class="chat-message {% if m.sender_id == session.get('user_id') %}mine{% else %}theirs{% endif %}"><small>{{ m.username }} · {{ m.created_at }}</small><p>{{ m.body }}</p></div>{% else %}<p class="chat-empty">傳送第一則訊息開始聊天。</p>{% endfor %}
      </section>
      <form class="chat-compose" method="post"><textarea name="body" rows="2" maxlength="1000" required placeholder="輸入訊息，最多 1000 字"></textarea><button class="button button-primary" type="submit">傳送</button></form>
    </div><script>const box=document.getElementById('chat-messages');if(box)box.scrollTop=box.scrollHeight;</script>
    {% endblock %}
    """
    return render_template_string(html, conv=conv, msgs=msgs, other=other)


@app.route("/health")
def health():
    return {"status": "ok"}


# Initialize tables on both local startup and Gunicorn import.
init_db()

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", "5000")), debug=False)
