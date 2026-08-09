# -*- coding: utf-8 -*-
from flask import Flask,request,session,redirect,jsonify,Response
import psycopg2,psycopg2.extras,os,hashlib,datetime,urllib.request,re,html as _html,pathlib,json as _json,time,secrets
from contextlib import contextmanager
from cryptography.fernet import Fernet
# ── Security Scanner ──────────────────────────────────────────────────────────
import ssl,socket,threading,urllib.parse,ipaddress
from bs4 import BeautifulSoup
try:
    from anthropic import Anthropic as _Anthropic
    _anthropic_client=_Anthropic()
except Exception: _anthropic_client=None

_BASE=pathlib.Path(__file__).parent.resolve()
app=Flask(__name__)

def _require_env(name):
    """Read a secret from the environment only. Never fall back to a
    hardcoded value baked into source, since source can leak (repos,
    logs, chat transcripts, etc)."""
    val=os.environ.get(name,"")
    if not val:
        raise RuntimeError(f"{name} environment variable is not set. Refusing to start with an insecure default.")
    return val

app.secret_key=_require_env("SECRET_KEY")
app.config['PERMANENT_SESSION_LIFETIME']=datetime.timedelta(days=90)
app.config['SESSION_PERMANENT']=True
app.config['SESSION_COOKIE_SAMESITE']='Lax'
app.config['SESSION_COOKIE_HTTPONLY']=True
app.config['SESSION_COOKIE_SECURE']=os.environ.get("FLASK_ENV","production")!="development"

def get_database_url():
    url=os.environ.get("DATABASE_URL","")
    if not url: raise RuntimeError("DATABASE_URL not set.")
    if url.startswith("postgres://"): url="postgresql://"+url[len("postgres://"):]
    if "sslmode" not in url: url+=("&" if "?" in url else "?")+"sslmode=require"
    return url

DATABASE_URL=get_database_url()
ADMIN_USER="Eagleone"
_KEY_FILE=str(_BASE/"secret.key")
if not os.path.exists(_KEY_FILE): open(_KEY_FILE,"wb").write(Fernet.generate_key())
fernet=Fernet(open(_KEY_FILE,"rb").read())
VAPID_PUBLIC_KEY=_require_env("VAPID_PUBLIC_KEY")
VAPID_PRIVATE_KEY=_require_env("VAPID_PRIVATE_KEY")
VAPID_CLAIMS={"sub":"mailto:admin@voxpopuli.app"}

# ── Password hashing ─────────────────────────────────────────────────────────
# Salted (via werkzeug's pbkdf2/scrypt) instead of bare sha256, which is fast
# to brute-force with rainbow tables at scale. Legacy sha256 hashes already in
# the DB are still verified correctly and silently upgraded on next login.
def hash_pw(pw):
    from werkzeug.security import generate_password_hash
    return generate_password_hash(pw)

def verify_pw(stored_hash,pw):
    from werkzeug.security import check_password_hash
    if not stored_hash: return False
    if stored_hash.startswith(("pbkdf2:","scrypt:")):
        return check_password_hash(stored_hash,pw)
    return len(stored_hash)==64 and secrets.compare_digest(stored_hash,hashlib.sha256(pw.encode()).hexdigest())

def _migrate_pw_if_legacy(username,stored_hash,pw):
    if stored_hash and not stored_hash.startswith(("pbkdf2:","scrypt:")):
        try:
            with db() as con: execute(con,"UPDATE users SET password_hash=%s WHERE username=%s",(hash_pw(pw),username))
        except Exception: pass

get_ip=lambda:request.headers.get("X-Forwarded-For",request.remote_addr).split(",")[0].strip()
logged_in=lambda:"username" in session
me=lambda:session.get("username","")
ok=lambda **kw:jsonify({"ok":True,**kw})
err=lambda e:jsonify({"ok":False,"error":e})
utc_now=lambda:datetime.datetime.utcnow().isoformat()
utc_cutoff=lambda minutes=2:(datetime.datetime.utcnow()-datetime.timedelta(minutes=minutes)).isoformat()

THEMES={
    "green":{"p":"#00ff00","bg":"#000","ac":"#003300","name":"MATRIX"},
    "cyan":{"p":"#00ffff","bg":"#000a0a","ac":"#003333","name":"OCEAN"},
    "amber":{"p":"#ffb300","bg":"#0a0500","ac":"#332200","name":"AMBER"},
    "red":{"p":"#ff2222","bg":"#0a0000","ac":"#330000","name":"ALERT"},
    "purple":{"p":"#cc44ff","bg":"#050010","ac":"#220033","name":"NEXUS"},
    "white":{"p":"#4488ff","bg":"#000814","ac":"#001a3a","name":"GHOST"},
}

# ── Database ────────────────────────────────────────────────────────────────
@contextmanager
def db():
    last_exc=None;con=None
    for attempt in range(5):
        try:
            con=psycopg2.connect(DATABASE_URL,connect_timeout=10);con.autocommit=False;break
        except psycopg2.OperationalError as exc:
            last_exc=exc;wait=2**attempt
            app.logger.warning(f"DB connect attempt {attempt+1} failed, retrying in {wait}s: {exc}");time.sleep(wait)
    else: raise last_exc
    try: yield con;con.commit()
    except Exception: con.rollback();raise
    finally: con.close()

def execute(con,sql,params=None):
    cur=con.cursor();cur.execute(sql,params or ());return cur
def fetchall(con,sql,params=None):
    cur=execute(con,sql,params);return cur.fetchall()
def fetchone(con,sql,params=None):
    cur=execute(con,sql,params);return cur.fetchone()

_TABLES=[
    "CREATE TABLE IF NOT EXISTS users(id SERIAL PRIMARY KEY,username TEXT UNIQUE NOT NULL,password_hash TEXT NOT NULL,theme TEXT DEFAULT 'green',is_admin INTEGER DEFAULT 0,created_at TEXT DEFAULT CURRENT_TIMESTAMP)",
    "CREATE TABLE IF NOT EXISTS visits(id SERIAL PRIMARY KEY,date TEXT NOT NULL,ip TEXT NOT NULL,UNIQUE(date,ip))",
    "CREATE TABLE IF NOT EXISTS active_users(ip TEXT PRIMARY KEY,last_seen TEXT NOT NULL)",
    "CREATE TABLE IF NOT EXISTS user_sessions(username TEXT PRIMARY KEY,last_seen TEXT NOT NULL)",
    "CREATE TABLE IF NOT EXISTS push_subscriptions(username TEXT NOT NULL,endpoint TEXT NOT NULL,p256dh TEXT NOT NULL,auth TEXT NOT NULL,PRIMARY KEY(username,endpoint))",
    "CREATE TABLE IF NOT EXISTS password_resets(id SERIAL PRIMARY KEY,username TEXT NOT NULL,temp_password TEXT,status TEXT DEFAULT 'pending',requested_at TEXT DEFAULT CURRENT_TIMESTAMP)",
    "CREATE TABLE IF NOT EXISTS scan_links(id SERIAL PRIMARY KEY,url TEXT UNIQUE NOT NULL,added_by TEXT,created_at TEXT DEFAULT CURRENT_TIMESTAMP)",
    "CREATE TABLE IF NOT EXISTS security_events(id SERIAL PRIMARY KEY,ip TEXT NOT NULL,event_type TEXT NOT NULL,path TEXT,detail TEXT,created_at TEXT DEFAULT CURRENT_TIMESTAMP)",
    "CREATE TABLE IF NOT EXISTS ip_geo_cache(ip TEXT PRIMARY KEY,lat DOUBLE PRECISION,lon DOUBLE PRECISION,city TEXT,region TEXT,country TEXT,fetched_at TEXT)",
    "CREATE INDEX IF NOT EXISTS idx_security_events_created ON security_events(created_at)",
    "CREATE INDEX IF NOT EXISTS idx_security_events_ip ON security_events(ip)",
]

def _do_init_db():
    with db() as con:
        cur=con.cursor()
        for sql in _TABLES: cur.execute(sql)
        cur.execute("UPDATE users SET is_admin=1 WHERE username=%s",(ADMIN_USER,))

def init_db():
    for attempt in range(5):
        try: _do_init_db();return
        except Exception as exc:
            wait=3**attempt
            app.logger.warning(f"init_db attempt {attempt+1} failed, retrying in {wait}s: {exc}");time.sleep(wait)
    raise RuntimeError("Could not initialise database after 5 attempts.")
init_db()

# ── Auth helpers ────────────────────────────────────────────────────────────
def is_admin(u=None):
    u=u or me()
    if not u: return False
    if u==ADMIN_USER: return True
    with db() as con: row=fetchone(con,"SELECT is_admin FROM users WHERE username=%s",(u,))
    return bool(row and row[0])
def require_login():
    if not logged_in(): return err("NOT LOGGED IN")
def require_admin():
    if not is_admin():
        log_security_event(get_ip(),"admin_probe",path=request.path,detail=f"user={me() or 'anon'}")
        return err("FORBIDDEN")

# ── Security event logging + IP geolocation (for the hazard map) ────────────
_PRIVATE_IP_PREFIXES=("10.","127.","172.16.","172.17.","172.18.","172.19.","172.2","172.30.","172.31.","192.168.","0.")

def log_security_event(ip,event_type,path="",detail=""):
    """Record a flagged/suspicious event (failed login, admin probing, etc.)
    so it can be plotted on the admin hazard map. Never raises — logging
    a security event should never itself break the request."""
    if not ip: return
    try:
        with db() as con:
            execute(con,"INSERT INTO security_events(ip,event_type,path,detail) VALUES(%s,%s,%s,%s)",(ip,event_type,path[:255],detail[:255]))
    except Exception as e:
        app.logger.warning(f"log_security_event failed: {e}")

def _geolocate_ip(ip):
    """Look up (and cache) approximate location for an IP. Returns None for
    private/local IPs or on lookup failure — never raises."""
    if not ip or ip.startswith(_PRIVATE_IP_PREFIXES) or ip in ("localhost","::1"):
        return None
    with db() as con:
        row=fetchone(con,"SELECT lat,lon,city,region,country FROM ip_geo_cache WHERE ip=%s",(ip,))
    if row and row[0] is not None:
        return {"lat":row[0],"lon":row[1],"city":row[2],"region":row[3],"country":row[4]}
    try:
        import requests as _req
        r=_req.get(f"https://ipapi.co/{ip}/json/",timeout=5)
        d=r.json()
        if d.get("error"): return None
        lat,lon=d.get("latitude"),d.get("longitude")
        if lat is None or lon is None: return None
        city,region,country=d.get("city") or "",d.get("region") or "",d.get("country_name") or ""
        with db() as con:
            execute(con,"INSERT INTO ip_geo_cache(ip,lat,lon,city,region,country,fetched_at) VALUES(%s,%s,%s,%s,%s,%s,%s) ON CONFLICT (ip) DO UPDATE SET lat=EXCLUDED.lat,lon=EXCLUDED.lon,city=EXCLUDED.city,region=EXCLUDED.region,country=EXCLUDED.country,fetched_at=EXCLUDED.fetched_at",(ip,lat,lon,city,region,country,utc_now()))
        return {"lat":lat,"lon":lon,"city":city,"region":region,"country":country}
    except Exception as e:
        app.logger.warning(f"geolocate failed for {ip}: {e}")
        return None

def send_push(username,title,body,tag="vox"):
    try:
        from pywebpush import webpush
        with db() as con: subs=fetchall(con,"SELECT endpoint,p256dh,auth FROM push_subscriptions WHERE username=%s",(username,))
        for endpoint,p256dh,auth in subs:
            try:
                webpush(subscription_info={"endpoint":endpoint,"keys":{"p256dh":p256dh,"auth":auth}},
                    data=_json.dumps({"title":title,"body":body,"tag":tag}),
                    vapid_private_key=VAPID_PRIVATE_KEY,vapid_claims=VAPID_CLAIMS)
            except Exception as ex:
                if hasattr(ex,'response') and ex.response and ex.response.status_code in (404,410):
                    with db() as con2: execute(con2,"DELETE FROM push_subscriptions WHERE endpoint=%s",(endpoint,))
    except Exception: pass

# ── Base design (theme / CSS / shell chrome) ───────────────────────────────
def theme_css(t):
    c=THEMES.get(t,THEMES["green"]);p,bg,ac=c["p"],c["bg"],c["ac"]
    return "".join([
        f":root{{--p:{p};--bg:{bg};--ac:{ac};--p10:{p}33;--p30:{p}66;--r:12px}}",
        f"html,body{{margin:0;padding:0;min-height:100vh}}",
        f"body{{background-color:{bg};background-image:linear-gradient(var(--p10) 1px,transparent 1px),linear-gradient(90deg,var(--p10) 1px,transparent 1px);background-size:35px 35px;color:var(--p);font-family:'Courier New',monospace;font-weight:bold;text-transform:uppercase;overflow-x:hidden;}}",
        f"body::before,body::after{{content:\"\";position:fixed;left:0;width:100%;pointer-events:none;z-index:1}}",
        f"body::before{{top:0;height:16px;background:linear-gradient(to bottom,transparent,var(--p30),transparent);filter:blur(3px);animation:scan 7s linear infinite}}",
        f"body::after{{top:0;height:6px;background:var(--p);opacity:.18;filter:blur(1px);animation:scan 13s linear infinite 2s}}",
        f".scanline-a{{position:fixed;left:0;width:100%;height:10px;background:linear-gradient(to bottom,transparent,var(--p30),transparent);filter:blur(2px);animation:scan 5s linear infinite 1s;pointer-events:none;z-index:1}}",
        f".scanline-b{{position:fixed;left:0;width:100%;height:4px;background:var(--p);opacity:.12;animation:scan 9s linear infinite 4s;pointer-events:none;z-index:1}}",
        f".scanline-c{{position:fixed;left:0;width:100%;height:24px;background:linear-gradient(to bottom,transparent,{p}22,transparent);filter:blur(5px);animation:scan 18s linear infinite 0s;pointer-events:none;z-index:1}}",
        f".crt-overlay{{position:fixed;inset:0;background:repeating-linear-gradient(0deg,transparent,transparent 2px,{p}08 2px,{p}08 4px);pointer-events:none;z-index:1;animation:crtflicker 0.15s infinite}}",
        "@keyframes scan{0%{top:-10%}100%{top:110%}}@keyframes crtflicker{0%,100%{opacity:1}50%{opacity:.97}}@keyframes fadeIn{from{opacity:0;transform:translateY(-6px)}to{opacity:1;transform:translateY(0)}}@keyframes tcPulse{0%,100%{opacity:1;box-shadow:0 0 6px var(--p)}50%{opacity:.4;box-shadow:none}}",
        ".logo-wrap{display:flex;justify-content:center;padding:28px 0 16px;position:relative;z-index:2}",
        ".title-row{display:grid;grid-template-columns:auto 1fr auto;align-items:center;gap:14px;margin:0 0 20px;position:relative;z-index:2}",
        ".title-center{display:flex;align-items:center;justify-content:center}.title-row-right{display:flex;align-items:center;justify-content:flex-end;gap:8px}",
        ".menu-wrap{position:relative;z-index:1000}.title-row-wrap{max-width:960px;margin:0 auto;padding:28px 16px 0;position:relative;z-index:1000}",
        ".command-wrapper{position:relative;z-index:2}",
        ".dropdown-menu{display:none;position:absolute;top:calc(100% + 8px);left:0;background:rgba(0,0,0,.98);border:2px solid var(--p);border-radius:var(--r);box-shadow:0 0 30px var(--p30);z-index:9999;min-width:220px;max-width:260px;width:max-content}",
        ".dropdown-menu.open{display:block;animation:fadeIn .15s ease}",
        ".dropdown-item{display:flex;align-items:center;gap:12px;padding:12px 16px;color:var(--p);text-decoration:none;font-size:12px;font-family:'Courier New',monospace;text-transform:uppercase;border-bottom:1px solid var(--p10);cursor:pointer;transition:.15s;white-space:nowrap}",
        ".dropdown-item:hover{background:var(--p);color:#000}.dropdown-item:last-child{border-bottom:none;border-radius:0 0 var(--r) var(--r)}.dropdown-item i{width:20px;text-align:center;font-size:13px}.dropdown-divider{border-top:1px solid var(--p30);margin:4px 0}",
        ".menu-trigger{cursor:pointer;user-select:none;border:2px solid var(--p);border-radius:8px;padding:0;color:var(--p);background:var(--p10);font-family:'Courier New',monospace;font-size:12px;font-weight:bold;text-transform:uppercase;box-shadow:0 0 8px var(--p30);transition:.2s;white-space:nowrap;display:inline-flex;align-items:center;overflow:hidden}",
        ".menu-trigger:hover{background:var(--p);color:#000;box-shadow:0 0 16px var(--p)}",
        ".hero-btn{border:2px solid var(--p);border-radius:10px;padding:10px 20px;color:var(--p);background:var(--p10);cursor:pointer;font-family:'Courier New',monospace;font-size:15px;font-weight:bold;text-transform:uppercase;letter-spacing:2px;white-space:nowrap;box-shadow:0 0 18px var(--p30);transition:.2s}",
        ".hero-btn:hover{background:var(--p);color:#000;box-shadow:0 0 30px var(--p)}",
        ".content-box{width:min(100%,900px);box-sizing:border-box;margin:24px auto;padding:24px 30px;border:2px dashed var(--p);border-radius:var(--r);box-shadow:0 0 8px var(--p30);font-size:17px;background:transparent;line-height:1.7;position:relative;z-index:2}",
        ".three-column-grid{display:grid;grid-template-columns:repeat(auto-fit,minmax(240px,1fr));gap:18px;padding:0 16px;margin:0 auto 24px;position:relative;z-index:2;width:min(100%,900px);box-sizing:border-box}",
        ".column{border:3px solid var(--p);border-radius:var(--r);padding:24px 20px;background:transparent;box-shadow:0 0 20px var(--p30);display:flex;flex-direction:column;align-items:center;text-align:center;position:relative;z-index:2}",
        ".column h3{margin:0 0 10px;font-size:16px}.column p{margin:0;font-size:13px;opacity:.8}",
        ".btn-action{border:2px solid var(--p);border-radius:8px;padding:10px 22px;color:var(--p);text-decoration:none;display:inline-block;background:var(--p10);margin-top:14px;cursor:pointer;font-family:'Courier New',monospace;font-size:13px;text-transform:uppercase;transition:.2s;position:relative;z-index:2}",
        ".modal-overlay{display:none;position:fixed;inset:0;background:rgba(0,0,0,.93);z-index:9000;justify-content:center;align-items:flex-start;overflow-y:auto;padding:20px;box-sizing:border-box}",
        ".modal-overlay.open{display:flex}.modal-box{border:3px solid var(--p);border-radius:var(--r);padding:28px 24px;min-width:min(340px,92vw);max-width:540px;width:100%;background:#000;box-shadow:0 0 60px var(--p);text-align:center;margin:auto;position:relative;z-index:2}",
        ".modal-box h2{margin:0 0 18px;letter-spacing:5px;text-shadow:0 0 20px var(--p);font-size:clamp(14px,4vw,22px)}",
        ".field-wrap{position:relative;margin:8px 0}.field,.field-plain{width:100%;box-sizing:border-box;background:#000;border:2px solid var(--p);border-radius:8px;color:var(--p);font-family:'Courier New',monospace;font-size:13px;text-transform:none}",
        ".field{padding:11px 42px 11px 12px}.field-plain{padding:11px 12px;margin:8px 0}.field:focus,.field-plain:focus{outline:none;box-shadow:0 0 12px var(--p)}",
        ".eye-btn{position:absolute;right:10px;top:50%;transform:translateY(-50%);background:none;border:none;color:var(--p);cursor:pointer;font-size:15px;padding:4px}",
        ".error-msg{color:#f44;margin:6px 0;font-size:12px;min-height:16px;text-align:left}.success-msg{color:#4f4;margin:6px 0;font-size:12px;min-height:16px;text-align:left}",
        ".section-label{text-align:left;font-size:10px;opacity:.5;margin:14px 0 4px;border-bottom:1px solid var(--p30);padding-bottom:4px;letter-spacing:2px}",
        ".theme-grid{display:grid;grid-template-columns:repeat(3,1fr);gap:8px;margin:10px 0}.theme-btn{padding:8px 4px;border:2px solid;border-radius:8px;cursor:pointer;font-family:'Courier New',monospace;font-weight:bold;text-transform:uppercase;font-size:11px;background:#000}",
        ".tab-bar{display:flex;border-bottom:2px solid var(--p)}.tab{flex:1;padding:10px;cursor:pointer;font-size:12px;text-align:center;background:var(--p10);border:none;color:var(--p);font-family:'Courier New',monospace;text-transform:uppercase;border-right:1px solid var(--p);transition:.2s}",
        ".tab:last-child{border-right:0}.tab.active{background:var(--p);color:#000}.tab-content{display:none}.tab-content.active{display:block}",
        "@keyframes slideIn{from{transform:translateX(120%);opacity:0}to{transform:translateX(0);opacity:1}}@keyframes slideOut{from{transform:translateX(0);opacity:1}to{transform:translateX(120%);opacity:0}}",
        ".traffic-counter{position:fixed;top:12px;right:16px;z-index:9000;background:rgba(0,0,0,.92);border:2px solid var(--p);border-radius:10px;padding:6px 12px;font-size:10px;font-family:'Courier New',monospace;text-transform:uppercase;box-shadow:0 0 20px var(--p30);line-height:1.7;pointer-events:none}",
        ".tc-row{display:flex;align-items:center;gap:7px}.tc-dot{width:7px;height:7px;border-radius:50%;background:var(--p);box-shadow:0 0 6px var(--p);animation:tcPulse 2s infinite;flex-shrink:0}.tc-label{opacity:.5;font-size:9px}.tc-val{font-weight:900;text-shadow:0 0 8px var(--p)}",
        "@media(max-width:700px){",
        ".title-row-wrap{padding:10px 8px 0}.logo-wrap{padding:12px 0 6px}",
        ".title-row{display:flex;flex-direction:row;align-items:center;justify-content:space-between;gap:6px;margin:0 0 10px}",
        ".title-center{flex:1}.title-row-right{display:flex;gap:5px;flex-shrink:0}",
        ".hero-btn{font-size:10px;padding:5px 8px;letter-spacing:0;border-radius:7px}.menu-trigger{font-size:10px;padding:5px 8px}",
        ".traffic-counter{top:6px;right:6px;padding:3px 8px;font-size:8px;line-height:1.4;display:flex;flex-direction:row;gap:8px;align-items:center}.tc-row{gap:4px}",
        ".content-box{width:100%;padding:12px 14px;font-size:13px;line-height:1.6;margin:12px 0;box-sizing:border-box}",
        ".three-column-grid{grid-template-columns:1fr;gap:10px;padding:2px;margin-bottom:12px}.column{padding:14px 12px}.column h3{font-size:12px}.column p{font-size:12px}.btn-action{font-size:11px;padding:7px 14px;margin-top:10px}",
        ".modal-overlay{padding:20px;align-items:center}.modal-box{max-width:96%;width:100%;border-radius:var(--r);padding:20px 16px;margin:auto;max-height:90vh;overflow-y:auto}",
        ".modal-box h2{font-size:13px;letter-spacing:2px;margin-bottom:10px}.field-plain{font-size:12px;padding:9px 10px}.field{font-size:12px;padding:9px 36px 9px 10px}.theme-grid{gap:5px}.theme-btn{font-size:9px;padding:6px 2px}",
        ".dropdown-menu{min-width:170px;left:auto;right:0;z-index:9999}.dropdown-item{padding:10px 12px;font-size:11px}",
        "#adminContent{max-height:180px}",
        "}",
    ])

def pw_field(fid,ph,ac="current-password"):
    return (f'<div class="field-wrap"><input class="field" id="{fid}" placeholder="{ph}" type="password" autocomplete="{ac}">'
            f'<button class="eye-btn" type="button" onclick="togglePw(\'{fid}\',this)">&#128065;</button></div>')

def theme_btns(fn):
    entries=[("green","#0f0"),("cyan","#0ff"),("amber","#fb0"),("red","#f22"),("purple","#c4f"),("white","#fff")]
    return "".join(f'<button class="theme-btn" style="color:{c};border-color:{c};" onclick="{fn}(\'{k}\')">&#9679; {k.upper()}</button>' for k,c in entries)

def cyber_box(title,body,*,title_right="",extra_header="",footer="",radius="var(--r)",mb="24px",max_h=None,border_top=True,body_style=""):
    mh=f"max-height:{max_h};overflow-y:auto;" if max_h else ""
    return (f'<div class="command-wrapper" style="width:100%;margin-bottom:{mb};box-sizing:border-box;">'
            f'<div style="padding:10px 14px;border:2px solid var(--p);border-radius:{radius} {radius} 0 0;background:var(--p10);display:flex;align-items:center;justify-content:space-between;flex-wrap:wrap;gap:8px;">'
            f'<span style="font-size:13px;letter-spacing:2px;">{title}</span>{title_right}</div>'
            f'{extra_header}'
            f'<div style="border:2px solid var(--p);border-top:none;border-radius:0 0 {radius} {radius};{mh}{body_style}">{body}</div>'
            f'{footer}</div>')

_LOGO_SVG="""<svg viewBox="0 0 400 420" width="260" height="273" xmlns="http://www.w3.org/2000/svg" style="overflow:visible;"><defs><style>@keyframes spinFwd{from{transform:rotate(0deg)}to{transform:rotate(360deg)}}@keyframes spinRev{from{transform:rotate(0deg)}to{transform:rotate(-360deg)}}@keyframes fireGlow{0%,100%{filter:drop-shadow(0 0 2px var(--p))}50%{filter:drop-shadow(0 0 4px var(--p))}}.orbit-a{transform-origin:200px 195px;animation:spinFwd 8s linear infinite}.orbit-b{transform-origin:200px 195px;animation:spinRev 12s linear infinite}.logo-badge{animation:fireGlow 2.2s ease-in-out infinite}</style><radialGradient id="lgbgG" cx="50%" cy="50%" r="50%"><stop offset="0%" stop-color="var(--ac)"/><stop offset="100%" stop-color="#000a06"/></radialGradient><radialGradient id="lgrimG" cx="50%" cy="35%" r="65%"><stop offset="0%" stop-color="var(--p)" stop-opacity="0.15"/><stop offset="100%" stop-color="#000" stop-opacity="0"/></radialGradient><filter id="lgglow" x="-30%" y="-30%" width="160%" height="160%"><feGaussianBlur stdDeviation="2" result="b"/><feMerge><feMergeNode in="b"/><feMergeNode in="SourceGraphic"/></feMerge></filter><clipPath id="lgcirc"><circle cx="200" cy="195" r="122"/></clipPath><path id="lgarcB" d="M 98,238 A 112,112 0 0,0 302,238"/></defs>
<g stroke="var(--p)" stroke-width="1.2" opacity="0.45"><line x1="200" y1="16" x2="200" y2="32"/><line x1="200" y1="358" x2="200" y2="374"/><line x1="28" y1="195" x2="44" y2="195"/><line x1="356" y1="195" x2="372" y2="195"/><line x1="64" y1="71" x2="75" y2="82"/><line x1="336" y1="71" x2="325" y2="82"/><line x1="64" y1="319" x2="75" y2="308"/><line x1="336" y1="319" x2="325" y2="308"/></g>
<circle cx="200" cy="195" r="158" fill="#050a08" stroke="var(--p)" stroke-width="1.5" opacity="0.5"/><circle cx="200" cy="195" r="151" fill="none" stroke="var(--p)" stroke-width="0.4" opacity="0.25"/>
<g class="orbit-a"><ellipse cx="200" cy="195" rx="144" ry="50" fill="none" stroke="var(--p)" stroke-width="1.8" opacity="0.65" filter="url(#lgglow)" transform="rotate(-25 200 195)"/><ellipse cx="200" cy="195" rx="144" ry="50" fill="none" stroke="var(--p)" stroke-width="1.0" opacity="0.35" transform="rotate(25 200 195)"/><ellipse cx="200" cy="195" rx="144" ry="50" fill="none" stroke="var(--p)" stroke-width="0.6" opacity="0.2" transform="rotate(75 200 195)"/></g>
<g class="orbit-b"><ellipse cx="200" cy="195" rx="136" ry="46" fill="none" stroke="var(--p)" stroke-width="1.4" opacity="0.5" filter="url(#lgglow)" transform="rotate(55 200 195)"/><ellipse cx="200" cy="195" rx="136" ry="46" fill="none" stroke="var(--p)" stroke-width="0.7" opacity="0.25" transform="rotate(-55 200 195)"/></g>
<g class="logo-badge"><circle cx="200" cy="195" r="122" fill="url(#lgbgG)" stroke="var(--p)" stroke-width="2.8"/><circle cx="200" cy="195" r="122" fill="url(#lgrimG)"/><circle cx="200" cy="195" r="116" fill="none" stroke="var(--p)" stroke-width="0.6" opacity="0.35"/></g>
<circle cx="200" cy="195" r="80" fill="none" stroke="var(--p)" stroke-width="1.6" opacity="0.45" filter="url(#lgglow)"/><circle cx="200" cy="195" r="75" fill="none" stroke="var(--p)" stroke-width="0.5" opacity="0.2"/>
<text x="200" y="210" text-anchor="middle" font-family="'Courier New',Courier,monospace" font-weight="900" font-size="58" letter-spacing="10" fill="var(--p)" filter="url(#lgglow)">VOX</text><text x="200" y="210" text-anchor="middle" font-family="'Courier New',Courier,monospace" font-weight="900" font-size="58" letter-spacing="10" fill="none" stroke="var(--p)" stroke-width="1.2" opacity="0.7">VOX</text>
<path d="M 84,244 A 122,122 0 0,0 316,244" fill="var(--ac)" stroke="var(--p)" stroke-width="1.6" opacity="0.9"/><path d="M 90,252 A 116,116 0 0,0 310,252" fill="none" stroke="var(--p)" stroke-width="0.4" opacity="0.35"/>
<text font-family="'Courier New',Courier,monospace" font-weight="900" font-size="15" letter-spacing="4" fill="var(--p)" filter="url(#lgglow)"><textPath href="#lgarcB" startOffset="50%" text-anchor="middle">VOX POPULI</textPath></text>
<path d="M 56,195 A 144,144 0 0,1 344,195" fill="none" stroke="var(--p)" stroke-width="0.4" opacity="0.2" stroke-dasharray="3 6"/>
</svg>"""

def shell(content,user=None,theme="green"):
    t=THEMES.get(theme,THEMES["green"]);admin=is_admin(user)
    if user:
        at_badge=(' <span style="font-size:9px;opacity:.8;margin-left:5px;letter-spacing:1px;vertical-align:middle;">&#9733; ADMIN</span>' if admin else '')
        menu_html=(f'<div class="menu-wrap"><div class="menu-trigger" onclick="event.stopPropagation();document.getElementById(\'accountMenu\').classList.toggle(\'open\')" style="display:flex;align-items:center;gap:8px;padding:8px 16px;border-radius:8px;">'
            f'<span style="font-size:16px;">&#9776;</span><span style="border-left:1px solid var(--p);opacity:.4;height:16px;"></span>'
            f'<span style="font-size:12px;letter-spacing:1px;">{user}</span>{at_badge}<span style="font-size:10px;opacity:.6;">&#9663;</span></div>'
            f'<div class="dropdown-menu" id="accountMenu"><div class="dropdown-item" style="opacity:.5;font-size:10px;cursor:default;pointer-events:none;padding:8px 16px;">&#9658; {user.upper()} [{t["name"]}]</div>'
            f'<div class="dropdown-divider"></div><a class="dropdown-item" onclick="openModal(\'settingsModal\')"><i class="fas fa-cog"></i> SETTINGS</a>'
            f'<a class="dropdown-item" onclick="enableNotifications()" id="notifMenuItem"><i class="fas fa-bell"></i> ENABLE NOTIFICATIONS</a>'
            +('<a class="dropdown-item" href="/security"><i class="fas fa-shield-alt"></i> SECURITY HUB</a>' if admin else '')
            +'<a class="dropdown-item" href="/logout"><i class="fas fa-sign-out-alt"></i> LOGOUT</a></div></div>')
        grid_style='grid-template-columns:auto 1fr auto'
        right_btns=(
            '<a href="/security" id="secNavBtn" title="SECURITY HUB" style="display:inline-flex;align-items:center;gap:6px;border:2px solid var(--p);border-radius:8px;padding:6px 12px;color:var(--p);background:var(--p10);font-family:\'Courier New\',monospace;font-size:11px;font-weight:bold;text-transform:uppercase;text-decoration:none;box-shadow:0 0 8px var(--p30);transition:.2s;" onmouseover="this.style.background=\'var(--p)\';this.style.color=\'#000\'" onmouseout="this.style.background=\'var(--p10)\';this.style.color=\'var(--p)\'">&#128737; SEC <span id="secStatusDot" style="width:9px;height:9px;border-radius:50%;background:#555;display:inline-block;margin-left:2px;transition:.4s;"></span></a>'
            if admin else ''
        )
    else:
        menu_html='';grid_style='grid-template-columns:1fr auto'
        right_btns=('<button class="hero-btn" onclick="openModal(\'loginModal\')">&#9658; LOGIN</button>'
                    '<button class="hero-btn" onclick="openModal(\'registerModal\')">&#9658; JOIN</button>')
    admin_panel=(
        '<div id="stContentAdmin" class="st-tab-content" style="display:none;">'
        '<div style="display:grid;grid-template-columns:1fr 1fr;gap:8px;margin-bottom:8px;">'
        '<button class="btn-action" style="margin:0;padding:8px;font-size:11px;grid-column:span 2;" onclick="adminShowUsers()">&#128100; USERS</button>'
        '<button class="btn-action" style="margin:0;padding:8px;font-size:11px;" onclick="adminShowTraffic()">&#128200; TRAFFIC</button>'
        '<button class="btn-action" style="margin:0;padding:8px;font-size:11px;border-color:#fb0;color:#fb0;" onclick="adminShowResets()">&#128274; PW RESETS</button>'
        '</div><div id="adminContent" style="max-height:300px;overflow-y:auto;text-align:left;font-size:11px;border:1px solid var(--p30);border-radius:8px;padding:4px;">'
        '<div style="padding:12px;opacity:.4;text-align:center;">SELECT AN ACTION ABOVE</div></div></div>'
    ) if admin else ''
    admin_tab='<button class="tab" id="stTabAdmin" onclick="switchStTab(\'admin\')">&#9733; ADMIN</button>' if admin else ''
    JS=f"""
let regThemeVal='green';
const api=(url,body)=>fetch(url,body?{{method:'POST',headers:{{'Content-Type':'application/json'}},body:JSON.stringify(body)}}:undefined).then(r=>r.json());
const $=id=>document.getElementById(id);
const openModal=id=>{{const el=$(id);if(el)el.classList.add('open');}};
const closeModal=id=>{{const el=$(id);if(el)el.classList.remove('open');}};
function togglePw(id,btn){{const i=$(id);i.type=i.type==='password'?'text':'password';btn.innerHTML=i.type==='password'?'&#128065;':'&#128584;';}}
document.querySelectorAll('.modal-overlay').forEach(m=>m.addEventListener('click',e=>{{if(e.target===m)m.classList.remove('open');}}));
document.addEventListener('click',e=>{{const menu=$('accountMenu');if(menu&&!e.target.closest('.menu-wrap'))menu.classList.remove('open');}});
function setRegTheme(t){{regThemeVal=t;}}
async function doLogin(){{const errEl=$('loginErr');errEl.textContent='';const d=await api('/api/login',{{username:$('loginUser').value.trim(),password:$('loginPass').value}});if(d.ok){{location.reload();}}else{{errEl.textContent='ERROR: '+d.error;}}}}
async function doRegister(){{const p=$('regPass').value,p2=$('regPass2').value,dob=$('regDob').value,errEl=$('regErr');errEl.textContent='';if(!dob){{errEl.textContent='DATE OF BIRTH REQUIRED';return;}}if((Date.now()-new Date(dob))/31557600000<18){{errEl.textContent='YOU MUST BE 18 OR OLDER TO JOIN';return;}}if(p!==p2){{errEl.textContent='PASSWORDS DO NOT MATCH';return;}}const d=await api('/api/register',{{username:$('regUser').value.trim(),password:p,theme:regThemeVal}});if(d.ok){{location.reload();}}else{{errEl.textContent='ERROR: '+d.error;}}}}
async function doResetRequest(){{const u=$('resetUser').value.trim(),errEl=$('resetErr'),okEl=$('resetOk');errEl.textContent='';okEl.textContent='';if(!u){{errEl.textContent='USERNAME REQUIRED';return;}}const d=await api('/api/reset/request',{{username:u}});if(d.ok){{okEl.textContent='REQUEST SENT — AN ADMIN WILL SET A TEMP PASSWORD FOR YOU.';}}else{{errEl.textContent='ERROR: '+d.error;}}}}
async function changePassword(){{const cur=$('pwCurrent').value,nw=$('pwNew').value,nw2=$('pwNew2').value;const errEl=$('pwErr'),okEl=$('pwOk');errEl.textContent='';okEl.textContent='';if(!cur||!nw||!nw2){{errEl.textContent='ALL FIELDS REQUIRED';return;}}if(nw!==nw2){{errEl.textContent='PASSWORDS DO NOT MATCH';return;}}if(nw.length<6){{errEl.textContent='TOO SHORT (MIN 6)';return;}}const d=await api('/api/change-password',{{current:cur,new_password:nw}});if(d.ok){{okEl.textContent='PASSWORD UPDATED';['pwCurrent','pwNew','pwNew2'].forEach(i=>$(i).value='');}}else{{errEl.textContent='ERROR: '+d.error;}}}}
async function changeTheme(t){{await api('/api/theme',{{theme:t}});location.reload();}}
function switchStTab(tab){{['theme','pw','admin'].forEach(k=>{{const K=k[0].toUpperCase()+k.slice(1);const c=$('stContent'+K),b=$('stTab'+K);if(c)c.style.display=k===tab?'block':'none';if(b)b.classList.toggle('active',k===tab);}});if(tab==='admin')adminShowUsers();}}
const adminBox=()=>$('adminContent');
const adminErr=msg=>{{if(adminBox())adminBox().innerHTML=`<div style="padding:10px;color:#f44;">${{msg}}</div>`;}};
async function adminShowUsers(){{if(!adminBox())return;adminBox().innerHTML='<div style="padding:10px;opacity:.4;text-align:center;">LOADING...</div>';const d=await api('/api/admin/users');if(!d.ok){{adminErr('ACCESS DENIED');return;}}adminBox().innerHTML='<div style="padding:6px 10px;opacity:.5;font-size:10px;border-bottom:1px solid var(--p10);">&#128100; USERS</div>'+d.users.map(u=>`<div style="padding:8px 10px;border-bottom:1px solid var(--p10);display:flex;justify-content:space-between;align-items:center;gap:6px;flex-wrap:wrap;"><span>${{u.username}}${{u.is_admin?' &#9733;':''}} <span style="opacity:.4;font-size:10px;">${{u.created_at}}</span></span><div style="display:flex;gap:4px;"><button class="btn-action" style="padding:3px 8px;font-size:10px;margin:0;" onclick="adminToggleAdmin('${{u.username}}',${{!u.is_admin}})">${{u.is_admin?'REVOKE':'GRANT ADMIN'}}</button><button class="btn-action" style="padding:3px 8px;font-size:10px;margin:0;border-color:#f44;color:#f44;" onclick="adminRemoveUser('${{u.username}}')">&#10006;</button></div></div>`).join('');}}
async function adminToggleAdmin(u,g){{await api('/api/admin/set-admin',{{username:u,grant:g}});adminShowUsers();}}
async function adminRemoveUser(u){{if(!confirm('REMOVE: '+u+'?'))return;const d=await api('/api/admin/remove-user',{{username:u}});d.ok?adminShowUsers():alert('ERROR: '+d.error);}}
async function adminShowTraffic(){{adminBox().innerHTML='<div style="padding:10px;opacity:.4;text-align:center;">LOADING...</div>';const d=await api('/api/admin/traffic');if(!d.ok){{adminErr('ERROR');return;}}const max=Math.max(...d.days.map(r=>r.visitors),1);let html=`<div style="padding:8px 10px;background:var(--p10);border-bottom:1px solid var(--p30);display:flex;justify-content:space-between;font-size:11px;"><span>&#128200; SITE TRAFFIC</span><span>TODAY: <b>${{d.today}}</b> &nbsp;|&nbsp; ALL TIME: <b>${{d.total}}</b></span></div>`;d.days.forEach(r=>{{const pct=Math.round(r.visitors/max*100);html+=`<div style="padding:6px 10px;border-bottom:1px solid var(--p10);display:flex;align-items:center;gap:8px;font-size:11px;"><span style="width:80px;flex-shrink:0;opacity:.7;">${{r.date}}</span><div style="flex:1;background:var(--p10);border-radius:4px;height:14px;overflow:hidden;"><div style="width:${{pct}}%;height:100%;background:var(--p);box-shadow:0 0 8px var(--p);border-radius:4px;transition:.3s;"></div></div><span style="width:28px;text-align:right;">${{r.visitors}}</span></div>`;}});adminBox().innerHTML=html;}}
async function adminShowResets(){{adminBox().innerHTML='<div style="padding:10px;opacity:.4;text-align:center;">LOADING...</div>';const d=await api('/api/admin/reset-requests');if(!d.ok){{adminErr('ERROR');return;}}if(!d.requests.length){{adminBox().innerHTML='<div style="padding:12px;opacity:.4;text-align:center;">NO PENDING RESET REQUESTS</div>';return;}}let html='<div style="padding:6px 10px;opacity:.5;font-size:10px;border-bottom:1px solid var(--p10);">&#128274; PASSWORD RESET REQUESTS</div>';d.requests.forEach(r=>{{html+=`<div style="padding:10px;border-bottom:1px solid var(--p10);display:flex;justify-content:space-between;align-items:center;flex-wrap:wrap;gap:6px;"><div><span style="font-size:12px;">${{r.username}}</span><span style="font-size:10px;opacity:.5;margin-left:8px;">${{r.requested_at}}</span>${{r.temp_password?`<div style="font-size:11px;margin-top:4px;color:#4f4;">TEMP PW: <b>${{r.temp_password}}</b></div>`:''}}</div><div style="display:flex;gap:4px;flex-wrap:wrap;"><input id="tmpPw_${{r.id}}" class="field-plain" placeholder="SET TEMP PW..." style="margin:0;padding:5px 8px;font-size:11px;width:120px;border-radius:6px;"><button class="btn-action" style="margin:0;padding:4px 10px;font-size:10px;" onclick="adminApproveReset(${{r.id}})">&#10003; SET</button><button class="btn-action" style="margin:0;padding:4px 10px;font-size:10px;border-color:#f44;color:#f44;" onclick="adminDenyReset(${{r.id}})">&#10006;</button></div></div>`;}});adminBox().innerHTML=html;}}
async function adminApproveReset(id){{const inp=document.getElementById('tmpPw_'+id),pw=inp?inp.value.trim():'';if(!pw){{alert('ENTER A TEMPORARY PASSWORD');return;}}const d=await api('/api/admin/reset-approve',{{id,temp_password:pw}});d.ok?adminShowResets():alert('ERROR: '+d.error);}}
async function adminDenyReset(id){{if(!confirm('DENY THIS RESET REQUEST?'))return;const d=await api('/api/admin/reset-deny',{{id}});d.ok?adminShowResets():alert('ERROR: '+d.error);}}
function enableNotifications(){{if(!('Notification' in window)){{alert('NOTIFICATIONS NOT SUPPORTED ON THIS BROWSER');return;}}if(Notification.permission==='granted'){{setupPushSubscription();const m=$('notifMenuItem');if(m)m.style.display='none';return;}}Notification.requestPermission().then(p=>{{if(p==='granted'){{setupPushSubscription();const m=$('notifMenuItem');if(m)m.style.display='none';}}else alert('NOTIFICATION PERMISSION DENIED.');}});}}
function requestNotifPermission(){{if(!('Notification' in window))return;if(Notification.permission==='granted'){{setupPushSubscription();return;}}}}
function urlBase64ToUint8Array(b64){{const padding='='.repeat((4-b64.length%4)%4),base64=(b64+padding).replace(/-/g,'+').replace(/_/g,'/'),raw=atob(base64);return Uint8Array.from({{length:raw.length}},(_,i)=>raw.charCodeAt(i));}}
async function setupPushSubscription(){{try{{if(!('serviceWorker' in navigator)||!('PushManager' in window))return;const reg=await navigator.serviceWorker.ready;const existing=await reg.pushManager.getSubscription();if(existing){{await api('/api/push/subscribe',existing.toJSON());return;}}const kd=await api('/api/push/vapid-public-key');if(!kd.ok||!kd.key)return;const sub=await reg.pushManager.subscribe({{userVisibleOnly:true,applicationServerKey:urlBase64ToUint8Array(kd.key)}});await api('/api/push/subscribe',sub.toJSON());}}catch(e){{}}}}
async function loadTrafficCounter(){{try{{const d=await api('/api/traffic/public');if(d.ok){{$('tcOnline').textContent=d.online;$('tcToday').textContent=d.today;$('tcTotal').textContent=d.total;if($('tcMembers'))$('tcMembers').textContent=d.members;}}}}catch(e){{}}}}
loadTrafficCounter();setInterval(loadTrafficCounter,10000);requestNotifPermission();
(async function secNavPoll(){{
  const dot=document.getElementById('secStatusDot');if(!dot)return;
  async function updateDot(){{
    try{{
      const s=await fetch('/api/security/status').then(r=>r.json());
      const r=await fetch('/api/security/reports').then(r=>r.json());
      if(!s.ok||!r.ok||!r.reports.length){{dot.style.background='#555';dot.title='NO SCANS YET';return;}}
      const rpt=r.reports[0];
      const harmful=rpt.harmful_content?.length??0;
      const broken=rpt.broken_links?.length??0;
      const sslOk=rpt.ssl?.ok;
      if(harmful>0||!sslOk){{dot.style.background='#ff2222';dot.style.boxShadow='0 0 8px #ff2222';dot.title='CRITICAL ISSUES';}}
      else if(broken>0||(rpt.content_changes?.length??0)>0){{dot.style.background='#ffaa00';dot.style.boxShadow='0 0 8px #ffaa00';dot.title='WARNINGS';}}
      else{{dot.style.background='#00ff88';dot.style.boxShadow='0 0 8px #00ff88';dot.title='ALL CLEAR';}}
    }}catch{{dot.style.background='#555';}}
  }}
  updateDot();setInterval(updateDot,30000);
}})();
(function(){{const c=document.createElement('canvas');c.style.cssText='position:fixed;top:0;left:0;width:100%;height:100%;z-index:0;pointer-events:none;opacity:0.18;';document.body.insertBefore(c,document.body.firstChild);const ctx=c.getContext('2d');const chars='ABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789@#$%^&*()アイウエオカキクケコサシスセソタチツテトナニヌネノ';let cols,drops,color;function getColor(){{return getComputedStyle(document.documentElement).getPropertyValue('--p').trim()||'#00ff00';}}function resize(){{c.width=window.innerWidth;c.height=window.innerHeight;cols=Math.floor(c.width/16);drops=Array(cols).fill(1);color=getColor();}}resize();window.addEventListener('resize',resize);new MutationObserver(()=>{{color=getColor();}}).observe(document.documentElement,{{attributes:true,attributeFilter:['style']}});setInterval(()=>{{color=getColor();ctx.fillStyle='rgba(0,0,0,0.05)';ctx.fillRect(0,0,c.width,c.height);ctx.fillStyle=color;ctx.font='14px Courier New';for(let i=0;i<drops.length;i++){{ctx.fillText(chars[Math.floor(Math.random()*chars.length)],i*16,drops[i]*16);if(drops[i]*16>c.height&&Math.random()>0.975)drops[i]=0;drops[i]++;}}}} ,50);}})();"""
    return f"""<!DOCTYPE html>
<html lang="en"><head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width,initial-scale=1,maximum-scale=1">
<link rel="stylesheet" href="https://cdnjs.cloudflare.com/ajax/libs/font-awesome/5.15.4/css/all.min.css">
<link rel="manifest" href="/manifest.json">
<meta name="theme-color" content="#00ff00">
<meta name="apple-mobile-web-app-capable" content="yes">
<meta name="apple-mobile-web-app-status-bar-style" content="black">
<meta name="apple-mobile-web-app-title" content="VOX">
<link rel="apple-touch-icon" href="/icon-192.png">
<script>if('serviceWorker' in navigator)navigator.serviceWorker.register('/sw.js');</script>
<style>{theme_css(theme)}</style>
</head><body>
<div class="crt-overlay"></div><div class="scanline-a"></div><div class="scanline-b"></div><div class="scanline-c"></div>
<div class="title-row-wrap">
  <div class="logo-wrap">{_LOGO_SVG}</div>
  <div class="title-row" style="{grid_style}">
    {menu_html}<div class="title-center"></div><div class="title-row-right">{right_btns}</div>
  </div>
</div>
<div class="page-content" style="width:100%;max-width:960px;margin:0 auto;padding:0 12px 40px;box-sizing:border-box;">{content}</div>
<div class="modal-overlay" id="loginModal"><div class="modal-box">
  <h2>// ACCESS //</h2><div id="loginErr" class="error-msg"></div>
  <input class="field-plain" id="loginUser" placeholder="USERNAME" type="text" autocomplete="username" style="text-transform:none;">
  {pw_field("loginPass","PASSWORD")}
  <br><button class="btn-action" onclick="doLogin()">&#9658; AUTHENTICATE</button>
  <button class="btn-action" style="margin-left:8px;" onclick="closeModal('loginModal')">&#10006; CANCEL</button>
  <div style="margin-top:14px;font-size:11px;opacity:.6;">FORGOT YOUR PASSWORD? <span style="text-decoration:underline;cursor:pointer;color:var(--p);" onclick="closeModal('loginModal');openModal('resetModal')">REQUEST A RESET</span></div>
</div></div>
<div class="modal-overlay" id="resetModal"><div class="modal-box">
  <h2>// PASSWORD RESET //</h2>
  <div style="font-size:11px;opacity:.6;margin-bottom:14px;">ENTER YOUR USERNAME AND AN ADMIN WILL SET A TEMPORARY PASSWORD FOR YOU.</div>
  <div id="resetErr" class="error-msg"></div><div id="resetOk" class="success-msg"></div>
  <input class="field-plain" id="resetUser" placeholder="YOUR USERNAME" type="text" style="text-transform:none;"><br>
  <button class="btn-action" onclick="doResetRequest()">&#9658; REQUEST RESET</button>
  <button class="btn-action" style="margin-left:8px;" onclick="closeModal('resetModal')">&#10006; CANCEL</button>
</div></div>
<div class="modal-overlay" id="registerModal"><div class="modal-box">
  <h2>// ENLIST //</h2>
  <div style="font-size:10px;opacity:.5;margin-bottom:10px;">&#9888; YOU MUST BE 18 OR OLDER TO JOIN</div>
  <div id="regErr" class="error-msg"></div>
  <input class="field-plain" id="regUser" placeholder="CHOOSE USERNAME" type="text" autocomplete="username" style="text-transform:none;">
  {pw_field("regPass","CHOOSE PASSWORD","new-password")}
  {pw_field("regPass2","CONFIRM PASSWORD","new-password")}
  <div class="section-label">DATE OF BIRTH:</div>
  <input class="field-plain" id="regDob" type="date" style="color-scheme:dark;">
  <div class="section-label">SELECT THEME:</div>
  <div class="theme-grid">{theme_btns("setRegTheme")}</div><br>
  <button class="btn-action" onclick="doRegister()">&#9658; ENLIST</button>
  <button class="btn-action" style="margin-left:8px;" onclick="closeModal('registerModal')">&#10006; CANCEL</button>
</div></div>
<div class="modal-overlay" id="settingsModal"><div class="modal-box" style="max-width:660px;width:96%;">
  <h2>// SETTINGS //</h2>
  <div class="tab-bar" style="margin-bottom:16px;">
    <button class="tab active" id="stTabTheme" onclick="switchStTab('theme')">&#127774; THEME</button>
    <button class="tab" id="stTabPw" onclick="switchStTab('pw')">&#128274; PASSWORD</button>
    {admin_tab}
  </div>
  <div id="stContentTheme" class="st-tab-content" style="display:block;">
    <div class="section-label">CHANGE THEME:</div><div class="theme-grid">{theme_btns("changeTheme")}</div>
  </div>
  <div id="stContentPw" class="st-tab-content" style="display:none;">
    <div class="section-label">CHANGE PASSWORD:</div>
    <div id="pwErr" class="error-msg"></div><div id="pwOk" class="success-msg"></div>
    {pw_field("pwCurrent","CURRENT PASSWORD")}
    {pw_field("pwNew","NEW PASSWORD (MIN 6)","new-password")}
    {pw_field("pwNew2","CONFIRM NEW PASSWORD","new-password")}<br>
    <button class="btn-action" onclick="changePassword()">&#9658; UPDATE PASSWORD</button>
  </div>
  {admin_panel}<br>
  <button class="btn-action" onclick="closeModal('settingsModal')">&#10006; CLOSE</button>
</div></div>
<div class="traffic-counter">
  <div class="tc-row"><div class="tc-dot"></div><span class="tc-label">ONLINE</span>&nbsp;<span class="tc-val" id="tcOnline">...</span></div>
  <div class="tc-row"><span class="tc-label">TODAY</span>&nbsp;<span class="tc-val" id="tcToday">...</span></div>
  <div class="tc-row"><span class="tc-label">ALL&#8209;TIME</span>&nbsp;<span class="tc-val" id="tcTotal">...</span></div>
  <div class="tc-row"><span class="tc-label">MEMBERS</span>&nbsp;<span class="tc-val" id="tcMembers">...</span></div>
</div>
<script>{JS}</script>
</body></html>"""

# ── Home / core routes ──────────────────────────────────────────────────────
@app.route("/")
def home():
    user=session.get("username");theme=session.get("theme","green")
    install_banner=('<div id="installBanner" style="display:block;width:100%;margin:0 0 16px;box-sizing:border-box;"><div style="border:2px solid var(--p);border-radius:var(--r);padding:10px 16px;background:var(--p10);display:flex;align-items:center;justify-content:space-between;flex-wrap:wrap;gap:8px;"><span style="font-size:11px;letter-spacing:1px;">&#128242; INSTALL VOX APP &mdash; ACCESS FROM YOUR HOME SCREEN</span><button id="enableNotifBtn" class="btn-action" style="margin:0;padding:6px 16px;font-size:11px;" onclick="enableNotifications()">&#128276; ENABLE NOTIFICATIONS</button><div style="display:flex;gap:8px;align-items:center;"><button id="installBtn" class="btn-action" style="margin:0;padding:6px 16px;font-size:11px;" onclick="triggerInstall()">&#11015; INSTALL</button><button onclick="document.getElementById(\'installBanner\').style.display=\'none\';localStorage.setItem(\'voxInstallDismissed\',\'1\')" style="background:none;border:none;color:var(--p);cursor:pointer;font-size:14px;padding:2px 6px;">&#10006;</button></div></div><div id="iosInstallMsg" style="display:none;border:1px solid var(--p30);border-top:none;border-radius:0 0 var(--r) var(--r);padding:8px 16px;font-size:10px;opacity:.7;letter-spacing:1px;">&#63743; ON IOS: TAP THE SHARE BUTTON THEN &ldquo;ADD TO HOME SCREEN&rdquo;</div></div>'
        '<script>let _installPrompt=null;window.addEventListener(\'beforeinstallprompt\',e=>{e.preventDefault();_installPrompt=e;if(!localStorage.getItem(\'voxInstallDismissed\')){const b=document.getElementById(\'installBanner\');if(b)b.style.display=\'block\';}});window.addEventListener(\'appinstalled\',()=>{const b=document.getElementById(\'installBanner\');if(b)b.style.display=\'none\';localStorage.setItem(\'voxInstallDismissed\',\'1\');});if(typeof Notification!==\'undefined\'&&Notification.permission!==\'granted\'&&Notification.permission!==\'denied\'){const btn=document.getElementById(\'enableNotifBtn\');if(btn)btn.style.display=\'inline-block\';}if(typeof Notification!==\'undefined\'&&Notification.permission===\'granted\'){const m=document.getElementById(\'notifMenuItem\');if(m)m.style.display=\'none\';}function triggerInstall(){if(_installPrompt){_installPrompt.prompt();_installPrompt.userChoice.then(r=>{if(r.outcome===\'accepted\')localStorage.setItem(\'voxInstallDismissed\',\'1\');_installPrompt=null;});}}const isIOS=/iphone|ipad|ipod/i.test(navigator.userAgent)&&!window.MSStream;const isStandalone=window.navigator.standalone===true||window.matchMedia(\'(display-mode: standalone)\').matches;if(!isStandalone&&!localStorage.getItem(\'voxInstallDismissed\')){const b=document.getElementById(\'installBanner\');if(b)b.style.display=\'block\';}if(isIOS&&!isStandalone&&!localStorage.getItem(\'voxInstallDismissed\')){const b=document.getElementById(\'installBanner\');const ios=document.getElementById(\'iosInstallMsg\');const btn=document.getElementById(\'installBtn\');if(b)b.style.display=\'block\';if(ios)ios.style.display=\'block\';if(btn)btn.style.display=\'none\';}</script>') if user else ""
    content=f'<div class="command-wrapper">{install_banner}</div>'
    return shell(content,user=user,theme=theme)

# ── Auth API ─────────────────────────────────────────────────────────────────
@app.route("/api/register",methods=["POST"])
def api_register():
    d=request.json or {};u,p,t=d.get("username","").strip(),d.get("password",""),d.get("theme","green")
    if not u or not p: return err("FIELDS REQUIRED")
    if len(u)<3: return err("USERNAME TOO SHORT (MIN 3)")
    if len(p)<6: return err("PASSWORD TOO SHORT (MIN 6)")
    if t not in THEMES: t="green"
    try:
        with db() as con:
            execute(con,"INSERT INTO users(username,password_hash,theme,is_admin) VALUES(%s,%s,%s,%s)",(u,hash_pw(p),t,1 if u==ADMIN_USER else 0))
        session["username"]=u;session["theme"]=t;session.permanent=True;return ok()
    except psycopg2.errors.UniqueViolation: return err("USERNAME TAKEN")
    except Exception as e: return err(str(e))

@app.route("/api/login",methods=["POST"])
def api_login():
    d=request.json or {};u,p=d.get("username","").strip(),d.get("password","")
    with db() as con:
        row=fetchone(con,"SELECT password_hash,theme FROM users WHERE username=%s",(u,))
        if not row or not verify_pw(row[0],p):
            log_security_event(get_ip(),"failed_login",path="/api/login",detail=f"username={u}")
            return err("INVALID CREDENTIALS")
    _migrate_pw_if_legacy(u,row[0],p)
    session["username"]=u;session["theme"]=row[1] or 'green';session.permanent=True;return ok()

@app.route("/logout")
def logout(): session.clear();return redirect("/")

@app.route("/api/theme",methods=["POST"])
def api_theme():
    if e:=require_login(): return e
    t=(request.json or {}).get("theme","green")
    if t not in THEMES: return err("INVALID THEME")
    with db() as con: execute(con,"UPDATE users SET theme=%s WHERE username=%s",(t,me()))
    session["theme"]=t;return ok()

@app.route("/api/change-password",methods=["POST"])
def api_change_password():
    if e:=require_login(): return e
    d=request.json or {};cur_pw,new_pw=d.get("current",""),d.get("new_password","")
    if not cur_pw or not new_pw: return err("FIELDS REQUIRED")
    if len(new_pw)<6: return err("PASSWORD TOO SHORT")
    with db() as con:
        row=fetchone(con,"SELECT password_hash FROM users WHERE username=%s",(me(),))
        if not row or not verify_pw(row[0],cur_pw): return err("CURRENT PASSWORD INCORRECT")
        execute(con,"UPDATE users SET password_hash=%s WHERE username=%s",(hash_pw(new_pw),me()))
    return ok()

@app.route("/api/reset/request",methods=["POST"])
def api_reset_request():
    username=(request.json or {}).get("username","").strip()
    if not username: return err("USERNAME REQUIRED")
    with db() as con:
        if not fetchone(con,"SELECT id FROM users WHERE username=%s",(username,)): return err("USERNAME NOT FOUND")
        if fetchone(con,"SELECT id FROM password_resets WHERE username=%s AND status='pending'",(username,)): return ok()
        execute(con,"INSERT INTO password_resets(username) VALUES(%s)",(username,))
    return ok()

# ── Admin API ────────────────────────────────────────────────────────────────
@app.route("/api/admin/users")
def api_admin_users():
    if e:=require_admin(): return e
    with db() as con: rows=fetchall(con,"SELECT username,is_admin,created_at FROM users ORDER BY is_admin DESC,created_at ASC")
    return ok(users=[{"username":r[0],"is_admin":bool(r[1]),"created_at":str(r[2])} for r in rows])

@app.route("/api/admin/set-admin",methods=["POST"])
def api_admin_set():
    if e:=require_admin(): return e
    d=request.json or {};target,grant=d.get("username",""),d.get("grant",False)
    if target==ADMIN_USER: return err("CANNOT MODIFY ROOT ADMIN")
    with db() as con: execute(con,"UPDATE users SET is_admin=%s WHERE username=%s",(1 if grant else 0,target))
    return ok()

@app.route("/api/admin/remove-user",methods=["POST"])
def api_admin_remove_user():
    if e:=require_admin(): return e
    target=(request.json or {}).get("username","")
    if target==ADMIN_USER: return err("CANNOT REMOVE ROOT ADMIN")
    with db() as con: execute(con,"DELETE FROM users WHERE username=%s",(target,))
    return ok()

@app.route("/api/admin/traffic")
def api_admin_traffic():
    if e:=require_admin(): return e
    with db() as con:
        rows=fetchall(con,"SELECT date,COUNT(*) FROM visits GROUP BY date ORDER BY date DESC LIMIT 30")
        total=fetchone(con,"SELECT COUNT(DISTINCT ip) FROM visits")[0]
        today=fetchone(con,"SELECT COUNT(*) FROM visits WHERE date=CURRENT_DATE::text")[0]
    return ok(days=[{"date":r[0],"visitors":r[1]} for r in rows],total=total,today=today)

@app.route("/api/admin/reset-requests")
def api_admin_reset_requests():
    if e:=require_admin(): return e
    with db() as con: rows=fetchall(con,"SELECT id,username,temp_password,status,requested_at FROM password_resets WHERE status='pending' ORDER BY requested_at DESC")
    return ok(requests=[{"id":r[0],"username":r[1],"temp_password":r[2],"status":r[3],"requested_at":str(r[4])} for r in rows])

@app.route("/api/admin/reset-approve",methods=["POST"])
def api_admin_reset_approve():
    if e:=require_admin(): return e
    d=request.json or {};rid,temp_pw=d.get("id"),d.get("temp_password","").strip()
    if not rid or not temp_pw: return err("MISSING FIELDS")
    if len(temp_pw)<4: return err("TEMP PASSWORD TOO SHORT")
    with db() as con:
        row=fetchone(con,"SELECT username FROM password_resets WHERE id=%s",(rid,))
        if not row: return err("REQUEST NOT FOUND")
        execute(con,"UPDATE users SET password_hash=%s WHERE username=%s",(hash_pw(temp_pw),row[0]))
        execute(con,"UPDATE password_resets SET status='approved',temp_password=%s WHERE id=%s",(temp_pw,rid))
    return ok()

@app.route("/api/admin/reset-deny",methods=["POST"])
def api_admin_reset_deny():
    if e:=require_admin(): return e
    rid=(request.json or {}).get("id")
    if not rid: return err("MISSING ID")
    with db() as con: execute(con,"UPDATE password_resets SET status='denied' WHERE id=%s",(rid,))
    return ok()

# ── Traffic tracking ─────────────────────────────────────────────────────────
@app.before_request
def track_visit():
    if request.path.startswith(("/api","/static")): return
    now=utc_now()
    with db() as con:
        execute(con,"INSERT INTO visits(date,ip) VALUES(%s,%s) ON CONFLICT DO NOTHING",(datetime.date.today().isoformat(),get_ip()))
        u=session.get("username")
        if u: execute(con,"INSERT INTO user_sessions(username,last_seen) VALUES(%s,%s) ON CONFLICT (username) DO UPDATE SET last_seen=EXCLUDED.last_seen",(u,now))

@app.route("/api/traffic/public")
def api_traffic_public():
    ip,now=get_ip(),utc_now();cutoff=utc_cutoff(2);u=session.get("username")
    with db() as con:
        execute(con,"INSERT INTO visits(date,ip) VALUES(%s,%s) ON CONFLICT DO NOTHING",(datetime.date.today().isoformat(),ip))
        execute(con,"INSERT INTO active_users(ip,last_seen) VALUES(%s,%s) ON CONFLICT (ip) DO UPDATE SET last_seen=EXCLUDED.last_seen",(ip,now))
        execute(con,"DELETE FROM active_users WHERE last_seen < %s",(cutoff,))
        if u: execute(con,"INSERT INTO user_sessions(username,last_seen) VALUES(%s,%s) ON CONFLICT (username) DO UPDATE SET last_seen=EXCLUDED.last_seen",(u,now))
        execute(con,"DELETE FROM user_sessions WHERE last_seen < %s",(cutoff,))
        today=fetchone(con,"SELECT COUNT(*) FROM visits WHERE date=CURRENT_DATE::text")[0]
        total=fetchone(con,"SELECT COUNT(DISTINCT ip) FROM visits")[0]
        online=fetchone(con,"SELECT COUNT(*) FROM active_users")[0]
        members=fetchone(con,"SELECT COUNT(*) FROM users")[0]
    return ok(today=today,total=total,online=online,members=members)

# ── Web push / notifications ─────────────────────────────────────────────────
@app.route("/api/push/vapid-public-key")
def api_vapid_public_key(): return ok(key=VAPID_PUBLIC_KEY)

@app.route("/api/push/subscribe",methods=["POST"])
def api_push_subscribe():
    if e:=require_login(): return e
    d=request.json or {};endpoint=d.get("endpoint","");p256dh=d.get("keys",{}).get("p256dh","");auth=d.get("keys",{}).get("auth","")
    if not endpoint or not p256dh or not auth: return err("MISSING FIELDS")
    with db() as con: execute(con,"INSERT INTO push_subscriptions(username,endpoint,p256dh,auth) VALUES(%s,%s,%s,%s) ON CONFLICT (username,endpoint) DO UPDATE SET p256dh=EXCLUDED.p256dh,auth=EXCLUDED.auth",(me(),endpoint,p256dh,auth))
    return ok()

@app.route("/api/push/unsubscribe",methods=["POST"])
def api_push_unsubscribe():
    if e:=require_login(): return e
    endpoint=(request.json or {}).get("endpoint","")
    if endpoint:
        with db() as con: execute(con,"DELETE FROM push_subscriptions WHERE username=%s AND endpoint=%s",(me(),endpoint))
    return ok()

# ── PWA assets ───────────────────────────────────────────────────────────────
@app.route("/manifest.json")
def manifest():
    data={"name":"Vox Populi","short_name":"VOX","description":"Vox Populi Community","start_url":"/","display":"standalone","background_color":"#000000","theme_color":"#00ff00","orientation":"portrait-primary","icons":[{"src":"/icon-192.png","sizes":"192x192","type":"image/png","purpose":"any maskable"},{"src":"/icon-512.png","sizes":"512x512","type":"image/png","purpose":"any maskable"}],"categories":["social","news"],"shortcuts":[{"name":"Home","url":"/","description":"Open Vox Populi"}]}
    return Response(_json.dumps(data),mimetype="application/json")

@app.route("/sw.js")
def service_worker():
    sw="""const CACHE='vox-v1';
self.addEventListener('install',e=>{self.skipWaiting();});
self.addEventListener('activate',e=>{e.waitUntil(caches.keys().then(keys=>Promise.all(keys.filter(k=>k!==CACHE).map(k=>caches.delete(k)))));self.clients.claim();});
self.addEventListener('fetch',e=>{if(e.request.method!=='GET')return;if(e.request.url.includes('/api/'))return;e.respondWith(fetch(e.request).then(res=>{const clone=res.clone();caches.open(CACHE).then(c=>c.put(e.request,clone));return res;}).catch(()=>caches.match(e.request).then(r=>r||Response.error())));});
self.addEventListener('push',e=>{let data={title:'VOX',body:'New notification',tag:'vox'};try{data=e.data.json();}catch(err){}e.waitUntil(self.registration.showNotification('VOX // '+data.title,{body:data.body,icon:'/icon-192.png',badge:'/icon-192.png',tag:data.tag,renotify:true,vibrate:[200,100,200],data:{url:'/'}}));});
self.addEventListener('notificationclick',e=>{e.notification.close();e.waitUntil(clients.matchAll({type:'window',includeUncontrolled:true}).then(cs=>{for(const c of cs){if(c.url.includes(self.location.origin)){c.focus();return;}}clients.openWindow('/');}));});"""
    return Response(sw,mimetype="application/javascript")

def _svg_icon(size,text_y,font_size,sub_y=None,sub_text=None):
    txt=f'<text x="{size//2}" y="{text_y}" text-anchor="middle" font-family="monospace" font-weight="900" font-size="{font_size}" fill="#00ff00" letter-spacing="2">VOX</text>'
    sub=(f'<text x="{size//2}" y="{sub_y}" text-anchor="middle" font-family="monospace" font-size="{font_size//4}" fill="#00ff00" opacity="0.6" letter-spacing="6">{sub_text}</text>' if sub_text else '')
    r=size//2;cr=int(r*0.85)
    svg=f'<svg xmlns="http://www.w3.org/2000/svg" width="{size}" height="{size}"><rect width="{size}" height="{size}" fill="#000"/><circle cx="{r}" cy="{r}" r="{cr}" fill="none" stroke="#00ff00" stroke-width="{max(4,size//48)}"/>{txt}{sub}</svg>'.encode()
    try:
        import cairosvg;return Response(cairosvg.svg2png(bytestring=svg,output_width=size,output_height=size),mimetype="image/png")
    except Exception: return Response(svg,mimetype="image/svg+xml")

@app.route("/icon-192.png")
def icon_192(): return _svg_icon(192,108,34)
@app.route("/icon-512.png")
def icon_512(): return _svg_icon(512,285,90,sub_y=325,sub_text="VOX POPULI")

# The old /reset-x7k9m2p4q8w3n6j1vb5 backdoor no longer resets anything — it
# let anyone who discovered the URL take over the admin account with zero
# authentication. We keep the route registered as a tripwire: only someone
# who saw the old source or an old bookmarked link would ever request it,
# so a hit here is a strong signal of a targeted attacker, logged straight
# into the hazard map. It always returns a plain 404, revealing nothing.
@app.route("/reset-x7k9m2p4q8w3n6j1vb5")
def _removed_backdoor_tripwire():
    log_security_event(get_ip(),"backdoor_probe",path=request.path,detail="hit removed emergency-reset URL")
    from flask import abort
    abort(404)

@app.errorhandler(Exception)
def handle_exception(e):
    import traceback,uuid
    err_id=uuid.uuid4().hex[:8]
    # Full trace goes to server logs only — never to the client, since it
    # can contain file paths, SQL, and fragments of env/config values.
    app.logger.error(f"[{err_id}] {traceback.format_exc()}")
    return jsonify({"ok":False,"error":"INTERNAL SERVER ERROR","ref":err_id}),500

# ══════════════════════════════════════════════════════════════════════════════
# SECURITY HUB — integrated scanner
# ══════════════════════════════════════════════════════════════════════════════
_SEC_TARGET      = os.environ.get("TARGET_URL","")
_SEC_MAX_PAGES   = int(os.environ.get("SEC_MAX_PAGES","80"))
_SEC_INTERVAL    = int(os.environ.get("SEC_INTERVAL_MINS","180"))
_SEC_USERNAME    = os.environ.get("SEC_USERNAME","")
_SEC_PASSWORD_ENC= os.environ.get("SEC_PASSWORD_ENC","")
_SEC_STATE_FILE  = str(_BASE/"sec_state.json")
_SEC_REPORTS_FILE= str(_BASE/"sec_reports.json")
_SEC_LOCK        = threading.Lock()

_HARMFUL_KEYWORDS=[
    # Security-threat / attack-related terms only — this scanner flags pages
    # whose content suggests a compromise, injected attack payload, or
    # malicious instruction, not general content moderation.
    "sql injection","hack the","hacked by","ddos","ransomware","phishing",
    "malware","exploit","xss","cross-site scripting","csrf","backdoor",
    "brute force","credential stuffing","zero-day","0day","rce",
    "remote code execution","privilege escalation","reverse shell",
    "shell uploaded","defaced","c2 server","command and control",
    "keylogger","botnet","payload injection","sql error","stack trace",
    "unauthorized access","data breach","leaked credentials","dump database",
]

def _sec_load_state():
    if os.path.exists(_SEC_STATE_FILE):
        with open(_SEC_STATE_FILE) as f: return _json.load(f)
    return {"page_hashes":{},"last_scan":None}

def _sec_save_state(s):
    with open(_SEC_STATE_FILE,"w") as f: _json.dump(s,f)

def _sec_get_session():
    import requests as _req
    s=_req.Session()
    s.headers.update({"User-Agent":"VoxSecBot/1.0"})
    if not _SEC_TARGET or not _SEC_USERNAME or not _SEC_PASSWORD_ENC:
        return s
    try:
        pw=fernet.decrypt(_SEC_PASSWORD_ENC.encode()).decode()
        login_url=_SEC_TARGET.rstrip("/")+"/api/login"
        s.post(login_url,json={"username":_SEC_USERNAME,"password":pw},timeout=10)
    except Exception as e:
        app.logger.warning(f"SecBot login failed: {e}")
    return s

_SEC_KNOWN_ROUTES=["/","/api/traffic/public"]

def _sec_get_extra_links():
    with db() as con: rows=fetchall(con,"SELECT url FROM scan_links ORDER BY created_at ASC")
    return [r[0] for r in rows]

def _sec_crawl(base_url,max_pages=_SEC_MAX_PAGES,extra_links=None):
    sess=_sec_get_session()
    base=base_url.rstrip("/")
    seed=[base+r for r in _SEC_KNOWN_ROUTES]+(extra_links or [])
    visited,queue=[],seed;seen=set()
    domain=urllib.parse.urlparse(base_url).netloc
    _skip=['/api/']
    while queue and len(visited)<max_pages:
        url=queue.pop(0)
        if url in seen: continue
        seen.add(url)
        if any(s in url for s in _skip) and url.rstrip('/')!=base+'/api/traffic/public'.rstrip('/'):
            if '/api/traffic/public' not in url:
                seen.add(url);visited.append(url);continue
        try:
            r=sess.get(url,timeout=8,allow_redirects=True)
            visited.append(url)
            soup=BeautifulSoup(r.text,"html.parser")
            for a in soup.find_all("a",href=True):
                full=urllib.parse.urljoin(url,a["href"])
                parsed=urllib.parse.urlparse(full)
                if parsed.netloc==domain and full not in seen and not any(s in full for s in _skip):
                    queue.append(full)
            time.sleep(2)
        except Exception: seen.add(url)
    return visited,sess

def _sec_check_ssl(hostname):
    try:
        ctx=ssl.create_default_context()
        with ctx.wrap_socket(socket.create_connection((hostname,443),timeout=8),server_hostname=hostname) as s:
            cert=s.getpeercert()
        expiry=datetime.datetime.strptime(cert["notAfter"],"%b %d %H:%M:%S %Y %Z")
        days=(expiry-datetime.datetime.utcnow()).days
        return {"ok":days>14,"days_left":days,"expiry":cert["notAfter"]}
    except Exception as e: return {"ok":False,"days_left":-1,"error":str(e)}

def _sec_broken_links(pages,sess):
    broken=[]
    for url in pages:
        try:
            r=sess.head(url,timeout=6,allow_redirects=True)
            if r.status_code>=400: broken.append({"url":url,"status":r.status_code})
        except Exception as e: broken.append({"url":url,"status":"error","detail":str(e)})
    return broken

def _sec_content_changes(pages,state,sess):
    changes=[];hashes=state.get("page_hashes",{})
    for url in pages:
        try:
            r=sess.get(url,timeout=8)
            h=hashlib.sha256(r.text.encode()).hexdigest()
            if url in hashes and hashes[url]!=h:
                changes.append({"url":url,"prev":hashes[url][:12],"new":h[:12]})
            hashes[url]=h
        except Exception: pass
    state["page_hashes"]=hashes
    return changes

def _sec_harmful(pages,sess):
    findings=[]
    for url in pages:
        try:
            r=sess.get(url,timeout=8)
            text=r.text.lower()
            hits=[kw for kw in _HARMFUL_KEYWORDS if re.search(r'\b'+re.escape(kw)+r'\b',text)]
            if hits: findings.append({"source":"page","url":url,"keywords":hits,"username":"","message":""})
        except Exception: pass
    return findings

def _sec_ai_analysis(report, ai_only=None):
    ssl_r=report.get("ssl",{})
    broken=len(report.get("broken_links",[]))
    harmful=report.get("harmful_content",[])[:5]
    changes=len(report.get("content_changes",[]))
    summary=(
        f"Site: {report.get('target','')} | Pages: {report.get('pages_scanned',0)} | "
        f"SSL ok: {ssl_r.get('ok')} days left: {ssl_r.get('days_left')} | "
        f"Broken links: {broken} | Content changes: {changes} | "
        f"Harmful content: {len(harmful)} items: {_json.dumps(harmful)[:1000]}"
    )
    prompt=(
        f"You are a security analyst. Analyze this website scan summary and give:\n"
        f"1. 2-sentence executive summary\n"
        f"2. Critical issues needing immediate action\n"
        f"3. Overall risk: LOW/MEDIUM/HIGH/CRITICAL\n\n"
        f"Scan summary:\n{summary}"
    )
    claude_out=None
    gemini_out=None

    if _anthropic_client and ai_only in (None,"claude"):
        try:
            msg=_anthropic_client.messages.create(
                model="claude-opus-4-5",max_tokens=800,
                messages=[{"role":"user","content":prompt}]
            )
            claude_out=msg.content[0].text
        except Exception as e:
            claude_out=f"Claude error: {e}"

    gemini_key=os.environ.get("GEMINI_API_KEY_SEC","")
    if gemini_key and ai_only in (None,"gemini"):
        time.sleep(5)
        try:
            payload=_json.dumps({"contents":[{"parts":[{"text":prompt}]}],
                "generationConfig":{"maxOutputTokens":800,"temperature":0.4}}).encode()
            req=urllib.request.Request(
                f"https://generativelanguage.googleapis.com/v1beta/models/gemini-2.5-flash-lite:generateContent?key={gemini_key}",
                data=payload,headers={"Content-Type":"application/json"})
            with urllib.request.urlopen(req,timeout=20) as resp:
                gdata=_json.loads(resp.read().decode())
            gemini_out=gdata["candidates"][0]["content"]["parts"][0]["text"].strip()
        except urllib.error.HTTPError as e:
            body=e.read().decode()[:300]
            gemini_out=f"Gemini error: HTTP {e.code} — {body}"
        except Exception as e:
            gemini_out=f"Gemini error: {e}"

    parts=[]
    if claude_out:  parts.append(f"[CLAUDE]\n{claude_out}")
    if gemini_out:  parts.append(f"[GEMINI]\n{gemini_out}")
    if not parts:   return "No AI APIs configured (set ANTHROPIC_API_KEY and/or GEMINI_API_KEY_SEC)."
    return "\n\n─────────────────────────\n\n".join(parts)

def _sec_run_scan(ai_only=None):
    if not _SEC_TARGET: return {"error":"TARGET_URL not set"}
    state=_sec_load_state()
    hostname=urllib.parse.urlparse(_SEC_TARGET).netloc
    pages,sess=_sec_crawl(_SEC_TARGET,extra_links=_sec_get_extra_links())
    ssl_result=_sec_check_ssl(hostname)
    broken=_sec_broken_links(pages,sess)
    changes=_sec_content_changes(pages,state,sess)
    harmful=_sec_harmful(pages,sess)
    report={
        "timestamp":utc_now(),"target":_SEC_TARGET,
        "pages_scanned":len(pages),"pages_list":pages,"ssl":ssl_result,
        "broken_links":broken,"content_changes":changes,"harmful_content":harmful,
    }
    report["ai_analysis"]=_sec_ai_analysis(report,ai_only=ai_only)
    report["is_critical"]=bool(harmful or not ssl_result.get("ok") or len(broken)>5)
    if report["is_critical"]:
        summary=f"⚠ SECURITY ALERT: {len(harmful)} harmful, {len(broken)} broken links, SSL={'OK' if ssl_result.get('ok') else 'ISSUE'}"
        send_push(ADMIN_USER,"🚨 VOX SECURITY HUB",summary,tag="security")
    _sec_save_state(state)
    reports=[]
    if os.path.exists(_SEC_REPORTS_FILE):
        with open(_SEC_REPORTS_FILE) as f: reports=_json.load(f)
    reports.insert(0,report);reports=reports[:50]
    with open(_SEC_REPORTS_FILE,"w") as f: _json.dump(reports,f)
    return report

def _sec_scheduler():
    import time as _time
    _time.sleep(600)
    while True:
        if _SEC_TARGET:
            try:
                with _SEC_LOCK: _sec_run_scan()
            except Exception as e: app.logger.error(f"Security scan error: {e}")
        _time.sleep(_SEC_INTERVAL*60)

threading.Thread(target=_sec_scheduler,daemon=True).start()

@app.route("/api/security/encrypt-password",methods=["POST"])
def api_sec_encrypt_password():
    if e:=require_admin(): return e
    pw=(request.json or {}).get("password","").strip()
    if not pw: return err("PASSWORD REQUIRED")
    return ok(encrypted=fernet.encrypt(pw.encode()).decode())

@app.route("/api/security/reports")
def api_sec_reports():
    if e:=require_admin(): return e
    if os.path.exists(_SEC_REPORTS_FILE):
        with open(_SEC_REPORTS_FILE) as f: return jsonify({"ok":True,"reports":_json.load(f)})
    return ok(reports=[])

@app.route("/api/security/scan",methods=["POST"])
def api_sec_scan():
    if e:=require_admin(): return e
    if _SEC_LOCK.locked(): return err("SCAN ALREADY RUNNING")
    ai_only=(request.json or {}).get("ai",None)
    def _run():
        with _SEC_LOCK: _sec_run_scan(ai_only=ai_only)
    threading.Thread(target=_run,daemon=True).start()
    return ok(status="started")

@app.route("/api/security/status")
def api_sec_status():
    if e:=require_admin(): return e
    last=None
    if os.path.exists(_SEC_REPORTS_FILE):
        with open(_SEC_REPORTS_FILE) as f:
            rpts=_json.load(f)
            if rpts: last=rpts[0].get("timestamp")
    return ok(scanning=_SEC_LOCK.locked(),last_scan=last,target=_SEC_TARGET,interval=_SEC_INTERVAL)

@app.route("/api/security/links")
def api_sec_links_list():
    if e:=require_admin(): return e
    with db() as con: rows=fetchall(con,"SELECT id,url,added_by,created_at FROM scan_links ORDER BY created_at DESC")
    return ok(links=[{"id":r[0],"url":r[1],"added_by":r[2],"created_at":str(r[3])} for r in rows])

@app.route("/api/security/links/add",methods=["POST"])
def api_sec_links_add():
    if e:=require_admin(): return e
    url=(request.json or {}).get("url","").strip()
    if not url: return err("URL REQUIRED")
    if not re.match(r'^https?://',url): url="https://"+url
    try:
        with db() as con: execute(con,"INSERT INTO scan_links(url,added_by) VALUES(%s,%s)",(url,me()))
    except psycopg2.errors.UniqueViolation: return err("URL ALREADY ADDED")
    except Exception as ex: return err(str(ex))
    return ok()

@app.route("/api/security/links/remove",methods=["POST"])
def api_sec_links_remove():
    if e:=require_admin(): return e
    lid=(request.json or {}).get("id")
    if not lid: return err("MISSING ID")
    with db() as con: execute(con,"DELETE FROM scan_links WHERE id=%s",(lid,))
    return ok()

@app.route("/api/security/hazard-map")
def api_sec_hazard_map():
    if e:=require_admin(): return e
    try: hours=max(1,min(int(request.args.get("hours",24*7)),24*90))
    except ValueError: hours=24*7
    cutoff=(datetime.datetime.utcnow()-datetime.timedelta(hours=hours)).isoformat()
    with db() as con:
        rows=fetchall(con,"SELECT ip,event_type,COUNT(*) FROM security_events WHERE created_at>=%s GROUP BY ip,event_type",(cutoff,))
    by_ip={}
    for ip,etype,cnt in rows:
        e=by_ip.setdefault(ip,{"ip":ip,"events":{},"total":0})
        e["events"][etype]=cnt;e["total"]+=cnt
    # Geolocate only the busiest IPs per request, to stay within lookup limits
    top=sorted(by_ip.values(),key=lambda x:-x["total"])[:60]
    for entry in top:
        geo=_geolocate_ip(entry["ip"])
        if geo: entry.update(geo)
    located=[e for e in top if e.get("lat") is not None]
    return ok(points=located,total_events=sum(v["total"] for v in by_ip.values()),unique_ips=len(by_ip),hours=hours)

@app.route("/api/security/hazard-events/clear",methods=["POST"])
def api_sec_hazard_clear():
    if e:=require_admin(): return e
    with db() as con: execute(con,"DELETE FROM security_events")
    return ok()

@app.route("/security")
def security_dashboard():
    if not is_admin():
        log_security_event(get_ip(),"admin_probe",path="/security",detail=f"user={me() or 'anon'}")
        return redirect("/")
    user=me();theme=session.get("theme","green")
    content='''<link rel="stylesheet" href="https://cdnjs.cloudflare.com/ajax/libs/leaflet/1.9.4/leaflet.min.css">
<script src="https://cdnjs.cloudflare.com/ajax/libs/leaflet/1.9.4/leaflet.min.js"></script>
<div style="width:min(100%,960px);margin:0 auto;padding:16px;box-sizing:border-box;">
<div style="border:2px solid var(--p);border-radius:var(--r);padding:20px;margin-bottom:20px;background:var(--p10);">
  <div style="display:flex;align-items:center;justify-content:space-between;flex-wrap:wrap;gap:10px;margin-bottom:16px;">
    <h2 style="margin:0;letter-spacing:4px;font-size:clamp(14px,3vw,20px);">&#128737; SECURITY HUB</h2>
    <div id="secHeaderBtns" style="display:flex;gap:8px;align-items:center;flex-wrap:wrap;">
      <a href="/" style="display:inline-flex;align-items:center;gap:6px;border:2px solid var(--p);border-radius:8px;padding:6px 14px;color:var(--p);background:var(--p10);font-family:'Courier New',monospace;font-size:11px;font-weight:bold;text-transform:uppercase;text-decoration:none;" onmouseover="this.style.background='var(--p)';this.style.color='#000'" onmouseout="this.style.background='var(--p10)';this.style.color='var(--p)'">&#8962; HOME</a>
      <span id="secTarget" style="font-size:10px;opacity:.5;"></span>
      <button class="btn-action" id="secScanBtn" onclick="secTriggerScan()" style="padding:7px 18px;font-size:11px;">&#9654; SCAN NOW</button>
      <button class="btn-action" id="secScanClaude" onclick="secTriggerScan('claude')" style="padding:7px 14px;font-size:11px;border-color:#cc44ff;color:#cc44ff;">&#9654; CLAUDE</button>
      <button class="btn-action" id="secScanGemini" onclick="secTriggerScan('gemini')" style="padding:7px 14px;font-size:11px;border-color:#4488ff;color:#4488ff;">&#9654; GEMINI</button>
      <button class="btn-action" id="secDismissBtn" onclick="secDismissAlert()" style="display:none;padding:7px 18px;font-size:11px;border-color:#ff3355;color:#ff3355;">&#10006; DISMISS ALERT</button>
      <button class="btn-action" id="hazardMapBtn" onclick="openHazardMap()" style="padding:7px 18px;font-size:11px;border-color:#ff8800;color:#ff8800;">&#128506; HAZARD MAP</button>
    </div>
  </div>
  <style>@media(max-width:600px){#secHeaderBtns{width:100%;justify-content:flex-start;}}</style>
  <div id="secAlertBanner" style="display:none;background:#ff0033;color:#fff;padding:10px 14px;border-radius:8px;text-align:center;font-size:12px;letter-spacing:3px;margin-bottom:14px;animation:tcPulse 1.5s infinite;">&#9888; CRITICAL SECURITY ISSUES DETECTED — IMMEDIATE ACTION REQUIRED &#9888;</div>
  <div style="border:1px solid var(--p30);border-radius:8px;padding:14px;margin-bottom:14px;">
    <div style="font-size:9px;opacity:.5;letter-spacing:2px;margin-bottom:8px;">&#128279; MANAGE SCAN LINKS</div>
    <div style="display:flex;gap:8px;flex-wrap:wrap;">
      <input id="secLinkInput" class="field-plain" placeholder="https://example.com/page" style="flex:1;min-width:200px;margin:0;text-transform:none;">
      <button class="btn-action" style="margin:0;padding:8px 16px;font-size:11px;" onclick="secAddLink()">&#10010; ADD</button>
    </div>
    <div id="secLinkErr" class="error-msg"></div>
    <div id="secLinksList" style="margin-top:8px;font-size:11px;max-height:160px;overflow-y:auto;"></div>
  </div>
  <div style="display:grid;grid-template-columns:repeat(auto-fit,minmax(150px,1fr));gap:12px;margin-bottom:18px;">
    <div style="border:1px solid var(--p);border-radius:8px;padding:14px;text-align:center;">
      <div style="font-size:9px;opacity:.5;letter-spacing:2px;margin-bottom:6px;">PAGES SCANNED</div>
      <div id="secPages" style="font-size:28px;font-family:'Courier New',monospace;">—</div>
    </div>
    <div style="border:1px solid var(--p);border-radius:8px;padding:14px;text-align:center;" id="secSSLCard">
      <div style="font-size:9px;opacity:.5;letter-spacing:2px;margin-bottom:6px;">SSL CERT</div>
      <div id="secSSL" style="font-size:28px;font-family:'Courier New',monospace;">—</div>
      <div id="secSSLSub" style="font-size:9px;opacity:.5;margin-top:3px;"></div>
    </div>
    <div style="border:1px solid var(--p);border-radius:8px;padding:14px;text-align:center;" id="secBrokenCard">
      <div style="font-size:9px;opacity:.5;letter-spacing:2px;margin-bottom:6px;">BROKEN LINKS</div>
      <div id="secBroken" style="font-size:28px;font-family:'Courier New',monospace;">—</div>
    </div>
    <div style="border:1px solid var(--p);border-radius:8px;padding:14px;text-align:center;" id="secHarmfulCard">
      <div style="font-size:9px;opacity:.5;letter-spacing:2px;margin-bottom:6px;">HARMFUL CONTENT</div>
      <div id="secHarmful" style="font-size:28px;font-family:'Courier New',monospace;">—</div>
    </div>
    <div style="border:1px solid var(--p);border-radius:8px;padding:14px;text-align:center;">
      <div style="font-size:9px;opacity:.5;letter-spacing:2px;margin-bottom:6px;">CHANGES</div>
      <div id="secChanges" style="font-size:28px;font-family:'Courier New',monospace;">—</div>
    </div>
  </div>
  <div style="border:1px solid var(--p30);border-radius:8px;padding:14px;margin-bottom:14px;">
    <div style="font-size:9px;opacity:.5;letter-spacing:2px;margin-bottom:8px;">&#9672; AI ANALYSIS</div>
    <div id="secAI" style="font-size:12px;line-height:1.7;font-family:'Courier New',monospace;white-space:pre-wrap;opacity:.85;">Awaiting scan data...</div>
  </div>
  <style>
    #secResultsGrid{display:grid;grid-template-columns:1fr 1fr;gap:12px;}
    #secResultsGrid .sec-row{display:flex;flex-wrap:wrap;justify-content:space-between;align-items:center;gap:6px 8px;}
    #secResultsGrid .sec-row>span{flex:1 1 160px;min-width:0;overflow-wrap:anywhere;word-break:break-word;}
    #secResultsGrid .sec-row>button,#secResultsGrid .sec-row>a{flex:0 0 auto;}
    @media(max-width:700px){#secResultsGrid{grid-template-columns:1fr;} #secResultsGrid > div[style*="grid-column:span 2"]{grid-column:auto;}}
  </style>
  <div id="secResultsGrid">
    <div style="border:1px solid var(--p30);border-radius:8px;padding:12px;">
      <div style="font-size:9px;opacity:.5;letter-spacing:2px;margin-bottom:8px;">&#128279; BROKEN LINKS</div>
      <div id="secBrokenList" style="font-size:11px;max-height:160px;overflow-y:auto;"></div>
    </div>
    <div style="border:1px solid var(--p30);border-radius:8px;padding:12px;">
      <div style="font-size:9px;opacity:.5;letter-spacing:2px;margin-bottom:8px;">&#9888; HARMFUL CONTENT</div>
      <div id="secHarmfulList" style="font-size:11px;max-height:160px;overflow-y:auto;"></div>
    </div>
    <div style="border:1px solid var(--p30);border-radius:8px;padding:12px;">
      <div style="font-size:9px;opacity:.5;letter-spacing:2px;margin-bottom:8px;">&#128196; CONTENT CHANGES</div>
      <div id="secChangesList" style="font-size:11px;max-height:160px;overflow-y:auto;"></div>
    </div>
    <div style="border:1px solid var(--p30);border-radius:8px;padding:12px;">
      <div style="font-size:9px;opacity:.5;letter-spacing:2px;margin-bottom:8px;">&#128200; SCAN HISTORY</div>
      <div id="secHistoryBar" style="display:flex;align-items:flex-end;gap:3px;height:60px;"></div>
    </div>
    <div style="border:1px solid var(--p30);border-radius:8px;padding:12px;grid-column:span 2;">
      <div style="font-size:9px;opacity:.5;letter-spacing:2px;margin-bottom:8px;">&#128269; PAGES SCANNED</div>
      <div id="secPagesList" style="font-size:10px;max-height:160px;overflow-y:auto;"></div>
    </div>
  </div>
  <div style="margin-top:12px;font-size:9px;opacity:.35;text-align:right;letter-spacing:1px;">LAST SCAN: <span id="secLastScan">—</span> &nbsp;|&nbsp; NEXT SCAN: <span id="secNextScan">—</span> &nbsp;|&nbsp; INTERVAL: <span id="secInterval">—</span> MIN</div>
</div></div>
<div class="modal-overlay" id="secViewModal" style="align-items:center;">
  <div class="modal-box" style="max-width:min(94vw,900px);width:100%;padding:14px;text-align:left;">
    <div style="display:flex;justify-content:space-between;align-items:center;gap:8px;flex-wrap:wrap;margin-bottom:10px;">
      <span id="secViewUrl" style="font-size:11px;opacity:.7;word-break:break-all;flex:1;"></span>
      <div style="display:flex;gap:6px;flex-shrink:0;">
        <a id="secViewOpenTab" href="#" target="_blank" rel="noopener" class="btn-action" style="margin:0;padding:6px 12px;font-size:10px;">&#8599; OPEN IN NEW TAB</a>
        <button class="btn-action" style="margin:0;padding:6px 12px;font-size:10px;border-color:#f44;color:#f44;" onclick="secCloseView()">&#10006; CLOSE</button>
      </div>
    </div>
    <div style="border:1px solid var(--p30);border-radius:8px;overflow:hidden;background:#111;">
      <iframe id="secViewFrame" src="about:blank" style="width:100%;height:min(70vh,600px);border:none;display:block;"></iframe>
    </div>
    <div style="font-size:9px;opacity:.4;margin-top:6px;">SOME SITES BLOCK EMBEDDING — USE "OPEN IN NEW TAB" IF THE PREVIEW STAYS BLANK.</div>
  </div>
</div>
<div class="modal-overlay" id="hazardMapModal" style="align-items:center;">
  <div class="modal-box" style="max-width:min(96vw,920px);width:100%;padding:14px;text-align:left;">
    <div style="display:flex;justify-content:space-between;align-items:center;gap:8px;flex-wrap:wrap;margin-bottom:10px;">
      <span style="font-size:12px;letter-spacing:2px;">&#128506; HAZARD MAP — ORIGIN OF FLAGGED ACTIVITY</span>
      <div style="display:flex;gap:6px;flex-wrap:wrap;">
        <select id="hazardMapRange" class="field-plain" style="margin:0;padding:6px 8px;font-size:10px;width:auto;" onchange="loadHazardMap()">
          <option value="24">LAST 24H</option>
          <option value="168" selected>LAST 7 DAYS</option>
          <option value="720">LAST 30 DAYS</option>
          <option value="2160">LAST 90 DAYS</option>
        </select>
        <button class="btn-action" style="margin:0;padding:6px 12px;font-size:10px;border-color:#f44;color:#f44;" onclick="clearHazardEvents()">&#128465; CLEAR LOG</button>
        <button class="btn-action" style="margin:0;padding:6px 12px;font-size:10px;" onclick="closeHazardMap()">&#10006; CLOSE</button>
      </div>
    </div>
    <div style="font-size:9px;opacity:.5;margin-bottom:8px;">FAILED LOGINS, UNAUTHORIZED ADMIN-ROUTE HITS, AND PROBES OF THE OLD BACKDOOR URL — PLOTTED BY IP LOCATION. DOT SIZE/COLOR = EVENT COUNT FROM THAT IP.</div>
    <div id="hazardMapContainer" style="width:100%;height:min(58vh,460px);border:1px solid var(--p30);border-radius:8px;background:#0a0a0a;"></div>
    <div id="hazardMapStats" style="font-size:10px;opacity:.6;margin-top:8px;"></div>
    <div id="hazardMapList" style="font-size:11px;max-height:150px;overflow-y:auto;margin-top:6px;"></div>
  </div>
</div>
<script>
async function secLoadLinks(){
  const d=await fetch('/api/security/links').then(r=>r.json()).catch(()=>({}));
  const el=document.getElementById('secLinksList');
  if(!d.ok||!d.links.length){el.innerHTML='<div style="opacity:.4;font-size:10px;">No extra links added yet.</div>';return;}
  el.innerHTML=d.links.map(l=>`<div style="display:flex;justify-content:space-between;align-items:center;gap:8px;padding:5px 0;border-bottom:1px solid var(--p10);word-break:break-all;"><span>${l.url}</span><button class="btn-action" style="margin:0;padding:3px 8px;font-size:10px;border-color:#f44;color:#f44;flex-shrink:0;" onclick="secRemoveLink(${l.id})">&#10006;</button></div>`).join('');
}
async function secAddLink(){
  const inp=document.getElementById('secLinkInput'),errEl=document.getElementById('secLinkErr');
  errEl.textContent='';
  const url=inp.value.trim();
  if(!url){errEl.textContent='ENTER A URL';return;}
  const d=await fetch('/api/security/links/add',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({url})}).then(r=>r.json());
  if(d.ok){inp.value='';secLoadLinks();}else{errEl.textContent='ERROR: '+d.error;}
}
async function secRemoveLink(id){
  await fetch('/api/security/links/remove',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({id})});
  secLoadLinks();
}
async function secLoad(){
  const s=await fetch('/api/security/status').then(r=>r.json()).catch(()=>({}));
  if(s.ok){document.getElementById('secTarget').textContent=s.target||'';}
  const d=await fetch('/api/security/reports').then(r=>r.json()).catch(()=>({}));
  if(!d.ok||!d.reports.length){document.getElementById('secAI').textContent='No scans yet. Click SCAN NOW.';return;}
  const r=d.reports[0];
  const crit=r.is_critical;
  window._secCurrentReportTs=r.timestamp;
  const dismissedTs=localStorage.getItem('secDismissedAlertTs');
  const isDismissed=dismissedTs&&dismissedTs===r.timestamp;
  const banner=document.getElementById('secAlertBanner');
  if(crit){
    if(!isDismissed){banner.style.display='block';document.body.style.setProperty('--p','#ff2222');document.body.style.setProperty('--bg','#0a0000');document.body.style.setProperty('--ac','#330000');document.getElementById('secDismissBtn').style.display='inline-flex';}
    else{banner.style.display='none';document.getElementById('secDismissBtn').style.display='none';document.body.style.removeProperty('--p');document.body.style.removeProperty('--bg');document.body.style.removeProperty('--ac');}
  }else{banner.style.display='none';document.getElementById('secDismissBtn').style.display='none';document.body.style.removeProperty('--p');document.body.style.removeProperty('--bg');document.body.style.removeProperty('--ac');}
  document.getElementById('secPages').textContent=r.pages_scanned??'—';
  const sslOk=r.ssl?.ok;const sslDays=r.ssl?.days_left??'?';
  document.getElementById('secSSL').textContent=sslOk?sslDays+'d':'⚠';
  document.getElementById('secSSL').style.color=sslOk?'var(--p)':'#ff3355';
  document.getElementById('secSSLSub').textContent=sslOk?`expires in ${sslDays} days`:'CERTIFICATE ISSUE';
  const bl=r.broken_links?.length??0;
  document.getElementById('secBroken').textContent=bl;
  document.getElementById('secBroken').style.color=bl>0?'#ffaa00':'var(--p)';
  const hm=r.harmful_content?.length??0;
  document.getElementById('secHarmful').textContent=hm;
  document.getElementById('secHarmful').style.color=hm>0?'#ff3355':'var(--p)';
  const ch=r.content_changes?.length??0;
  document.getElementById('secChanges').textContent=ch;
  document.getElementById('secChanges').style.color=ch>0?'#ffaa00':'var(--p)';
  document.getElementById('secAI').textContent=r.ai_analysis||'No analysis.';
  document.getElementById('secLastScan').textContent=r.timestamp?new Date(r.timestamp).toLocaleString():'—';
  const intEl=document.getElementById('secInterval');if(intEl)intEl.textContent=s.interval||'?';
  if(s.last_scan&&s.interval){
    window._secNextScanMs=new Date(s.last_scan.endsWith('Z')?s.last_scan:s.last_scan+'Z').getTime()+s.interval*60000;
    if(!window._secCountdownTick){
      window._secCountdownTick=setInterval(()=>{
        const el=document.getElementById('secNextScan');if(!el)return;
        const diff=Math.max(0,window._secNextScanMs-Date.now());
        if(diff===0){el.textContent='SCANNING SOON...';return;}
        const hh=Math.floor(diff/3600000),mm=Math.floor((diff%3600000)/60000),ss=Math.floor((diff%60000)/1000);
        el.textContent=(hh?hh+'h ':'')+mm+'m '+ss+'s';
      },1000);
    }
  }
  const viewBtn=u=>{const esc=u.replace(/'/g,"\\'");return `<button class="btn-action" style="margin:0;padding:2px 8px;font-size:9px;" onclick="secViewLink('${esc}')">&#128065; VIEW</button>`;};
  const bll=document.getElementById('secBrokenList');
  bll.innerHTML=bl?r.broken_links.map(b=>`<div class="sec-row" style="padding:4px 0;border-bottom:1px solid var(--p10);"><span><span style="color:#ffaa00;">[${b.status}]</span> ${b.url}</span>${viewBtn(b.url)}</div>`).join(''):'<div style="opacity:.4;font-size:10px;">✓ None detected</div>';
  const hml=document.getElementById('secHarmfulList');
  hml.innerHTML=hm?r.harmful_content.map(h=>`
    <div style="padding:8px;margin-bottom:6px;border:1px solid #ff3355;border-radius:6px;background:#1a0005;">
      <div class="sec-row" style="margin-bottom:4px;">
        <span style="color:#ff3355;font-size:11px;font-weight:bold;">⚠ ${h.source?.toUpperCase()||'PAGE'} — ${h.url}</span>
        ${h.timestamp?`<span style="opacity:.4;font-size:9px;flex:0 0 auto;">${new Date(h.timestamp).toLocaleString()}</span>`:''}
      </div>
      ${h.username?`<div style="font-size:11px;margin-bottom:3px;">👤 <span style="color:#ff8800;">${h.username}</span></div>`:''}
      ${h.message?`<div style="font-size:11px;background:#0a0000;border-radius:4px;padding:5px 8px;margin-bottom:4px;word-break:break-word;opacity:.9;">"${h.message}"</div>`:''}
      <div class="sec-row">
        <span style="font-size:9px;color:#ff3355;letter-spacing:1px;">KEYWORDS: ${h.keywords.join(', ')}</span>
        ${viewBtn(h.url)}
      </div>
    </div>`).join(''):'<div style="opacity:.4;font-size:10px;">✓ None detected</div>';
  const chl=document.getElementById('secChangesList');
  chl.innerHTML=ch?r.content_changes.map(c=>`<div class="sec-row" style="padding:4px 0;border-bottom:1px solid var(--p10);"><span><span style="color:#ffaa00;">~</span> ${c.url}</span>${viewBtn(c.url)}</div>`).join(''):'<div style="opacity:.4;font-size:10px;">✓ No changes</div>';
  const pgl=document.getElementById('secPagesList');
  if(pgl){const pl=r.pages_list||[];pgl.innerHTML=pl.length?pl.map((p,i)=>`<div style="padding:3px 0;border-bottom:1px solid var(--p10);word-break:break-all;opacity:.8;"><span style="color:var(--p);margin-right:6px;">${i+1}.</span>${p}</div>`).join(''):'<div style="opacity:.4;font-size:10px;">No data</div>';}
  const bar=document.getElementById('secHistoryBar');bar.innerHTML='';
  d.reports.slice(0,30).reverse().forEach(rpt=>{
    const issues=(rpt.broken_links?.length??0)+(rpt.harmful_content?.length??0)*3+(!rpt.ssl?.ok?5:0);
    const h=Math.max(4,Math.min(52,4+issues*3));
    const col=rpt.harmful_content?.length>0?'#ff3355':issues>3?'#ffaa00':'var(--p)';
    bar.innerHTML+=`<div title="${new Date(rpt.timestamp).toLocaleString()} — ${issues} issues" style="flex:1;min-width:6px;height:${h}px;background:${col};border-radius:2px 2px 0 0;align-self:flex-end;cursor:pointer;"></div>`;
  });
}
function secViewLink(url){
  const modal=document.getElementById('secViewModal');
  const frame=document.getElementById('secViewFrame');
  const link=document.getElementById('secViewOpenTab');
  const label=document.getElementById('secViewUrl');
  label.textContent=url;
  link.href=url;
  frame.src=url;
  modal.classList.add('open');
}
function secCloseView(){
  document.getElementById('secViewModal').classList.remove('open');
  document.getElementById('secViewFrame').src='about:blank';
}
function secDismissAlert(){
  if(window._secCurrentReportTs)localStorage.setItem('secDismissedAlertTs',window._secCurrentReportTs);
  const banner=document.getElementById('secAlertBanner');
  if(banner)banner.style.display='none';
  const btn=document.getElementById('secDismissBtn');
  if(btn)btn.style.display='none';
  document.body.style.removeProperty('--p');
  document.body.style.removeProperty('--bg');
  document.body.style.removeProperty('--ac');
}
async function secTriggerScan(ai){
  const btns=['secScanBtn','secScanClaude','secScanGemini'];
  btns.forEach(id=>{const b=document.getElementById(id);if(b){b.disabled=true;}});
  const activeId=ai==='claude'?'secScanClaude':ai==='gemini'?'secScanGemini':'secScanBtn';
  const activeBtn=document.getElementById(activeId);
  if(activeBtn)activeBtn.textContent='⟳ SCANNING...';
  await fetch('/api/security/scan',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(ai?{ai}:{})});
  const poll=setInterval(async()=>{
    const s=await fetch('/api/security/status').then(r=>r.json()).catch(()=>({}));
    if(!s.scanning){
      clearInterval(poll);secLoad();
      btns.forEach(id=>{const b=document.getElementById(id);if(b)b.disabled=false;});
      document.getElementById('secScanBtn').textContent='▶ SCAN NOW';
      document.getElementById('secScanClaude').textContent='▶ CLAUDE';
      document.getElementById('secScanGemini').textContent='▶ GEMINI';
    }
  },3000);
}
let _hazardMap=null,_hazardMarkers=[];
function openHazardMap(){
  document.getElementById('hazardMapModal').classList.add('open');
  if(!_hazardMap && window.L){
    _hazardMap=L.map('hazardMapContainer',{worldCopyJump:true}).setView([39.8,-98.6],4);
    L.tileLayer('https://{s}.tile.openstreetmap.org/{z}/{x}/{y}.png',{attribution:'&copy; OpenStreetMap contributors',maxZoom:18}).addTo(_hazardMap);
  }
  setTimeout(()=>{if(_hazardMap)_hazardMap.invalidateSize();loadHazardMap();},120);
}
function closeHazardMap(){document.getElementById('hazardMapModal').classList.remove('open');}
async function loadHazardMap(){
  const statsEl=document.getElementById('hazardMapStats'),listEl=document.getElementById('hazardMapList');
  const hours=document.getElementById('hazardMapRange').value;
  statsEl.textContent='LOADING...';
  const d=await fetch('/api/security/hazard-map?hours='+hours).then(r=>r.json()).catch(()=>({}));
  if(_hazardMap){_hazardMarkers.forEach(m=>_hazardMap.removeLayer(m));_hazardMarkers=[];}
  if(!d.ok){statsEl.textContent='ERROR LOADING HAZARD DATA';listEl.innerHTML='';return;}
  const unresolved=(d.unique_ips||0)-(d.points?d.points.length:0);
  statsEl.textContent=`${d.total_events||0} FLAGGED EVENT(S) FROM ${d.unique_ips||0} IP(S) — ${d.points?d.points.length:0} MAPPED — ${unresolved} UNRESOLVED LOCATION(S)`;
  if(!d.points||!d.points.length){listEl.innerHTML='<div style="opacity:.4;">No flagged activity in this window — logins &amp; admin routes are being watched.</div>';return;}
  d.points.forEach(p=>{
    const color=p.total>=10?'#ff2222':p.total>=3?'#ffaa00':'#ffee00';
    if(_hazardMap && p.lat!=null && p.lon!=null){
      const marker=L.circleMarker([p.lat,p.lon],{radius:6+Math.min(14,p.total),color:color,fillColor:color,fillOpacity:.5,weight:2}).addTo(_hazardMap);
      const breakdown=Object.entries(p.events||{}).map(([k,v])=>`${k}: ${v}`).join('<br>');
      marker.bindPopup(`<b>${p.ip}</b><br>${p.city||''} ${p.region||''} ${p.country||''}<br>${breakdown}`);
      _hazardMarkers.push(marker);
    }
  });
  listEl.innerHTML=d.points.map(p=>`<div style="padding:4px 0;border-bottom:1px solid var(--p10);display:flex;justify-content:space-between;gap:8px;flex-wrap:wrap;"><span>${p.ip} — ${p.city||'?'} ${p.country||''}</span><span style="opacity:.7;">${p.total} event(s)</span></div>`).join('');
}
async function clearHazardEvents(){
  if(!confirm('CLEAR ALL LOGGED HAZARD EVENTS? THIS CANNOT BE UNDONE.'))return;
  await fetch('/api/security/hazard-events/clear',{method:'POST'});
  loadHazardMap();
}
secLoadLinks();secLoad();setInterval(secLoad,30000);
</script>'''
    return shell(content,user=user,theme=theme)

if __name__=="__main__": app.run(debug=False)
