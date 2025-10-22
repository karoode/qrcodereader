# qr.py — Visitor QR & Badge server + Google Forms webhook + Dashboard + Logo & Font upload
# التعديلات:
# - إرسال واتساب: يرسل صورة QR فقط (مو الباج)
# - زيادة حجم الخط إلى 64 وتفعيل استخدام خط مخصص عبر BADGE_FONT_PATH
# - صفحات رفع للّوغو والخط إلى الديسك (Render)

import os, io, sqlite3, string, secrets, base64, mimetypes, json
from datetime import datetime, date
from flask import Flask, request, jsonify, send_file, render_template_string, make_response, redirect, url_for
from PIL import Image, ImageDraw, ImageFont
import qrcode
import cv2
import numpy as np
from pyzbar import pyzbar

# اختياري: requests للبروكسي
try:
    import requests
except Exception:
    requests = None

APP_TITLE = "Visitor QR"
DB_PATH   = os.environ.get("QR_DB", "data/visitors.db").strip()
GF_SHARED_SECRET = os.environ.get("GF_SHARED_SECRET", "").strip()

# ---------- Badge & font config ----------
FONT_SIZE        = int(os.environ.get("BADGE_FONT_SIZE", "64"))  # <-- كبرنا الحجم إلى 64
LINE_SPACING     = 1.5
BADGE_W, BADGE_H = 1200, 1800
BADGE_DPI        = int(os.environ.get("BADGE_DPI", "600"))

# Logo
LOGO_PATH         = os.environ.get("BADGE_LOGO", "logo.png")
LOGO_CM           = float(os.environ.get("BADGE_LOGO_CM", "2.0"))
HOLE_SAFE_CM      = float(os.environ.get("BADGE_HOLE_SAFE_CM", "0.8"))
LOGO_SHIFT_UP_CM  = float(os.environ.get("BADGE_LOGO_SHIFT_UP_CM", "0.2"))
TEXT_TOP_GAP_CM   = float(os.environ.get("BADGE_TEXT_TOP_GAP_CM", "0.7"))

# QR على الباج
QR_CM_DEFAULT = float(os.environ.get("BADGE_QR_CM", "2.4"))
QR_CM         = QR_CM_DEFAULT
QR_BORDER     = int(os.environ.get("BADGE_QR_BORDER", "1"))

# واتساب بروكسي (يرسل صورة)
WA_PROXY_URL      = (os.environ.get("WA_PROXY_URL") or "").strip()

# مفاتيح آمنة مبسطة للرفع
LOGO_UPLOAD_KEY   = (os.environ.get("LOGO_UPLOAD_KEY") or "").strip()
FONT_UPLOAD_KEY   = (os.environ.get("FONT_UPLOAD_KEY") or LOGO_UPLOAD_KEY).strip()

# مسار الخط المخصص (ينصح: /app/data/timesbd.ttf على Render)
BADGE_FONT_PATH   = (os.environ.get("BADGE_FONT_PATH") or "").strip()

app = Flask(__name__)
app.config['MAX_CONTENT_LENGTH'] = 20 * 1024 * 1024  # 20MB

# ---------- DB ----------
def ensure_db():
    d = os.path.dirname(DB_PATH)
    if d: os.makedirs(d, exist_ok=True)
    with sqlite3.connect(DB_PATH) as con:
        con.execute("""
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

def rand_pin(): return f"{secrets.randbelow(10000):04d}"

# ---------- Fonts ----------
def load_times_bold(size=FONT_SIZE):
    # يفضّل الملف المخصص إذا موجود
    if BADGE_FONT_PATH and os.path.exists(BADGE_FONT_PATH):
        try: return ImageFont.truetype(BADGE_FONT_PATH, size=size)
        except Exception: pass
    # مسارات شائعة لـ Times New Roman Bold
    candidates = [
        "/Library/Fonts/Times New Roman Bold.ttf",
        "C:/Windows/Fonts/timesbd.ttf",
        "/usr/share/fonts/truetype/msttcorefonts/Times_New_Roman_Bold.ttf",
        "/usr/share/fonts/truetype/liberation/LiberationSerif-Bold.ttf",
        "/usr/share/fonts/truetype/dejavu/DejaVuSerif-Bold.ttf",
    ]
    for p in candidates:
        if os.path.exists(p):
            try: return ImageFont.truetype(p, size=size)
            except Exception: pass
    return ImageFont.load_default()

def text_size(draw, text, font):
    if not text: return (0,0)
    l,t,r,b = draw.textbbox((0,0), text, font=font)
    return r-l, b-t

def cm_to_px(cm, dpi=BADGE_DPI): return int(round((cm/2.54)*dpi))

def wrap_lines(draw, text, font, max_w):
    text = (text or "").strip()
    if not text: return [""]
    words = text.split()
    lines, cur = [], ""
    for w in words:
        cand = (cur+" "+w).strip()
        if text_size(draw, cand, font)[0] <= max_w or not cur:
            cur = cand
        else:
            lines.append(cur); cur = w
    if cur: lines.append(cur)
    return lines

# ---------- QR ----------
def build_qr_image(visitor_id, size_px):
    qr = qrcode.QRCode(version=None, error_correction=qrcode.constants.ERROR_CORRECT_M,
                       box_size=10, border=QR_BORDER)
    qr.add_data(visitor_id); qr.make(fit=True)
    img = qr.make_image(fill_color="black", back_color="white").convert("RGB")
    return img.resize((size_px, size_px), Image.NEAREST)

def make_qr_png(visitor_id, label_name=None, size=1024):
    qr_img = build_qr_image(visitor_id, size)
    if not label_name:
        b = io.BytesIO(); qr_img.save(b, "PNG"); b.seek(0); return b
    canvas = Image.new("RGB", (size, size+160), "white")
    canvas.paste(qr_img,(0,0))
    d = ImageDraw.Draw(canvas); f = load_times_bold(FONT_SIZE)
    tw, th = text_size(d, label_name, f)
    d.text(((size-tw)//2, size+(160-th)//2), label_name, font=f, fill=(15,15,15))
    b = io.BytesIO(); canvas.save(b,"PNG"); b.seek(0); return b

# ---------- Badge ----------
def compose_badge_portrait(name, company, position, visitor_id=None, w=BADGE_W, h=BADGE_H):
    img = Image.new("RGB",(w,h),(255,255,255))
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
            r = max(1e-6, logo.width/float(logo.height))
            if r>=1.0: nw,nh = logo_side, int(round(logo_side/r))
            else:      nh,nw = logo_side, int(round(logo_side*r))
            logo = logo.resize((nw,nh), Image.LANCZOS)
            lx = (w-nw)//2
            img.paste(logo,(lx,y_logo),logo)
            after_logo = y_logo+nh
        except Exception: pass

    side_pad      = cm_to_px(0.6)
    top_text      = after_logo + cm_to_px(TEXT_TOP_GAP_CM)
    max_text_w    = w - 2*side_pad
    gap_label_val = cm_to_px(0.6)
    labels        = ["Name:", "Company:", "Position:"]
    lw_max        = max(text_size(d,lbl,f_label)[0] for lbl in labels)
    value_x       = side_pad + lw_max + gap_label_val
    value_w       = max_text_w - lw_max - gap_label_val
    line_h        = int(round(LINE_SPACING * FONT_SIZE))

    def row(label, value, y_top):
        d.text((side_pad,y_top), label, font=f_label, fill=(10,10,10))
        for i,ln in enumerate(wrap_lines(d,value,f_label,value_w)):
            d.text((value_x, y_top+i*line_h), ln, font=f_label, fill=(10,10,10))
        return max(line_h, line_h*len(wrap_lines(d,value,f_label,value_w)))

    y = top_text
    y += row("Name:",     (name or ""),     y); y += cm_to_px(0.35)
    y += row("Company:",  (company or ""),  y); y += cm_to_px(0.35)
    y += row("Position:", (position or ""), y)

    if visitor_id:
        bottom_cm   = float(os.environ.get("BADGE_QR_BOTTOM_CM","0.8"))
        min_cm      = float(os.environ.get("BADGE_QR_MIN_CM","1.8"))
        max_cm      = float(os.environ.get("BADGE_QR_MAX_CM","4.0"))
        max_ratio   = float(os.environ.get("BADGE_QR_MAX_RATIO","0.28"))
        safety_cm   = float(os.environ.get("BADGE_QR_SAFETY_CM","1.6"))

        bottom_margin = cm_to_px(bottom_cm)
        safety_gap    = cm_to_px(safety_cm)
        qr_nominal    = cm_to_px(QR_CM)
        qr_min        = cm_to_px(min_cm)
        qr_max        = min(int(w*max_ratio), cm_to_px(max_cm))
        qr_side       = max(qr_min, min(qr_nominal, qr_max))
        available_h   = h - bottom_margin - (y + safety_gap)
        qr_side       = qr_min if available_h < qr_min else min(qr_side, available_h)

        qx = (w-qr_side)//2
        qy = h - bottom_margin - qr_side
        img.paste(build_qr_image(visitor_id, qr_side),(qx,qy))

    return img

def make_badge_png(name, company, position, visitor_id=None, rotate_ccw=False):
    img = compose_badge_portrait(name, company, position, visitor_id)
    if rotate_ccw: img = img.transpose(Image.ROTATE_90)
    b = io.BytesIO(); img.save(b,"PNG"); b.seek(0); return b

# ---------- Decode ----------
def decode_pyzbar(img_bgr):
    gray = cv2.cvtColor(img_bgr, cv2.COLOR_BGR2GRAY)
    res  = pyzbar.decode(gray)
    if not res: return "", []
    b = res[0]
    txt = b.data.decode("utf-8", errors="replace")
    if b.polygon and len(b.polygon)>=4:
        poly = [[float(p.x), float(p.y)] for p in b.polygon]
    else:
        x,y,w,h = b.rect; poly = [[x,y],[x+w,y],[x+w,y+h],[x,y+h]]
    return txt, poly

def preprocess_contrast(img): return cv2.convertScaleAbs(img, alpha=1.35, beta=8)

def robust_decode(img):
    h,w = img.shape[:2]
    if max(w,h)<800:
        s = 800.0/max(w,h); img = cv2.resize(img,None,fx=s,fy=s,interpolation=cv2.INTER_CUBIC)
    elif max(w,h)>2000:
        s = 2000.0/max(w,h); img = cv2.resize(img,None,fx=s,fy=s,interpolation=cv2.INTER_AREA)
    t,p = decode_pyzbar(img)
    if t: return t,p
    boosted = preprocess_contrast(img)
    t,p = decode_pyzbar(boosted)
    if t: return t,p
    for k in [cv2.ROTATE_90_CLOCKWISE, cv2.ROTATE_180, cv2.ROTATE_90_COUNTERCLOCKWISE]:
        t,p = decode_pyzbar(cv2.rotate(boosted,k))
        if t: return t,p
    g = cv2.cvtColor(boosted, cv2.COLOR_BGR2GRAY)
    g = cv2.medianBlur(g,3)
    thr = cv2.adaptiveThreshold(g,255,cv2.ADAPTIVE_THRESH_GAUSSIAN_C,cv2.THRESH_BINARY,31,5)
    t,p = decode_pyzbar(cv2.cvtColor(thr,cv2.COLOR_GRAY2BGR))
    if t: return t,p
    inv = cv2.bitwise_not(thr)
    t,p = decode_pyzbar(cv2.cvtColor(inv,cv2.COLOR_GRAY2BGR))
    if t: return t,p
    return "", []

# ---------- Dashboard ----------
INDEX_HTML = """
<!doctype html><html lang="ar" dir="rtl"><head><meta charset="utf-8"/>
<title>{{title}}</title><meta name="viewport" content="width=device-width, initial-scale=1"/>
<style>
:root{ --bg:#ffffff; --fg:#0f141a; --muted:#6b7280; --border:#e5e7eb; --accent:#111827;}
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
</style></head><body>
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
function activate(btn){ const p = btn.parentElement.querySelectorAll('.btn'); p.forEach(b=>b.classList.remove('active')); btn.classList.add('active'); }
function showQR(id, btn){  const img=document.getElementById('img_'+id); img.src=img.dataset.qr;    activate(btn); }
function showBadge(id, btn){const img=document.getElementById('img_'+id); img.src=img.dataset.badge; activate(btn); }
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

def pick(d,*keys):
    for k in keys:
        if k and k in d and d[k]: return d[k]
    return ""

def _ensure_dir(path):
    d = os.path.dirname(path)
    if d: os.makedirs(d, exist_ok=True)

# ---------- Routes ----------
@app.route("/")
def index():
    ensure_db()
    with sqlite3.connect(DB_PATH) as con:
        con.row_factory = sqlite3.Row
        cur = con.cursor()
        cur.execute("SELECT COUNT(*) AS c FROM visitors"); total = cur.fetchone()["c"]
        cur.execute("SELECT COUNT(*) AS c FROM visitors WHERE DATE(substr(created_at,1,10)) = DATE(?)",(date.today().isoformat(),))
        today = cur.fetchone()["c"]
        try:
            cur.execute("SELECT COUNT(*) AS c FROM visitors WHERE wa_sent=1"); wa = cur.fetchone()["c"]
        except Exception: wa = 0
        cur.execute("SELECT id,name,company,position,created_at FROM visitors ORDER BY datetime(created_at) DESC LIMIT 12")
        last = [dict(r) for r in cur.fetchall()]
    return render_template_string(INDEX_HTML, title=APP_TITLE, kpis=dict(total=total,today=today,wa=wa), last=last)

@app.route("/ui_logo")
def ui_logo():
    if not os.path.exists(LOGO_PATH):
        img = Image.new("RGB",(160,40),"white")
        b = io.BytesIO(); img.save(b,"PNG"); b.seek(0); return send_file(b, mimetype="image/png")
    return send_file(LOGO_PATH, mimetype=(mimetypes.guess_type(LOGO_PATH)[0] or "image/png"))

# --- Logo upload
@app.route("/logo/form")
def logo_form():
    if LOGO_UPLOAD_KEY and (request.args.get("key") or "") != LOGO_UPLOAD_KEY: return "Forbidden", 403
    return f"""<!doctype html><meta charset="utf-8"/>
<h3>Upload Badge Logo → {LOGO_PATH}</h3>
<form method="POST" action="/logo/upload?key={LOGO_UPLOAD_KEY}" enctype="multipart/form-data">
  <input type="file" name="file" accept="image/*" required/>
  <button type="submit">Upload</button>
</form>
<p>Current: <img src="/ui_logo?ts={int(datetime.utcnow().timestamp())}" style="max-height:80px"/></p>"""

@app.route("/logo/upload", methods=["POST"])
def logo_upload():
    if LOGO_UPLOAD_KEY and (request.args.get("key") or "") != LOGO_UPLOAD_KEY: return "Forbidden", 403
    if "file" not in request.files: return "No file", 400
    f = request.files["file"]; _ensure_dir(LOGO_PATH); f.save(LOGO_PATH)
    return redirect(url_for("logo_form", key=LOGO_UPLOAD_KEY))

# --- Font upload (يحفظ في BADGE_FONT_PATH)
@app.route("/font/form")
def font_form():
    if FONT_UPLOAD_KEY and (request.args.get("key") or "") != FONT_UPLOAD_KEY: return "Forbidden", 403
    target = BADGE_FONT_PATH or "/app/data/timesbd.ttf"
    return f"""<!doctype html><meta charset="utf-8"/>
<h3>Upload Badge Font (TTF/OTF) → {target}</h3>
<form method="POST" action="/font/upload?key={FONT_UPLOAD_KEY}" enctype="multipart/form-data">
  <input type="file" name="file" accept=".ttf,.otf,font/ttf,font/otf" required/>
  <button type="submit">Upload</button>
</form>
<p>Set env BADGE_FONT_PATH to: {target}</p>"""

@app.route("/font/upload", methods=["POST"])
def font_upload():
    if FONT_UPLOAD_KEY and (request.args.get("key") or "") != FONT_UPLOAD_KEY: return "Forbidden", 403
    target = BADGE_FONT_PATH or "/app/data/timesbd.ttf"
    if "file" not in request.files: return "No file", 400
    f = request.files["file"]; _ensure_dir(target); f.save(target)
    return f"Saved font to {target}. Set BADGE_FONT_PATH and redeploy."

# --- Simple create
@app.route("/create", methods=["POST"])
def create():
    ensure_db()
    f = request.form
    name     = (f.get("name") or "").strip()
    company  = (f.get("company") or "").strip()
    position = (f.get("position") or "").strip()
    email    = (f.get("email") or "").strip().lower()
    phone    = (f.get("phone") or "").strip()
    if not all([name,company,position,email,phone]): return jsonify(ok=False,error="missing_fields"),400
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
                        (vid,name,company,position,email,phone,pin,datetime.utcnow().isoformat()))
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
    if (request.args.get("dl") or "").lower() in ("1","true","yes","download"):
        try: return send_file(bio, mimetype="image/png", as_attachment=True, download_name=f"{vid}.png")
        except TypeError: return send_file(bio, mimetype="image/png", as_attachment=True, attachment_filename=f"{vid}.png")
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
    return send_file(make_badge_png(row["name"],row["company"],row["position"],visitor_id=vid,rotate_ccw=False),
                     mimetype="image/png")

@app.route("/card_landscape/<vid>.png")
def card_landscape_png(vid):
    ensure_db()
    with sqlite3.connect(DB_PATH) as con:
        con.row_factory = sqlite3.Row
        cur = con.cursor()
        cur.execute("SELECT name,company,position FROM visitors WHERE id=?", (vid,))
        row = cur.fetchone()
        if not row: return "Not found", 404
    return send_file(make_badge_png(row["name"],row["company"],row["position"],visitor_id=vid,rotate_ccw=True),
                     mimetype="image/png")
# --- Lookup visitor by ID (JSON)
@app.route("/lookup/<vid>.json")
def lookup_json(vid):
    ensure_db()
    with sqlite3.connect(DB_PATH) as con:
        con.row_factory = sqlite3.Row
        cur = con.cursor()
        cur.execute("SELECT id,name,company,position FROM visitors WHERE id=?", (vid,))
        row = cur.fetchone()
        if not row:
            return jsonify(ok=False, error="not_found"), 404
        rec = dict(row)
    return jsonify(ok=True, **rec)

# ---------- Google Forms webhook ----------
@app.route("/forms/google", methods=["POST","OPTIONS"])
def forms_google():
    if request.method == "OPTIONS": return corsify(make_response(("",204)))

    incoming_secret = (request.headers.get("X-Secret") or
                       (request.form.get("secret") if request.form else "") or
                       (request.json.get("secret") if request.is_json and request.json else "") or "").strip()
    if GF_SHARED_SECRET and incoming_secret != GF_SHARED_SECRET:
        return corsify(jsonify(ok=False, error="unauthorized")), 401

    if request.is_json:
        data = request.get_json(silent=True) or {}
        if isinstance(data.get("namedValues"), dict):
            nv = data["namedValues"]
            def nv_get(k):
                v = nv.get(k) or nv.get(k.strip()) or []
                return v[0] if isinstance(v,list) and v else (v if isinstance(v,str) else "")
            data = {
                "name":     nv_get("Name")     or nv_get("الاسم"),
                "company":  nv_get("Company")  or nv_get("الشركة"),
                "position": nv_get("Position") or nv_get("المنصب"),
                "email":    nv_get("Email")    or nv_get("البريد"),
                "phone":    nv_get("Phone")    or nv_get("الهاتف"),
            }
    else:
        data = request.form.to_dict(flat=True)

    name     = (pick(data,"name","Name","الاسم","Full Name") or "").strip()
    company  = (pick(data,"company","Company","الشركة") or "").strip()
    position = (pick(data,"position","Position","المنصب") or "").strip()
    email    = (pick(data,"email","Email","البريد") or "").strip().lower()
    phone    = (pick(data,"phone","Phone","الهاتف") or "").strip()

    if not all([name,company,position,email,phone]):
        return corsify(jsonify(ok=False, error="missing_fields",
                               need=["name","company","position","email","phone"])),400

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
                        (vid,name,company,position,email,phone,pin,datetime.utcnow().isoformat()))
            con.commit()
            rec = dict(id=vid, name=name, company=company, position=position,
                       email=email, phone=phone, pin=pin, created_at=datetime.utcnow().isoformat())

    # توليد الصور
    qr_buf   = make_qr_png(rec["id"], label_name=rec["name"])
    # card_buf = make_badge_png(...)  # لم نعد نرسله لواتساب

    # إرسال واتساب: QR فقط
    wa_result = None
    if WA_PROXY_URL and requests is not None:
        try:
            files = {"file": ("qr.png", qr_buf.getvalue(), "image/png")}  # <-- نرسل QR
            data  = {"to": rec["phone"], "name": rec["name"]}
            r = requests.post(WA_PROXY_URL, files=files, data=data, timeout=60)
            if r.ok:
                wa_result = {"ok": True}
                with sqlite3.connect(DB_PATH) as con2:
                    con2.execute("UPDATE visitors SET wa_sent=1, wa_ts=? WHERE id=?",
                                 (datetime.utcnow().isoformat(), rec["id"]))
                    con2.commit()
            else:
                wa_result = {"ok": False, "code": r.status_code, "text": r.text}
        except Exception as e:
            wa_result = {"ok": False, "error": str(e)}

    base = request.host_url.rstrip("/")
    out = dict(
        ok=True, id=rec["id"], name=rec["name"],
        qr=f"{base}/qr/{rec['id']}.png",
        qr_download=f"{base}/qr/{rec['id']}.png?dl=1",
        card_portrait=f"{base}/card/{rec['id']}.png",
        card_landscape=f"{base}/card_landscape/{rec['id']}.png"
    )
    if wa_result is not None: out["whatsapp"] = {"mode":"proxy", **wa_result}
    return corsify(jsonify(out))

# ---- Decode endpoints تبقى كما هي ----
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
    lt_q = (request.args.get("landscape_trick") or "").lower()
    lt_f = (request.form.get("landscape_trick") or "").lower()
    landscape_trick = not (lt_q in ("0","false","no") or lt_f in ("0","false","no"))
    img=None
    for field in ("image","file","photo","frame","upload"):
        if field in request.files:
            arr=np.frombuffer(request.files[field].read(),np.uint8)
            img=cv2.imdecode(arr,cv2.IMREAD_COLOR); break
    if img is None and request.data and request.content_type and ("image/" in request.content_type or "application/octet-stream" in request.content_type):
        arr=np.frombuffer(request.data,np.uint8); img=cv2.imdecode(arr,cv2.IMREAD_COLOR)
    if img is None and request.is_json:
        data=request.get_json(silent=True) or {}; b64=(data.get("image_b64") or data.get("image") or "").strip()
        if b64:
            if "," in b64: b64=b64.split(",",1)[-1]
            try:
                arr=np.frombuffer(base64.b64decode(b64),np.uint8); img=cv2.imdecode(arr,cv2.IMREAD_COLOR)
            except Exception: img=None
    if img is None: return jsonify(ok=False,error="no_image_supplied"),400
    vid,_=robust_decode(img)
    if not vid: return jsonify(ok=False,error="decode_failed"),404
    with sqlite3.connect(DB_PATH) as con:
        con.row_factory=sqlite3.Row; cur=con.cursor()
        cur.execute("SELECT name,company,position FROM visitors WHERE id=?", (vid,))
        row=cur.fetchone()
    if not row: return jsonify(ok=False,error="unknown_visitor",vid=vid),404
    return send_file(make_badge_png(row["name"],row["company"],row["position"],visitor_id=vid,rotate_ccw=landscape_trick),
                     mimetype="image/png")

@app.route("/decode", methods=["POST"])
def decode_json():
    img=None
    if "image" in request.files:
        arr=np.frombuffer(request.files["image"].read(),np.uint8); img=cv2.imdecode(arr,cv2.IMREAD_COLOR)
    elif request.data and request.content_type and ("image/" in request.content_type or "application/octet-stream" in request.content_type):
        arr=np.frombuffer(request.data,np.uint8); img=cv2.imdecode(arr,cv2.IMREAD_COLOR)
    elif request.is_json:
        data=request.get_json(silent=True) or {}; b64=(data.get("image_b64") or "").strip()
        if "," in b64: b64=b64.split(",",1)[-1]
        if b64:
            try: arr=np.frombuffer(base64.b64decode(b64),np.uint8); img=cv2.imdecode(arr,cv2.IMREAD_COLOR)
            except Exception: img=None
    if img is None: return jsonify(ok=False,error="no_image_supplied"),400
    text,poly=robust_decode(img); return jsonify(ok=bool(text), text=text or "", poly=poly or [])

# ---------- Main ----------
if __name__ == "__main__":
    ensure_db()
    app.run(host="0.0.0.0", port=5001, threaded=True, debug=False)
