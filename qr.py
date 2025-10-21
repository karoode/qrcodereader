# qr.py — Visitor QR & Badge server + Google Forms webhook + Dashboard

import os, io, sqlite3, string, secrets, base64, mimetypes, json
from datetime import datetime, date
from flask import Flask, request, jsonify, send_file, render_template_string, make_response
from PIL import Image, ImageDraw, ImageFont
import qrcode
import cv2
import numpy as np
from pyzbar import pyzbar

APP_TITLE = "Visitor QR"
# قاعدة بيانات في مجلد ثابت سهل إنشاؤه على Render
DB_PATH   = os.environ.get("QR_DB", "data/visitors.db").strip()
# سر Google Forms من متغير البيئة GF_SHARED_SECRET
GF_SHARED_SECRET = os.environ.get("GF_SHARED_SECRET", "").strip()

# ---------- Badge & font config ----------
FONT_SIZE        = 48
LINE_SPACING     = 1.5
BADGE_W, BADGE_H = 1200, 1800
BADGE_DPI        = int(os.environ.get("BADGE_DPI", "600"))

# Logo
LOGO_PATH        = os.environ.get("BADGE_LOGO", "logo.png")
LOGO_CM          = float(os.environ.get("BADGE_LOGO_CM", "2.0"))
HOLE_SAFE_CM     = float(os.environ.get("BADGE_HOLE_SAFE_CM", "0.8"))
LOGO_SHIFT_UP_CM = float(os.environ.get("BADGE_LOGO_SHIFT_UP_CM", "0.2"))
TEXT_TOP_GAP_CM  = float(os.environ.get("BADGE_TEXT_TOP_GAP_CM", "0.7"))

# QR on badge (bottom-centered, visitor_id only)
QR_CM_DEFAULT = float(os.environ.get("BADGE_QR_CM", "2.4"))
QR_CM        = QR_CM_DEFAULT
QR_BORDER    = int(os.environ.get("BADGE_QR_BORDER", "1"))

app = Flask(__name__)
app.config['MAX_CONTENT_LENGTH'] = 20 * 1024 * 1024  # 20MB uploads


# ---------- DB helpers ----------
def ensure_db():
    """ينشئ الجداول عند الحاجة."""
    db_dir = os.path.dirname(DB_PATH)
    if db_dir:
        os.makedirs(db_dir, exist_ok=True)
    with sqlite3.connect(DB_PATH) as con:
        cur = con.cursor()
        # جدول الزوار
        cur.execute("""
            CREATE TABLE IF NOT EXISTS visitors(
              id TEXT PRIMARY KEY,
              name TEXT NOT NULL,
              company TEXT NOT NULL,
              position TEXT NOT NULL,
              email TEXT NOT NULL UNIQUE,
              phone TEXT NOT NULL UNIQUE,
              pin TEXT NOT NULL,
              created_at TEXT NOT NULL
            )
        """)
        # جدول تسجيل كل استلام من الفورم (حتى التكرارات)
        cur.execute("""
            CREATE TABLE IF NOT EXISTS submissions(
              sid INTEGER PRIMARY KEY AUTOINCREMENT,
              ts TEXT NOT NULL,
              day TEXT NOT NULL,
              visitor_id TEXT NOT NULL,
              name TEXT,
              company TEXT,
              email TEXT,
              phone TEXT
            )
        """)
        con.commit()


def rand_token(prefix="ajz_", n=10):
    a = string.ascii_lowercase + string.digits
    return prefix + "".join(secrets.choice(a) for _ in range(n))


def rand_pin():
    return f"{secrets.randbelow(10000):04d}"


# ---------- Fonts / drawing ----------
def load_times_bold(size=FONT_SIZE):
    candidates = [
        "/Library/Fonts/Times New Roman Bold.ttf",
        "C:/Windows/Fonts/timesbd.ttf",
        "/usr/share/fonts/truetype/msttcorefonts/Times_New_Roman_Bold.ttf",
        "/usr/share/fonts/truetype/freefont/FreeSerifBold.ttf",
        "/usr/share/fonts/truetype/liberation/LiberationSerif-Bold.ttf",
        "/usr/share/fonts/truetype/dejavu/DejaVuSerif-Bold.ttf",
    ]
    envp = os.environ.get("BADGE_FONT_PATH")
    if envp: candidates.insert(0, envp)
    for p in candidates:
        if os.path.exists(p):
            try:
                return ImageFont.truetype(p, size=size)
            except Exception:
                pass
    return ImageFont.load_default()


def text_size(draw, text, font):
    if not text: return (0, 0)
    l, t, r, b = draw.textbbox((0, 0), text, font=font)
    return r - l, b - t


def cm_to_px(cm, dpi=BADGE_DPI):
    return int(round((cm / 2.54) * dpi))


def wrap_lines(draw, text, font, max_w):
    text = (text or "").strip()
    if not text:
        return [""]
    words = text.split()
    lines, cur = [], ""
    for w in words:
        candidate = (cur + " " + w).strip()
        if text_size(draw, candidate, font)[0] <= max_w or not cur:
            cur = candidate
        else:
            lines.append(cur)
            cur = w
    if cur: lines.append(cur)
    return lines


# ---------- QR helpers ----------
def build_qr_image(visitor_id, size_px):
    qr = qrcode.QRCode(version=None,
                       error_correction=qrcode.constants.ERROR_CORRECT_M,
                       box_size=10, border=QR_BORDER)
    qr.add_data(visitor_id)
    qr.make(fit=True)
    img = qr.make_image(fill_color="black", back_color="white").convert("RGB")
    return img.resize((size_px, size_px), Image.NEAREST)


def make_qr_png(visitor_id, label_name=None, size=1024):
    qr_img = build_qr_image(visitor_id, size)
    if not label_name:
        b = io.BytesIO()
        qr_img.save(b, "PNG"); b.seek(0)
        return b
    canvas = Image.new("RGB", (size, size + 160), "white")
    canvas.paste(qr_img, (0, 0))
    d = ImageDraw.Draw(canvas); f = load_times_bold(FONT_SIZE)
    tw, th = text_size(d, label_name, f)
    d.text(((size - tw)//2, size + (160 - th)//2), label_name, font=f, fill=(15, 15, 15))
    b = io.BytesIO(); canvas.save(b, "PNG"); b.seek(0)
    return b


# ---------- Badge compose ----------
def compose_badge_portrait(name, company, position, visitor_id=None, w=BADGE_W, h=BADGE_H):
    img = Image.new("RGB", (w, h), (255, 255, 255))
    d   = ImageDraw.Draw(img)
    f_label = load_times_bold(FONT_SIZE)

    # Logo أعلى — مرفوع لفوق بمقدار LOGO_SHIFT_UP_CM
    safe_top_px   = cm_to_px(HOLE_SAFE_CM)
    logo_shift_px = cm_to_px(LOGO_SHIFT_UP_CM)
    logo_side     = cm_to_px(LOGO_CM)
    y_logo        = max(cm_to_px(0.10), safe_top_px - logo_shift_px)

    after_logo = y_logo
    if os.path.exists(LOGO_PATH):
        try:
            logo = Image.open(LOGO_PATH).convert("RGBA")
            r = logo.width / float(logo.height)
            if r >= 1.0:
                nw, nh = logo_side, int(round(logo_side / r))
            else:
                nh, nw = logo_side, int(round(logo_side * r))
            logo = logo.resize((nw, nh), Image.LANCZOS)
            lx = (w - nw)//2
            img.paste(logo, (lx, y_logo), logo)
            after_logo = y_logo + nh
        except Exception:
            pass

    # نص يسار + التفاف
    side_pad      = cm_to_px(0.6)
    top_text      = after_logo + cm_to_px(TEXT_TOP_GAP_CM)
    max_text_w    = w - 2*side_pad
    gap_label_val = cm_to_px(0.6)
    labels        = ["Name:", "Company:", "Position:"]
    lw_max        = max(text_size(d, lbl, f_label)[0] for lbl in labels)
    value_x       = side_pad + lw_max + gap_label_val
    value_w       = max_text_w - lw_max - gap_label_val
    line_h        = int(round(LINE_SPACING * FONT_SIZE))

    def draw_row(label, value, y_top):
        d.text((side_pad, y_top), label, font=f_label, fill=(10, 10, 10))
        lines = wrap_lines(d, value, f_label, value_w)
        for i, ln in enumerate(lines):
            d.text((value_x, y_top + i*line_h), ln, font=f_label, fill=(10, 10, 10))
        used_h = max(line_h, line_h * len(lines))
        return used_h

    y = top_text
    y += draw_row("Name:",     (name or ""),     y)
    y += cm_to_px(0.35)
    y += draw_row("Company:",  (company or ""),  y)
    y += cm_to_px(0.35)
    y += draw_row("Position:", (position or ""), y)

    # QR أسفل
    if visitor_id:
        bottom_cm   = float(os.environ.get("BADGE_QR_BOTTOM_CM", "0.8"))
        min_cm      = float(os.environ.get("BADGE_QR_MIN_CM", "1.8"))
        max_cm      = float(os.environ.get("BADGE_QR_MAX_CM", "4.0"))
        max_ratio   = float(os.environ.get("BADGE_QR_MAX_RATIO", "0.28"))
        safety_cm   = float(os.environ.get("BADGE_QR_SAFETY_CM", "1.6"))

        bottom_margin = cm_to_px(bottom_cm)
        safety_gap    = cm_to_px(safety_cm)

        qr_nominal = cm_to_px(QR_CM)
        qr_min     = cm_to_px(min_cm)
        qr_max     = min(int(w * max_ratio), cm_to_px(max_cm))

        qr_side = max(qr_min, min(qr_nominal, qr_max))

        available_h = h - bottom_margin - (y + safety_gap)
        if available_h < qr_min:
            qr_side = qr_min
        else:
            qr_side = min(qr_side, available_h)

        qx = (w - qr_side) // 2
        qy = h - bottom_margin - qr_side

        qr_img = build_qr_image(visitor_id, qr_side)
        img.paste(qr_img, (qx, qy))

    return img


def make_badge_png(name, company, position, visitor_id=None, rotate_ccw=False):
    img = compose_badge_portrait(name, company, position, visitor_id=visitor_id)
    if rotate_ccw:
        img = img.transpose(Image.ROTATE_90)  # 90° CCW
    b = io.BytesIO()
    img.save(b, "PNG")
    b.seek(0)
    return b


# ---------- Robust QR decode ----------
def decode_pyzbar(img_bgr):
    gray = cv2.cvtColor(img_bgr, cv2.COLOR_BGR2GRAY)
    res  = pyzbar.decode(gray)
    if not res: return "", []
    b = res[0]
    txt = b.data.decode("utf-8", errors="replace")
    if b.polygon and len(b.polygon) >= 4:
        poly = [[float(p.x), float(p.y)] for p in b.polygon]
    else:
        x, y, w, h = b.rect
        poly = [[x, y], [x+w, y], [x+w, y+h], [x, y+h]]
    return txt, poly


def preprocess_contrast(img):
    return cv2.convertScaleAbs(img, alpha=1.35, beta=8)


def robust_decode(img):
    h, w = img.shape[:2]
    if max(w, h) < 800:
        s = 800.0 / max(w, h)
        img = cv2.resize(img, None, fx=s, fy=s, interpolation=cv2.INTER_CUBIC)
    elif max(w, h) > 2000:
        s = 2000.0 / max(w, h)
        img = cv2.resize(img, None, fx=s, fy=s, interpolation=cv2.INTER_AREA)

    t, p = decode_pyzbar(img)
    if t: return t, p

    boosted = preprocess_contrast(img)
    t, p = decode_pyzbar(boosted)
    if t: return t, p

    for k in [cv2.ROTATE_90_CLOCKWISE, cv2.ROTATE_180, cv2.ROTATE_90_COUNTERCLOCKWISE]:
        rot = cv2.rotate(boosted, k)
        t, p = decode_pyzbar(rot)
        if t: return t, p

    g = cv2.cvtColor(boosted, cv2.COLOR_BGR2GRAY)
    g = cv2.medianBlur(g, 3)
    thr = cv2.adaptiveThreshold(g, 255, cv2.ADAPTIVE_THRESH_GAUSSIAN_C,
                                cv2.THRESH_BINARY, 31, 5)
    t, p = decode_pyzbar(cv2.cvtColor(thr, cv2.COLOR_GRAY2BGR))
    if t: return t, p
    inv = cv2.bitwise_not(thr)
    t, p = decode_pyzbar(cv2.cvtColor(inv, cv2.COLOR_GRAY2BGR))
    if t: return t, p
    return "", []


# ---------- HTML ----------
DASH_HTML = """
<!doctype html><html lang="ar" dir="rtl">
<head>
<meta charset="utf-8"/><title>{{title}}</title>
<meta name="viewport" content="width=device-width, initial-scale=1"/>
<link href="https://cdn.jsdelivr.net/npm/bootstrap@5.3.3/dist/css/bootstrap.min.css" rel="stylesheet">
<style>
  body{background:#f4f6fb;color:#111827}
  .card{background:#fff;border:1px solid #e5e7eb;border-radius:14px}
  .muted{color:#6b7280}
  .chip{background:#eef2ff;color:#3730a3;border:1px solid #c7d2fe;border-radius:999px;padding:.25rem .75rem;display:inline-block}
  .table td,.table th{vertical-align:middle}
  .mono{font-family:ui-monospace, SFMono-Regular, Menlo, Monaco, Consolas, "Liberation Mono", "Courier New", monospace}
</style>
</head>
<body class="p-3 p-md-4">
  <div class="container-fluid">
    <div class="d-flex flex-wrap align-items-center justify-content-between mb-4">
      <h3 class="m-0">{{title}}</h3>
      <div class="d-flex align-items-center gap-2">
        <span class="chip">اليوم: {{today}}</span>
        <a class="btn btn-sm btn-outline-secondary" href="/stats.json" target="_blank">stats.json</a>
        <a class="btn btn-sm btn-outline-secondary" href="/health" target="_blank">health</a>
      </div>
    </div>

    <div class="row g-4 mb-2">
      <div class="col-12 col-md-4">
        <div class="card p-3">
          <div class="muted">عدد الطلبات (submissions)</div>
          <div class="display-6">{{submissions_count}}</div>
        </div>
      </div>
      <div class="col-12 col-md-4">
        <div class="card p-3">
          <div class="muted">عدد الزوار (visitors)</div>
          <div class="display-6">{{visitors_count}}</div>
        </div>
      </div>
      <div class="col-12 col-md-4">
        <div class="card p-3">
          <div class="muted">طلبات اليوم</div>
          <div class="display-6">{{today_count}}</div>
        </div>
      </div>
    </div>

    <div class="card p-3">
      <div class="d-flex align-items-center justify-content-between">
        <div class="muted">آخر {{rows|length}} إرسال</div>
      </div>
      <div class="table-responsive mt-2">
        <table class="table table-hover align-middle">
          <thead class="table-light">
            <tr>
              <th style="min-width:180px;">الوقت</th>
              <th>الاسم</th>
              <th>الشركة</th>
              <th>الهاتف</th>
              <th style="min-width:220px;">روابط</th>
            </tr>
          </thead>
          <tbody>
            {% for r in rows %}
            <tr>
              <td class="mono">{{r['ts']}}</td>
              <td>{{r['name'] or ''}}</td>
              <td>{{r['company'] or ''}}</td>
              <td>{{r['phone'] or ''}}</td>
              <td>
                <a class="btn btn-sm btn-outline-primary me-2" href="/qr/{{r['visitor_id']}}.png" target="_blank">QR</a>
                <a class="btn btn-sm btn-outline-secondary me-2" href="/qr/{{r['visitor_id']}}.png?dl=1">تحميل</a>
                <a class="btn btn-sm btn-outline-dark me-2" href="/card/{{r['visitor_id']}}.png" target="_blank">Badge</a>
                <a class="btn btn-sm btn-outline-dark" href="/card_landscape/{{r['visitor_id']}}.png" target="_blank">Landscape</a>
              </td>
            </tr>
            {% endfor %}
            {% if rows|length == 0 %}
            <tr><td colspan="5" class="muted">لا يوجد إرسال بعد.</td></tr>
            {% endif %}
          </tbody>
        </table>
      </div>
    </div>
  </div>
</body>
</html>
"""

# ---------- Helpers ----------
def corsify(resp):
    h = resp.headers
    h["Access-Control-Allow-Origin"] = "*"
    h["Access-Control-Allow-Methods"] = "POST, OPTIONS"
    h["Access-Control-Allow-Headers"] = "Content-Type, X-Secret"
    return resp


def pick(d, *keys):
    for k in keys:
        if not k: continue
        if k in d and d[k]: return d[k]
    return ""


def _today_str():
    return date.today().strftime("%Y-%m-%d")


# ---------- Routes ----------
@app.route("/")
def dashboard():
    """لوحة الإحصاءات بدل صفحة التسجيل اليدوي."""
    ensure_db()
    with sqlite3.connect(DB_PATH) as con:
        con.row_factory = sqlite3.Row
        cur = con.cursor()
        cur.execute("SELECT COUNT(*) FROM submissions")
        sub_count = cur.fetchone()[0]
        cur.execute("SELECT COUNT(*) FROM visitors")
        vis_count = cur.fetchone()[0]
        cur.execute("SELECT COUNT(*) FROM submissions WHERE day=?", (_today_str(),))
        today_count = cur.fetchone()[0]
        cur.execute(
            "SELECT ts, visitor_id, name, company, phone FROM submissions ORDER BY sid DESC LIMIT 50"
        )
        rows = [dict(r) for r in cur.fetchall()]

    return render_template_string(
        DASH_HTML,
        title=APP_TITLE,
        today=_today_str(),
        submissions_count=sub_count,
        visitors_count=vis_count,
        today_count=today_count,
        rows=rows
    )


@app.route("/stats.json")
def stats_json():
    ensure_db()
    with sqlite3.connect(DB_PATH) as con:
        con.row_factory = sqlite3.Row
        cur = con.cursor()
        cur.execute("SELECT COUNT(*) AS c FROM submissions")
        sub_count = cur.fetchone()["c"]
        cur.execute("SELECT COUNT(*) AS c FROM visitors")
        vis_count = cur.fetchone()["c"]
        cur.execute("SELECT COUNT(*) AS c FROM submissions WHERE day=?", (_today_str(),))
        today_count = cur.fetchone()["c"]
        cur.execute(
            "SELECT sid, ts, day, visitor_id, name, company, email, phone FROM submissions ORDER BY sid DESC LIMIT 200"
        )
        rows = [dict(r) for r in cur.fetchall()]
    return jsonify(ok=True, submissions=sub_count, visitors=vis_count, today=today_count, rows=rows)


@app.route("/ui_logo")
def ui_logo():
    if not os.path.exists(LOGO_PATH):
        img = Image.new("RGB", (1, 1), "white")
        buf = io.BytesIO(); img.save(buf, "PNG"); buf.seek(0)
        return send_file(buf, mimetype="image/png")
    mime = mimetypes.guess_type(LOGO_PATH)[0] or "image/png"
    return send_file(LOGO_PATH, mimetype=mime)


# (أبقينا /create لو احتجته مستقبلاً، لكنه لم يعد يظهر في /)
@app.route("/create", methods=["POST"])
def create():
    ensure_db()
    f = request.form
    name     = (f.get("name") or "").strip()
    company  = (f.get("company") or "").strip()
    position = (f.get("position") or "").strip()
    email    = (f.get("email") or "").strip().lower()
    phone    = (f.get("phone") or "").strip()
    if not all([name, company, position, email, phone]):
        return "missing fields", 400
    with sqlite3.connect(DB_PATH) as con:
        con.row_factory = sqlite3.Row
        cur = con.cursor()
        cur.execute("SELECT * FROM visitors WHERE email=? OR phone=?", (email, phone))
        row = cur.fetchone()
        if row:
            rec = dict(row)
        else:
            vid = rand_token(); pin = rand_pin()
            cur.execute("""INSERT INTO visitors(id,name,company,position,email,phone,pin,created_at)
                           VALUES(?,?,?,?,?,?,?,?)""",
                        (vid, name, company, position, email, phone, pin, datetime.utcnow().isoformat()))
            con.commit()
            rec = dict(id=vid, name=name, company=company, position=position,
                       email=email, phone=phone, pin=pin, created_at=datetime.utcnow().isoformat())
        # سجل كطلب أيضاً
        cur.execute("""INSERT INTO submissions(ts,day,visitor_id,name,company,email,phone)
                       VALUES(?,?,?,?,?,?,?)""",
                    (datetime.utcnow().isoformat(timespec="seconds"), _today_str(),
                     rec["id"], name, company, email, phone))
        con.commit()
    return jsonify(ok=True, id=rec["id"])


@app.route("/qr/<vid>.png")
def qr_png(vid):
    ensure_db()
    with sqlite3.connect(DB_PATH) as con:
        con.row_factory = sqlite3.Row
        cur = con.cursor()
        cur.execute("SELECT name FROM visitors WHERE id=?", (vid,))
        row = cur.fetchone()
        if not row: return "Not found", 404
    bio = make_qr_png(vid, label_name=row["name"])
    dl = (request.args.get("dl") or "").strip().lower() in ("1", "true", "yes", "download")
    if dl:
        try:
            return send_file(bio, mimetype="image/png", as_attachment=True, download_name=f"{vid}.png")
        except TypeError:
            # لأن بعض الإصدارات الأقدم من Flask
            return send_file(bio, mimetype="image/png", as_attachment=True,
                             attachment_filename=f"{vid}.png")
    return send_file(bio, mimetype="image/png")


# ---------- Google Forms webhook ----------
@app.route("/forms/google", methods=["POST", "OPTIONS"])
def forms_google():
    if request.method == "OPTIONS":
        return corsify(make_response(("", 204)))

    # تحقق السر من الهيدر X-Secret أو من الحقل "secret"
    incoming_secret = (request.headers.get("X-Secret") or
                       (request.form.get("secret") if request.form else "") or
                       (request.json.get("secret") if request.is_json and request.json else "") or "").strip()
    if GF_SHARED_SECRET and incoming_secret != GF_SHARED_SECRET:
        return corsify(jsonify(ok=False, error="unauthorized")), 401

    # يدعم JSON بصيغة namedValues أو body عادي
    if request.is_json:
        data = request.get_json(silent=True) or {}
        if "namedValues" in data and isinstance(data["namedValues"], dict):
            nv = data["namedValues"]
            def nv_get(key):
                v = nv.get(key) or nv.get(key.strip()) or []
                return v[0] if isinstance(v, list) and v else (v if isinstance(v, str) else "")
            data = {
                "name":     nv_get("Name")     or nv_get("الاسم"),
                "company":  nv_get("Company")  or nv_get("الشركة"),
                "position": nv_get("Position") or nv_get("المنصب"),
                "email":    nv_get("Email")    or nv_get("البريد"),
                "phone":    nv_get("Phone")    or nv_get("الهاتف"),
            }
    else:
        data = request.form.to_dict(flat=True)

    def _pick(*keys):
        for k in keys:
            v = data.get(k)
            if v: return v
        return ""

    name     = (_pick("name", "Name", "الاسم", "full_name", "Full Name") or "").strip()
    company  = (_pick("company", "Company", "الشركة") or "").strip()
    position = (_pick("position", "Position", "المنصب") or "").strip()
    email    = (_pick("email", "Email", "البريد") or "").strip().lower()
    phone    = (_pick("phone", "Phone", "الهاتف") or "").strip()

    if not all([name, company, position, email, phone]):
        return corsify(jsonify(ok=False, error="missing_fields",
                               need=["name", "company", "position", "email", "phone"])), 400

    ensure_db()
    with sqlite3.connect(DB_PATH) as con:
        con.row_factory = sqlite3.Row
        cur = con.cursor()
        cur.execute("SELECT * FROM visitors WHERE email=? OR phone=?", (email, phone))
        row = cur.fetchone()
        if row:
            rec = dict(row)
        else:
            vid = rand_token(); pin = rand_pin()
            cur.execute("""INSERT INTO visitors(id,name,company,position,email,phone,pin,created_at)
                           VALUES(?,?,?,?,?,?,?,?)""",
                        (vid, name, company, position, email, phone, pin, datetime.utcnow().isoformat()))
            con.commit()
            rec = dict(id=vid, name=name, company=company, position=position,
                       email=email, phone=phone, pin=pin, created_at=datetime.utcnow().isoformat())
        # سجل الطلب
        cur.execute("""INSERT INTO submissions(ts,day,visitor_id,name,company,email,phone)
                       VALUES(?,?,?,?,?,?,?)""",
                    (datetime.utcnow().isoformat(timespec="seconds"), _today_str(),
                     rec["id"], name, company, email, phone))
        con.commit()

    # روابط مفيدة
    base = request.host_url.rstrip("/")
    qr_url = f"{base}/qr/{rec['id']}.png"
    dl_url = f"{qr_url}?dl=1"
    card   = f"{base}/card/{rec['id']}.png"
    card_l = f"{base}/card_landscape/{rec['id']}.png"

    return corsify(jsonify(
        ok=True, id=rec["id"], name=rec["name"],
        qr=qr_url, qr_download=dl_url,
        card_portrait=card, card_landscape=card_l
    ))


@app.route("/card/<vid>.png")
def card_png(vid):
    ensure_db()
    with sqlite3.connect(DB_PATH) as con:
        con.row_factory = sqlite3.Row
        cur = con.cursor()
        cur.execute("SELECT name,company,position FROM visitors WHERE id=?", (vid,))
        row = cur.fetchone()
        if not row: return "Not found", 404
    return send_file(
        make_badge_png(row["name"], row["company"], row["position"], visitor_id=vid, rotate_ccw=False),
        mimetype="image/png"
    )


@app.route("/card_landscape/<vid>.png")
def card_landscape_png(vid):
    ensure_db()
    with sqlite3.connect(DB_PATH) as con:
        con.row_factory = sqlite3.Row
        cur = con.cursor()
        cur.execute("SELECT name,company,position FROM visitors WHERE id=?", (vid,))
        row = cur.fetchone()
        if not row: return "Not found", 404
    return send_file(
        make_badge_png(row["name"], row["company"], row["position"], visitor_id=vid, rotate_ccw=True),
        mimetype="image/png"
    )


@app.route("/health", methods=["GET"])
def health():
    return jsonify(ok=True, msg="up", host=request.host), 200


# ----------- (اختبارات قراءة شارات) ------------
@app.route("/decode_badge", methods=["GET"])
@app.route("/decode_badge/", methods=["GET"])
@app.route("/decode_badge/<path:_extra>", methods=["GET"])
def decode_badge_form(_extra=""):
    return """
    <html><body>
      <h3>Upload test</h3>
      <form method="POST" action="/decode_badge" enctype="multipart/form-data">
        <input type="file" name="image" accept="image/*"/>
        <label><input type="checkbox" name="landscape_trick" checked> landscape-trick (rotate 90° CCW)</label>
        <button type="submit">Send</button>
      </form>
    </body></html>
    """, 200


@app.route("/decode_badge", methods=["POST"])
@app.route("/decode_badge/", methods=["POST"])
@app.route("/decode_badge/<path:_extra>", methods=["POST"])
def decode_badge(_extra=""):
    lt_q = (request.args.get("landscape_trick") or "").strip().lower()
    lt_f = (request.form.get("landscape_trick") or "").strip().lower()
    landscape_trick = not (lt_q in ("0", "false", "no") or lt_f in ("0", "false", "no"))

    img = None
    for field in ("image", "file", "photo", "frame", "upload"):
        if field in request.files:
            arr = np.frombuffer(request.files[field].read(), np.uint8)
            img = cv2.imdecode(arr, cv2.IMREAD_COLOR)
            break
    if img is None and request.data and request.content_type and (
        "image/" in request.content_type or "application/octet-stream" in request.content_type):
        arr = np.frombuffer(request.data, np.uint8)
        img = cv2.imdecode(arr, cv2.IMREAD_COLOR)
    if img is None and request.is_json:
        data = request.get_json(silent=True) or {}
        b64 = (data.get("image_b64") or data.get("image") or "").strip()
        if b64:
            if "," in b64: b64 = b64.split(",", 1)[-1]
            try:
                arr = np.frombuffer(base64.b64decode(b64), np.uint8)
                img = cv2.imdecode(arr, cv2.IMREAD_COLOR)
            except Exception:
                img = None
    if img is None:
        return jsonify(ok=False, error="no_image_supplied"), 400

    vid, _ = robust_decode(img)
    if not vid:
        return jsonify(ok=False, error="decode_failed"), 404

    with sqlite3.connect(DB_PATH) as con:
        con.row_factory = sqlite3.Row
        cur = con.cursor()
        cur.execute("SELECT name,company,position FROM visitors WHERE id=?", (vid,))
        row = cur.fetchone()

    if not row:
        return jsonify(ok=False, error="unknown_visitor", vid=vid), 404

    return send_file(
        make_badge_png(row["name"], row["company"], row["position"], visitor_id=vid, rotate_ccw=landscape_trick),
        mimetype="image/png"
    )


@app.route("/decode", methods=["POST"])
def decode_json():
    img = None
    if "image" in request.files:
        arr = np.frombuffer(request.files["image"].read(), np.uint8)
        img = cv2.imdecode(arr, cv2.IMREAD_COLOR)
    elif request.data and request.content_type and (
        "image/" in request.content_type or "application/octet-stream" in request.content_type):
        arr = np.frombuffer(request.data, np.uint8)
        img = cv2.imdecode(arr, cv2.IMREAD_COLOR)
    elif request.is_json:
        data = request.get_json(silent=True) or {}
        b64 = (data.get("image_b64") or "").strip()
        if "," in b64: b64 = b64.split(",", 1)[-1]
        if b64:
            try:
                arr = np.frombuffer(base64.b64decode(b64), np.uint8)
                img = cv2.imdecode(arr, cv2.IMREAD_COLOR)
            except Exception:
                img = None

    if img is None:
        return jsonify(ok=False, error="no_image_supplied"), 400

    text, poly = robust_decode(img)
    return jsonify(ok=bool(text), text=text or "", poly=poly or [])


# ---------- Main ----------
if __name__ == "__main__":
    ensure_db()
    # على Render استخدم أمر التشغيل:
    # gunicorn -b 0.0.0.0:$PORT qr:app
    app.run(host="0.0.0.0", port=5001, threaded=True, debug=False)
