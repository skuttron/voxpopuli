# -*- coding: utf-8 -*-
from flask import Flask,request,session,redirect,jsonify,Response
import psycopg2,psycopg2.extras,os,hashlib,datetime,urllib.request,re,html as _html,pathlib,json as _json,time
from contextlib import contextmanager
from cryptography.fernet import Fernet
# ── Security Scanner ──────────────────────────────────────────────────────────
import ssl,socket,threading,urllib.parse
from bs4 import BeautifulSoup
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
VAPID_PUBLIC_KEY=os.environ.get("VAPID_PUBLIC_KEY","BAyH6Y_hbhzzmRgt3pd5Qa7guYKYKfsVCVIZsJGF0zYPfBupcKm24bduVIj4585JSjeeu3aeR19d4tBzlHgQIdU")
VAPID_PRIVATE_KEY=os.environ.get("VAPID_PRIVATE_KEY","MIGHAgEAMBMGByqGSM49AgEGCCqGSM49AwEHBG0wawIBAQQgOqLakrDhZhnH_KBh5nwx2l0jyGfOWplqyE82s4Ryws2hRANCAAQMh-mP4W4c85kYLd6XeUGu4LmCmCn7FQlSGbCRhdM2D3wbqXCptuG3blSI-OfOSUo3nrt2nkdfXeLQc5R4ECHV")
VAPID_CLAIMS={"sub":"mailto:admin@voxpopuli.app"}
hash_pw=lambda pw:hashlib.sha256(pw.encode()).hexdigest()
enc=lambda t:fernet.encrypt(t.encode()).decode()
dec=lambda t:fernet.decrypt(t.encode()).decode() if t else ""
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
            f'<div class="dropdown-divider"></div><a class="dropdown-item" onclick="openModal(\'settingsModal\')"><i class="fas fa-cog"></i> SETTINGS</a>'
            f'<a class="dropdown-item" onclick="enableNotifications()" id="notifMenuItem"><i class="fas fa-bell"></i> ENABLE NOTIFICATIONS</a>'
            f'<a class="dropdown-item" href="/security"><i class="fas fa-shield-alt"></i> SECURITY HUB</a><a class="dropdown-item" href="/logout"><i class="fas fa-sign-out-alt"></i> LOGOUT</a></div></div>')
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
const api=(url,body)=>fetch(url,body?{{method:'POST',headers:{{'Content-Type':'application/json'}},body:JSON.stringify(body)}}:undefined).then(r=>r.json());
const $=id=>document.getElementById(id);
const isMobile=()=>window.innerWidth<=700;
const openModal=id=>{{const el=$(id);if(el)el.classList.add('open');}};
const closeModal=id=>{{const el=$(id);if(el)el.classList.remove('open');}};
function 
