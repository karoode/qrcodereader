# qr.py — Visitor QR & Badge server + Google Forms webhook + Dashboard with Badge/QR toggle

import os, io, sqlite3, string, secrets, base64, mimetypes, json
from datetime import datetime, date
from flask import Flask, request, jsonify, send_file, render_template_string, make_response
from PIL import Image, ImageDraw, ImageFont
import qrcode
import cv2
import numpy as np
from pyzbar import pyzbar

# اختياري: للاتصال بسيرفر واتساب البروكسي إذا مفعّل
try:
    import requests  # تأكدت تضيف requests بالrequirements
except Exception:
    requests = None

APP_TITLE = "Visitor QR"
DB_PATH   = os.environ.get("QR_DB", "data/visitors.db").strip()
GF_SHARED_SECRET = os.environ.get("GF_SHARED_SECRET", "").strip()

# ---------- Badge & font config ----------
FONT_SIZE        = 48
LINE_SPACING     = 1.5
BADGE_W, BADGE_H = 1200, 1800
BADGE_DPI        = int(os.environ.get("BADGE_DPI", "600"))

# Logo
LOGO_PATH         = os.environ.get("BADGE_LOGO", "logo.png")  # حط اسم الملف بالـrepo
LOGO_CM           = float(os.environ.get("BADGE_LOGO_CM", "2.0"))
HOLE_SAFE_CM      = float(os.environ.get("BADGE_HOLE_SAFE_CM", "0.8"))
LOGO_SHIFT_UP_CM  = float(os.environ.get("BADGE_LOGO_SHIFT_UP_CM", "0.2"))
TEXT_TOP_GAP_CM   = float(os.environ.get("BADGE_TEXT_TOP_GAP_CM", "0.7"))

# QR on badge (bottom-centered)
QR_CM_DEFAULT = float(os.environ.get("BADGE_QR_CM", "2.4"))
QR_CM         = QR_CM_DEFAULT
QR_BORDER     = int(os.environ.get("BADGE_QR_BORDER", "1"))

# اختياري: بروكسي إرسال واتساب كصورة باستخدام التمبلت نفسها
WA_PROXY_URL  = (os.environ.get("WA_PROXY_URL") or "").strip()  # مثال: https://your-whats-server.onrender.com/send-image

app = Flask(__name__)
app.config['MAX_CONTENT_LENGTH'] = 20 * 1024 * 1024  # 20MB uploads

# ---------- DB helpers ----------
def ensure_db():
    db_dir = os.path.dirname(DB_PATH)
    if db_dir:
        os.makedirs(db_dir, exist_ok=True)
    with sqlite3.connect(DB_PATH) as con:
        cur = con.cursor()
        cur.execute("""
            CREATE TABLE IF NOT EXISTS visitors(
              id TEXT PRIMARY KEY,
              name TEXT NOT NULL,
              company TEXT NOT NULL,
              position TEXT NOT NULL,
              email TEXT NOT NULL UNIQUE,
              phone TEXT NOT NULL UNIQUE,
              pin TEXT NOT NULL,
              created_at TEXT NOT NULL,
              wa_sent INTEGER DEFAULT 0,
              wa_ts TEXT
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
    if not text: return (0,0)
    l, t, r, b = draw.textbbox((0,0), text, font=font)
    return r-l, b-t

def cm_to_px(cm, dpi=BADGE_DPI):
    return int(round((cm / 2.54) * dpi))

def wrap_lines(draw, text, font, max_w):
    text = (text or "").strip()
    if not text:
        return [""]
    words = text.split()
    lines, cur = [], ""
    for w in words:
        cand = (cur + " " + w).strip()
        if text_size(draw, cand, font)[0] <= max_w or not cur:
            cur = cand
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
        b = io.BytesIO(); qr_img.save(b, "PNG"); b.seek(0); return b
    canvas = Image.new("RGB", (size, size + 160), "white")
    canvas.paste(qr_img, (0,0))
    d = ImageDraw.Draw(canvas); f = load_times_bold(FONT_SIZE)
    tw, th = text_size(d, label_name, f)
    d.text(((size - tw)//2, size + (160 - th)//2), label_name, font=f, fill=(15,15,15))
    b = io.BytesIO(); canvas.save(b, "PNG"); b.seek(0); return b

# ---------- Badge compose ----------
def compose_badge_portrait(name, company, position, visitor_id=None, w=BADGE_W, h=BADGE_H):
    img = Image.new("RGB", (w, h), (255, 255, 255))
    d   = ImageDraw.Draw(img)
    f_label = load_times_bold(FONT_SIZE)

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
        d.text((side_pad, y_top), label, font=f_label, fill=(10,10,10))
        lines = wrap_lines(d, value, f_label, value_w)
        for i, ln in enumerate(lines):
            d.text((value_x, y_top + i*line_h), ln, font=f_label, fill=(10,10,10))
        used_h = max(line_h, line_h * len(lines))
        return used_h

    y = top_text
    y += draw_row("Name:",     (name or ""),     y)
    y += cm_to_px(0.35)
    y += draw_row("Company:",  (company or ""),  y)
    y += cm_to_px(0.35)
    y += draw_row("Position:", (position or ""), y)

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
    if b.polygon and len(b.polygon)>=4:
        poly = [[float(p.x), float(p.y)] for p in b.polygon]
    else:
        x,y,w,h = b.rect
        poly = [[x,y],[x+w,y],[x+w,y+h],[x,y+h]]
    return txt, poly

def preprocess_contrast(img): return cv2.convertScaleAbs(img, alpha=1.35, beta=8)

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
    thr = cv2.adaptiveThreshold(g,255,cv2.ADAPTIVE_THRESH_GAUSSIAN_C,
                                cv2.THRESH_BINARY,31,5)
    t, p = decode_pyzbar(cv2.cvtColor(thr, cv2.COLOR_GRAY2BGR))
    if t: return t, p
    inv = cv2.bitwise_not(thr)
    t, p = decode_pyzbar(cv2.cvtColor(inv, cv2.COLOR_GRAY2BGR))
    if t: return t, p
    return "", []

# ---------- HTML (dashboard with toggle) ----------
INDEX_HTML = """
<!doctype html><html lang="ar" dir="rtl"><head><meta charset="utf-8"/>
<title>{{title}}</title>
<meta name="viewport" content="width=device-width, initial-scale=1"/>
<style>
:root{ --bg:#ffffff; --fg:#0f141a; --muted:#6b7280; --border:#e5e7eb; --chip:#eef2ff; --chipb:#c7d2fe; --accent:#111827;}
*{box-sizing:border-box}
body{background:var(--bg);color:var(--fg);font-family:system-ui,-apple-system,Segoe UI,Roboto,Ubuntu,"Helvetica Neue",Arial;margin:0;}
.wrap{max-width:1100px;margin:28px auto;padding:0 16px;position:relative;}
.logo-fixed{ position:absolute; left:16px; top:6px; width:120px; height:auto; object-fit:contain; }
h1{margin:0 0 18px 0; padding-inline-start:140px;}
.grid{display:grid;grid-template-columns:repeat(3,minmax(0,1fr));gap:14px;margin-bottom:18px}
.card{background:#fff;border:1px solid var(--border);border-radius:14px;padding:14px}
.kpi{font-size:36px;font-weight:800}
.muted{color:var(--muted)}
.cards{display:grid;grid-template-columns:repeat(3,minmax(0,1fr));gap:18px}
.vcard{background:#fff;border:1px solid var(--border);border-radius:14px;padding:12px}
.vimg{width:100%;background:#fff;border:1px solid var(--border);border-radius:12px;display:block;aspect-ratio:1/1;object-fit:contain}
.btns{display:flex;gap:6px;margin:8px 0 6px 0}
.btn{border:1px solid var(--border);background:#fff;border-radius:9px;padding:6px 10px;cursor:pointer;font-size:13px}
.btn.active{border-color:#111827}
.name{font-weight:800;margin-top:2px}
.sub{font-size:12px;color:var(--muted)}
</style>
</head><body>
<div class="wrap">
  <img class="logo-fixed" src="/ui_logo" alt="logo"/>
  <h1>{{title}}</h1>

  <div class="grid">
    <div class="card"><div class="muted">إجمالي الطلبات</div><div class="kpi">{{kpis.total}}</div></div>
    <div class="card"><div class="muted">طلبات اليوم</div><div class="kpi">{{kpis.today}}</div></div>
    <div class="card"><div class="muted">إرسال واتساب</div><div class="kpi">{{kpis.wa}}</div></div>
  </div>

  <div class="cards">
    {% for v in last %}
    <div class="vcard">
      <img class="vimg" id="img_{{v['id']}}" src="/qr/{{v['id']}}.png" 
           data-qr="/qr/{{v['id']}}.png" data-badge="/card/{{v['id']}}.png" alt="preview"/>
      <div class="btns">
        <button class="btn active" onclick="showQR('{{v['id']}}', this)">QR</button>
        <button class="btn" onclick="showBadge('{{v['id']}}', this)">Badge</button>
        <a class="btn" href="/qr/{{v['id']}}.png?dl=1">تحميل QR</a>
        <a class="btn" href="/card/{{v['id']}}.png" target="_blank">فتح الباج</a>
      </div>
      <div class="name">{{v['name']}}</div>
      <div class="sub">{{v['company']}} — {{v['created_at'][:16].replace('T',' ')}}</div>
    </div>
    {% endfor %}
  </div>
</div>

<script>
function activate(btn){ 
  const p = btn.parentElement.querySelectorAll('.btn'); 
  p.forEach(b=>b.classList.remove('active')); 
  btn.classList.add('active');
}
function showQR(id, btn){
  const img = document.getElementById('img_'+id);
  img.src = img.dataset.qr;
  activate(btn);
}
function showBadge(id, btn){
  const img = document.getElementById('img_'+id);
  img.src = img.dataset.badge;
  activate(btn);
}
</script>
</body></html>
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

# ---------- Routes ----------
@app.route("/")
def index():
    ensure_db()
    # KPIs
    with sqlite3.connect(DB_PATH) as con:
        con.row_factory = sqlite3.Row
        cur = con.cursor()
        cur.execute("SELECT COUNT(*) AS c FROM visitors")
        total = cur.fetchone()["c"]
        cur.execute("SELECT COUNT(*) AS c FROM visitors WHERE DATE(substr(created_at,1,10)) = DATE(?)",
                    (date.today().isoformat(),))
        today = cur.fetchone()["c"]
        # إذا العمود موجود يطلع العدد، وإلا اعتبره 0
        try:
            cur.execute("SELECT COUNT(*) AS c FROM visitors WHERE wa_sent=1")
            wa = cur.fetchone()["c"]
        except Exception:
            wa = 0
        cur.execute("SELECT id,name,company,position,created_at FROM visitors ORDER BY datetime(created_at) DESC LIMIT 12")
        last = [dict(r) for r in cur.fetchall()]

    return render_template_string(
        INDEX_HTML,
        title=APP_TITLE,
        kpis=dict(total=total, today=today, wa=wa),
        last=last
    )

@app.route("/ui_logo")
def ui_logo():
    if not os.path.exists(LOGO_PATH):
        img = Image.new("RGB", (160,40), "white")
        buf = io.BytesIO(); img.save(buf, "PNG"); buf.seek(0)
        return send_file(buf, mimetype="image/png")
    mime = mimetypes.guess_type(LOGO_PATH)[0] or "image/png"
    return send_file(LOGO_PATH, mimetype=mime)

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
        return jsonify(ok=False, error="missing_fields"), 400
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
    dl = (request.args.get("dl") or "").strip().lower() in ("1","true","yes","download")
    if dl:
        try:
            return send_file(bio, mimetype="image/png", as_attachment=True, download_name=f"{vid}.png")
        except TypeError:
            return send_file(bio, mimetype="image/png", as_attachment=True, attachment_filename=f"{vid}.png")
    return send_file(bio, mimetype="image/png")

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

# ---------- Google Forms webhook ----------
@app.route("/forms/google", methods=["POST", "OPTIONS"])
def forms_google():
    if request.method == "OPTIONS":
        return corsify(make_response(("", 204)))

    incoming_secret = (request.headers.get("X-Secret") or
                       (request.form.get("secret") if request.form else "") or
                       (request.json.get("secret") if request.is_json and request.json else "") or "").strip()
    if GF_SHARED_SECRET and incoming_secret != GF_SHARED_SECRET:
        return corsify(jsonify(ok=False, error="unauthorized")), 401

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

    name     = (pick(data, "name", "Name", "الاسم", "full_name", "Full Name") or "").strip()
    company  = (pick(data, "company", "Company", "الشركة") or "").strip()
    position = (pick(data, "position", "Position", "المنصب") or "").strip()
    email    = (pick(data, "email", "Email", "البريد") or "").strip().lower()
    phone    = (pick(data, "phone", "Phone", "الهاتف") or "").strip()

    if not all([name, company, position, email, phone]):
        return corsify(jsonify(ok=False, error="missing_fields",
                               need=["name","company","position","email","phone"])), 400

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

    # توليد صورة QR وبطاقـة
    qr_buf   = make_qr_png(rec["id"], label_name=rec["name"])
    card_buf = make_badge_png(rec["name"], rec["company"], rec["position"], visitor_id=rec["id"], rotate_ccw=False)

    # إرسال واتساب عبر بروكسي (اختياري)
    wa_result = None
    if WA_PROXY_URL and requests is not None:
        try:
            files = {"file": ("badge.png", card_buf.getvalue(), "image/png")}
            data  = {"to": rec["phone"], "name": rec["name"]}
            r = requests.post(WA_PROXY_URL, files=files, data=data, timeout=60)
            if r.ok:
                wa_result = {"ok": True}
                with sqlite3.connect(DB_PATH) as con:
                    con.execute("UPDATE visitors SET wa_sent=1, wa_ts=? WHERE id=?",
                                (datetime.utcnow().isoformat(), rec["id"]))
                    con.commit()
            else:
                wa_result = {"ok": False, "code": r.status_code, "text": r.text}
        except Exception as e:
            wa_result = {"ok": False, "error": str(e)}

    base = request.host_url.rstrip("/")
    qr_url = f"{base}/qr/{rec['id']}.png"
    dl_url = f"{qr_url}?dl=1"
    card   = f"{base}/card/{rec['id']}.png"
    card_l = f"{base}/card_landscape/{rec['id']}.png"

    out = dict(ok=True, id=rec["id"], name=rec["name"],
               qr=qr_url, qr_download=dl_url,
               card_portrait=card, card_landscape=card_l)
    if wa_result is not None:
        out["whatsapp"] = {"mode": "proxy", **wa_result}
    return corsify(jsonify(out))

# ---- Badge decode & helpers (تبقى كما هي) ----
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
    landscape_trick = True
    if lt_q in ("0","false","no"): landscape_trick = False
    if lt_f in ("0","false","no"): landscape_trick = False

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
    app.run(host="0.0.0.0", port=5001, threaded=True, debug=False)
