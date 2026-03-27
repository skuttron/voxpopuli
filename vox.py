# -*- coding: utf-8 -*-
from flask import Flask,request,session,redirect,jsonify,Response
import psycopg2,psycopg2.extras,os,hashlib,datetime,urllib.request,re,html as _html,pathlib,json as _json,time
from contextlib import contextmanager
from cryptography.fernet import Fernet
# ── Security Scanner ──────────────────────────────────────────────────────────
import ssl,socket,threading,urllib.parse
try:
    from bs4 import BeautifulSoup
except ImportError:
    BeautifulSoup=None
try:
    from anthropic import Anthropic as _Anthropic
    _anthropic_client=_Anthropic()
except Exception: _anthropic_client=None
_BASE=pathlib.Path(__file__).parent.resolve()
app=Flask(__name__)
app.secret_key=os.environ.get("SECRET_KEY","fallback-if-missing")
app.config['PERMANENT_SESSION_LIFETIME']=datetime.timedelta(days=90)
app.config['SESSION_PERMANENT']=True
# FIX 1: Added SameSite + HttpOnly so sessions persist properly across gunicorn workers
app.config['SESSION_COOKIE_SAMESITE']='Lax'
app.config['SESSION_COOKIE_HTTPONLY']=True
app.config['SESSION_COOKIE_SECURE']=True  # FIX 2: Required for HTTPS (Railway) — prevents session loss
def get_database_url():
    url=os.environ.get("DATABASE_URL","")
    if not url: raise RuntimeError("DATABASE_URL not set.")
    if url.startswith("postgres://"): url="postgresql://"+url[len("postgres://"):]
    if "sslmode" not in url: url+=("&" if "?" in url else "?")+"sslmode=require"
    return url
DATABASE_URL=get_database_url()
ADMIN_USER="Eagleone"
# FERNET_KEY must be set as a Railway env var so it survives redeploys.
# To generate one: python3 -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"
_KEY_FILE=str(_BASE/"secret.key")
_fernet_key=os.environ.get("FERNET_KEY","")
if _fernet_key:
    # Preferred: use stable env var key
    fernet=Fernet(_fernet_key.encode() if isinstance(_fernet_key,str) else _fernet_key)
else:
    # Fallback: file-based key (loses messages on redeploy — set FERNET_KEY to fix)
    if not os.path.exists(_KEY_FILE): open(_KEY_FILE,"wb").write(Fernet.generate_key())
    fernet=Fernet(open(_KEY_FILE,"rb").read())
    app.logger.warning("FERNET_KEY env var not set — using file key, messages will break on redeploy!")
VAPID_PUBLIC_KEY=os.environ.get("VAPID_PUBLIC_KEY","BAyH6Y_hbhzzmRgt3pd5Qa7guYKYKfsVCVIZsJGF0zYPfBupcKm24bduVIj4585JSjeeu3aeR19d4tBzlHgQIdU")
VAPID_PRIVATE_KEY=os.environ.get("VAPID_PRIVATE_KEY","MIGHAgEAMBMGByqGSM49AgEGCCqGSM49AwEHBG0wawIBAQQgOqLakrDhZhnH_KBh5nwx2l0jyGfOWplqyE82s4Ryws2hRANCAAQMh-mP4W4c85kYLd6XeUGu4LmCmCn7FQlSGbCRhdM2D3wbqXCptuG3blSI-OfOSUo3nrt2nkdfXeLQc5R4ECHV")
VAPID_CLAIMS={"sub":"mailto:admin@voxpopuli.app"}
hash_pw=lambda pw:hashlib.sha256(pw.encode()).hexdigest()
enc=lambda t:fernet.encrypt(t.encode()).decode()
def dec(t):
    if not t: return ""
    try: return fernet.decrypt(t.encode() if isinstance(t,str) else t).decode()
    except Exception: return "[ENCRYPTED]"
dec=dec
get_ip=lambda:request.headers.get("X-Forwarded-For",request.remote_addr).split(",")[0].strip()
logged_in=lambda:"username" in session
me=lambda:session.get("username","")
ok=lambda **kw:jsonify({"ok":True,**kw})
err=lambda e:jsonify({"ok":False,"error":e})
utc_now=lambda:datetime.datetime.utcnow().isoformat()
utc_cutoff=lambda minutes=2:(datetime.datetime.utcnow()-datetime.timedelta(minutes=minutes)).isoformat()
dec_messages=lambda rows:[{"sender":r[0],"content":dec(r[1]),"timestamp":r[2]} for r in rows]
VALID_EMOJIS={"like","dislike","love","lol","wow","angry","fire"}
THEMES={
    "green":{"p":"#00ff00","bg":"#000","ac":"#003300","name":"MATRIX"},
    "cyan":{"p":"#00ffff","bg":"#000a0a","ac":"#003333","name":"OCEAN"},
    "amber":{"p":"#ffb300","bg":"#0a0500","ac":"#332200","name":"AMBER"},
    "red":{"p":"#ff2222","bg":"#0a0000","ac":"#330000","name":"ALERT"},
    "purple":{"p":"#cc44ff","bg":"#050010","ac":"#220033","name":"NEXUS"},
    "white":{"p":"#4488ff","bg":"#000814","ac":"#001a3a","name":"GHOST"},
}
NAV_ITEMS=[
    ("fa-broadcast-tower","COMMS","https://www.seeedstudio.com/XIAO-ESP32S3-for-Meshtastic-LoRa-with-3D-Printed-Enclosure-p-6314.html"),
    ("fa-dove","VOX POPULI","#"),("fa-link","LINKTREE","#"),("fa-vault","\U0001f510 P-VAULT","#"),
    ("fa-shield-alt","P-BLK","#"),("fa-user-check","P-VETT","#"),("fa-globe","VOX NEWS","#"),("fa-circle","BLANK 2","#"),
]
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
    "CREATE TABLE IF NOT EXISTS messages(id SERIAL PRIMARY KEY,sender TEXT NOT NULL,recipient TEXT NOT NULL,content_enc TEXT NOT NULL,timestamp TEXT DEFAULT CURRENT_TIMESTAMP,read INTEGER DEFAULT 0)",
    "CREATE TABLE IF NOT EXISTS groups(id SERIAL PRIMARY KEY,name TEXT UNIQUE NOT NULL,created_by TEXT NOT NULL,locked INTEGER DEFAULT 0,created_at TEXT DEFAULT CURRENT_TIMESTAMP)",
    "CREATE TABLE IF NOT EXISTS group_members(group_id INTEGER NOT NULL,username TEXT NOT NULL,PRIMARY KEY(group_id,username))",
    "CREATE TABLE IF NOT EXISTS group_messages(id SERIAL PRIMARY KEY,group_id INTEGER NOT NULL,sender TEXT NOT NULL,content_enc TEXT NOT NULL,timestamp TEXT DEFAULT CURRENT_TIMESTAMP)",
    "CREATE TABLE IF NOT EXISTS visits(id SERIAL PRIMARY KEY,date TEXT NOT NULL,ip TEXT NOT NULL,UNIQUE(date,ip))",
    "CREATE TABLE IF NOT EXISTS active_users(ip TEXT PRIMARY KEY,last_seen TEXT NOT NULL)",
    "CREATE TABLE IF NOT EXISTS user_sessions(username TEXT PRIMARY KEY,last_seen TEXT NOT NULL)",
    "CREATE TABLE IF NOT EXISTS chat_read_at(username TEXT NOT NULL,chat_type TEXT NOT NULL,chat_id TEXT NOT NULL,read_at TEXT NOT NULL,PRIMARY KEY(username,chat_type,chat_id))",
    "CREATE TABLE IF NOT EXISTS group_banned(group_id INTEGER NOT NULL,username TEXT NOT NULL,PRIMARY KEY(group_id,username))",
    "CREATE TABLE IF NOT EXISTS dm_blocked(blocker TEXT NOT NULL,blocked TEXT NOT NULL,PRIMARY KEY(blocker,blocked))",
    "CREATE TABLE IF NOT EXISTS posts(id SERIAL PRIMARY KEY,username TEXT NOT NULL,content TEXT NOT NULL,created_at TEXT DEFAULT CURRENT_TIMESTAMP)",
    "CREATE TABLE IF NOT EXISTS post_reactions(post_id INTEGER NOT NULL,username TEXT NOT NULL,emoji TEXT NOT NULL,PRIMARY KEY(post_id,username))",
    "CREATE TABLE IF NOT EXISTS private_rooms(id SERIAL PRIMARY KEY,name TEXT NOT NULL,created_by TEXT NOT NULL,created_at TEXT DEFAULT CURRENT_TIMESTAMP)",
    "CREATE TABLE IF NOT EXISTS private_room_members(room_id INTEGER NOT NULL,username TEXT NOT NULL,PRIMARY KEY(room_id,username))",
    "CREATE TABLE IF NOT EXISTS private_room_messages(id SERIAL PRIMARY KEY,room_id INTEGER NOT NULL,sender TEXT NOT NULL,content_enc TEXT NOT NULL,timestamp TEXT DEFAULT CURRENT_TIMESTAMP)",
    "CREATE TABLE IF NOT EXISTS push_subscriptions(username TEXT NOT NULL,endpoint TEXT NOT NULL,p256dh TEXT NOT NULL,auth TEXT NOT NULL,PRIMARY KEY(username,endpoint))",
    "CREATE TABLE IF NOT EXISTS password_resets(id SERIAL PRIMARY KEY,username TEXT NOT NULL,temp_password TEXT,status TEXT DEFAULT 'pending',requested_at TEXT DEFAULT CURRENT_TIMESTAMP)",
]
def _do_init_db():
    with db() as con:
        cur=con.cursor()
        for sql in _TABLES: cur.execute(sql)
        cur.execute("UPDATE chat_read_at SET read_at=replace(substr(read_at,1,19),'T',' ') WHERE read_at LIKE '%T%'")
        cur.execute("UPDATE users SET is_admin=1 WHERE username=%s",(ADMIN_USER,))
        for ch in ["GENERAL","SURVIVAL","BARTER","HOMESTEAD"]:
            cur.execute("INSERT INTO groups(name,created_by) VALUES(%s,%s) ON CONFLICT (name) DO NOTHING",(ch,"SYSTEM"))
        cur.execute("SELECT username FROM users");users=[r[0] for r in cur.fetchall()]
        cur.execute("SELECT id FROM groups");groups=[r[0] for r in cur.fetchall()]
        cur.execute("SELECT group_id,username FROM group_banned");banned={(r[0],r[1]) for r in cur.fetchall()}
        for gid in groups:
            for uname in users:
                if (gid,uname) not in banned:
                    cur.execute("INSERT INTO group_members(group_id,username) VALUES(%s,%s) ON CONFLICT DO NOTHING",(gid,uname))
def init_db():
    for attempt in range(5):
        try: _do_init_db();return
        except Exception as exc:
            wait=3**attempt
            app.logger.warning(f"init_db attempt {attempt+1} failed, retrying in {wait}s: {exc}");time.sleep(wait)
    raise RuntimeError("Could not initialise database after 5 attempts.")
init_db()
def is_admin(u=None):
    u=u or me()
    if not u: return False
    if u==ADMIN_USER: return True
    with db() as con: row=fetchone(con,"SELECT is_admin FROM users WHERE username=%s",(u,))
    return bool(row and row[0])
def require_login():
    if not logged_in(): return err("NOT LOGGED IN")
def require_admin():
    if not is_admin(): return err("FORBIDDEN")
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
def read_at_map(con,username,chat_type):
    rows=fetchall(con,"SELECT chat_id,read_at FROM chat_read_at WHERE username=%s AND chat_type=%s",(username,chat_type))
    return {r[0]:r[1] for r in rows}
def unread_count(con,table,id_col,id_val,username,cutoff):
    row=fetchone(con,f"SELECT COUNT(*) FROM {table} WHERE {id_col}=%s AND sender!=%s AND timestamp>%s",(id_val,username,cutoff))
    return row[0] if row else 0
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
        ".tile-grid{display:inline-grid;grid-template-columns:repeat(8,minmax(0,1fr));gap:4px;margin:8px 0;position:relative;z-index:2;box-sizing:border-box;padding:2px 0;width:100%}",
        ".tile{border:2px solid var(--p);border-radius:8px;padding:8px 2px;background:transparent;color:var(--p);text-decoration:none;transition:.25s;display:flex;flex-direction:column;align-items:center;justify-content:center;box-shadow:0 0 8px var(--p30);text-align:center;position:relative;z-index:2;width:100%}",
        ".tile:hover{background:var(--p);color:#000;box-shadow:0 0 20px var(--p);transform:scale(1.04)}.tile i{font-size:13px;margin-bottom:3px}.tile div{font-size:8px;letter-spacing:1px}",
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
        ".comms-layout{display:grid;grid-template-columns:160px 1fr;min-height:320px;border:2px solid var(--p);border-radius:0 0 var(--r) var(--r);overflow:hidden}",
        ".comms-sidebar{border-right:2px solid var(--p);background:rgba(0,0,0,.9);display:flex;flex-direction:column;overflow:hidden}.comms-sidebar-header{padding:8px 12px;border-bottom:1px solid var(--p);font-size:10px;opacity:.55;flex-shrink:0}",
        ".conv-list{flex:1;overflow-y:auto}.conv-item{padding:8px 10px;cursor:pointer;border-bottom:1px solid var(--p10);font-size:11px;display:flex;justify-content:space-between;align-items:center;transition:.1s}",
        ".conv-item:hover,.conv-item.active{background:var(--p10)}.conv-item.active{border-left:3px solid var(--p)}",
        ".comms-main{display:flex;flex-direction:column;background:rgba(0,0,0,.75);overflow:hidden}",
        ".comms-thread-header{padding:9px 14px;border-bottom:2px solid var(--p);background:var(--p10);font-size:12px;display:flex;justify-content:space-between;align-items:center;flex-shrink:0}",
        ".comms-messages{flex:1;overflow-y:auto;padding:10px;max-height:320px;min-height:0;display:flex;flex-direction:column;gap:8px}",
        ".bubble-row{display:flex;align-items:flex-end;gap:8px}.bubble-row.mine{flex-direction:row-reverse}",
        ".bubble-avatar{width:34px;height:34px;border-radius:50%;border:2px solid var(--p);background:var(--ac);display:flex;align-items:center;justify-content:center;font-size:11px;flex-shrink:0}",
        ".bubble-content{max-width:78%}.bubble{padding:11px 15px;font-size:14px;line-height:1.5;word-break:break-word}",
        ".bubble-row:not(.mine) .bubble{background:var(--ac);border:1.5px solid var(--p30);border-radius:4px 16px 16px 16px}",
        ".bubble-row.mine .bubble{background:var(--p);border:1.5px solid var(--p);border-radius:16px 4px 16px 16px;color:#000}",
        ".bubble-meta{font-size:10px;opacity:.5;margin-top:3px;padding:0 4px}.bubble-row.mine .bubble-meta{text-align:right}",
        ".comms-compose{padding:9px 10px;border-top:2px solid var(--p);display:flex;gap:7px;flex-shrink:0;align-items:center}",
        ".comms-compose input{flex:1;padding:9px 14px;background:rgba(0,0,0,.8);border:2px solid var(--p);border-radius:20px;color:var(--p);font-family:'Courier New',monospace;font-size:12px;text-transform:uppercase}",
        ".comms-compose input:focus{outline:none;box-shadow:0 0 10px var(--p)}",
        ".send-btn{border:2px solid var(--p);border-radius:20px;padding:8px 16px;background:var(--p10);color:var(--p);cursor:pointer;font-family:'Courier New',monospace;font-size:11px;text-transform:uppercase;transition:.2s}",
        ".sidebar-footer{padding:8px;border-top:1px solid var(--p30);flex-shrink:0}",
        "@keyframes slideIn{from{transform:translateX(120%);opacity:0}to{transform:translateX(0);opacity:1}}@keyframes slideOut{from{transform:translateX(0);opacity:1}to{transform:translateX(120%);opacity:0}}",
        ".notif-toast{position:fixed;bottom:70px;right:16px;z-index:9500;background:rgba(0,0,0,.95);border:2px solid var(--p);border-radius:10px;padding:10px 14px;max-width:280px;font-family:'Courier New',monospace;font-size:11px;color:var(--p);box-shadow:0 0 20px var(--p30);animation:slideIn .3s ease;cursor:pointer}",
        ".notif-toast.hiding{animation:slideOut .3s ease forwards}.notif-toast .nt-title{font-weight:bold;font-size:12px;margin-bottom:3px;letter-spacing:1px}.notif-toast .nt-body{opacity:.8;text-transform:none;line-height:1.4}",
        ".traffic-counter{position:fixed;top:12px;right:16px;z-index:9000;background:rgba(0,0,0,.92);border:2px solid var(--p);border-radius:10px;padding:6px 12px;font-size:10px;font-family:'Courier New',monospace;text-transform:uppercase;box-shadow:0 0 20px var(--p30);line-height:1.7;pointer-events:none}",
        ".tc-row{display:flex;align-items:center;gap:7px}.tc-dot{width:7px;height:7px;border-radius:50%;background:var(--p);box-shadow:0 0 6px var(--p);animation:tcPulse 2s infinite;flex-shrink:0}.tc-label{opacity:.5;font-size:9px}.tc-val{font-weight:900;text-shadow:0 0 8px var(--p)}",
        ".search-box{width:min(100%,900px);margin:0 auto 24px;padding:0 16px;box-sizing:border-box;display:flex;flex-direction:column;gap:10px;position:relative;z-index:2}",
        ".search-row{display:flex;gap:8px;align-items:center}.search-input{flex:1;padding:12px 20px;background:rgba(0,0,0,.8);border:2px solid var(--p);border-radius:30px;color:var(--p);font-family:'Courier New',monospace;font-size:13px;text-transform:uppercase;box-shadow:0 0 16px var(--p30);transition:.2s}",
        ".search-input:focus{outline:none;box-shadow:0 0 24px var(--p)}.search-btn{border:2px solid var(--p);border-radius:30px;padding:11px 22px;background:var(--p10);color:var(--p);cursor:pointer;font-family:'Courier New',monospace;font-size:13px;text-transform:uppercase;transition:.2s;white-space:nowrap}",
        ".search-btn:hover{background:var(--p);color:#000;box-shadow:0 0 20px var(--p)}",
        "@media(max-width:700px){",
        ".title-row-wrap{padding:10px 8px 0}.logo-wrap{padding:12px 0 6px}",
        ".title-row{display:flex;flex-direction:row;align-items:center;justify-content:space-between;gap:6px;margin:0 0 10px}",
        ".title-center{flex:1}.title-row-right{display:flex;gap:5px;flex-shrink:0}",
        ".hero-btn{font-size:10px;padding:5px 8px;letter-spacing:0;border-radius:7px}.menu-trigger{font-size:10px;padding:5px 8px}",
        ".traffic-counter{top:6px;right:6px;padding:3px 8px;font-size:8px;line-height:1.4;display:flex;flex-direction:row;gap:8px;align-items:center}.tc-row{gap:4px}",
        ".tile-grid{display:inline-grid!important;grid-template-columns:repeat(4,minmax(48px,auto))!important;gap:4px!important;padding:0 4px!important;box-sizing:border-box;margin:8px 0}",
        ".tile{padding:7px 2px;border-radius:6px;width:100%;flex:none;min-width:0;max-width:none;font-size:8px}.tile i{font-size:12px;margin-bottom:3px}.tile div{font-size:8px;letter-spacing:0}",
        ".search-box{width:100%;margin:0 0 14px;box-sizing:border-box}.search-input{font-size:11px;padding:9px 12px}.search-btn{font-size:11px;padding:9px 12px}",
        ".content-box{width:100%;padding:12px 14px;font-size:13px;line-height:1.6;margin:12px 0;box-sizing:border-box}",
        ".three-column-grid{grid-template-columns:1fr;gap:10px;padding:2px;margin-bottom:12px}.column{padding:14px 12px}.column h3{font-size:12px}.column p{font-size:12px}.btn-action{font-size:11px;padding:7px 14px;margin-top:10px}",
        ".modal-overlay{padding:20px;align-items:center}.modal-box{max-width:96%;width:100%;border-radius:var(--r);padding:20px 16px;margin:auto;max-height:90vh;overflow-y:auto}",
        ".modal-box h2{font-size:13px;letter-spacing:2px;margin-bottom:10px}.field-plain{font-size:12px;padding:9px 10px}.field{font-size:12px;padding:9px 36px 9px 10px}.theme-grid{gap:5px}.theme-btn{font-size:9px;padding:6px 2px}",
        ".dropdown-menu{min-width:170px;left:auto;right:0;z-index:9999}.dropdown-item{padding:10px 12px;font-size:11px}",
        "#adminContent{max-height:180px}",
        ".comms-layout{display:flex;flex-direction:column;min-height:0}",
        ".comms-sidebar{border-right:0;border-bottom:2px solid var(--p);display:none;min-height:0;overflow-y:auto;max-height:280px}.comms-sidebar.mobile-show{display:flex;flex-direction:column}",
        ".comms-main{display:none;flex-direction:column;min-height:0;flex:1}.comms-main.mobile-show{display:flex}",
        ".conv-list{flex:1;overflow-y:auto}.conv-item{padding:14px 12px;font-size:13px}",
        ".comms-messages{flex:1;min-height:0;max-height:none;padding:12px;overflow-y:auto}",
        ".bubble{font-size:15px;padding:11px 15px}.bubble-content{max-width:82%}.bubble-avatar{width:30px;height:30px;font-size:10px}.comms-compose input{font-size:14px;padding:12px 14px}",
        ".send-btn{padding:11px 14px;font-size:12px}.mobile-back-btn{display:flex!important}",
        "}@media(min-width:701px){.mobile-back-btn{display:none!important}.comms-sidebar,.comms-main{display:flex}}",
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
<g clip-path="url(#lgcirc)" stroke="var(--p)" stroke-width="0.7" fill="none" opacity="0.22"><line x1="100" y1="148" x2="138" y2="148"/><line x1="138" y1="148" x2="138" y2="124"/><line x1="138" y1="124" x2="168" y2="124"/><line x1="300" y1="148" x2="262" y2="148"/><line x1="262" y1="148" x2="262" y2="124"/><line x1="262" y1="124" x2="232" y2="124"/><line x1="105" y1="235" x2="132" y2="235"/><line x1="132" y1="235" x2="132" y2="255"/><line x1="132" y1="255" x2="160" y2="255"/><line x1="295" y1="235" x2="268" y2="235"/><line x1="268" y1="235" x2="268" y2="255"/><line x1="268" y1="255" x2="240" y2="255"/><circle cx="138" cy="148" r="2.5" fill="var(--p)" opacity="0.55"/><circle cx="262" cy="148" r="2.5" fill="var(--p)" opacity="0.55"/><circle cx="132" cy="235" r="2.5" fill="var(--p)" opacity="0.55"/><circle cx="268" cy="235" r="2.5" fill="var(--p)" opacity="0.55"/><line x1="115" y1="175" x2="115" y2="210"/><line x1="285" y1="175" x2="285" y2="210"/><line x1="152" y1="108" x2="248" y2="108"/><line x1="152" y1="280" x2="248" y2="280"/><line x1="168" y1="108" x2="168" y2="118"/><line x1="232" y1="108" x2="232" y2="118"/><line x1="170" y1="155" x2="150" y2="155"/><line x1="150" y1="155" x2="150" y2="170"/><line x1="230" y1="155" x2="250" y2="155"/><line x1="250" y1="155" x2="250" y2="170"/><line x1="170" y1="240" x2="155" y2="240"/><line x1="155" y1="240" x2="155" y2="225"/><line x1="230" y1="240" x2="245" y2="240"/><line x1="245" y1="240" x2="245" y2="225"/><circle cx="150" cy="170" r="2" fill="var(--p)" opacity="0.4"/><circle cx="250" cy="170" r="2" fill="var(--p)" opacity="0.4"/><circle cx="155" cy="225" r="2" fill="var(--p)" opacity="0.4"/><circle cx="245" cy="225" r="2" fill="var(--p)" opacity="0.4"/></g>
<circle cx="200" cy="195" r="80" fill="none" stroke="var(--p)" stroke-width="1.6" opacity="0.45" filter="url(#lgglow)"/><circle cx="200" cy="195" r="75" fill="none" stroke="var(--p)" stroke-width="0.5" opacity="0.2"/>
<text x="200" y="210" text-anchor="middle" font-family="'Courier New',Courier,monospace" font-weight="900" font-size="58" letter-spacing="10" fill="var(--p)" filter="url(#lgglow)">VOX</text><text x="200" y="210" text-anchor="middle" font-family="'Courier New',Courier,monospace" font-weight="900" font-size="58" letter-spacing="10" fill="none" stroke="var(--p)" stroke-width="1.2" opacity="0.7">VOX</text>
<path d="M 84,244 A 122,122 0 0,0 316,244" fill="var(--ac)" stroke="var(--p)" stroke-width="1.6" opacity="0.9"/><path d="M 90,252 A 116,116 0 0,0 310,252" fill="none" stroke="var(--p)" stroke-width="0.4" opacity="0.35"/>
<text font-family="'Courier New',Courier,monospace" font-weight="900" font-size="15" letter-spacing="4" fill="var(--p)" filter="url(#lgglow)"><textPath href="#lgarcB" startOffset="50%" text-anchor="middle">VOX POPULI</textPath></text>
<g font-family="'Courier New',Courier,monospace" font-size="7.5" fill="var(--p)" opacity="0.38"><text x="30" y="290">N-15-77</text><text x="30" y="300">SYS:ACTIV</text><text x="30" y="310">STEALTH MODE</text><text x="280" y="290">N-15-77</text><text x="275" y="300">STR:ON</text><text x="268" y="310">VOX.POPULI.LVL3</text></g>
<path d="M 56,195 A 144,144 0 0,1 344,195" fill="none" stroke="var(--p)" stroke-width="0.4" opacity="0.2" stroke-dasharray="3 6"/>
</svg>"""
def shell(content,user=None,theme="green",unread=0):
    t=THEMES.get(theme,THEMES["green"]);admin=is_admin(user)
    if user:
        at_badge=(' <span style="font-size:9px;opacity:.8;margin-left:5px;letter-spacing:1px;vertical-align:middle;">&#9733; ADMIN</span>' if admin else '')
        menu_html=(f'<div class="menu-wrap"><div class="menu-trigger" onclick="event.stopPropagation();document.getElementById(\'accountMenu\').classList.toggle(\'open\')" style="display:flex;align-items:center;gap:8px;padding:8px 16px;border-radius:8px;">'
            f'<span style="font-size:16px;">&#9776;</span><span style="border-left:1px solid var(--p);opacity:.4;height:16px;"></span>'
            f'<span style="font-size:12px;letter-spacing:1px;">{user}</span>{at_badge}<span style="font-size:10px;opacity:.6;">&#9663;</span></div>'
            f'<div class="dropdown-menu" id="accountMenu"><div class="dropdown-item" style="opacity:.5;font-size:10px;cursor:default;pointer-events:none;padding:8px 16px;">&#9658; {user.upper()} [{t["name"]}]</div>'
            f'<div class="dropdown-divider"></div><a class="dropdown-item" onclick="event.stopPropagation();openModal(\'settingsModal\')"><i class="fas fa-cog"></i> SETTINGS</a>'
            f'<a class="dropdown-item" onclick="event.stopPropagation();enableNotifications()" id="notifMenuItem"><i class="fas fa-bell"></i> ENABLE NOTIFICATIONS</a>'
            f'<a class="dropdown-item" href="/logout"><i class="fas fa-sign-out-alt"></i> LOGOUT</a></div></div>')
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
        '<button class="btn-action" style="margin:0;padding:8px;font-size:11px;" onclick="adminShowUsers()">&#128100; USERS</button>'
        '<button class="btn-action" style="margin:0;padding:8px;font-size:11px;" onclick="adminShowDMs()">&#128172; DM LOGS</button>'
        '<button class="btn-action" style="margin:0;padding:8px;font-size:11px;" onclick="adminShowGroups()">&#128483; GROUP LOGS</button>'
        '<button class="btn-action" style="margin:0;padding:8px;font-size:11px;" onclick="adminShowLookup()">&#128269; CHAT LOOKUP</button>'
        '<button class="btn-action" style="margin:0;padding:8px;font-size:11px;grid-column:span 2;" onclick="adminShowTraffic()">&#128200; TRAFFIC</button>'
        '<button class="btn-action" style="margin:0;padding:8px;font-size:11px;grid-column:span 2;border-color:#fb0;color:#fb0;" onclick="adminShowResets()">&#128274; PASSWORD RESETS</button>'
        '</div><div id="adminLookupBar" style="display:none;margin-bottom:8px;"><div style="display:flex;gap:6px;">'
        '<input id="adminLookupInput" class="field-plain" placeholder="ENTER USERNAME..." style="margin:0;flex:1;padding:8px 12px;font-size:12px;border-radius:20px;" oninput="adminLookupSuggest()" onkeydown="if(event.key===\'Enter\')adminLookupRun()">'
        '<button class="btn-action" style="margin:0;padding:8px 14px;font-size:11px;" onclick="adminLookupRun()">&#128269;</button>'
        '</div><div id="adminLookupSuggest" style="font-size:11px;border:1px solid var(--p30);border-radius:8px;margin-top:4px;display:none;max-height:100px;overflow-y:auto;"></div></div>'
        '<div id="adminContent" style="max-height:300px;overflow-y:auto;text-align:left;font-size:11px;border:1px solid var(--p30);border-radius:8px;padding:4px;">'
        '<div style="padding:12px;opacity:.4;text-align:center;">SELECT AN ACTION ABOVE</div></div></div>'
    ) if admin else ''
    admin_tab='<button class="tab" id="stTabAdmin" onclick="switchStTab(\'admin\')">&#9733; ADMIN</button>' if admin else ''
    JS=f"""
let activeDMUser=null,activeGroupId=null,activeGroupName=null,activePrivateRoomId=null,activePrivateRoomName=null;
let regThemeVal='green',onlineUsers=new Set();
let _prevNotif={{dm:-1,group:-1,private:-1,posts:-1,groups:{{}},private_rooms:{{}}}};
let _notifReady=false,_notifPermission=false;
const IS_ADMIN={str(admin).lower()};
const api=async(url,body)=>{{try{{const r=await fetch(url,body?{{method:'POST',headers:{{'Content-Type':'application/json'}},body:JSON.stringify(body)}}:undefined);if(r.status===401||r.redirected||r.url.includes('/login')){{location.reload();return{{ok:false,error:'SESSION_EXPIRED'}};}}const ct=r.headers.get('content-type')||'';if(!ct.includes('json'))return{{ok:false,error:'NOT_JSON'}};return await r.json();}}catch(e){{return{{ok:false,error:String(e)}};}}}};

const $=id=>document.getElementById(id);
const isMobile=()=>window.innerWidth<=700;
const openModal=id=>{{const el=$(id);if(el)el.classList.add('open');}};
const closeModal=id=>{{const el=$(id);if(el)el.classList.remove('open');}};
function togglePw(id,btn){{const i=$(id);i.type=i.type==='password'?'text':'password';btn.innerHTML=i.type==='password'?'&#128065;':'&#128584;';}}
document.querySelectorAll('.modal-overlay').forEach(m=>m.addEventListener('click',e=>{{if(e.target===m)m.classList.remove('open');}}));
document.addEventListener('click',e=>{{const menu=$('accountMenu');if(menu&&!e.target.closest('.menu-wrap'))menu.classList.remove('open');if(!e.target.closest('#newDmUser')&&!e.target.closest('#dmUserSuggest'))hideDmSuggest();}});
function setRegTheme(t){{regThemeVal=t;}}
async function doLogin(){{const errEl=$('loginErr');errEl.textContent='';const d=await api('/api/login',{{username:$('loginUser').value.trim(),password:$('loginPass').value}});if(d.ok){{location.reload();}}else{{errEl.textContent='ERROR: '+d.error;}}}}
async function doRegister(){{const p=$('regPass').value,p2=$('regPass2').value,dob=$('regDob').value,errEl=$('regErr');errEl.textContent='';if(!dob){{errEl.textContent='DATE OF BIRTH REQUIRED';return;}}if((Date.now()-new Date(dob))/31557600000<18){{errEl.textContent='YOU MUST BE 18 OR OLDER TO JOIN';return;}}if(p!==p2){{errEl.textContent='PASSWORDS DO NOT MATCH';return;}}const d=await api('/api/register',{{username:$('regUser').value.trim(),password:p,theme:regThemeVal}});if(d.ok){{location.reload();}}else{{errEl.textContent='ERROR: '+d.error;}}}}
async function doResetRequest(){{const u=$('resetUser').value.trim(),errEl=$('resetErr'),okEl=$('resetOk');errEl.textContent='';okEl.textContent='';if(!u){{errEl.textContent='USERNAME REQUIRED';return;}}const d=await api('/api/reset/request',{{username:u}});if(d.ok){{okEl.textContent='REQUEST SENT — AN ADMIN WILL SET A TEMP PASSWORD FOR YOU.';}}else{{errEl.textContent='ERROR: '+d.error;}}}}
async function changePassword(){{const cur=$('pwCurrent').value,nw=$('pwNew').value,nw2=$('pwNew2').value;const errEl=$('pwErr'),okEl=$('pwOk');errEl.textContent='';okEl.textContent='';if(!cur||!nw||!nw2){{errEl.textContent='ALL FIELDS REQUIRED';return;}}if(nw!==nw2){{errEl.textContent='PASSWORDS DO NOT MATCH';return;}}if(nw.length<6){{errEl.textContent='TOO SHORT (MIN 6)';return;}}const d=await api('/api/change-password',{{current:cur,new_password:nw}});if(d.ok){{okEl.textContent='PASSWORD UPDATED';['pwCurrent','pwNew','pwNew2'].forEach(i=>$(i).value='');}}else{{errEl.textContent='ERROR: '+d.error;}}}}
async function changeTheme(t){{await api('/api/theme',{{theme:t}});location.reload();}}
function switchStTab(tab){{['theme','pw','admin'].forEach(k=>{{const K=k[0].toUpperCase()+k.slice(1);const c=$('stContent'+K),b=$('stTab'+K);if(c)c.style.display=k===tab?'block':'none';if(b)b.classList.toggle('active',k===tab);}});if(tab==='admin')adminShowUsers();}}
const adminBox=()=>$('adminContent');
const adminErr=msg=>{{if(adminBox())adminBox().innerHTML=`<div style="padding:10px;color:#f44;">${{msg}}</div>`;}};
const msgRow=(m,fn)=>`<div style="padding:6px 10px 6px 20px;border-top:1px solid var(--p10);display:flex;justify-content:space-between;align-items:flex-start;gap:6px;"><div><span style="opacity:.5;font-size:10px;">${{m.sender}} &middot; ${{m.timestamp}}</span><br>${{m.content}}</div><button class="btn-action" style="padding:2px 6px;font-size:10px;margin:0;border-color:#f44;color:#f44;flex-shrink:0;" onclick="${{fn}}(${{m.id}})">&#128465;</button></div>`;
async function adminShowUsers(){{if(!adminBox())return;adminBox().innerHTML='<div style="padding:10px;opacity:.4;text-align:center;">LOADING...</div>';const d=await api('/api/admin/users');if(!d.ok){{adminErr('ACCESS DENIED');return;}}const dot='<span style="display:inline-block;width:7px;height:7px;border-radius:50%;background:#0f0;box-shadow:0 0 5px #0f0;margin-right:5px;vertical-align:middle;"></span>';adminBox().innerHTML='<div style="padding:6px 10px;opacity:.5;font-size:10px;border-bottom:1px solid var(--p10);">&#128100; USERS</div>'+d.users.map(u=>`<div style="padding:8px 10px;border-bottom:1px solid var(--p10);display:flex;justify-content:space-between;align-items:center;gap:6px;flex-wrap:wrap;"><span>${{onlineUsers.has(u.username)?dot:''}}<span>${{u.username}}</span>${{u.is_admin?' &#9733;':''}} <span style="opacity:.4;font-size:10px;">${{u.created_at}}</span></span><div style="display:flex;gap:4px;"><button class="btn-action" style="padding:3px 8px;font-size:10px;margin:0;" onclick="adminToggleAdmin('${{u.username}}',${{!u.is_admin}})">${{u.is_admin?'REVOKE':'GRANT ADMIN'}}</button><button class="btn-action" style="padding:3px 8px;font-size:10px;margin:0;border-color:#f44;color:#f44;" onclick="adminRemoveUser('${{u.username}}')">&#10006;</button></div></div>`).join('');}}
async function adminToggleAdmin(u,g){{await api('/api/admin/set-admin',{{username:u,grant:g}});adminShowUsers();}}
async function adminRemoveUser(u){{if(!confirm('REMOVE: '+u+'?'))return;const d=await api('/api/admin/remove-user',{{username:u}});d.ok?adminShowUsers():alert('ERROR: '+d.error);}}
async function adminShowDMs(){{adminBox().innerHTML='<div style="padding:10px;opacity:.4;text-align:center;">LOADING...</div>';const d=await api('/api/admin/dm-log');if(!d.ok){{adminErr('ERROR');return;}}if(!d.messages.length){{adminBox().innerHTML='<div style="padding:10px;opacity:.4;text-align:center;">NO MESSAGES</div>';return;}}const convos={{}};d.messages.forEach(m=>{{const k=[m.sender,m.recipient].sort().join('|');if(!convos[k])convos[k]={{users:[m.sender,m.recipient].sort(),messages:[]}};convos[k].messages.push(m);}});let html='<div style="padding:6px 10px;opacity:.5;font-size:10px;border-bottom:1px solid var(--p10);">&#128172; DM CONVERSATIONS</div>';Object.values(convos).forEach(c=>{{const[u1,u2]=c.users;html+=`<div style="border-bottom:2px solid var(--p30);"><div style="padding:8px 10px;background:var(--p10);display:flex;justify-content:space-between;align-items:center;"><span style="font-size:11px;">${{u1}} &#8596; ${{u2}} (${{c.messages.length}})</span><button class="btn-action" style="padding:3px 8px;font-size:10px;margin:0;border-color:#f44;color:#f44;" onclick="adminDeleteConvo('${{u1}}','${{u2}}')">&#128465; ALL</button></div>`;c.messages.forEach(m=>{{html+=msgRow(m,'adminDeleteDM');}});html+='</div>';}});adminBox().innerHTML=html;}}
async function adminDeleteDM(id){{await api('/api/admin/delete-dm',{{id}});adminShowDMs();}}
async function adminDeleteConvo(u1,u2){{if(!confirm('DELETE CHAT: '+u1+' & '+u2+'?'))return;const d=await api('/api/admin/delete-convo',{{user1:u1,user2:u2}});d.ok?adminShowDMs():alert('ERROR: '+d.error);}}
async function adminShowGroups(){{adminBox().innerHTML='<div style="padding:10px;opacity:.4;text-align:center;">LOADING...</div>';const[d,dg]=await Promise.all([api('/api/admin/group-log'),api('/api/groups')]);if(!d.ok){{adminErr('ERROR');return;}}const ch={{}};d.messages.forEach(m=>{{if(!ch[m.group_id])ch[m.group_id]={{id:m.group_id,name:m.group,messages:[],locked:false}};ch[m.group_id].messages.push(m);}});if(dg.ok)dg.groups.forEach(g=>{{if(!ch[g.id])ch[g.id]={{id:g.id,name:g.name,messages:[],locked:g.locked}};else ch[g.id].locked=g.locked;}});if(!Object.keys(ch).length){{adminBox().innerHTML='<div style="padding:10px;opacity:.4;text-align:center;">NO CHANNELS</div>';return;}}let html='<div style="padding:6px 10px;opacity:.5;font-size:10px;border-bottom:1px solid var(--p10);">&#128483; CHANNELS</div>';Object.values(ch).forEach(c=>{{const lc=c.locked?'#fa0':'#4af',li=c.locked?'&#128274;':'&#128275;';html+=`<div style="border-bottom:2px solid var(--p30);"><div style="padding:8px 10px;background:var(--p10);display:flex;justify-content:space-between;align-items:center;gap:4px;flex-wrap:wrap;"><span style="font-size:11px;">${{li}} ${{c.name}} (${{c.messages.length}})${{c.locked?' <span style="color:#fa0;font-size:10px;">LOCKED</span>':''}}</span><div style="display:flex;gap:4px;"><button class="btn-action" style="padding:3px 8px;font-size:10px;margin:0;border-color:${{lc}};color:${{lc}};" onclick="adminLockChannel(${{c.id}},${{!c.locked}})">${{c.locked?'UNLOCK':'LOCK'}}</button><button class="btn-action" style="padding:3px 8px;font-size:10px;margin:0;border-color:#f44;color:#f44;" onclick="adminDeleteChannel(${{c.id}},'${{c.name}}')">&#128465;</button></div></div>`;c.messages.forEach(m=>{{html+=msgRow(m,'adminDeleteGroupMsg');}});html+='</div>';}});adminBox().innerHTML=html;}}
async function adminLockChannel(gid,lock){{const d=await api('/api/admin/lock-channel',{{group_id:gid,lock}});d.ok?adminShowGroups():alert('ERROR: '+d.error);}}
async function adminDeleteGroupMsg(id){{await api('/api/admin/delete-group-msg',{{id}});adminShowGroups();}}
async function adminDeleteChannel(gid,gname){{if(!confirm('DELETE #'+gname+'?'))return;const d=await api('/api/admin/delete-channel',{{group_id:gid}});d.ok?adminShowGroups():alert('ERROR: '+d.error);}}
function adminShowLookup(){{$('adminLookupBar').style.display='block';adminBox().innerHTML='<div style="padding:12px;opacity:.4;text-align:center;">TYPE A USERNAME TO LOOK UP DM HISTORY</div>';$('adminLookupInput').value='';$('adminLookupSuggest').style.display='none';$('adminLookupInput').focus();}}
async function adminLookupSuggest(){{const q=$('adminLookupInput').value.trim(),box=$('adminLookupSuggest');if(!q){{box.style.display='none';return;}}const d=await api('/api/users/search?q='+encodeURIComponent(q));if(!d.ok||!d.users.length){{box.style.display='none';return;}}box.style.display='block';box.innerHTML=d.users.map(u=>`<div style="padding:10px 14px;cursor:pointer;border-bottom:1px solid var(--p10);" onmouseover="this.style.background='var(--p10)'" onmouseout="this.style.background=''" onmousedown="event.preventDefault();$('adminLookupInput').value='${{u}}';$('adminLookupSuggest').style.display='none';adminLookupRun();">${{u}}</div>`).join('');}}
async function adminLookupRun(){{const username=$('adminLookupInput').value.trim();if(!username)return;$('adminLookupSuggest').style.display='none';adminBox().innerHTML='<div style="padding:10px;opacity:.4;text-align:center;">LOADING...</div>';const d=await api('/api/admin/user-chat?username='+encodeURIComponent(username));if(!d.ok){{adminErr(d.error);return;}}if(!d.conversations.length){{adminBox().innerHTML=`<div style="padding:12px;opacity:.4;text-align:center;">NO DM HISTORY FOR ${{username.toUpperCase()}}</div>`;return;}}let html=`<div style="padding:6px 10px;background:var(--p10);border-bottom:2px solid var(--p30);font-size:11px;">&#128269; ${{username.toUpperCase()}} — ${{d.total}} msgs / ${{d.conversations.length}} convos</div>`;d.conversations.forEach(conv=>{{html+=`<div style="border-bottom:2px solid var(--p30);"><div style="padding:8px 10px;background:var(--p10);display:flex;justify-content:space-between;align-items:center;"><span>${{username}} &#8596; ${{conv.partner}} (${{conv.messages.length}})</span><button class="btn-action" style="padding:3px 8px;font-size:10px;margin:0;border-color:#f44;color:#f44;" onclick="adminDeleteConvo('${{username}}','${{conv.partner}}')">&#128465; ALL</button></div>`;conv.messages.forEach(m=>{{const mine=m.sender===username;html+=`<div style="padding:6px 10px 6px ${{mine?'30px':'10px'}};border-top:1px solid var(--p10);display:flex;justify-content:space-between;align-items:flex-start;gap:6px;"><div><span style="opacity:.5;font-size:10px;">${{mine?'&#9658;':'&#9664;'}} ${{m.sender}} &#8594; ${{m.recipient}} &middot; ${{m.timestamp}}</span><br>${{m.content}}</div><button class="btn-action" style="padding:2px 6px;font-size:10px;margin:0;border-color:#f44;color:#f44;flex-shrink:0;" onclick="adminDeleteDM(${{m.id}});adminLookupRun();">&#128465;</button></div>`;}});html+='</div>';}});adminBox().innerHTML=html;}}
async function adminShowTraffic(){{$('adminLookupBar').style.display='none';adminBox().innerHTML='<div style="padding:10px;opacity:.4;text-align:center;">LOADING...</div>';const d=await api('/api/admin/traffic');if(!d.ok){{adminErr('ERROR');return;}}const max=Math.max(...d.days.map(r=>r.visitors),1);let html=`<div style="padding:8px 10px;background:var(--p10);border-bottom:1px solid var(--p30);display:flex;justify-content:space-between;font-size:11px;"><span>&#128200; SITE TRAFFIC</span><span>TODAY: <b>${{d.today}}</b> &nbsp;|&nbsp; ALL TIME: <b>${{d.total}}</b></span></div>`;d.days.forEach(r=>{{const pct=Math.round(r.visitors/max*100);html+=`<div style="padding:6px 10px;border-bottom:1px solid var(--p10);display:flex;align-items:center;gap:8px;font-size:11px;"><span style="width:80px;flex-shrink:0;opacity:.7;">${{r.date}}</span><div style="flex:1;background:var(--p10);border-radius:4px;height:14px;overflow:hidden;"><div style="width:${{pct}}%;height:100%;background:var(--p);box-shadow:0 0 8px var(--p);border-radius:4px;transition:.3s;"></div></div><span style="width:28px;text-align:right;">${{r.visitors}}</span></div>`;}});adminBox().innerHTML=html;}}
async function adminShowResets(){{$('adminLookupBar').style.display='none';adminBox().innerHTML='<div style="padding:10px;opacity:.4;text-align:center;">LOADING...</div>';const d=await api('/api/admin/reset-requests');if(!d.ok){{adminErr('ERROR');return;}}if(!d.requests.length){{adminBox().innerHTML='<div style="padding:12px;opacity:.4;text-align:center;">NO PENDING RESET REQUESTS</div>';return;}}let html='<div style="padding:6px 10px;opacity:.5;font-size:10px;border-bottom:1px solid var(--p10);">&#128274; PASSWORD RESET REQUESTS</div>';d.requests.forEach(r=>{{html+=`<div style="padding:10px;border-bottom:1px solid var(--p10);display:flex;justify-content:space-between;align-items:center;flex-wrap:wrap;gap:6px;"><div><span style="font-size:12px;">${{r.username}}</span><span style="font-size:10px;opacity:.5;margin-left:8px;">${{r.requested_at}}</span>${{r.temp_password?`<div style="font-size:11px;margin-top:4px;color:#4f4;">TEMP PW: <b>${{r.temp_password}}</b></div>`:''}}</div><div style="display:flex;gap:4px;flex-wrap:wrap;"><input id="tmpPw_${{r.id}}" class="field-plain" placeholder="SET TEMP PW..." style="margin:0;padding:5px 8px;font-size:11px;width:120px;border-radius:6px;"><button class="btn-action" style="margin:0;padding:4px 10px;font-size:10px;" onclick="adminApproveReset(${{r.id}})">&#10003; SET</button><button class="btn-action" style="margin:0;padding:4px 10px;font-size:10px;border-color:#f44;color:#f44;" onclick="adminDenyReset(${{r.id}})">&#10006;</button></div></div>`;}});adminBox().innerHTML=html;}}
async function adminApproveReset(id){{const inp=document.getElementById('tmpPw_'+id),pw=inp?inp.value.trim():'';if(!pw){{alert('ENTER A TEMPORARY PASSWORD');return;}}const d=await api('/api/admin/reset-approve',{{id,temp_password:pw}});d.ok?adminShowResets():alert('ERROR: '+d.error);}}
async function adminDenyReset(id){{if(!confirm('DENY THIS RESET REQUEST?'))return;const d=await api('/api/admin/reset-deny',{{id}});d.ok?adminShowResets():alert('ERROR: '+d.error);}}
let activeNewsTab='world';
function switchNewsTab(tab){{activeNewsTab=tab;[['newsTabWorld','world'],['newsTabUS','usnews'],['newsTabEpstein','epstein']].forEach(([id,t])=>{{const el=$(id);if(!el)return;el.style.background=tab===t?'var(--p)':'var(--p10)';el.style.color=tab===t?'#000':'var(--p)';}});
if($('newsFeed'))loadNewsFeed();}}
function formatNewsTime(pubDateStr){{if(!pubDateStr)return'';try{{const d=new Date(pubDateStr);if(isNaN(d.getTime()))return'';const now=new Date(),diffMs=now-d,diffMins=Math.floor(diffMs/60000),diffHrs=Math.floor(diffMins/60),diffDays=Math.floor(diffHrs/24);if(diffMins<2)return'JUST NOW';if(diffMins<60)return diffMins+'M AGO';if(diffHrs<24)return diffHrs+'H AGO';if(diffDays<7)return diffDays+'D AGO';return d.toLocaleDateString('en-US',{{month:'short',day:'numeric'}}).toUpperCase();}}catch{{return'';}}}}
async function loadNewsFeed(){{try{{const d=await api('/api/news?type='+activeNewsTab);const feed=$('newsFeed'),status=$('newsFeedStatus');if(!feed)return;if(!d.ok||!d.items||!d.items.length){{feed.innerHTML='<div style="padding:12px;opacity:.4;text-align:center;font-size:11px;">NO FEED AVAILABLE</div>';return;}}const redWords=['emergency','warning','attack','war','crisis','disaster','flood','earthquake','hurricane','tornado','outbreak','pandemic','shooting','explosion','terror','threat','evacuation','martial','nuclear','wildfire','tsunami','collapse','breach','invasion'];const yelWords=['recall','shortage','inflation','protest','strike','arrest','investigation','storm','drought','fire','accident','crash','leak','shutdown','ban','sanction','tariff'];const srcColors={{'SURVIVAL':'#00ff00','OFF-GRID':'#00ff00','COMMUNITY':'#fb0'}};function getPriority(item){{const t=(item.title+' '+(item.desc||'')).toLowerCase();if(redWords.some(w=>t.includes(w)))return'#f44';if(yelWords.some(w=>t.includes(w)))return'#fb0';return srcColors[item.cat]||'var(--p)';}}const priorityLabel={{'#f44':'&#9888; ALERT','#fb0':'&#9650; WATCH','#00ff00':'&#9679; CLEAR'}};feed.innerHTML=d.items.map(item=>{{const c=getPriority(item),pl=priorityLabel[c]||'',ts=formatNewsTime(item.pub_date);return `<div style="padding:10px 14px;border-bottom:1px solid var(--p10);border-left:3px solid ${{c}};display:flex;flex-direction:column;gap:4px;"><div style="display:flex;align-items:center;gap:8px;flex-wrap:wrap;"><span style="font-size:9px;padding:2px 6px;border:1px solid ${{c}};color:${{c}};border-radius:4px;flex-shrink:0;font-weight:bold;">${{pl}} ${{item.cat}}</span>${{ts?`<span style="font-size:9px;opacity:.45;flex-shrink:0;">${{ts}}</span>`:''}}<a href="${{item.url}}" target="_blank" rel="noopener noreferrer" style="color:var(--p);font-size:12px;font-weight:bold;text-decoration:none;line-height:1.4;">${{item.title}}</a></div>${{item.desc?`<div style="font-size:11px;opacity:.6;line-height:1.5;text-transform:none;padding-left:2px;">${{item.desc}}</div>`:''}}</div>`;}}).join('');if(status)status.textContent='UPDATED '+new Date().toLocaleTimeString();}}catch(e){{const feed=$('newsFeed');if(feed)feed.innerHTML='<div style="padding:12px;color:#f44;font-size:11px;">FEED ERROR</div>';}}}}
function markPostsRead(){{api('/api/mark-read',{{type:'posts',id:'posts'}});_prevNotif.posts=0;setBadge('badgeBoard',0);}}
function updatePostCount(el){{const c=$('postCount');if(c)c.textContent=el.value.length+'/500';}}
async function loadPosts(){{const feed=$('postFeed');if(!feed)return;feed.innerHTML='<div style="padding:12px;opacity:.4;text-align:center;font-size:11px;">LOADING...</div>';const d=await api('/api/posts');if(!d.ok||!d.posts||!d.posts.length){{feed.innerHTML='<div style="padding:16px;opacity:.4;text-align:center;font-size:11px;">NO POSTS YET - BE THE FIRST!</div>';return;}}const EMOJI_LIST=[['&#128077;','&#128078;','&#10084;&#65039;','&#128514;','&#128558;','&#128545;','&#128293;'],['like','dislike','love','lol','wow','angry','fire']];const myName=d.me||'';feed.innerHTML=d.posts.map(p=>{{const mine=p.username===myName;const rxBar=EMOJI_LIST[0].map((e,i)=>{{const r=(p.reactions&&p.reactions[EMOJI_LIST[1][i]])||{{count:0,mine:false}};return `<button onclick="reactPost(${{p.id}},'${{EMOJI_LIST[1][i]}}')" style="background:${{r.mine?'var(--p)':'var(--p10)'}};color:${{r.mine?'#000':'var(--p)'}};border:1px solid ${{r.mine?'var(--p)':'var(--p30)'}};border-radius:20px;padding:2px 8px;font-size:12px;cursor:pointer;margin:2px;transition:.15s;">${{e}}${{r.count?' <b>'+r.count+'</b>':''}}</button>`;}}).join('');return `<div class="bubble-row ${{mine?'mine':''}}" style="padding:8px 10px;gap:8px;"><div class="bubble-avatar">${{p.username.substring(0,2).toUpperCase()}}</div><div class="bubble-content" style="max-width:85%;"><div class="bubble" style="${{mine?'':'background:var(--ac);border:1.5px solid var(--p30);border-radius:4px 16px 16px 16px;'}}"><div style="font-size:14px;line-height:1.5;text-transform:none;white-space:pre-wrap;word-break:break-word;">${{p.content}}</div><div style="margin-top:6px;">${{rxBar}}</div></div><div class="bubble-meta" style="display:flex;gap:8px;align-items:center;"><span>${{mine?'YOU':p.username}} &middot; ${{p.created_at}}</span>${{p.can_delete?`<button onclick="deletePost(${{p.id}})" style="background:none;border:none;color:#f44;cursor:pointer;font-size:10px;padding:0;font-family:'Courier New',monospace;">&#128465;</button>`:''}}</div></div></div>`;}}).join('');}}
async function submitPost(){{const content=$('postContent').value.trim(),errEl=$('postErr');errEl.textContent='';if(!content){{errEl.textContent='WRITE SOMETHING FIRST';return;}}if(content.length>500){{errEl.textContent='MAX 500 CHARACTERS';return;}}const d=await api('/api/posts/create',{{content}});if(!d.ok){{errEl.textContent='ERROR: '+d.error;return;}}$('postContent').value='';updatePostCount($('postContent'));loadPosts();}}
async function reactPost(postId,emoji){{await api('/api/posts/react',{{post_id:postId,emoji}});loadPosts();}}
async function deletePost(postId){{if(!confirm('DELETE THIS POST?'))return;await api('/api/posts/delete',{{post_id:postId}});loadPosts();}}
function enableNotifications(){{if(!('Notification' in window)){{alert('NOTIFICATIONS NOT SUPPORTED ON THIS BROWSER');return;}}if(Notification.permission==='granted'){{setupPushSubscription();const m=$('notifMenuItem');if(m)m.style.display='none';return;}}Notification.requestPermission().then(p=>{{if(p==='granted'){{_notifPermission=true;setupPushSubscription();const m=$('notifMenuItem');if(m)m.style.display='none';}}else alert('NOTIFICATION PERMISSION DENIED.');}});}}
function requestNotifPermission(){{if(!('Notification' in window))return;if(Notification.permission==='granted'){{_notifPermission=true;setupPushSubscription();return;}}if(Notification.permission!=='denied')Notification.requestPermission().then(p=>{{_notifPermission=p==='granted';if(_notifPermission)setupPushSubscription();}});}}
function urlBase64ToUint8Array(b64){{const padding='='.repeat((4-b64.length%4)%4),base64=(b64+padding).replace(/-/g,'+').replace(/_/g,'/'),raw=atob(base64);return Uint8Array.from({{length:raw.length}},(_,i)=>raw.charCodeAt(i));}}
async function setupPushSubscription(){{try{{if(!('serviceWorker' in navigator)||!('PushManager' in window))return;const reg=await navigator.serviceWorker.ready;const existing=await reg.pushManager.getSubscription();if(existing){{await api('/api/push/subscribe',existing.toJSON());return;}}const kd=await api('/api/push/vapid-public-key');if(!kd.ok||!kd.key)return;const sub=await reg.pushManager.subscribe({{userVisibleOnly:true,applicationServerKey:urlBase64ToUint8Array(kd.key)}});await api('/api/push/subscribe',sub.toJSON());}}catch(e){{}}}}
function showToast(title,body,onClick){{const t=document.createElement('div');t.className='notif-toast';t.innerHTML=`<div class="nt-title">&#128276; ${{title}}</div><div class="nt-body">${{body}}</div>`;t.onclick=()=>{{if(onClick)onClick();t.classList.add('hiding');setTimeout(()=>t.remove(),300);}};document.body.appendChild(t);setTimeout(()=>{{t.classList.add('hiding');setTimeout(()=>t.remove(),300);}},5000);}}
function pushNotif(title,body,action){{if(_notifPermission&&Notification.permission==='granted'){{try{{const n=new Notification('VOX // '+title,{{body,icon:'/favicon.ico',badge:'/favicon.ico',tag:action}});n.onclick=()=>{{window.focus();n.close();if(action==='dm')switchTab('dm');else if(action==='group')switchTab('group');else if(action==='private')switchTab('private');else if(action==='board')switchTab('board');}};}}catch(e){{}}}}}}
function setBadge(id,count){{const b=$(id);if(!b)return;if(count>0){{b.textContent=count;b.style.display='inline';}}else b.style.display='none';}}
async function checkNotifications(){{try{{const d=await api('/api/notifications');if(!d.ok)return;if(!_notifReady){{_prevNotif={{dm:d.dm,group:d.group,private:d.private,posts:d.posts,groups:d.groups||{{}},private_rooms:d.private_rooms||({{}})}};_notifReady=true;setBadge('badgeDM',d.dm);setBadge('badgeGroup',d.group);setBadge('badgePrivate',d.private);setBadge('badgeBoard',d.posts);document.title=d.total>0?'('+d.total+') VOX':'VOX';return;}}if(d.dm>0&&d.dm>_prevNotif.dm&&_prevNotif.dm>=0){{const diff=d.dm-Math.max(_prevNotif.dm,0);showToast('DIRECT MESSAGE',diff+' new message'+(diff>1?'s':''),()=>switchTab('dm'));pushNotif('DIRECT MESSAGE',diff+' new DM'+(diff>1?'s':''),'dm');}}const newGroups=d.groups||{{}},prevGroups=_prevNotif.groups||{{}};Object.entries(newGroups).forEach(([gid,info])=>{{const prevCount=(prevGroups[gid]&&prevGroups[gid].count)||0;if(info.count>prevCount){{const diff=info.count-prevCount;showToast('# '+info.name,diff+' new message'+(diff>1?'s':''),()=>{{switchTab('group');loadGroupThread(parseInt(gid),info.name,true);}});pushNotif('# '+info.name,diff+' new message'+(diff>1?'s':''),'group');}}}});const newPriv=d.private_rooms||{{}},prevPriv=_prevNotif.private_rooms||{{}};Object.entries(newPriv).forEach(([rid,info])=>{{const prevCount=(prevPriv[rid]&&prevPriv[rid].count)||0;if(info.count>prevCount){{const diff=info.count-prevCount;showToast('&#128274; '+info.name,diff+' new message'+(diff>1?'s':''),()=>{{switchTab('private');loadPrivateThread(parseInt(rid),info.name,true);}});pushNotif('&#128274; '+info.name,diff+' new message'+(diff>1?'s':''),'private');}}}});if(d.posts>0&&d.posts>_prevNotif.posts&&_prevNotif.posts>=0){{showToast('COMMUNITY BOARD','New post from a member',()=>switchTab('board'));pushNotif('COMMUNITY','New community post','board');}}setBadge('badgeDM',d.dm);setBadge('badgeGroup',d.group);setBadge('badgePrivate',d.private);setBadge('badgeBoard',d.posts);document.title=d.total>0?'('+d.total+') VOX':'VOX';_prevNotif={{dm:d.dm,group:d.group,private:d.private,posts:d.posts,groups:newGroups,private_rooms:newPriv}};}}catch(e){{}}}}
async function loadTrafficCounter(){{try{{const d=await api('/api/traffic/public');if(d.ok){{$('tcOnline').textContent=d.online;$('tcToday').textContent=d.today;$('tcTotal').textContent=d.total;if($('tcMembers'))$('tcMembers').textContent=d.members;}}}}catch(e){{}}}}
async function loadOnlineUsers(){{try{{const d=await api('/api/online');if(d.ok){{onlineUsers=new Set(d.online);if($('dmConvList'))loadDMConversations();}}}}catch(e){{}}}}
function homeRunSearch(){{const q=$('homeSearchInput');if(!q)return;const query=q.value.trim();if(!query)return;window.open('https://www.google.com/search?q='+encodeURIComponent(query),'_blank','noopener,noreferrer');}}
function renderBubbles(messages,me,container){{if(!messages||!messages.length){{container.innerHTML='<div style="opacity:.3;text-align:center;margin:auto;font-size:12px;">NO MESSAGES YET</div>';return;}}const atBottom=container.scrollHeight-container.scrollTop-container.clientHeight<60;container.innerHTML=messages.map(m=>{{const mine=m.sender===me;return `<div class="bubble-row ${{mine?'mine':''}}"><div class="bubble-avatar">${{m.sender.substring(0,2).toUpperCase()}}</div><div class="bubble-content"><div class="bubble">${{m.content}}</div><div class="bubble-meta">${{mine?'YOU':m.sender}} &middot; ${{m.timestamp}}</div></div></div>`;}}).join('');if(atBottom||container.scrollTop===0)container.scrollTop=container.scrollHeight;}}
function mobileShowChat(type){{if(!isMobile())return;const sidebarId={{dm:'dmSidebar',group:'groupSidebar',private:'privateSidebar'}}[type];const mainId={{dm:'dmMain',group:'groupMain',private:'privateMain'}}[type];const s=$(sidebarId),m=$(mainId);if(s)s.classList.remove('mobile-show');if(m){{m.classList.add('mobile-show');const btn=m.querySelector('.mobile-back-btn');if(btn)btn.style.display='flex';}}}}
function mobileShowSidebar(type){{if(!isMobile())return;const sidebarId={{dm:'dmSidebar',group:'groupSidebar',private:'privateSidebar'}}[type];const mainId={{dm:'dmMain',group:'groupMain',private:'privateMain'}}[type];const s=$(sidebarId),m=$(mainId);if(s)s.classList.add('mobile-show');if(m)m.classList.remove('mobile-show');const b=m&&m.querySelector('.mobile-back-btn');if(b)b.style.display='none';}}
async function dmUserSearch(){{const q=$('newDmUser').value.trim(),box=$('dmUserSuggest');if(!q){{box.style.display='none';return;}}const d=await api('/api/users/search?q='+encodeURIComponent(q));box.style.display=d.ok&&d.users.length?'block':'none';if(d.ok&&d.users.length)box.innerHTML=d.users.map(u=>`<div style="padding:10px 14px;cursor:pointer;border-bottom:1px solid var(--p10);" onmouseover="this.style.background='var(--p10)'" onmouseout="this.style.background=''" onmousedown="event.preventDefault();dmSelectUser('${{u}}');">&#128100; ${{u}}</div>`).join('');}}
function hideDmSuggest(){{const b=$('dmUserSuggest');if(b)b.style.display='none';}}
function dmSelectUser(u){{$('newDmUser').value=u;hideDmSuggest();loadDMThread(u,true);mobileShowChat('dm');}}
async function loadDMConversations(){{const d=await api('/api/dm/conversations'),box=$('dmConvList');if(!d.ok||!d.conversations.length){{box.innerHTML=`<div style="padding:10px;font-size:11px;opacity:.4;">${{d.ok?'NO DMS YET':'LOGIN TO VIEW'}}</div>`;return;}}box.innerHTML=d.conversations.map(c=>`<div class="conv-item ${{activeDMUser===c.username?'active':''}}" onclick="loadDMThread('${{c.username}}',true)"><span>${{onlineUsers.has(c.username)?'<span style="display:inline-block;width:7px;height:7px;border-radius:50%;background:#0f0;box-shadow:0 0 5px #0f0;margin-right:5px;vertical-align:middle;"></span>':''}}${{c.username}}</span>${{c.unread>0?`<span style="background:var(--p);color:#000;border-radius:50%;padding:1px 5px;font-size:9px;font-weight:bold;">${{c.unread}}</span>`:''}}</div>`).join('');}}
async function dmDelete(u){{if(!confirm('DELETE conversation with '+u+'?'))return;await api('/api/dm/delete',{{username:u}});if(activeDMUser===u){{activeDMUser=null;$('dmThreadTitle').textContent='SELECT A CONVERSATION';$('dmMessages').innerHTML='';$('dmCompose').style.display='none';}}loadDMConversations();}}
async function dmBlock(u){{if(!confirm('BLOCK '+u+'? This will delete your conversation and prevent future messages.'))return;await api('/api/dm/block',{{username:u}});if(activeDMUser===u){{activeDMUser=null;$('dmThreadTitle').textContent='SELECT A CONVERSATION';$('dmMessages').innerHTML='';$('dmCompose').style.display='none';}}loadDMConversations();}}
async function loadDMThread(username,updateSidebar=true){{activeDMUser=username;mobileShowChat('dm');api('/api/mark-read',{{type:'dm',id:username}});_prevNotif.dm=0;const d=await api('/api/dm/thread?with='+encodeURIComponent(username));if(!d.ok)return;$('dmThreadTitle').innerHTML=`DM: ${{username}}&nbsp;<button class="send-btn" style="font-size:10px;padding:3px 10px;border-color:#f84;color:#f84;margin-left:6px;" onclick="dmDelete('${{username}}')">&#128465; DEL</button><button class="send-btn" style="font-size:10px;padding:3px 10px;border-color:#f44;color:#f44;margin-left:4px;" onclick="dmBlock('${{username}}')">&#128683; BLOCK</button>`;$('dmCompose').style.display='flex';renderBubbles(d.messages,d.me,$('dmMessages'));if(updateSidebar)loadDMConversations();}}
async function sendDM(){{const content=$('dmInput').value.trim();if(!content||!activeDMUser)return;$('dmInput').value='';const d=await api('/api/dm/send',{{to:activeDMUser,content}});if(!d.ok){{alert('ERROR: '+d.error);return;}}loadDMThread(activeDMUser,true);}}
async function loadGroups(){{const d=await api('/api/groups'),box=$('groupList');if(!d.ok||!d.groups.length){{box.innerHTML=`<div style="padding:10px;font-size:11px;opacity:.4;">${{d.ok?'NO CHANNELS':'LOGIN TO VIEW'}}</div>`;return;}}box.innerHTML=d.groups.map(g=>`<div class="conv-item ${{activeGroupId==g.id?'active':''}}" onclick="loadGroupThread(${{g.id}},'${{g.name}}',true)"><span>${{g.locked?'&#128274;':'#'}}<span style="${{g.locked?'color:#fa0':g.banned?'color:#f44':''}}">${{g.name}}</span></span>${{g.banned?'<span style="font-size:9px;color:#f44;">BANNED</span>':g.unread>0?`<span style="background:var(--p);color:#000;border-radius:50%;padding:1px 5px;font-size:9px;font-weight:bold;">${{g.unread}}</span>`:(g.member?'<span style="font-size:9px;color:var(--p);">&#9679;</span>':'')}}</div>`).join('');}}
async function createGroup(){{const name=$('newGroupName').value.trim().toUpperCase();if(!name)return;const d=await api('/api/groups/create',{{name}});$('newGroupName').value='';if(d.ok){{loadGroups();loadGroupThread(d.id,name,false);}}else alert('ERROR: '+d.error);}}
async function deleteGroup(gid,gname){{if(!confirm('DELETE #'+gname+'?'))return;const d=await api('/api/admin/delete-channel',{{group_id:gid}});if(d.ok){{if(activeGroupId===gid){{activeGroupId=null;activeGroupName=null;$('groupThreadTitle').textContent='SELECT A CHANNEL';$('groupMessages').innerHTML='';$('groupCompose').style.display='none';}}loadGroups();}}else alert('ERROR: '+d.error);}}
async function loadGroupThread(gid,gname,updateSidebar=true){{activeGroupId=gid;activeGroupName=gname;mobileShowChat('group');api('/api/mark-read',{{type:'group',id:gid}});if(_prevNotif.groups){{delete _prevNotif.groups[String(gid)];_prevNotif.group=Object.values(_prevNotif.groups).reduce((s,v)=>s+v.count,0);}}setBadge('badgeGroup',_prevNotif.group);const d=await api('/api/groups/'+gid+'/messages'),msgBox=$('groupMessages');if(!d.ok){{msgBox.innerHTML='<div style="opacity:.4;text-align:center;margin:auto;font-size:12px;">ERROR</div>';return;}}const lockBadge=d.locked?'<span style="color:#fa0;font-size:10px;margin-left:6px;">&#128274; LOCKED</span>':'';const renameBtnG=d.admin?`<button onclick="renameChat('group',${{gid}})" style="background:none;border:1px solid var(--p);border-radius:4px;color:var(--p);cursor:pointer;font-size:9px;padding:2px 7px;margin-left:6px;font-family:'Courier New',monospace;">&#9998;</button>`:'';const delBtnG=d.admin?`<button onclick="deleteGroup(${{gid}},'${{gname}}')" style="background:none;border:1px solid #f44;border-radius:4px;color:#f44;cursor:pointer;font-size:9px;padding:2px 7px;margin-left:4px;font-family:'Courier New',monospace;">&#128465;</button>`:'';$('groupThreadTitle').innerHTML='# '+(gname||'CHANNEL')+lockBadge+renameBtnG+delBtnG;const jlBtn=$('joinLeaveBtn');if(d.admin&&d.members&&d.members.length){{jlBtn.style.display='inline-block';jlBtn.textContent='&#128100; MEMBERS &#9663;';jlBtn.onclick=()=>{{const existing=$('memberDropdown');if(existing){{existing.remove();return;}}const dd=document.createElement('div');dd.id='memberDropdown';dd.style.cssText='position:absolute;right:0;top:100%;background:#000;border:2px solid var(--p);border-radius:8px;z-index:9999;min-width:200px;box-shadow:0 0 20px var(--p30);max-height:200px;overflow-y:auto;';dd.innerHTML=`<div style="padding:6px 12px;font-size:9px;opacity:.5;border-bottom:1px solid var(--p30);">&#128100; MEMBERS</div>`+d.members.map(u=>`<div style="padding:8px 12px;display:flex;justify-content:space-between;align-items:center;border-bottom:1px solid var(--p10);font-size:11px;"><span>${{onlineUsers.has(u)?'<span style="display:inline-block;width:7px;height:7px;border-radius:50%;background:#0f0;box-shadow:0 0 5px #0f0;margin-right:5px;vertical-align:middle;"></span>':''}}${{u}}</span></div>`).join('');jlBtn.parentElement.style.position='relative';jlBtn.parentElement.appendChild(dd);setTimeout(()=>document.addEventListener('click',function h(e){{if(!dd.contains(e.target)&&e.target!==jlBtn){{dd.remove();document.removeEventListener('click',h);}}}},0));}};}}else{{jlBtn.style.display='none';}}$('groupCompose').style.display=d.member&&!d.locked?'flex':'none';if(!d.member)msgBox.innerHTML='<div style="opacity:.4;text-align:center;margin:auto;font-size:12px;">YOU ARE BANNED FROM THIS CHANNEL</div>';else renderBubbles(d.messages,d.me,msgBox);if(updateSidebar)loadGroups();}}
async function sendGroupMsg(){{const content=$('groupInput').value.trim();if(!content||activeGroupId===null)return;$('groupInput').value='';await api('/api/groups/send',{{group_id:activeGroupId,content}});loadGroupThread(activeGroupId,activeGroupName,false);}}
async function loadPrivateRooms(){{const box=$('privateRoomList');if(!box)return;const d=await api('/api/private/rooms');if(!d.ok){{box.innerHTML='<div style="padding:10px;font-size:11px;opacity:.4;">NO ACCESS</div>';return;}}const footer=$('privateAdminFooter');if(footer)footer.style.display=d.is_admin?'block':'none';if(!d.rooms.length){{box.innerHTML='<div style="padding:10px;font-size:11px;opacity:.4;">NO ROOMS YET</div>';return;}}box.innerHTML=d.rooms.map(r=>`<div class="conv-item ${{activePrivateRoomId==r.id?'active':''}}" onclick="loadPrivateThread(${{r.id}},'${{r.name}}')"><span>&#128274; ${{r.name}}</span>${{r.unread>0?`<span style="background:var(--p);color:#000;border-radius:50%;padding:1px 5px;font-size:9px;font-weight:bold;">${{r.unread}}</span>`:''}}</div>`).join('');}}
async function createPrivateRoom(){{const name=$('newRoomName').value.trim().toUpperCase();if(!name)return;const d=await api('/api/private/create',{{name}});$('newRoomName').value='';if(d.ok){{loadPrivateRooms();loadPrivateThread(d.id,name);}}else alert('ERROR: '+d.error);}}
async function loadPrivateThread(rid,rname,updateSidebar=true){{activePrivateRoomId=rid;activePrivateRoomName=rname;api('/api/mark-read',{{type:'private',id:rid}});if(_prevNotif.private_rooms){{delete _prevNotif.private_rooms[String(rid)];_prevNotif.private=Object.values(_prevNotif.private_rooms).reduce((s,v)=>s+v.count,0);}}setBadge('badgePrivate',_prevNotif.private);if(isMobile())mobileShowChat('private');const d=await api('/api/private/'+rid+'/messages');if(!d.ok)return;const renameBtnP=d.is_admin?`<button onclick="renameChat('private',${{rid}})" style="background:none;border:1px solid var(--p);border-radius:4px;color:var(--p);cursor:pointer;font-size:9px;padding:2px 7px;margin-left:6px;font-family:'Courier New',monospace;">&#9998;</button>`:'';$('privateRoomTitle').innerHTML='&#128274; '+(rname||'PRIVATE ROOM')+renameBtnP;const mb=$('privateMembersBtn');if(mb)mb.style.display=d.is_admin?'inline-block':'none';$('privateCompose').style.display='flex';renderBubbles(d.messages,d.me,$('privateMessages'));if(updateSidebar)loadPrivateRooms();}}
async function sendPrivateMsg(){{const content=$('privateInput').value.trim();if(!content||!activePrivateRoomId)return;$('privateInput').value='';const d=await api('/api/private/send',{{room_id:activePrivateRoomId,content}});if(!d.ok){{alert('ERROR: '+d.error);return;}}loadPrivateThread(activePrivateRoomId,activePrivateRoomName,false);}}
async function showPrivateMembers(){{const d=await api('/api/private/'+activePrivateRoomId+'/members');if(!d.ok)return;const existing=$('privateMemberDropdown');if(existing){{existing.remove();return;}}const dd=document.createElement('div');dd.id='privateMemberDropdown';dd.style.cssText='position:absolute;right:0;top:100%;background:#000;border:2px solid var(--p);border-radius:8px;z-index:9999;min-width:240px;box-shadow:0 0 20px var(--p30);max-height:240px;overflow-y:auto;';const addRow=`<div style="padding:8px 12px;border-bottom:1px solid var(--p30);position:relative;"><div style="display:flex;gap:6px;"><input id="addMemberInput" class="field-plain" placeholder="&#128269; SEARCH USERS..." style="margin:0;flex:1;padding:6px 10px;font-size:11px;border-radius:20px;" autocomplete="off" oninput="memberSearchSuggest()" onkeydown="if(event.key==='Escape')hideMemberSuggest();"><button class="btn-action" style="margin:0;padding:4px 10px;font-size:10px;" onclick="addPrivateMember()">ADD</button></div><div id="memberSuggest" style="display:none;position:absolute;left:12px;right:12px;top:calc(100% - 4px);background:#000;border:2px solid var(--p);border-radius:8px;box-shadow:0 0 20px var(--p30);z-index:99999;max-height:140px;overflow-y:auto;font-size:11px;"></div></div>`;dd.innerHTML=addRow+d.members.map(u=>`<div style="padding:8px 12px;display:flex;justify-content:space-between;align-items:center;border-bottom:1px solid var(--p10);font-size:11px;"><span>&#128100; ${{u}}</span><button class="btn-action" style="margin:0;padding:2px 8px;font-size:10px;border-color:#f44;color:#f44;" onclick="removePrivateMember('${{u}}')">REMOVE</button></div>`).join('');$('privateMembersBtn').parentElement.style.position='relative';$('privateMembersBtn').parentElement.appendChild(dd);setTimeout(()=>document.addEventListener('click',function h(e){{if(!dd.contains(e.target)&&e.target!==$('privateMembersBtn')){{dd.remove();document.removeEventListener('click',h);}}}},0));}}
async function memberSearchSuggest(){{const q=$('addMemberInput').value.trim(),box=$('memberSuggest');if(!q){{if(box)box.style.display='none';return;}}const d=await api('/api/users/search?q='+encodeURIComponent(q));if(!box)return;if(!d.ok||!d.users.length){{box.style.display='none';return;}}box.style.display='block';box.innerHTML=d.users.map(u=>`<div style="padding:8px 12px;cursor:pointer;border-bottom:1px solid var(--p10);" onmouseover="this.style.background='var(--p10)'" onmouseout="this.style.background=''" onmousedown="event.preventDefault();$('addMemberInput').value='${{u}}';hideMemberSuggest();">&#128100; ${{u}}</div>`).join('');}}
function hideMemberSuggest(){{const b=$('memberSuggest');if(b)b.style.display='none';}}
async function addPrivateMember(){{hideMemberSuggest();const u=$('addMemberInput').value.trim();if(!u)return;const d=await api('/api/private/add-member',{{room_id:activePrivateRoomId,username:u}});if(!d.ok){{alert('ERROR: '+d.error);return;}}const dd=$('privateMemberDropdown');if(dd)dd.remove();showPrivateMembers();}}
async function removePrivateMember(u){{if(!confirm('REMOVE '+u+' FROM ROOM?'))return;await api('/api/private/remove-member',{{room_id:activePrivateRoomId,username:u}});const dd=$('privateMemberDropdown');if(dd)dd.remove();showPrivateMembers();}}
async function renameChat(type,id){{const current=type==='group'?activeGroupName:activePrivateRoomName;const newName=prompt('RENAME CHAT:',current);if(!newName||!newName.trim()||newName.trim().toUpperCase()===current)return;const d=await api('/api/'+type+'/rename',{{id,name:newName.trim().toUpperCase()}});if(!d.ok){{alert('ERROR: '+d.error);return;}}if(type==='group'){{activeGroupName=newName.trim().toUpperCase();loadGroupThread(id,activeGroupName,true);}}else{{activePrivateRoomName=newName.trim().toUpperCase();loadPrivateThread(id,activePrivateRoomName,true);}}}}
function switchTab(tab){{['DM','Group','Private','Board'].forEach(t=>{{const tl=t.toLowerCase();const tabEl=$('tab'+t),contentEl=$('tabContent'+t);if(tabEl)tabEl.classList.toggle('active',tl===tab);if(contentEl){{contentEl.style.display=tl===tab?'block':'none';contentEl.classList.toggle('active',tl===tab);}}}});if(tab==='dm'){{loadDMConversations();setBadge('badgeDM',0);api('/api/mark-read',{{type:'dm',id:activeDMUser||''}});_prevNotif.dm=0;}}else if(tab==='group'){{loadGroups();}}else if(tab==='private'){{loadPrivateRooms();}}else if(tab==='board'){{loadPosts();markPostsRead();}}}}
loadTrafficCounter();setInterval(loadTrafficCounter,10000);requestNotifPermission();checkNotifications();setInterval(checkNotifications,8000);loadOnlineUsers();setInterval(loadOnlineUsers,10000);
(async function secNavPoll(){{
  const dot=document.getElementById('secStatusDot');if(!dot)return;
  const DISMISS_KEY='vox_sec_dismiss';
  // Inject alert bar into body (once)
  if(!document.getElementById('_secAlertBar')){{
    const bar=document.createElement('div');
    bar.id='_secAlertBar';
    bar.style.cssText='display:none;position:fixed;top:0;left:0;width:100%;z-index:99999;background:#cc0000;color:#fff;font-family:\'Courier New\',monospace;font-size:12px;font-weight:bold;letter-spacing:2px;text-transform:uppercase;padding:10px 16px;box-sizing:border-box;display:none;align-items:center;justify-content:space-between;gap:12px;animation:tcPulse 1.5s infinite;';
    bar.innerHTML='<span id="_secAlertMsg">&#9888; CRITICAL SECURITY ALERT</span><button onclick="window._secDismiss()" style="background:#fff;color:#cc0000;border:none;border-radius:6px;padding:5px 14px;font-family:\'Courier New\',monospace;font-size:11px;font-weight:bold;cursor:pointer;letter-spacing:1px;">&#10006; DISMISS</button>';
    document.body.prepend(bar);
  }}
  window._secDismiss=function(){{
    const dismissed=JSON.parse(localStorage.getItem(DISMISS_KEY)||'{{}}');
    dismissed.until=Date.now()+(2*60*60*1000); // dismiss for 2 hours
    localStorage.setItem(DISMISS_KEY,JSON.stringify(dismissed));
    document.getElementById('_secAlertBar').style.display='none';
    // Restore original theme CSS vars
    const root=document.documentElement;
    root.style.removeProperty('--p');root.style.removeProperty('--bg');
    root.style.removeProperty('--ac');root.style.removeProperty('--p10');
    root.style.removeProperty('--p30');
  }};
  async function updateDot(){{
    try{{
      const r=await fetch('/api/security/reports').then(r=>r.json());
      if(!r.ok||!r.reports.length){{dot.style.background='#555';dot.title='NO SCANS YET';return;}}
      const rpt=r.reports[0];
      const harmful=rpt.harmful_content?.length??0;
      const broken=rpt.broken_links?.length??0;
      const sslOk=rpt.ssl?.ok;
      const changes=rpt.content_changes?.length??0;
      const dismissed=JSON.parse(localStorage.getItem(DISMISS_KEY)||'{{}}');
      const isDismissed=dismissed.until&&Date.now()<dismissed.until;
      const bar=document.getElementById('_secAlertBar');
      const root=document.documentElement;
      if((harmful>0||!sslOk)&&r.reports[0].pages_scanned>0){{
        dot.style.background='#ff2222';dot.style.boxShadow='0 0 8px #ff2222';dot.title='CRITICAL — CLICK TO VIEW';
        if(!isDismissed){{
          // Switch entire theme to red alert
          root.style.setProperty('--p','#ff2222');root.style.setProperty('--bg','#0a0000');
          root.style.setProperty('--ac','#330000');root.style.setProperty('--p10','#ff222233');
          root.style.setProperty('--p30','#ff222266');
          if(bar){{bar.style.display='flex';document.getElementById('_secAlertMsg').textContent='\u26a0 CRITICAL: '+(harmful>0?harmful+' HARMFUL PAGE'+(harmful>1?'S':'')+' DETECTED':'SSL CERTIFICATE ISSUE');}}
        }}
      }}else if(broken>0||changes>0){{
        dot.style.background='#ffaa00';dot.style.boxShadow='0 0 8px #ffaa00';dot.title='WARNINGS — CLICK TO VIEW';
        if(bar)bar.style.display='none';
        root.style.removeProperty('--p');root.style.removeProperty('--bg');
        root.style.removeProperty('--ac');root.style.removeProperty('--p10');root.style.removeProperty('--p30');
      }}else{{
        dot.style.background='#00ff88';dot.style.boxShadow='0 0 8px #00ff88';dot.title='ALL CLEAR';
        if(bar)bar.style.display='none';
        root.style.removeProperty('--p');root.style.removeProperty('--bg');
        root.style.removeProperty('--ac');root.style.removeProperty('--p10');root.style.removeProperty('--p30');
      }}
    }}catch{{dot.style.background='#555';}}
  }}
  updateDot();setInterval(updateDot,30000);
}})();
if($('newsFeed')){{loadNewsFeed();setInterval(()=>{{if($('newsFeed'))loadNewsFeed();}},300000);}}
if($('dmConvList')){{['tabContentDM','tabContentGroup','tabContentPrivate','tabContentBoard'].forEach((id,i)=>{{const el=$(id);if(el)el.style.display=i===0?'block':'none';}});loadDMConversations();loadGroups();loadPrivateRooms();if(isMobile()){{mobileShowSidebar('dm');mobileShowSidebar('group');mobileShowSidebar('private');}}setInterval(()=>{{if(activeDMUser)loadDMThread(activeDMUser,false);if(activeGroupId)loadGroupThread(activeGroupId,activeGroupName,false);if(activePrivateRoomId)loadPrivateThread(activePrivateRoomId,activePrivateRoomName,false);}},5000);}}
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
@app.route("/")
def home():
    user=session.get("username");theme=session.get("theme","green");admin=is_admin(user)
    # COMMS tile     = logged-in users only (not public)
    # VOX POPULI tile = admin only
    # All other tiles = admin only
    def _tile(i,l,h):
        if l=="COMMS" and not user: return ""            # must be logged in
        if l!="COMMS" and not admin: return ""           # everything else = admin only
        if h=="#": return f'<a class="tile" href="#"><i class="fas {i}"></i><div>| {l} |</div></a>'
        return f'<a class="tile" href="{h}" target="_blank" rel="noopener noreferrer"><i class="fas {i}"></i><div>| {l} |</div></a>'
    tiles="".join(_tile(i,l,h) for i,l,h in NAV_ITEMS)
    def _bdg(i): return f'<span id="{i}" style="display:none;background:var(--p);color:#000;border-radius:50%;padding:1px 5px;font-size:9px;margin-left:3px;"></span>'
    board_tab=f'''<div class="tab-content" id="tabContentBoard"><div style="display:flex;flex-direction:column;height:100%;"><div style="flex:1;overflow-y:auto;max-height:320px;background:rgba(0,0,0,.75);" id="postFeed"><div style="padding:16px;opacity:.4;text-align:center;font-size:11px;">LOADING...</div></div><div id="postErr" class="error-msg" style="padding:0 12px;margin:0;"></div><div style="padding:9px 10px;border-top:2px solid var(--p);display:flex;gap:7px;align-items:flex-end;background:rgba(0,0,0,.9);"><textarea id="postContent" placeholder="SHARE WITH THE COMMUNITY..." oninput="updatePostCount(this)" style="flex:1;padding:9px 14px;background:rgba(0,0,0,.8);border:2px solid var(--p);border-radius:12px;color:var(--p);font-family:'Courier New',monospace;font-size:12px;text-transform:none;resize:none;height:38px;max-height:120px;overflow-y:auto;line-height:1.4;box-sizing:border-box;" onkeydown="if(event.key==='Enter'&&!event.shiftKey){{event.preventDefault();submitPost();}}"></textarea><div style="display:flex;flex-direction:column;align-items:flex-end;gap:4px;"><span id="postCount" style="font-size:9px;opacity:.3;">0/500</span><button class="send-btn" onclick="submitPost()">&#9658;</button></div></div></div></div>'''
    chat_tabs=(f'<div class="tab-bar" style="border-left:2px solid var(--p);border-right:2px solid var(--p);">'
        f'<button class="tab active" id="tabDM" onclick="switchTab(\'dm\')">DIRECT MSG {_bdg("badgeDM")}</button>'
        f'<button class="tab" id="tabGroup" onclick="switchTab(\'group\')">CHANNELS {_bdg("badgeGroup")}</button>'
        f'<button class="tab" id="tabPrivate" onclick="switchTab(\'private\')">&#128274; PRIVATE {_bdg("badgePrivate")}</button>'
        f'<button class="tab" id="tabBoard" onclick="switchTab(\'board\')">&#9998; BOARD {_bdg("badgeBoard")}</button></div>')
    dm_tab=('<div class="tab-content active" id="tabContentDM"><div class="comms-layout" style="border-top:none;"><div class="comms-sidebar" id="dmSidebar"><div class="comms-sidebar-header">CONVERSATIONS</div><div class="conv-list" id="dmConvList"><div style="padding:10px;font-size:11px;opacity:.4;">LOADING...</div></div><div class="sidebar-footer" style="position:relative;"><input class="field-plain" id="newDmUser" placeholder="&#128269; SEARCH USER..." type="text" style="margin:0;font-size:11px;padding:7px 10px;border-radius:20px;width:100%;box-sizing:border-box;" oninput="dmUserSearch()" onkeydown="if(event.key===\'Escape\')hideDmSuggest();" autocomplete="off"><div id="dmUserSuggest" style="display:none;position:absolute;bottom:calc(100% + 4px);left:0;right:0;background:#000;border:2px solid var(--p);border-radius:8px;box-shadow:0 0 20px var(--p30);z-index:9999;max-height:160px;overflow-y:auto;font-size:11px;"></div></div></div><div class="comms-main" id="dmMain"><div class="comms-thread-header"><button class="mobile-back-btn send-btn" style="padding:6px 12px;font-size:11px;margin-right:8px;display:none;" onclick="mobileShowSidebar(\'dm\')">&#9664; BACK</button><span id="dmThreadTitle" style="flex:1;">SELECT A CONVERSATION</span></div><div class="comms-messages" id="dmMessages"><div style="opacity:.3;text-align:center;margin:auto;font-size:12px;">SELECT A CONVERSATION</div></div><div class="comms-compose" id="dmCompose" style="display:none;"><input type="text" id="dmInput" placeholder="MESSAGE..." onkeydown="if(event.key===\'Enter\')sendDM()"><button class="send-btn" onclick="sendDM()">&#9658;</button></div></div></div></div>')
    grp_footer=(f'<div class="sidebar-footer" id="groupCreateFooter" style="display:{"block" if admin else "none"};"><input class="field-plain" id="newGroupName" placeholder="CHANNEL NAME" type="text" style="margin:0;font-size:11px;padding:7px;border-radius:20px;" onkeydown="if(event.key===\'Enter\')createGroup()"><button class="btn-action" style="margin-top:6px;padding:5px 8px;font-size:10px;width:100%;" onclick="createGroup()">+ CREATE</button></div>')
    grp_tab=(f'<div class="tab-content" id="tabContentGroup"><div class="comms-layout" style="border-top:none;"><div class="comms-sidebar" id="groupSidebar"><div class="comms-sidebar-header">CHANNELS</div><div class="conv-list" id="groupList"><div style="padding:10px;font-size:11px;opacity:.4;">LOADING...</div></div>{grp_footer}</div><div class="comms-main" id="groupMain"><div class="comms-thread-header"><button class="mobile-back-btn send-btn" style="padding:6px 12px;font-size:11px;margin-right:8px;display:none;" onclick="mobileShowSidebar(\'group\')">&#9664; BACK</button><span id="groupThreadTitle" style="flex:1;">SELECT A CHANNEL</span><button class="send-btn" id="joinLeaveBtn" style="display:none;font-size:10px;padding:5px 12px;"></button></div><div class="comms-messages" id="groupMessages"><div style="opacity:.3;text-align:center;margin:auto;font-size:12px;">SELECT A CHANNEL</div></div><div class="comms-compose" id="groupCompose" style="display:none;"><input type="text" id="groupInput" placeholder="BROADCAST..." onkeydown="if(event.key===\'Enter\')sendGroupMsg()"><button class="send-btn" onclick="sendGroupMsg()">&#9658;</button></div></div></div></div>')
    prv_tab=('<div class="tab-content" id="tabContentPrivate"><div class="comms-layout" style="border-top:none;"><div class="comms-sidebar" id="privateSidebar"><div class="comms-sidebar-header">PRIVATE ROOMS</div><div class="conv-list" id="privateRoomList"><div style="padding:10px;font-size:11px;opacity:.4;">LOADING...</div></div><div class="sidebar-footer" id="privateAdminFooter" style="display:none;"><input class="field-plain" id="newRoomName" placeholder="ROOM NAME" type="text" style="margin:0;font-size:11px;padding:7px;border-radius:20px;" onkeydown="if(event.key===\'Enter\')createPrivateRoom()"><button class="btn-action" style="margin-top:6px;padding:5px 8px;font-size:10px;width:100%;" onclick="createPrivateRoom()">+ CREATE ROOM</button></div></div><div class="comms-main" id="privateMain"><div class="comms-thread-header"><button class="mobile-back-btn send-btn" style="padding:6px 12px;font-size:11px;margin-right:8px;display:none;" onclick="mobileShowSidebar(\'private\')">&#9664; BACK</button><span id="privateRoomTitle" style="flex:1;">SELECT A ROOM</span><button id="privateMembersBtn" style="display:none;background:none;border:1px solid var(--p);border-radius:6px;color:var(--p);cursor:pointer;font-size:10px;padding:3px 10px;font-family:\'Courier New\',monospace;" onclick="showPrivateMembers()">&#128100; MEMBERS</button></div><div class="comms-messages" id="privateMessages"><div style="opacity:.3;text-align:center;margin:auto;font-size:12px;">SELECT A ROOM</div></div><div class="comms-compose" id="privateCompose" style="display:none;"><input type="text" id="privateInput" placeholder="MESSAGE..." onkeydown="if(event.key===\'Enter\')sendPrivateMsg()"><button class="send-btn" onclick="sendPrivateMsg()">&#9658;</button></div></div></div></div>')
    chat_panel=cyber_box("// CHAT //",dm_tab+grp_tab+prv_tab+board_tab,
        title_right='<span style="font-size:10px;opacity:.7;letter-spacing:1px;">[SYSTEM STATUS] [ACTIVE] &mdash; THE VOICE OF THE PEOPLE.</span><span style="font-size:9px;opacity:.4;">&#11041; FERNET-256 E2E ENCRYPTED &middot; AUTO-REFRESH 5s</span>',
        extra_header=chat_tabs,body_style="border-top:none;",border_top=False) if user else ""
    search_bar=('<div class="search-box" style="width:100%;margin:0 0 24px;"><div class="search-row"><input class="search-input" id="homeSearchInput" placeholder="&#128270; GOOGLE SEARCH..." type="text" onkeydown="if(event.key===\'Enter\')homeRunSearch()"><button class="search-btn" onclick="homeRunSearch()">&#128270; SEARCH</button></div></div>') if user else ""
    install_banner=('<div id="installBanner" style="display:block;width:100%;margin:0 0 16px;box-sizing:border-box;"><div style="border:2px solid var(--p);border-radius:var(--r);padding:10px 16px;background:var(--p10);display:flex;align-items:center;justify-content:space-between;flex-wrap:wrap;gap:8px;"><span style="font-size:11px;letter-spacing:1px;">&#128242; INSTALL VOX APP &mdash; ACCESS FROM YOUR HOME SCREEN</span><button id="enableNotifBtn" class="btn-action" style="margin:0;padding:6px 16px;font-size:11px;" onclick="enableNotifications()">&#128276; ENABLE NOTIFICATIONS</button><div style="display:flex;gap:8px;align-items:center;"><button id="installBtn" class="btn-action" style="margin:0;padding:6px 16px;font-size:11px;" onclick="triggerInstall()">&#11015; INSTALL</button><button onclick="document.getElementById(\'installBanner\').style.display=\'none\';localStorage.setItem(\'voxInstallDismissed\',\'1\')" style="background:none;border:none;color:var(--p);cursor:pointer;font-size:14px;padding:2px 6px;">&#10006;</button></div></div><div id="iosInstallMsg" style="display:none;border:1px solid var(--p30);border-top:none;border-radius:0 0 var(--r) var(--r);padding:8px 16px;font-size:10px;opacity:.7;letter-spacing:1px;">&#63743; ON IOS: TAP THE SHARE BUTTON THEN &ldquo;ADD TO HOME SCREEN&rdquo;</div></div>'
        '<script>let _installPrompt=null;window.addEventListener(\'beforeinstallprompt\',e=>{e.preventDefault();_installPrompt=e;if(!localStorage.getItem(\'voxInstallDismissed\')){const b=document.getElementById(\'installBanner\');if(b)b.style.display=\'block\';}});window.addEventListener(\'appinstalled\',()=>{const b=document.getElementById(\'installBanner\');if(b)b.style.display=\'none\';localStorage.setItem(\'voxInstallDismissed\',\'1\');});if(typeof Notification!==\'undefined\'&&Notification.permission!==\'granted\'&&Notification.permission!==\'denied\'){const btn=document.getElementById(\'enableNotifBtn\');if(btn)btn.style.display=\'inline-block\';}if(typeof Notification!==\'undefined\'&&Notification.permission===\'granted\'){const m=document.getElementById(\'notifMenuItem\');if(m)m.style.display=\'none\';}function triggerInstall(){if(_installPrompt){_installPrompt.prompt();_installPrompt.userChoice.then(r=>{if(r.outcome===\'accepted\')localStorage.setItem(\'voxInstallDismissed\',\'1\');_installPrompt=null;});}}const isIOS=/iphone|ipad|ipod/i.test(navigator.userAgent)&&!window.MSStream;const isStandalone=window.navigator.standalone===true||window.matchMedia(\'(display-mode: standalone)\').matches;if(!isStandalone&&!localStorage.getItem(\'voxInstallDismissed\')){const b=document.getElementById(\'installBanner\');if(b)b.style.display=\'block\';}if(isIOS&&!isStandalone&&!localStorage.getItem(\'voxInstallDismissed\')){const b=document.getElementById(\'installBanner\');const ios=document.getElementById(\'iosInstallMsg\');const btn=document.getElementById(\'installBtn\');if(b)b.style.display=\'block\';if(ios)ios.style.display=\'block\';if(btn)btn.style.display=\'none\';}</script>') if user else ""
    news_tabs=('<div style="display:flex;border-left:2px solid var(--p);border-right:2px solid var(--p);"><button id="newsTabWorld" onclick="switchNewsTab(\'world\')" style="flex:1;padding:6px 2px;background:var(--p);color:#000;border:none;border-bottom:2px solid var(--p);font-family:\'Courier New\',monospace;font-size:9px;font-weight:bold;text-transform:uppercase;cursor:pointer;">&#127760; WORLD</button><button id="newsTabUS" onclick="switchNewsTab(\'usnews\')" style="flex:1;padding:6px 2px;background:var(--p10);color:var(--p);border:none;border-left:2px solid var(--p);border-bottom:2px solid var(--p);font-family:\'Courier New\',monospace;font-size:9px;font-weight:bold;text-transform:uppercase;cursor:pointer;">&#127482;&#127480; US NEWS</button><button id="newsTabEpstein" onclick="switchNewsTab(\'epstein\')" style="flex:1;padding:6px 2px;background:var(--p10);color:var(--p);border:none;border-left:2px solid var(--p);border-bottom:2px solid var(--p);font-family:\'Courier New\',monospace;font-size:9px;font-weight:bold;text-transform:uppercase;cursor:pointer;">&#128269; EPSTEIN</button></div>')
    news_panel=cyber_box("// LIVE NEWS //","<div id=\"newsFeed\" style=\"padding:16px;opacity:.4;text-align:center;font-size:11px;\">&#128256; FETCHING NEWS...</div>",
        title_right='<span id="newsFeedStatus" style="font-size:9px;opacity:.4;letter-spacing:1px;">LOADING...</span>',
        extra_header=news_tabs,max_h="320px",border_top=False) if user else ""
    content=(f'<div class="command-wrapper"><div style="text-align:center;width:100%;"><div class="tile-grid">{tiles}</div></div>'
        f'{install_banner}{search_bar}{chat_panel}{news_panel}'
        f'<div class="content-box">The system is broken! We rely on big corporations to supply us — that\'s why they can inflate prices!<br><br>We build the future we want to live in by growing our own food and bartering. Buy local, sell local!</div>'
        f'<div class="content-box">If you have landed here, you are wondering if there is a different way to live. We will show you exactly how, step by step.</div>'
        f'<div class="three-column-grid"><div class="column"><h3>THE TRUTH</h3><p>Wealth gap and corporate reliance truth.</p><a class="btn-action" href="https://www.youtube.com/watch?v=pb0OCI9qwIU" target="_blank" rel="noopener noreferrer">&#9658; WATCH</a></div>'
        f'<div class="column"><h3>ORGANIZE</h3><p>Grow food, barter, and rebuild community.</p><a class="btn-action" href="https://www.youtube.com/watch?v=shIfzNOcNvs" target="_blank" rel="noopener noreferrer">&#9658; LEARN</a></div>'
        f'<div class="column"><h3>COMMUNITY</h3><p>Join our TikTok community and say hello!</p><a class="btn-action" href="#" target="_blank" rel="noopener noreferrer">&#9658; ACCESS</a></div></div></div>')
    return shell(content,user=user,theme=theme)
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
            for gid in [r[0] for r in fetchall(con,"SELECT id FROM groups")]:
                execute(con,"INSERT INTO group_members(group_id,username) VALUES(%s,%s) ON CONFLICT DO NOTHING",(gid,u))
        session["username"]=u;session["theme"]=t;session.permanent=True;return ok()
    except psycopg2.errors.UniqueViolation: return err("USERNAME TAKEN")
    except Exception as e: return err(str(e))
@app.route("/api/login",methods=["POST"])
def api_login():
    d=request.json or {};u,p=d.get("username","").strip(),d.get("password","")
    with db() as con:
        row=fetchone(con,"SELECT password_hash,theme FROM users WHERE username=%s",(u,))
        if not row or row[0]!=hash_pw(p): return err("INVALID CREDENTIALS")
        for gid in [r[0] for r in fetchall(con,"SELECT id FROM groups")]:
            if not fetchone(con,"SELECT 1 FROM group_banned WHERE group_id=%s AND username=%s",(gid,u)):
                execute(con,"INSERT INTO group_members(group_id,username) VALUES(%s,%s) ON CONFLICT DO NOTHING",(gid,u))
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
        if not row or row[0]!=hash_pw(cur_pw): return err("CURRENT PASSWORD INCORRECT")
        execute(con,"UPDATE users SET password_hash=%s WHERE username=%s",(hash_pw(new_pw),me()))
    return ok()
@app.route("/api/users/search")
def api_user_search():
    q=request.args.get("q","").strip()
    if not q: return ok(users=[])
    with db() as con: rows=fetchall(con,"SELECT username FROM users WHERE username ILIKE %s LIMIT 10",(f"%{q}%",))
    return ok(users=[r[0] for r in rows])
@app.route("/api/posts")
def api_posts():
    if e:=require_login(): return e
    u=me()
    with db() as con:
        rows=fetchall(con,"SELECT id,username,content,created_at FROM posts ORDER BY created_at DESC LIMIT 50")
        rxrows=fetchall(con,"SELECT post_id,username,emoji FROM post_reactions")
    rx={}
    for pid,rxu,emoji in rxrows:
        rx.setdefault(pid,{}).setdefault(emoji,{"count":0,"mine":False})
        rx[pid][emoji]["count"]+=1
        if rxu==u: rx[pid][emoji]["mine"]=True
    posts=[{"id":r[0],"username":r[1],"content":r[2],"created_at":str(r[3])[:16],"reactions":rx.get(r[0],{}),"can_delete":is_admin(u) or r[1]==u} for r in rows]
    return ok(posts=posts,me=u)
@app.route("/api/posts/create",methods=["POST"])
def api_posts_create():
    if e:=require_login(): return e
    content=(request.json or {}).get("content","").strip()
    if not content: return err("EMPTY POST")
    if len(content)>500: return err("TOO LONG")
    u=me()
    with db() as con:
        execute(con,"INSERT INTO posts(username,content) VALUES(%s,%s)",(u,content))
        members=[r[0] for r in fetchall(con,"SELECT username FROM users WHERE username!=%s",(u,))]
    for member in members: send_push(member,"\U0001f4e2 COMMUNITY BOARD",f"{u}: {content[:80]}",tag="posts")
    return ok()
@app.route("/api/posts/react",methods=["POST"])
def api_posts_react():
    if e:=require_login(): return e
    d=request.json or {};post_id,emoji=d.get("post_id"),d.get("emoji","")
    if not post_id or emoji not in VALID_EMOJIS: return err("INVALID")
    u=me()
    with db() as con:
        existing=fetchone(con,"SELECT emoji FROM post_reactions WHERE post_id=%s AND username=%s",(post_id,u))
        if existing:
            if existing[0]==emoji: execute(con,"DELETE FROM post_reactions WHERE post_id=%s AND username=%s",(post_id,u))
            else: execute(con,"UPDATE post_reactions SET emoji=%s WHERE post_id=%s AND username=%s",(emoji,post_id,u))
        else: execute(con,"INSERT INTO post_reactions(post_id,username,emoji) VALUES(%s,%s,%s)",(post_id,u,emoji))
    return ok()
@app.route("/api/posts/delete",methods=["POST"])
def api_posts_delete():
    if e:=require_login(): return e
    post_id=(request.json or {}).get("post_id");u=me()
    with db() as con:
        row=fetchone(con,"SELECT username FROM posts WHERE id=%s",(post_id,))
        if not row: return err("NOT FOUND")
        if row[0]!=u and not is_admin(u): return err("FORBIDDEN")
        execute(con,"DELETE FROM post_reactions WHERE post_id=%s",(post_id,))
        execute(con,"DELETE FROM posts WHERE id=%s",(post_id,))
    return ok()
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
    with db() as con:
        for tbl,col in [("messages","sender"),("messages","recipient"),("group_messages","sender"),("group_members","username")]:
            execute(con,f"DELETE FROM {tbl} WHERE {col}=%s",(target,))
        execute(con,"DELETE FROM users WHERE username=%s",(target,))
    return ok()
@app.route("/api/admin/dm-log")
def api_admin_dm_log():
    if e:=require_admin(): return e
    with db() as con: rows=fetchall(con,"SELECT id,sender,recipient,content_enc,timestamp FROM messages ORDER BY timestamp DESC LIMIT 200")
    return ok(messages=[{"id":r[0],"sender":r[1],"recipient":r[2],"content":dec(r[3]),"timestamp":str(r[4])} for r in rows])
@app.route("/api/admin/delete-dm",methods=["POST"])
def api_admin_delete_dm():
    if e:=require_admin(): return e
    with db() as con: execute(con,"DELETE FROM messages WHERE id=%s",(request.json.get("id"),))
    return ok()
@app.route("/api/admin/group-log")
def api_admin_group_log():
    if e:=require_admin(): return e
    with db() as con:
        rows=fetchall(con,"SELECT gm.id,g.id,g.name,gm.sender,gm.content_enc,gm.timestamp FROM group_messages gm JOIN groups g ON g.id=gm.group_id ORDER BY gm.timestamp DESC LIMIT 200")
    return ok(messages=[{"id":r[0],"group_id":r[1],"group":r[2],"sender":r[3],"content":dec(r[4]),"timestamp":str(r[5])} for r in rows])
@app.route("/api/admin/user-chat")
def api_admin_user_chat():
    if e:=require_admin(): return e
    username=request.args.get("username","").strip()
    if not username: return err("USERNAME REQUIRED")
    with db() as con:
        if not fetchone(con,"SELECT id FROM users WHERE username=%s",(username,)): return err("USER NOT FOUND")
        rows=fetchall(con,"SELECT id,sender,recipient,content_enc,timestamp FROM messages WHERE sender=%s OR recipient=%s ORDER BY timestamp ASC",(username,username))
    convos={}
    for r in rows:
        p=r[2] if r[1]==username else r[1]
        convos.setdefault(p,[]).append({"id":r[0],"sender":r[1],"recipient":r[2],"content":dec(r[3]),"timestamp":str(r[4])})
    result=[{"partner":p,"messages":m} for p,m in convos.items()]
    return ok(username=username,conversations=result,total=sum(len(c["messages"]) for c in result))
@app.route("/api/admin/delete-convo",methods=["POST"])
def api_admin_delete_convo():
    if e:=require_admin(): return e
    u1,u2=(request.json or {}).get("user1",""),(request.json or {}).get("user2","")
    if not u1 or not u2: return err("MISSING USERS")
    with db() as con: execute(con,"DELETE FROM messages WHERE (sender=%s AND recipient=%s) OR (sender=%s AND recipient=%s)",(u1,u2,u2,u1))
    return ok()
@app.route("/api/admin/delete-channel",methods=["POST"])
def api_admin_delete_channel():
    if e:=require_admin(): return e
    gid=(request.json or {}).get("group_id")
    if not gid: return err("MISSING GROUP ID")
    with db() as con:
        for sql,p in [("DELETE FROM group_messages WHERE group_id=%s",(gid,)),("DELETE FROM group_members WHERE group_id=%s",(gid,)),
                      ("DELETE FROM group_banned WHERE group_id=%s",(gid,)),("DELETE FROM groups WHERE id=%s",(gid,))]:
            execute(con,sql,p)
    return ok()
@app.route("/api/admin/delete-group-msg",methods=["POST"])
def api_admin_delete_group_msg():
    if e:=require_admin(): return e
    with db() as con: execute(con,"DELETE FROM group_messages WHERE id=%s",(request.json.get("id"),))
    return ok()
@app.route("/api/admin/lock-channel",methods=["POST"])
def api_admin_lock_channel():
    if e:=require_admin(): return e
    d=request.json or {}
    with db() as con: execute(con,"UPDATE groups SET locked=%s WHERE id=%s",(1 if d.get("lock") else 0,d.get("group_id")))
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
@app.route("/api/online")
def api_online():
    if not logged_in(): return ok(online=[])
    cutoff=utc_cutoff(2)
    with db() as con: rows=fetchall(con,"SELECT username FROM user_sessions WHERE last_seen >= %s",(cutoff,))
    return ok(online=[r[0] for r in rows])
@app.route("/api/news")
def api_news():
    if e:=require_login(): return e
    import random
    feed_type=request.args.get("type","world")
    FEEDS={
        "world":[("https://news.google.com/rss/headlines/section/topic/WORLD","WORLD"),("https://news.google.com/rss/headlines/section/topic/NATION","NATION"),("https://news.google.com/rss/headlines/section/topic/BUSINESS","BUSINESS"),("https://news.google.com/rss/headlines/section/topic/HEALTH","HEALTH"),("https://news.google.com/rss/headlines/section/topic/SCIENCE","SCIENCE"),("https://news.google.com/rss/headlines/section/topic/TECHNOLOGY","TECH")],
        "usnews":[("https://news.google.com/rss/headlines/section/geo/US","US"),("https://news.google.com/rss/search?q=united+states+news","US NEWS"),("https://news.google.com/rss/search?q=US+politics+government","POLITICS"),("https://news.google.com/rss/search?q=US+economy+inflation","ECONOMY"),("https://news.google.com/rss/search?q=US+border+immigration","BORDER"),("https://news.google.com/rss/search?q=US+military+defense","MILITARY")],
        "epstein":[("https://news.google.com/rss/search?q=epstein+files+documents","FILES"),("https://news.google.com/rss/search?q=jeffrey+epstein+court","COURT"),("https://news.google.com/rss/search?q=epstein+ghislaine+maxwell","MAXWELL"),("https://news.google.com/rss/search?q=epstein+client+list","CLIENTS"),("https://news.google.com/rss/search?q=epstein+island+investigation","INVESTIGATION"),("https://news.google.com/rss/search?q=epstein+jeffrey+news+2024","LATEST")],
    }
    feeds=FEEDS.get(feed_type,FEEDS["world"])
    UAS=["Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36","Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.4 Safari/605.1.15"]
    def fetch_feed(url):
        req=urllib.request.Request(url,headers={"User-Agent":random.choice(UAS),"Accept":"application/rss+xml,application/xml,text/xml,*/*","Referer":"https://www.google.com/"})
        with urllib.request.urlopen(req,timeout=10) as r: return r.read().decode("utf-8",errors="replace")
    def get_tag(xml,t):
        m=re.search(r'<'+t+r'[^>]*>\s*(?:<!\[CDATA\[)?(.*?)(?:\]\]>)?\s*</'+t+r'>',xml,re.DOTALL)
        return re.sub(r'<[^>]+>','',_html.unescape(m.group(1))).strip() if m else ""
    items,seen=[],set()
    for feed_url,category in feeds:
        if len(items)>=40: break
        try:
            xml=fetch_feed(feed_url)
            blocks=re.findall(r'<item>(.*?)</item>',xml,re.DOTALL) or re.findall(r'<entry>(.*?)</entry>',xml,re.DOTALL)
            for item in blocks[:8]:
                title=get_tag(item,'title');url=""
                for pat in [r'<link[^>]+href="([^"]+)"',r'<link[^>]*>(https?://[^<]+)</link>',r'<guid[^>]*>(https?://[^<]+)</guid>']:
                    m=re.search(pat,item)
                    if m: url=m.group(1).strip();break
                pub_date=get_tag(item,'pubDate') or get_tag(item,'published') or get_tag(item,'updated') or ""
                if title and url and title not in seen:
                    seen.add(title);items.append({"title":title[:120],"url":url,"desc":get_tag(item,'description')[:200] or get_tag(item,'summary')[:200],"cat":category,"pub_date":pub_date})
        except Exception: continue
    random.shuffle(items);return ok(items=items[:40])
@app.route("/api/dm/conversations")
def api_dm_conversations():
    if not logged_in(): return jsonify({"ok":False})
    u=me()
    with db() as con:
        read_dm_at=read_at_map(con,u,'dm')
        partners=fetchall(con,"SELECT CASE WHEN sender=%s THEN recipient ELSE sender END as partner,MAX(timestamp) FROM messages WHERE sender=%s OR recipient=%s GROUP BY partner ORDER BY 2 DESC",(u,u,u))
        convos=[]
        for (partner,_) in partners:
            row=fetchone(con,"SELECT COUNT(*) FROM messages WHERE sender=%s AND recipient=%s AND timestamp>%s",(partner,u,read_dm_at.get(partner,'1970-01-01')))
            convos.append({"username":partner,"unread":row[0] if row else 0})
    return ok(conversations=convos)
@app.route("/api/dm/thread")
def api_dm_thread():
    if not logged_in(): return jsonify({"ok":False})
    u,other=me(),request.args.get("with","").strip()
    if not other: return jsonify({"ok":False})
    with db() as con:
        rows=fetchall(con,"SELECT sender,content_enc,timestamp FROM messages WHERE (sender=%s AND recipient=%s) OR (sender=%s AND recipient=%s) ORDER BY timestamp ASC LIMIT 100",(u,other,other,u))
        execute(con,"UPDATE messages SET read=1 WHERE recipient=%s AND sender=%s",(u,other))
    return ok(me=u,messages=dec_messages(rows))
@app.route("/api/dm/send",methods=["POST"])
def api_dm_send():
    if e:=require_login(): return e
    d=request.json or {};to,content=d.get("to","").strip(),d.get("content","").strip()
    if not to or not content: return err("FIELDS REQUIRED")
    u=me()
    with db() as con:
        if not fetchone(con,"SELECT id FROM users WHERE username=%s",(to,)): return err("USER NOT FOUND")
        if fetchone(con,"SELECT 1 FROM dm_blocked WHERE (blocker=%s AND blocked=%s) OR (blocker=%s AND blocked=%s)",(u,to,to,u)): return err("CANNOT MESSAGE THIS USER")
        execute(con,"INSERT INTO messages(sender,recipient,content_enc) VALUES(%s,%s,%s)",(u,to,enc(content)))
    send_push(to,f"DM from {u}",content[:80],tag="dm");return ok()
@app.route("/api/dm/delete",methods=["POST"])
def api_dm_delete():
    if e:=require_login(): return e
    other=(request.json or {}).get("username","").strip()
    if not other: return err("MISSING USERNAME")
    u=me()
    with db() as con: execute(con,"DELETE FROM messages WHERE (sender=%s AND recipient=%s) OR (sender=%s AND recipient=%s)",(u,other,other,u))
    return ok()
@app.route("/api/dm/block",methods=["POST"])
def api_dm_block():
    if e:=require_login(): return e
    other=(request.json or {}).get("username","").strip()
    if not other: return err("MISSING USERNAME")
    u=me()
    with db() as con:
        execute(con,"INSERT INTO dm_blocked(blocker,blocked) VALUES(%s,%s) ON CONFLICT DO NOTHING",(u,other))
        execute(con,"DELETE FROM messages WHERE (sender=%s AND recipient=%s) OR (sender=%s AND recipient=%s)",(u,other,other,u))
    return ok()
@app.route("/api/dm/unblock",methods=["POST"])
def api_dm_unblock():
    if e:=require_login(): return e
    other=(request.json or {}).get("username","").strip()
    with db() as con: execute(con,"DELETE FROM dm_blocked WHERE blocker=%s AND blocked=%s",(me(),other))
    return ok()
@app.route("/api/groups")
def api_groups():
    if not logged_in(): return jsonify({"ok":False})
    u=me()
    with db() as con:
        rows=fetchall(con,"SELECT id,name,locked FROM groups ORDER BY id ASC")
        member_ids={r[0] for r in fetchall(con,"SELECT group_id FROM group_members WHERE username=%s",(u,))}
        banned_ids={r[0] for r in fetchall(con,"SELECT group_id FROM group_banned WHERE username=%s",(u,))}
        rat=read_at_map(con,u,'group')
        unread={r[0]:unread_count(con,'group_messages','group_id',r[0],u,rat.get(str(r[0]),'1970-01-01')) for r in rows}
    return ok(groups=[{"id":r[0],"name":r[1],"member":r[0] in member_ids,"locked":bool(r[2]),"banned":r[0] in banned_ids,"unread":unread.get(r[0],0)} for r in rows])
@app.route("/api/groups/create",methods=["POST"])
def api_group_create():
    if e:=require_admin(): return e
    name=(request.json or {}).get("name","").strip().upper()
    if not name or len(name)<2: return err("NAME TOO SHORT")
    try:
        with db() as con:
            row=fetchone(con,"INSERT INTO groups(name,created_by) VALUES(%s,%s) RETURNING id",(name,me()));gid=row[0]
            for uname in [r[0] for r in fetchall(con,"SELECT username FROM users")]:
                execute(con,"INSERT INTO group_members(group_id,username) VALUES(%s,%s) ON CONFLICT DO NOTHING",(gid,uname))
        return ok(id=gid)
    except psycopg2.errors.UniqueViolation: return err("CHANNEL NAME TAKEN")
@app.route("/api/groups/<int:gid>/messages")
def api_group_messages(gid):
    if not logged_in(): return jsonify({"ok":False})
    u=me();admin=is_admin(u)
    with db() as con:
        banned=fetchone(con,"SELECT 1 FROM group_banned WHERE group_id=%s AND username=%s",(gid,u))
        member=fetchone(con,"SELECT 1 FROM group_members WHERE group_id=%s AND username=%s",(gid,u))
        if not banned and not member:
            execute(con,"INSERT INTO group_members(group_id,username) VALUES(%s,%s) ON CONFLICT DO NOTHING",(gid,u));member=True
        group=fetchone(con,"SELECT locked FROM groups WHERE id=%s",(gid,))
        rows=fetchall(con,"SELECT sender,content_enc,timestamp FROM group_messages WHERE group_id=%s ORDER BY timestamp ASC LIMIT 100",(gid,))
        members_list=[r[0] for r in fetchall(con,"SELECT username FROM group_members WHERE group_id=%s ORDER BY username",(gid,))] if admin else []
    is_member=admin or (bool(member) and not bool(banned))
    return ok(me=u,member=is_member,locked=bool(group and group[0]),admin=admin,members=members_list,messages=dec_messages(rows))
@app.route("/api/groups/send",methods=["POST"])
def api_group_send():
    if e:=require_login(): return e
    d=request.json or {};gid,content=d.get("group_id"),d.get("content","").strip()
    if not content: return err("EMPTY MESSAGE")
    u=me()
    with db() as con:
        if not fetchone(con,"SELECT 1 FROM group_members WHERE group_id=%s AND username=%s",(gid,u)): return err("NOT A MEMBER")
        if not is_admin(u):
            g=fetchone(con,"SELECT locked FROM groups WHERE id=%s",(gid,))
            if g and g[0]: return err("CHANNEL IS LOCKED")
        execute(con,"INSERT INTO group_messages(group_id,sender,content_enc) VALUES(%s,%s,%s)",(gid,u,enc(content)))
    with db() as con2:
        gname_row=fetchone(con2,"SELECT name FROM groups WHERE id=%s",(gid,))
        members=[r[0] for r in fetchall(con2,"SELECT username FROM group_members WHERE group_id=%s AND username!=%s",(gid,u))]
    gname=gname_row[0] if gname_row else "GROUP"
    for member in members: send_push(member,f"#{gname}",f"{u}: {content[:60]}",tag=f"group-{gid}")
    return ok()
@app.route("/api/groups/kick",methods=["POST"])
def api_group_kick():
    if e:=require_admin(): return e
    d=request.json or {};gid,target=d.get("group_id"),d.get("username","").strip()
    if not gid or not target: return err("MISSING FIELDS")
    with db() as con: execute(con,"DELETE FROM group_members WHERE group_id=%s AND username=%s",(gid,target))
    return ok()
@app.route("/api/groups/ban",methods=["POST"])
def api_group_ban():
    if e:=require_admin(): return e
    d=request.json or {};gid,target=d.get("group_id"),d.get("username","").strip()
    if not gid or not target: return err("MISSING FIELDS")
    with db() as con:
        execute(con,"DELETE FROM group_members WHERE group_id=%s AND username=%s",(gid,target))
        execute(con,"INSERT INTO group_banned(group_id,username) VALUES(%s,%s) ON CONFLICT DO NOTHING",(gid,target))
    return ok()
@app.route("/api/groups/unban",methods=["POST"])
def api_group_unban():
    if e:=require_admin(): return e
    d=request.json or {};gid,target=d.get("group_id"),d.get("username","").strip()
    with db() as con:
        execute(con,"DELETE FROM group_banned WHERE group_id=%s AND username=%s",(gid,target))
        execute(con,"INSERT INTO group_members(group_id,username) VALUES(%s,%s) ON CONFLICT DO NOTHING",(gid,target))
    return ok()
@app.route("/api/groups/join",methods=["POST"])
def api_group_join():
    if not logged_in(): return jsonify({"ok":False})
    with db() as con: execute(con,"INSERT INTO group_members(group_id,username) VALUES(%s,%s) ON CONFLICT DO NOTHING",(request.json.get("group_id"),me()))
    return ok()
@app.route("/api/groups/leave",methods=["POST"])
def api_group_leave():
    if not logged_in(): return jsonify({"ok":False})
    with db() as con: execute(con,"DELETE FROM group_members WHERE group_id=%s AND username=%s",(request.json.get("group_id"),me()))
    return ok()
@app.route("/api/group/rename",methods=["POST"])
def api_group_rename():
    if e:=require_admin(): return e
    d=request.json or {};gid,name=d.get("id"),d.get("name","").strip().upper()
    if not gid or not name: return err("MISSING FIELDS")
    try:
        with db() as con: execute(con,"UPDATE groups SET name=%s WHERE id=%s",(name,gid))
        return ok()
    except Exception: return err("NAME TAKEN")
@app.route("/api/private/rooms")
def api_private_rooms():
    if e:=require_login(): return e
    u=me();admin=is_admin(u)
    with db() as con:
        rows=fetchall(con,"SELECT id,name FROM private_rooms ORDER BY name ASC") if admin else fetchall(con,"SELECT r.id,r.name FROM private_rooms r JOIN private_room_members m ON r.id=m.room_id WHERE m.username=%s ORDER BY r.name ASC",(u,))
        rat=read_at_map(con,u,'private')
        unread={r[0]:unread_count(con,'private_room_messages','room_id',r[0],u,rat.get(str(r[0]),'1970-01-01')) for r in rows}
    return ok(rooms=[{"id":r[0],"name":r[1],"unread":unread.get(r[0],0)} for r in rows],is_admin=admin)
@app.route("/api/private/create",methods=["POST"])
def api_private_create():
    if e:=require_admin(): return e
    name=(request.json or {}).get("name","").strip().upper()
    if not name: return err("NAME REQUIRED")
    with db() as con:
        row=fetchone(con,"INSERT INTO private_rooms(name,created_by) VALUES(%s,%s) RETURNING id",(name,me()));rid=row[0]
        execute(con,"INSERT INTO private_room_members(room_id,username) VALUES(%s,%s) ON CONFLICT DO NOTHING",(rid,me()))
    return ok(id=rid)
@app.route("/api/private/<int:rid>/messages")
def api_private_messages(rid):
    if e:=require_login(): return e
    u=me();admin=is_admin(u)
    with db() as con:
        if not admin:
            if not fetchone(con,"SELECT 1 FROM private_room_members WHERE room_id=%s AND username=%s",(rid,u)): return err("ACCESS DENIED")
        rows=fetchall(con,"SELECT sender,content_enc,timestamp FROM private_room_messages WHERE room_id=%s ORDER BY timestamp ASC LIMIT 100",(rid,))
    return ok(me=u,is_admin=admin,messages=dec_messages(rows))
@app.route("/api/private/send",methods=["POST"])
def api_private_send():
    if e:=require_login(): return e
    d=request.json or {};rid,content=d.get("room_id"),d.get("content","").strip()
    if not rid or not content: return err("MISSING FIELDS")
    u=me()
    with db() as con:
        if not is_admin(u):
            if not fetchone(con,"SELECT 1 FROM private_room_members WHERE room_id=%s AND username=%s",(rid,u)): return err("ACCESS DENIED")
        execute(con,"INSERT INTO private_room_messages(room_id,sender,content_enc) VALUES(%s,%s,%s)",(rid,u,enc(content)))
    with db() as con2:
        rname_row=fetchone(con2,"SELECT name FROM private_rooms WHERE id=%s",(rid,))
        members=[r[0] for r in fetchall(con2,"SELECT username FROM private_room_members WHERE room_id=%s AND username!=%s",(rid,u))]
    rname=rname_row[0] if rname_row else "PRIVATE"
    for member in members: send_push(member,f"🔒 {rname}",f"{u}: {content[:60]}",tag=f"private-{rid}")
    return ok()
@app.route("/api/private/<int:rid>/members")
def api_private_members(rid):
    if e:=require_admin(): return e
    with db() as con: rows=fetchall(con,"SELECT username FROM private_room_members WHERE room_id=%s ORDER BY username",(rid,))
    return ok(members=[r[0] for r in rows])
@app.route("/api/private/add-member",methods=["POST"])
def api_private_add_member():
    if e:=require_admin(): return e
    d=request.json or {};rid,username=d.get("room_id"),d.get("username","").strip()
    if not rid or not username: return err("MISSING FIELDS")
    with db() as con:
        if not fetchone(con,"SELECT id FROM users WHERE username=%s",(username,)): return err("USER NOT FOUND")
        execute(con,"INSERT INTO private_room_members(room_id,username) VALUES(%s,%s) ON CONFLICT DO NOTHING",(rid,username))
    return ok()
@app.route("/api/private/remove-member",methods=["POST"])
def api_private_remove_member():
    if e:=require_admin(): return e
    d=request.json or {};rid,username=d.get("room_id"),d.get("username","").strip()
    if not rid or not username: return err("MISSING FIELDS")
    with db() as con: execute(con,"DELETE FROM private_room_members WHERE room_id=%s AND username=%s",(rid,username))
    return ok()
@app.route("/api/private/rename",methods=["POST"])
def api_private_rename():
    if e:=require_admin(): return e
    d=request.json or {};rid,name=d.get("id"),d.get("name","").strip().upper()
    if not rid or not name: return err("MISSING FIELDS")
    with db() as con: execute(con,"UPDATE private_rooms SET name=%s WHERE id=%s",(name,rid))
    return ok()
@app.route("/api/notifications")
def api_notifications():
    if e:=require_login(): return e
    u=me()
    with db() as con:
        read_dm_at=read_at_map(con,u,'dm')
        senders=[r[0] for r in fetchall(con,"SELECT DISTINCT sender FROM messages WHERE recipient=%s",(u,))]
        dm_unread=sum(fetchone(con,"SELECT COUNT(*) FROM messages WHERE sender=%s AND recipient=%s AND timestamp>%s",(s,u,read_dm_at.get(s,'1970-01-01')))[0] for s in senders)
        group_rows=fetchall(con,"SELECT id,name FROM groups WHERE id IN (SELECT group_id FROM group_members WHERE username=%s) ORDER BY id",(u,))
        read_grp_at=read_at_map(con,u,'group')
        groups_unread={str(gid):{"name":gname,"count":cnt} for gid,gname in group_rows for cnt in [unread_count(con,'group_messages','group_id',gid,u,read_grp_at.get(str(gid),'1970-01-01'))] if cnt}
        priv_rows=fetchall(con,"SELECT id,name FROM private_rooms ORDER BY id") if is_admin(u) else fetchall(con,"SELECT r.id,r.name FROM private_rooms r JOIN private_room_members m ON r.id=m.room_id WHERE m.username=%s ORDER BY r.id",(u,))
        read_prv_at=read_at_map(con,u,'private')
        privrooms_unread={str(rid):{"name":rname,"count":cnt} for rid,rname in priv_rows for cnt in [unread_count(con,'private_room_messages','room_id',rid,u,read_prv_at.get(str(rid),'1970-01-01'))] if cnt}
        posts_read=fetchone(con,"SELECT read_at FROM chat_read_at WHERE username=%s AND chat_type='posts' AND chat_id='posts'",(u,))
        new_posts=fetchone(con,"SELECT COUNT(*) FROM posts WHERE username!=%s AND created_at>%s",(u,posts_read[0] if posts_read else '1970-01-01'))[0]
    group_total=sum(v["count"] for v in groups_unread.values());priv_total=sum(v["count"] for v in privrooms_unread.values())
    return ok(dm=dm_unread,groups=groups_unread,group=group_total,private_rooms=privrooms_unread,private=priv_total,posts=new_posts,total=dm_unread+group_total+priv_total+new_posts)
@app.route("/api/mark-read",methods=["POST"])
def api_mark_read():
    if e:=require_login(): return e
    d=request.json or {};chat_type,chat_id=d.get("type",""),str(d.get("id",""))
    if not chat_type or not chat_id: return err("MISSING FIELDS")
    now=datetime.datetime.utcnow().strftime('%Y-%m-%d %H:%M:%S')
    with db() as con:
        execute(con,"INSERT INTO chat_read_at(username,chat_type,chat_id,read_at) VALUES(%s,%s,%s,%s) ON CONFLICT (username,chat_type,chat_id) DO UPDATE SET read_at=EXCLUDED.read_at",(me(),chat_type,chat_id,now))
        if chat_type=="dm": execute(con,"UPDATE messages SET read=1 WHERE recipient=%s AND sender=%s",(me(),chat_id))
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
@app.route("/api/ask",methods=["POST"])
def api_ask():
    if e:=require_login(): return e
    query=(request.json or {}).get("query","").strip()
    if not query: return err("EMPTY QUERY")
    api_key=os.environ.get("GEMINI_API_KEY","")
    if not api_key: return ok(answer="")
    try:
        payload=_json.dumps({"contents":[{"parts":[{"text":"Answer concisely under 150 words. Plain text only, no markdown.\n\n"+query}]}],"generationConfig":{"maxOutputTokens":300,"temperature":0.7}}).encode()
        req=urllib.request.Request(f"https://generativelanguage.googleapis.com/v1beta/models/gemini-2.0-flash:generateContent?key={api_key}",data=payload,headers={"Content-Type":"application/json"})
        with urllib.request.urlopen(req,timeout=15) as resp: data=_json.loads(resp.read().decode())
        return ok(answer=data["candidates"][0]["content"]["parts"][0]["text"].strip())
    except Exception: return ok(answer="")
@app.route("/manifest.json")
def manifest():
    data={"name":"Vox Populi","short_name":"VOX","description":"Vox Populi Community","start_url":"/","display":"standalone","background_color":"#000000","theme_color":"#00ff00","orientation":"portrait-primary","icons":[{"src":"/icon-192.png","sizes":"192x192","type":"image/png","purpose":"any maskable"},{"src":"/icon-512.png","sizes":"512x512","type":"image/png","purpose":"any maskable"}],"categories":["social","news"],"shortcuts":[{"name":"Chat","url":"/","description":"Open Vox community chat"}]}
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
@app.route("/favicon.ico")
def favicon(): return _svg_icon(32,20,10)
@app.route("/icon-192.png")
def icon_192(): return _svg_icon(192,108,34)
@app.route("/icon-512.png")
def icon_512(): return _svg_icon(512,285,90,sub_y=325,sub_text="VOX POPULI")
@app.route("/reset-x7k9m2p4q8w3n6j1vb5")
def emergency_reset():
    new_pw="Vox2024!"
    with db() as con: execute(con,"UPDATE users SET password_hash=%s WHERE username=%s",(hash_pw(new_pw),ADMIN_USER))
    return "<h1 style='font-family:monospace;background:#000;color:#0f0;padding:40px;'>DONE! Login: Eagleone / Vox2024! — CHANGE YOUR PASSWORD AFTER LOGGING IN.</h1>"
@app.errorhandler(Exception)
def handle_exception(e):
    import traceback;app.logger.error(traceback.format_exc())
    return f"<pre style='background:#111;color:#f44;padding:20px;font-size:12px;'>ERROR:\n{traceback.format_exc()}</pre>",500
# ══════════════════════════════════════════════════════════════════════════════
# SECURITY HUB — integrated scanner
# ══════════════════════════════════════════════════════════════════════════════
_SEC_TARGET   = os.environ.get("TARGET_URL","")          # site to scan (set in Railway vars)
_SEC_MAX_PAGES= int(os.environ.get("SEC_MAX_PAGES","80"))
_SEC_INTERVAL = int(os.environ.get("SEC_INTERVAL_MINS","60"))
_SEC_STATE_FILE= str(_BASE/"sec_state.json")
_SEC_REPORTS_FILE= str(_BASE/"sec_reports.json")
_SEC_LOCK     = threading.Lock()

# Whole-word patterns — won't match "skill", "bombardment", "klassic", etc.
# Also won't fire on your own app's source code or UI text.
_HARMFUL_PATTERNS=[
    r"\bkill\b",r"\bmurder\b",r"\bterrorist\b",r"\bnazi\b",
    r"\bwhite supremacy\b",r"\bnigger\b",r"\bfaggot\b",
    r"\bchink\b",r"\bspic\b",r"\bhate speech\b",
    r"\brake\b(?!.*css)",          # rape but not background-rake or similar
    r"\bmolest\b",r"\bchild porn\b",
    r"\bsql injection\b",r"\bransomware\b",r"\bphishing\b",
]
# Pages the scanner skips — your own app routes that need a logged-in session
_SEC_SKIP_PATHS=["/api/","/logout","/sw.js","/manifest.json","/icon-","/favicon.ico","/reset-"]

def _sec_load_state():
    if os.path.exists(_SEC_STATE_FILE):
        with open(_SEC_STATE_FILE) as f: return _json.load(f)
    return {"page_hashes":{},"last_scan":None}

def _sec_save_state(s):
    with open(_SEC_STATE_FILE,"w") as f: _json.dump(s,f)

def _sec_skip(url):
    """Return True for URLs we should never scan (API routes, assets, etc)."""
    path=urllib.parse.urlparse(url).path
    return any(path.startswith(p) for p in _SEC_SKIP_PATHS)

def _sec_crawl(base_url,max_pages=_SEC_MAX_PAGES):
    visited,queue=[],[base_url];seen=set()
    domain=urllib.parse.urlparse(base_url).netloc
    while queue and len(visited)<max_pages:
        url=queue.pop(0)
        if url in seen or _sec_skip(url): continue
        seen.add(url)
        try:
            r=requests.get(url,timeout=8,headers={"User-Agent":"VoxSecBot/1.0"},allow_redirects=True)
            # Only scan pages that the public can actually see (not login-walled content)
            if r.status_code==200:
                visited.append(url)
                if BeautifulSoup:
                    soup=BeautifulSoup(r.text,"html.parser")
                    for a in soup.find_all("a",href=True):
                        full=urllib.parse.urljoin(url,a["href"])
                        if urllib.parse.urlparse(full).netloc==domain and full not in seen and not _sec_skip(full):
                            queue.append(full)
        except Exception: seen.add(url)
    return visited

def _sec_check_ssl(hostname):
    try:
        ctx=ssl.create_default_context()
        with ctx.wrap_socket(socket.create_connection((hostname,443),timeout=8),server_hostname=hostname) as s:
            cert=s.getpeercert()
        expiry=datetime.datetime.strptime(cert["notAfter"],"%b %d %H:%M:%S %Y %Z")
        days=(expiry-datetime.datetime.utcnow()).days
        return {"ok":days>14,"days_left":days,"expiry":cert["notAfter"]}
    except Exception as e: return {"ok":False,"days_left":-1,"error":str(e)}

def _sec_broken_links(pages):
    broken=[]
    for url in pages:
        try:
            r=requests.head(url,timeout=6,allow_redirects=True,headers={"User-Agent":"VoxSecBot/1.0"})
            if r.status_code>=400: broken.append({"url":url,"status":r.status_code})
        except Exception as e: broken.append({"url":url,"status":"error","detail":str(e)})
    return broken

def _sec_content_changes(pages,state):
    changes=[];hashes=state.get("page_hashes",{})
    for url in pages:
        try:
            r=requests.get(url,timeout=8,headers={"User-Agent":"VoxSecBot/1.0"})
            h=hashlib.sha256(r.text.encode()).hexdigest()
            if url in hashes and hashes[url]!=h:
                changes.append({"url":url,"prev":hashes[url][:12],"new":h[:12]})
            hashes[url]=h
        except Exception: pass
    state["page_hashes"]=hashes
    return changes

def _sec_harmful(pages):
    import re as _re
    findings=[]
    for url in pages:
        if _sec_skip(url): continue
        try:
            r=requests.get(url,timeout=8,headers={"User-Agent":"VoxSecBot/1.0"})
            if r.status_code!=200: continue
            # Strip HTML tags so we only check visible text, not source code/CSS/JS
            if not BeautifulSoup: continue
            soup=BeautifulSoup(r.text,"html.parser")
            for tag in soup(["script","style","code","pre"]): tag.decompose()
            visible_text=soup.get_text(separator=" ").lower()
            hits=[p for p in _HARMFUL_PATTERNS if _re.search(p,visible_text)]
            if hits: findings.append({"url":url,"keywords":hits})
        except Exception: pass
    return findings

def _sec_ai_analysis(report):
    if not _anthropic_client: return "AI analysis unavailable — set ANTHROPIC_API_KEY in Railway environment variables."
    api_key=os.environ.get("ANTHROPIC_API_KEY","")
    if not api_key: return "AI analysis unavailable — ANTHROPIC_API_KEY not set."
    try:
        msg=_anthropic_client.messages.create(
            model="claude-haiku-4-5-20251001",max_tokens=600,
            messages=[{"role":"user","content":
                f"You are a security analyst. Analyze this scan and give:\n"
                f"1. 2-sentence executive summary\n2. Critical issues needing immediate action\n"
                f"3. Overall risk: LOW/MEDIUM/HIGH/CRITICAL\n\nData:\n{_json.dumps(report,indent=2)}"
            }]
        )
        return msg.content[0].text
    except Exception as e: return f"AI analysis error: {e}"

def _sec_run_scan():
    if not _SEC_TARGET: return {"error":"TARGET_URL not set"}
    state=_sec_load_state()
    # Always use https for scanning so SSL check works correctly
    target=_SEC_TARGET
    if target.startswith("http://"): target="https://"+target[7:]
    parsed=urllib.parse.urlparse(target)
    hostname=parsed.netloc
    pages=_sec_crawl(target)
    # Only check SSL if site is actually https
    ssl_result=_sec_check_ssl(hostname) if target.startswith("https://") else {"ok":True,"days_left":999,"note":"http only"}
    broken=_sec_broken_links(pages)
    changes=_sec_content_changes(pages,state)
    harmful=_sec_harmful(pages)
    report={
        "timestamp":utc_now(),"target":target,
        "pages_scanned":len(pages),"ssl":ssl_result,
        "broken_links":broken,"content_changes":changes,"harmful_content":harmful,
    }
    report["ai_analysis"]=_sec_ai_analysis(report)
    # Only critical if there are actual findings — not just SSL on http sites
    _ssl_ok=ssl_result.get("ok",True)
    _real_ssl_issue=not _ssl_ok and ssl_result.get("days_left",-1)>=0  # -1 means connection error
    report["is_critical"]=bool(harmful or _real_ssl_issue or len(broken)>5)
    # Notify admin via push if critical
    if report["is_critical"]:
        summary=f"⚠ SECURITY ALERT: {len(harmful)} harmful, {len(broken)} broken links, SSL={'OK' if ssl_result.get('ok') else 'ISSUE'}"
        send_push(ADMIN_USER,"🚨 VOX SECURITY HUB",summary,tag="security")
    # Persist
    _sec_save_state(state)
    reports=[]
    if os.path.exists(_SEC_REPORTS_FILE):
        with open(_SEC_REPORTS_FILE) as f: reports=_json.load(f)
    reports.insert(0,report);reports=reports[:50]
    with open(_SEC_REPORTS_FILE,"w") as f: _json.dump(reports,f)
    return report

# ── Auto-scan background thread ───────────────────────────────────────────────
def _sec_scheduler():
    import time as _time
    _time.sleep(30)  # give app time to start
    while True:
        if _SEC_TARGET:
            try:
                with _SEC_LOCK: _sec_run_scan()
            except Exception as e: app.logger.error(f"Security scan error: {e}")
        _time.sleep(_SEC_INTERVAL*60)

threading.Thread(target=_sec_scheduler,daemon=True).start()

# ── Security API routes ───────────────────────────────────────────────────────
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
    def _run():
        with _SEC_LOCK: _sec_run_scan()
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

@app.route("/security")
def security_dashboard():
    if not is_admin(): return redirect("/")
    user=me();theme=session.get("theme","green")
    content='''<div style="width:min(100%,960px);margin:0 auto;padding:16px;box-sizing:border-box;">
<div style="border:2px solid var(--p);border-radius:var(--r);padding:20px;margin-bottom:20px;background:var(--p10);">
  <div style="display:flex;align-items:center;justify-content:space-between;flex-wrap:wrap;gap:10px;margin-bottom:16px;">
    <div style="display:flex;align-items:center;gap:12px;">
      <a href="/" style="display:inline-flex;align-items:center;gap:6px;border:2px solid var(--p);border-radius:8px;padding:6px 12px;color:var(--p);background:var(--p10);font-family:'Courier New',monospace;font-size:11px;font-weight:bold;text-transform:uppercase;text-decoration:none;transition:.2s;" onmouseover="this.style.background='var(--p)';this.style.color='#000'" onmouseout="this.style.background='var(--p10)';this.style.color='var(--p)'">&#9664; BACK</a>
      <h2 style="margin:0;letter-spacing:4px;font-size:clamp(14px,3vw,20px);">&#128737; SECURITY HUB</h2>
    </div>
    <div style="display:flex;gap:8px;align-items:center;">
      <span id="secTarget" style="font-size:10px;opacity:.5;"></span>
      <button class="btn-action" id="secScanBtn" onclick="secTriggerScan()" style="padding:7px 18px;font-size:11px;">&#9654; SCAN NOW</button>
    </div>
  </div>
  <div id="secAlertBanner" style="display:none;background:#ff0033;color:#fff;padding:10px;border-radius:8px;text-align:center;font-size:12px;letter-spacing:3px;margin-bottom:14px;animation:tcPulse 1.5s infinite;">&#9888; CRITICAL SECURITY ISSUES DETECTED — IMMEDIATE ACTION REQUIRED &#9888;</div>
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
  <div style="display:grid;grid-template-columns:1fr 1fr;gap:12px;">
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
  </div>
  <div style="margin-top:12px;font-size:9px;opacity:.35;text-align:right;letter-spacing:1px;">LAST SCAN: <span id="secLastScan">—</span> &nbsp;|&nbsp; AUTO-SCAN EVERY <span id="secInterval">—</span> MIN</div>
</div></div>
<script>
async function secLoad(){
  const s=await fetch('/api/security/status').then(r=>r.json()).catch(()=>({}));
  if(s.ok){document.getElementById('secTarget').textContent=s.target||'';document.getElementById('secInterval').textContent=s.interval||'?';}
  const d=await fetch('/api/security/reports').then(r=>r.json()).catch(()=>({}));
  if(!d.ok||!d.reports.length){document.getElementById('secAI').textContent='No scans yet. Click SCAN NOW.';return;}
  const r=d.reports[0];
  // alert mode
  const crit=r.is_critical;
  const banner=document.getElementById('secAlertBanner');
  if(crit){
    banner.style.display='block';
    document.body.style.setProperty('--p','#ff2222');
    document.body.style.setProperty('--bg','#0a0000');
    document.body.style.setProperty('--ac','#330000');
  }else{banner.style.display='none';}
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
  // lists
  const bll=document.getElementById('secBrokenList');
  bll.innerHTML=bl?r.broken_links.map(b=>`<div style="padding:4px 0;border-bottom:1px solid var(--p10);word-break:break-all;"><span style="color:#ffaa00;">[${b.status}]</span> ${b.url}</div>`).join(''):'<div style="opacity:.4;font-size:10px;">✓ None detected</div>';
  const hml=document.getElementById('secHarmfulList');
  hml.innerHTML=hm?r.harmful_content.map(h=>`<div style="padding:4px 0;border-bottom:1px solid var(--p10);word-break:break-all;"><span style="color:#ff3355;">⚠</span> ${h.url}<br><span style="opacity:.5;font-size:9px;">${h.keywords.join(', ')}</span></div>`).join(''):'<div style="opacity:.4;font-size:10px;">✓ None detected</div>';
  const chl=document.getElementById('secChangesList');
  chl.innerHTML=ch?r.content_changes.map(c=>`<div style="padding:4px 0;border-bottom:1px solid var(--p10);word-break:break-all;"><span style="color:#ffaa00;">~</span> ${c.url}</div>`).join(''):'<div style="opacity:.4;font-size:10px;">✓ No changes</div>';
  // history bar
  const bar=document.getElementById('secHistoryBar');bar.innerHTML='';
  d.reports.slice(0,30).reverse().forEach(rpt=>{
    const issues=(rpt.broken_links?.length??0)+(rpt.harmful_content?.length??0)*3+(!rpt.ssl?.ok?5:0);
    const h=Math.max(4,Math.min(52,4+issues*3));
    const col=rpt.harmful_content?.length>0?'#ff3355':issues>3?'#ffaa00':'var(--p)';
    bar.innerHTML+=`<div title="${new Date(rpt.timestamp).toLocaleString()} — ${issues} issues" style="flex:1;min-width:6px;height:${h}px;background:${col};border-radius:2px 2px 0 0;align-self:flex-end;cursor:pointer;"></div>`;
  });
}
async function secTriggerScan(){
  const btn=document.getElementById('secScanBtn');
  btn.disabled=true;btn.textContent='⟳ SCANNING...';
  await fetch('/api/security/scan',{method:'POST'});
  const poll=setInterval(async()=>{
    const s=await fetch('/api/security/status').then(r=>r.json()).catch(()=>({}));
    if(!s.scanning){clearInterval(poll);secLoad();btn.disabled=false;btn.textContent='▶ SCAN NOW';}
  },3000);
}
secLoad();setInterval(secLoad,30000);
</script>'''
    return shell(content,user=user,theme=theme)

if __name__=="__main__": app.run(debug=False)
