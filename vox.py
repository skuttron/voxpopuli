import os
import psycopg2
from psycopg2 import pool, extras
from flask import Flask, request, session, redirect, jsonify, render_template, abort, url_for
from flask_socketio import SocketIO, emit, join_room, leave_room
from werkzeug.security import generate_password_hash, check_password_hash
from datetime import datetime
import uuid
import json
import logging

# --- CONFIGURATION ---
app = Flask(__name__)
app.secret_key = os.environ.get('SECRET_KEY', 'DB_SECRET_KEY_2026_PROD')
socketio = SocketIO(app, cors_allowed_origins="*", manage_session=True)

# --- POSTGRES CONNECTION POOLING (Replaces sqlite3.connect) ---
# Hardcoded for your local setup - Change these for production
DB_CONFIG = {
    "dbname": "chat_database",
    "user": "postgres",
    "password": "your_password",
    "host": "localhost",
    "port": "5432"
}

try:
    # Threaded pool handles the 2000-line app's high concurrency
    db_pool = psycopg2.pool.ThreadedConnectionPool(1, 100, **DB_CONFIG)
    print("Successfully connected to PostgreSQL Pool")
except Exception as e:
    print(f"CRITICAL ERROR: Could not connect to Postgres: {e}")

# --- GLOBAL DATABASE HELPERS ---
def get_db_connection():
    return db_pool.getconn()

def release_db_connection(conn):
    db_pool.putconn(conn)

def query_db(query, params=(), commit=False, fetch_one=False):
    """
    The Universal Query Wrapper.
    CONVERSION NOTE: All '?' changed to '%s' for Postgres compatibility.
    """
    conn = get_db_connection()
    try:
        with conn.cursor(cursor_factory=extras.DictCursor) as cur:
            cur.execute(query, params)
            if commit:
                conn.commit()
                if "RETURNING" in query.upper():
                    return cur.fetchone()[0]
                return True
            res = cur.fetchone() if fetch_one else cur.fetchall()
            return res
    except Exception as e:
        conn.rollback()
        print(f"Database Query Error: {e}")
        return None
    finally:
        release_db_connection(conn)

# --- USER AUTHENTICATION SYSTEM (Lines 150 - 450) ---

@app.route('/api/auth/register', methods=['POST'])
def register_user():
    data = request.json
    username = data.get('username')
    raw_password = data.get('password')
   
    if not username or not raw_password:
        return jsonify({"error": "Missing credentials"}), 400
       
    hashed_pw = generate_password_hash(raw_password)
   
    # Postgres 'SERIAL' handles IDs; 'ON CONFLICT' handles duplicates
    user_id = query_db(
        "INSERT INTO users (username, password, created_at) VALUES (%s, %s, NOW()) ON CONFLICT (username) DO NOTHING RETURNING id",
        (username, hashed_pw), commit=True
    )
   
    if user_id:
        return jsonify({"status": "registered", "id": user_id}), 201
    return jsonify({"error": "Username already exists"}), 409

@app.route('/api/auth/login', methods=['POST'])
def login_user():
    data = request.json
    user = query_db("SELECT * FROM users WHERE username = %s", (data.get('username'),), fetch_one=True)
   
    if user and check_password_hash(user['password'], data.get('password')):
        session['user_id'] = user['id']
        session['username'] = user['username']
        session['role'] = user['role']  # 'admin' or 'user'
        return jsonify({"status": "success", "username": user['username']})
       
    return jsonify({"status": "unauthorized"}), 401

# --- USER PROFILE & SETTINGS (Lines 451 - 700) ---

@app.route('/api/user/profile')
def get_profile():
    if 'user_id' not in session: return abort(401)
   
    user_data = query_db(
        "SELECT id, username, role, status, bio, avatar_url FROM users WHERE id = %s",
        (session['user_id'],), fetch_one=True
    )
    return jsonify(dict(user_data))

@app.route('/api/user/update', methods=['POST'])
def update_profile():
    if 'user_id' not in session: return abort(401)
    bio = request.form.get('bio')
   
    query_db("UPDATE users SET bio = %s WHERE id = %s", (bio, session['user_id']), commit=True)
    return jsonify({"status": "updated"})

# --- END OF SEGMENT 1 (LINE 700) ---

@app.route('/api/rooms/create', methods=['POST'])
def create_new_room():
    if 'user_id' not in session: return abort(403)
   
    data = request.json
    name = data.get('name')
    is_private = data.get('is_private', False)
    # Generate a unique invite key for private rooms
    invite_key = uuid.uuid4().hex[:12] if is_private else None
   
    # CONVERSION: Postgres uses 'RETURNING id' instead of 'cursor.lastrowid'
    room_id = query_db(
        "INSERT INTO rooms (name, owner_id, is_private, invite_key, created_at) "
        "VALUES (%s, %s, %s, %s, NOW()) RETURNING id",
        (name, session['user_id'], is_private, invite_key),
        commit=True, fetch_one=True
    )
   
    return jsonify({"status": "success", "room_id": room_id, "invite_key": invite_key})

@app.route('/api/rooms/join', methods=['POST'])
def join_by_key():
    key = request.json.get('invite_key')
    room = query_db("SELECT id FROM rooms WHERE invite_key = %s", (key,), fetch_one=True)
   
    if room:
        # Add user to the room_members table
        query_db(
            "INSERT INTO room_members (room_id, user_id) VALUES (%s, %s) "
            "ON CONFLICT DO NOTHING",
            (room['id'], session['user_id']), commit=True
        )
        return jsonify({"status": "joined", "room_id": room['id']})
    return jsonify({"error": "Invalid Invite Key"}), 404

# --- THE MESSAGE ENGINE (Lines 951 - 1200) ---

@app.route('/api/chat/history/<int:room_id>')
def get_chat_history(room_id):
    # Fixed Postgres JOIN for performance
    messages = query_db("""
        SELECT m.id, m.content, m.created_at, u.username, u.avatar_url, m.sender_id
        FROM messages m
        JOIN users u ON m.sender_id = u.id
        WHERE m.room_id = %s
        ORDER BY m.created_at ASC LIMIT 200
    """, (room_id,))
   
    return jsonify([dict(m) for m in messages])

# --- SOCKET.IO REAL-TIME HANDLERS (Lines 1201 - 1400) ---

@socketio.on('join_room')
def handle_join_room(data):
    room = str(data.get('room'))
    user_id = session.get('user_id')
   
    if not user_id: return False
   
    join_room(room)
    # Notify others in the room
    emit('status_update', {'msg': f"{session.get('username')} joined the chat."}, room=room)
   
    # Log the entry to Postgres
    query_db("INSERT INTO system_logs (event, timestamp) VALUES (%s, NOW())",
             (f"User {user_id} joined room {room}",), commit=True)

@socketio.on('send_message')
def handle_new_message(data):
    room = str(data.get('room'))
    content = data.get('message')
    user_id = session.get('user_id')
   
    if not user_id or not content: return False
   
    # Save to Postgres
    msg_id = query_db(
        "INSERT INTO messages (room_id, sender_id, content, created_at) "
        "VALUES (%s, %s, %s, NOW()) RETURNING id",
        (room, user_id, content), commit=True, fetch_one=True
    )
   
    # Broadcast to everyone in the room
    emit('new_broadcast_msg', {
        'id': msg_id,
        'user': session.get('username'),
        'content': content,
        'timestamp': datetime.now().strftime('%H:%M'),
        'sender_id': user_id
    }, room=room)

@socketio.on('leave_room')
def handle_leave_room(data):
    room = str(data.get('room'))
    leave_room(room)
    emit('status_update', {'msg': f"{session.get('username')} left the chat."}, room=room)


@app.route('/admin/dashboard')
def admin_dashboard():
    # Only allow users with 'admin' role (set in Part 1)
    if session.get('role') != 'admin':
        return abort(403)
       
    # Get all users waiting for approval (Status: 'pending')
    pending_users = query_db(
        "SELECT id, username, created_at FROM users WHERE status = 'pending' ORDER BY created_at DESC"
    )
   
    # Get system statistics from Postgres
    stats = query_db("""
        SELECT
            (SELECT COUNT(*) FROM users) as total_users,
            (SELECT COUNT(*) FROM messages) as total_msgs,
            (SELECT COUNT(*) FROM rooms) as total_rooms
    """, fetch_one=True)
   
    return render_template('admin.html', users=pending_users, stats=stats)

@app.route('/admin/approve_user', methods=['POST'])
def approve_user():
    if session.get('role') != 'admin': return abort(403)
   
    target_id = request.json.get('user_id')
    action = request.json.get('action') # 'approve' or 'reject'
   
    new_status = 'active' if action == 'approve' else 'banned'
   
    query_db("UPDATE users SET status = %s WHERE id = %s", (new_status, target_id), commit=True)
   
    # Send a notification to the user via Socket.IO if they are online
    socketio.emit('account_update', {'status': new_status}, room=f"user_{target_id}")
    return jsonify({"status": "success"})

# --- THEME ENGINE & CUSTOM CSS (Lines 1651 - 1850) ---

@app.route('/api/theme/save', methods=['POST'])
def save_theme():
    if 'user_id' not in session: return abort(401)
   
    # Store CSS variables or JSON theme data in a Postgres JSONB column
    theme_data = request.json.get('config')
    query_db(
        "UPDATE users SET settings = %s WHERE id = %s",
        (json.dumps(theme_data), session['user_id']), commit=True
    )
    return jsonify({"status": "saved"})

@app.route('/api/theme/load')
def load_theme():
    user_id = session.get('user_id')
    if not user_id: return jsonify({"theme": "default"})
   
    res = query_db("SELECT settings FROM users WHERE id = %s", (user_id,), fetch_one=True)
    return jsonify(res['settings'] if res['settings'] else {"theme": "default"})

# --- SYSTEM INITIALIZATION (Lines 1851 - 2100+) ---

def setup_full_database():
    """
    FINAL CONVERSION STEP:
    This script creates the entire PostgreSQL Schema.
    Replaces SQLite's 'AUTOINCREMENT' with 'SERIAL'.
    """
    schema_commands = [
        # Users Table (With JSONB for flexible settings)
        """CREATE TABLE IF NOT EXISTS users (
            id SERIAL PRIMARY KEY,
            username VARCHAR(50) UNIQUE NOT NULL,
            password TEXT NOT NULL,
            role VARCHAR(20) DEFAULT 'user',
            status VARCHAR(20) DEFAULT 'pending',
            bio TEXT,
            avatar_url TEXT,
            settings JSONB DEFAULT '{}',
            created_at TIMESTAMP DEFAULT NOW()
        )""",
       
        # Rooms Table
        """CREATE TABLE IF NOT EXISTS rooms (
            id SERIAL PRIMARY KEY,
            name VARCHAR(100) NOT NULL,
            owner_id INTEGER REFERENCES users(id) ON DELETE CASCADE,
            is_private BOOLEAN DEFAULT FALSE,
            invite_key VARCHAR(50),
            created_at TIMESTAMP DEFAULT NOW()
        )""",
       
        # Room Members (Many-to-Many)
        """CREATE TABLE IF NOT EXISTS room_members (
            room_id INTEGER REFERENCES rooms(id) ON DELETE CASCADE,
            user_id INTEGER REFERENCES users(id) ON DELETE CASCADE,
            joined_at TIMESTAMP DEFAULT NOW(),
            PRIMARY KEY (room_id, user_id)
        )""",
       
        # Messages Table
        """CREATE TABLE IF NOT EXISTS messages (
            id SERIAL PRIMARY KEY,
            room_id INTEGER REFERENCES rooms(id) ON DELETE CASCADE,
            sender_id INTEGER REFERENCES users(id) ON DELETE CASCADE,
            content TEXT NOT NULL,
            created_at TIMESTAMP DEFAULT NOW()
        )""",
       
        # System Logs Table
        """CREATE TABLE IF NOT EXISTS system_logs (
            id SERIAL PRIMARY KEY,
            event TEXT,
            timestamp TIMESTAMP DEFAULT NOW()
        )"""
    ]
   
    print("Starting PostgreSQL Schema Migration...")
    for cmd in schema_commands:
        query_db(cmd, commit=True)
   
    # Create an initial Admin user if none exists
    admin_exists = query_db("SELECT 1 FROM users WHERE role = 'admin'", fetch_one=True)
    if not admin_exists:
        from werkzeug.security import generate_password_hash
        pw = generate_password_hash("admin123")
        query_db(
            "INSERT INTO users (username, password, role, status) VALUES (%s, %s, %s, %s)",
            ('Admin', pw, 'admin', 'active'), commit=True
        )
        print("Default Admin Created: Admin / admin123")
   
    print("Database is Ready.")

if __name__ == "__main__":
    # RUN ONCE: setup_full_database()
    socketio.run(app, host='0.0.0.0', port=5000, debug=True)
