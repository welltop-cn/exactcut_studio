"""Local accounts, revocable sessions and batch ownership."""
import base64
import hashlib
import hmac
import re
import secrets
import sqlite3
import time
from pathlib import Path
from .util import read_json, write_json_atomic


def digest(password, salt):
    return hashlib.pbkdf2_hmac('sha256', password.encode(), salt.encode(), 600000).hex()


def connect(path):
    db = sqlite3.connect(path, timeout=10)
    db.row_factory = sqlite3.Row
    return db


def initialize(path, password):
    with connect(path) as db:
        db.executescript('''CREATE TABLE IF NOT EXISTS users (username TEXT PRIMARY KEY, name TEXT NOT NULL, role TEXT NOT NULL, salt TEXT NOT NULL, hash TEXT NOT NULL, enabled INTEGER NOT NULL DEFAULT 1);
        CREATE TABLE IF NOT EXISTS sessions (token TEXT PRIMARY KEY, username TEXT NOT NULL, expires REAL NOT NULL);
        CREATE TABLE IF NOT EXISTS attempts (username TEXT PRIMARY KEY, count INTEGER, until REAL);
        CREATE TABLE IF NOT EXISTS audit (at REAL, actor TEXT, action TEXT, target TEXT);''')
        if not db.execute('SELECT 1 FROM users WHERE username=?', ('admin',)).fetchone():
            salt = secrets.token_hex(16)
            db.execute('INSERT INTO users VALUES (?,?,?,?,?,1)', ('admin','管理员','admin',salt,digest(password,salt)))
    Path(path).chmod(0o600)


def public(row):
    return {k: row[k] for k in ('username','name','role','enabled')} if row else None


def authenticate(path, username, password):
    username = str(username).lower()
    if len(username)>32 or not isinstance(password,str) or len(password)>128:
        return None
    with connect(path) as db:
        attempt = db.execute('SELECT * FROM attempts WHERE username=?',(username,)).fetchone()
        if attempt and attempt['count'] >= 10 and attempt['until'] > time.time():
            return None
        row = db.execute('SELECT * FROM users WHERE username=?',(username,)).fetchone()
        valid = row and row['enabled'] and hmac.compare_digest(digest(str(password), row['salt']), row['hash'])
        if not valid:
            count = attempt['count']+1 if attempt and attempt['until']>time.time() else 1
            db.execute('INSERT OR REPLACE INTO attempts VALUES (?,?,?)',(username,count,time.time()+300))
            return None
        db.execute('DELETE FROM attempts WHERE username=?',(username,))
        return public(row)


def session(path, token):
    with connect(path) as db:
        return public(db.execute('SELECT u.* FROM users u JOIN sessions s ON s.username=u.username WHERE s.token=? AND s.expires>? AND u.enabled=1',(hashlib.sha256(token.encode()).hexdigest(),time.time())).fetchone())


def login(path, user):
    token = secrets.token_urlsafe(32)
    with connect(path) as db:
        db.execute('DELETE FROM sessions WHERE expires<?',(time.time(),))
        db.execute('INSERT INTO sessions VALUES (?,?,?)',(hashlib.sha256(token.encode()).hexdigest(),user['username'],time.time()+86400*7))
    return token


def logout(path, token):
    with connect(path) as db:
        db.execute('DELETE FROM sessions WHERE token=?',(hashlib.sha256(token.encode()).hexdigest(),))


def users(path):
    with connect(path) as db:
        return [public(r) for r in db.execute('SELECT * FROM users ORDER BY username')]


def get(path, username):
    """Single account lookup, enabled or not; used to revalidate a queued actor."""
    with connect(path) as db:
        return public(db.execute('SELECT * FROM users WHERE username=?', (str(username).lower(),)).fetchone())


def manage(path, actor, data):
    username = str(data.get('username','')).strip().lower()
    if not re.fullmatch(r'[a-z][a-z0-9_-]{2,31}',username):
        raise ValueError('登录名需为 3～32 位英文字母、数字、下划线或短横线，以字母开头')
    action = data.get('action','create')
    with connect(path) as db:
        row = db.execute('SELECT * FROM users WHERE username=?',(username,)).fetchone()
        if action == 'create':
            name = str(data.get('name','')).strip()
            if not name or len(name)>40 or re.search(r'[/\\#\x00-\x1f]',name):
                raise ValueError('请填写有效编导姓名（最多40字，不含路径符号）')
            if row:
                raise ValueError('登录名已存在')
            if db.execute('SELECT 1 FROM users WHERE name=?',(name,)).fetchone():
                raise ValueError('编导姓名已存在，请增加区分标识以避免成片重名')
            password = str(data.get('password',''))
            if not 12 <= len(password) <= 128:
                raise ValueError('密码长度需为 12～128 位')
            salt = secrets.token_hex(16)
            db.execute('INSERT INTO users VALUES (?,?,?,?,?,1)',(username,name,'producer',salt,digest(password,salt)))
        elif action in ('disable','enable','reset_password'):
            if not row or row['role']=='admin':
                raise ValueError('该账户不能执行此操作')
            if action=='reset_password':
                password = str(data.get('password',''))
                if not 12 <= len(password) <= 128:
                    raise ValueError('密码长度需为 12～128 位')
                salt = secrets.token_hex(16)
                db.execute('UPDATE users SET salt=?,hash=? WHERE username=?',(salt,digest(password,salt),username))
            else:
                db.execute('UPDATE users SET enabled=? WHERE username=?',(int(action=='enable'),username))
            db.execute('DELETE FROM sessions WHERE username=?',(username,))
        else:
            raise ValueError('未知账户操作')
        db.execute('INSERT INTO audit VALUES (?,?,?,?)',(time.time(),actor,action,username))
    return users(path)


def owner(folder):
    file = folder / 'access.json'
    return read_json(file)['owner'] if file.exists() else 'admin'


def allowed(folder, user):
    return user['role']=='admin' or owner(folder)==user['username']


def assign(folder, user):
    write_json_atomic(folder / 'access.json', {'owner':user['username']})


def audit(path, actor, action, target):
    with connect(path) as db:
        db.execute('INSERT INTO audit VALUES (?,?,?,?)',(time.time(),actor,action,target))
