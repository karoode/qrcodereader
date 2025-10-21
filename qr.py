# qr.py — Visitor QR & Badge server + Google Forms webhook
# تصميم البطاقة: Times New Roman Bold 48، التفاف نص ومحاذاة يسار، شعار بالأعلى، QR أسفل بالوسط.
# WhatsApp:
#   - Proxy Mode: يرسل PNG لسيرفر واتساب خارجي (WA_PROXY_URL=/send-image).
#   - Direct Mode (اختياري): يرفع للـGraph ويرسل تمبلت بصورة Header.

import os, io, sqlite3, string, secrets, base64, mimetypes, json
from datetime import datetime
from flask import Flask, request, jsonify, send_file, render_template_string, make_response
from PIL import Image, ImageDraw, ImageFont
import qrcode
import cv2
import numpy as np
from pyzbar import pyzbar
import requests

APP_TITLE = "Visitor QR"
DB_PATH   = os.environ.get("QR_DB", "data/visitors.db").strip()
GF_SHARED_SECRET = os.environ.get("GF_SHARED_SECRET", "").strip()

# WhatsApp — وضع البروكسي (سيرفرك الحالي)
WA_PROXY_URL     = os.environ.get("WA_PROXY_URL", "").strip()

# WhatsApp — الوضع المباشر (اختياري)
WHATSAPP_ENABLED = os.environ.get("WHATSAPP_ENABLED", "0").strip().lower() in ("1","true","yes")
WHATSAPP_TOKEN   = os.environ.get("WHATSAPP_TOKEN", "").strip()
PHONE_NUMBER_ID  = os.environ.get("PHONE_NUMBER_ID", "").strip()
GRAPH_VERSION    = os.environ.get("GRAPH_VERSION", "v21.0").strip()
TEMPLATE_NAME    = os.environ.get("TEMPLATE_NAME", "send_photo").strip()
TEMPLATE_LANG    = os.environ.get("TEMPLATE_LANG", "en").strip()

# ---------- Badge & font config ----------
FONT_SIZE        = 48
LINE_SPACING     = 1.5
BADGE_W, BADGE_H = 1200, 1800
BADGE_DPI        = int(os.environ.get("BADGE_DPI", "600"))

# Logo
LOGO_PATH     = os.environ.get("BADGE_LOGO", "logo.png")
LOGO_CM       = float(os.environ.get("BADGE_LOGO_CM", "2.0"))
HOLE_SAFE_CM  = float(os.environ.get("BADGE_HOLE_SAFE_CM", "0.8"))
LOGO_SHIFT_UP_CM   = float(os.environ.get("BADGE_LOGO_SHIFT_UP_CM", "0.2"))
TEXT_TOP_GAP_CM    = float(os.environ.get("BADGE_TEXT_TOP_GAP_CM", "0.7"))

# QR on badge (bottom-centered, visitor_id only)
QR_CM_DEFAULT = float(os.environ.get("BADGE_QR_CM", "2.4"))
QR_CM        = QR_CM_DEFAULT
QR_BORDER    = int(os.environ.get("BADGE_QR_BORDER", "1"))

app = Flask(__name__)
app.config['MAX_CONTENT_LENGTH'] = 20 * 1024 * 1024  # 20MB uploads

# ---------- DB ----------
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
              created_at TEXT NOT NULL
            )
        """)
        cur.execute("""
            CREATE TABLE IF NOT EXISTS wa_logs(
              id INTEGER PRIMARY KEY AUTOINCREMENT,
              ts TEXT NOT NULL,
              vid TEXT NOT NULL,
              phone TEXT NOT NULL,
              mode TEXT NOT NULL,
              ok INTEGER NOT NULL,
              resp TEXT
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
    if not text: return [""]
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
        b = io.BytesIO(); qr_img.save(b, "PNG"); b.seek(0); return b
    canvas = Image.new("RGB", (size, size + 160), "white")
    canvas.paste(qr_img, (0,0))
    d = ImageDraw.Draw(canvas); f = load_times_bold(FONT_SIZE)
    tw, th = text_size(d, label_name, f)
    d.text(((size - tw)//2, size + (160 - th)//2), label_name, font=f, fill=(15,15,15))
    b = io.BytesIO(); canvas.save(b, "PNG"); b.seek(0); return b

# ---------- Badge compose (تصميم قديم) ----------
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
        qr_side    = max(qr_min, min(qr_nominal, qr_max))

        available_h = h - bottom_margin - (y + safety_gap)
        qr_side     = min(qr_side, max(qr_min, available_h))

        qx = (w - qr_side) // 2
        qy = h - bottom_margin - qr_side
        img.paste(build_qr_image(visitor_id, qr_side), (qx, qy))

    return img

def make_badge_png(name, company, position, visitor_id=None, rotate_ccw=False):
    img = compose_badge_portrait(name, company, position, visitor_id=visitor_id)
    if rotate_ccw:
        img = img.transpose(Image.ROTATE_90)
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

# ---------- WhatsApp senders ----------
def wa_log(vid, phone, mode, ok, resp):
    try:
        with sqlite3.connect(DB_PATH) as con:
            cur = con.cursor()
            cur.execute("INSERT INTO wa_logs(ts, vid, phone, mode, ok, resp) VALUES (?,?,?,?,?,?)",
                        (datetime.utcnow().isoformat(timespec="seconds"), vid, phone, mode, 1 if ok else 0, (resp or "")[:2000]))
            con.commit()
    except Exception:
        pass

def send_via_proxy(phone: str, name: str, png_bytes: bytes, filename: str):
    if not WA_PROXY_URL:
        return False, "WA_PROXY_URL not set"
    files = {"file": (filename, io.BytesIO(png_bytes), "image/png")}
    data  = {"to": phone, "name": name}
    try:
        r = requests.post(WA_PROXY_URL, files=files, data=data, timeout=60)
        ok = 200 <= r.status_code < 300
        return ok, r.text
    except Exception as ex:
        return False, str(ex)

def upload_media_direct(png_bytes: bytes, filename: str):
    url = f"https://graph.facebook.com/{GRAPH_VERSION}/{PHONE_NUMBER_ID}/media"
    headers = {"Authorization": f"Bearer {WHATSAPP_TOKEN}"}
    files = {"file": (filename, io.BytesIO(png_bytes), "image/png")}
    data  = {"messaging_product": "whatsapp"}
    r = requests.post(url, headers=headers, files=files, data=data, timeout=60)
    r.raise_for_status()
    return r.json()["id"]

def send_template_direct(phone: str, media_id: str, name_param: str):
    url = f"https://graph.facebook.com/{GRAPH_VERSION}/{PHONE_NUMBER_ID}/messages"
    headers = {"Authorization": f"Bearer {WHATSAPP_TOKEN}", "Content-Type": "application/json"}
    payload = {
        "messaging_product": "whatsapp",
        "to": phone,
        "type": "template",
        "template": {
            "name": TEMPLATE_NAME,
            "language": {"code": TEMPLATE_LANG},
            "components": [
                {"type":"header","parameters":[{"type":"image","image":{"id": media_id}}]},
                {"type":"body","parameters":[{"type":"text","text": name_param or "User"}]}
            ]
        }
    }
    r = requests.post(url, headers=headers, json=payload, timeout=60)
    r.raise_for_status()
    return r.text

# ---------- HTML (Stats) ----------
INDEX_HTML = """
<!doctype html><html><head><meta charset="utf-8"/>
<title>{{title}}</title>
<style>
  :root{ --bg:#ffffff; --fg:#0f141a; --muted:#6b7280; --border:#e5e7eb; --card:#ffffff; }
  *{box-sizing:border-box}
  body{background:var(--bg);color:var(--fg);font-family:system-ui,-apple-system,Segoe UI,Roboto,Ubuntu,"Helvetica Neue",Arial;margin:0;}
  .wrap{max-width:1200px;margin:32px auto;padding:0 16px;}
  h2{margin:0 0 12px 0}
  .stats{display:flex;gap:16px;flex-wrap:wrap;margin-bottom:20px}
  .kpi{background:var(--card);border:1px solid var(--border);border-radius:12px;padding:14px 16px;min-width:220px}
  .kpi .label{color:var(--muted);font-size:13px}
  .kpi .value{font-weight:800;font-size:28px}
  .grid{display:grid;grid-template-columns:repeat(auto-fill,minmax(180px,1fr));gap:16px}
  .item{border:1px solid var(--border);border-radius:12px;background:#fff;padding:10px;display:flex;flex-direction:column;gap:8px}
  .item img{width:100%;height:auto;border-radius:8px;border:1px solid var(--border);background:#fff}
  .name{font-weight:700;font-size:14px}
  .muted{color:var(--muted);font-size:12px}
</style></head>
<body>
  <div class="wrap">
    <h2>{{title}}</h2>
    <div class="stats">
      <div class="kpi"><div class="label">إجمالي الطلبات</div><div class="value">{{tot}}</div></div>
      <div class="kpi"><div class="label">طلبات اليوم</div><div class="value">{{today}}</div></div>
      <div class="kpi"><div class="label">إرسال واتساب</div><div class="value">{{wa}}</div></div>
    </div>
    <div class="grid">
      {% for r in recs %}
      <div class="item">
        <img src="/qr/{{r['id']}}.png" alt="qr"/>
        <div class="name">{{r['name']}}</div>
        <div class="muted">{{r['company']}} — {{r['created_at'][:16].replace('T',' ')}}</div>
      </div>
      {% endfor %}
    </div>
  </div>
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

def today_ymd():
    return datetime.utcnow().strftime("%Y-%m-%d")

# ---------- Routes ----------
@app.route("/")
def index():
    ensure_db()
    with sqlite3.connect(DB_PATH) as con:
        con.row_factory = sqlite3.Row
        cur = con.cursor()
        cur.execute("SELECT COUNT(*) FROM visitors")
        tot = cur.fetchone()[0]
        cur.execute("SELECT COUNT(*) FROM visitors WHERE substr(created_at,1,10)=?", (today_ymd(),))
        today = cur.fetchone()[0]
        cur.execute("SELECT COALESCE(SUM(ok),0) FROM wa_logs")
        wa = cur.fetchone()[0]
        cur.execute("SELECT id,name,company,created_at FROM visitors ORDER BY created_at DESC LIMIT 24")
        recs = [dict(r) for r in cur.fetchall()]
    return render_template_string(INDEX_HTML, title=APP_TITLE, tot=tot, today=today, wa=wa, recs=recs)

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
            return send_file(bio, mimetype="image/png", as_attachment=True,
                             attachment_filename=f"{vid}.png")
    return send_file(bio, mimetype="image/png")

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

    # قراءة البيانات
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

    base = request.host_url.rstrip("/")
    qr_url = f"{base}/qr/{rec['id']}.png"
    dl_url = f"{qr_url}?dl=1"
    card   = f"{base}/card/{rec['id']}.png"
    card_l = f"{base}/card_landscape/{rec['id']}.png"

    # --- جهّز صورة الـQR كـPNG بايتس ---
    qr_buf = make_qr_png(rec["id"], label_name=rec["name"])
    png_bytes = qr_buf.getvalue()
    filename  = f"{rec['id']}.png"

    # --- إرسال واتساب ---
    wa = None
    if WA_PROXY_URL:  # Proxy Mode (سيرفرك الحالي)
        ok, resp = send_via_proxy(phone, rec["name"], png_bytes, filename)
        wa_log(rec["id"], phone, "proxy", ok, resp)
        wa = {"mode":"proxy","ok":ok,"resp":resp[:300]}
    elif WHATSAPP_ENABLED and WHATSAPP_TOKEN and PHONE_NUMBER_ID:  # Direct Mode (اختياري)
        try:
            media_id = upload_media_direct(png_bytes, filename)
            resp     = send_template_direct(phone, media_id, rec["name"])
            wa_log(rec["id"], phone, "direct", True, resp)
            wa = {"mode":"direct","ok":True}
        except Exception as ex:
            wa_log(rec["id"], phone, "direct", False, str(ex))
            wa = {"mode":"direct","ok":False,"error":str(ex)}

    return corsify(jsonify(
        ok=True, id=rec["id"], name=rec["name"],
        qr=qr_url, qr_download=dl_url,
        card_portrait=card, card_landscape=card_l,
        whatsapp=wa
    ))

# ---------- Cards ----------
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

@app.route("/health")
def health():
    return jsonify(ok=True, msg="up", host=request.host), 200

# ---------- Main ----------
if __name__ == "__main__":
    ensure_db()
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT","5001")), threaded=True, debug=False)
