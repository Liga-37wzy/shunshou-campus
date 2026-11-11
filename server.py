"""顺手校园: an original, local-first campus service prototype."""
from __future__ import annotations

import argparse
from contextlib import closing
import hashlib
import math
from pathlib import Path
import secrets
import sqlite3
import time

from flask import Flask, g, jsonify, request, session
from werkzeug.security import generate_password_hash, check_password_hash

ROOT = Path(__file__).resolve().parent
LOCATIONS = [
    {"id": "gate", "name": "南门快递站", "x": 120, "y": 760},
    {"id": "dorm", "name": "梧桐宿舍区", "x": 360, "y": 180},
    {"id": "library", "name": "图书馆", "x": 700, "y": 300},
    {"id": "canteen", "name": "第一食堂", "x": 480, "y": 530},
    {"id": "teaching", "name": "明理教学楼", "x": 900, "y": 600},
    {"id": "hub", "name": "顺手帮服务站", "x": 360, "y": 420},
]
POINTS = {p["id"]: p for p in LOCATIONS}
CATEGORIES = ("快递代拿", "物品代送", "日常代办", "易物配送")

SCHEMA = """
CREATE TABLE IF NOT EXISTS users (
 id INTEGER PRIMARY KEY, username TEXT UNIQUE NOT NULL, password TEXT NOT NULL,
 name TEXT NOT NULL, role TEXT NOT NULL DEFAULT 'student');
CREATE TABLE IF NOT EXISTS tasks (
 id INTEGER PRIMARY KEY, title TEXT NOT NULL, description TEXT NOT NULL,
 category TEXT NOT NULL, pickup TEXT NOT NULL, destination TEXT NOT NULL,
 reward INTEGER NOT NULL CHECK(reward>0), deadline INTEGER NOT NULL,
 publisher INTEGER NOT NULL REFERENCES users(id), runner INTEGER REFERENCES users(id),
 status TEXT NOT NULL DEFAULT 'open', created INTEGER NOT NULL);
CREATE TABLE IF NOT EXISTS events (
 id INTEGER PRIMARY KEY, task_id INTEGER NOT NULL REFERENCES tasks(id),
 actor INTEGER REFERENCES users(id), action TEXT NOT NULL, created INTEGER NOT NULL);
CREATE TABLE IF NOT EXISTS items (
 id INTEGER PRIMARY KEY, owner INTEGER NOT NULL REFERENCES users(id),
 title TEXT NOT NULL, description TEXT NOT NULL, wants TEXT NOT NULL,
 location TEXT NOT NULL, category TEXT NOT NULL, status TEXT NOT NULL DEFAULT 'available');
CREATE TABLE IF NOT EXISTS exchanges (
 id INTEGER PRIMARY KEY, offered INTEGER NOT NULL REFERENCES items(id),
 wanted INTEGER NOT NULL REFERENCES items(id), proposer INTEGER NOT NULL REFERENCES users(id),
 recipient INTEGER NOT NULL REFERENCES users(id), status TEXT NOT NULL DEFAULT 'pending',
 proposer_ok INTEGER NOT NULL DEFAULT 0, recipient_ok INTEGER NOT NULL DEFAULT 0,
 delivery_task_id INTEGER REFERENCES tasks(id), created INTEGER NOT NULL);
CREATE TABLE IF NOT EXISTS found (
 id INTEGER PRIMARY KEY, finder INTEGER NOT NULL REFERENCES users(id), title TEXT NOT NULL,
 description TEXT NOT NULL, location TEXT NOT NULL, status TEXT NOT NULL DEFAULT 'open',
 created INTEGER NOT NULL);
CREATE TABLE IF NOT EXISTS claims (
 id INTEGER PRIMARY KEY, found_id INTEGER NOT NULL REFERENCES found(id),
 claimant INTEGER NOT NULL REFERENCES users(id), evidence TEXT NOT NULL,
 status TEXT NOT NULL DEFAULT 'pending', created INTEGER NOT NULL,
 UNIQUE(found_id, claimant));
CREATE TABLE IF NOT EXISTS checkins (
 user_id INTEGER PRIMARY KEY REFERENCES users(id), expires INTEGER NOT NULL);
CREATE TABLE IF NOT EXISTS parcels (
 id INTEGER PRIMARY KEY, owner INTEGER NOT NULL REFERENCES users(id), label TEXT NOT NULL,
 code_hash TEXT NOT NULL, status TEXT NOT NULL DEFAULT 'pending', attempts INTEGER NOT NULL DEFAULT 0,
 created INTEGER NOT NULL, collected INTEGER);
CREATE INDEX IF NOT EXISTS tasks_status ON tasks(status, deadline);
CREATE INDEX IF NOT EXISTS events_task ON events(task_id);
"""


class DomainError(Exception):
    def __init__(self, message, status=400):
        self.message, self.status = message, status


def now():
    return int(time.time())


def db():
    if "db" not in g:
        from flask import current_app
        g.db = sqlite3.connect(current_app.config["DATABASE"], timeout=10)
        g.db.row_factory = sqlite3.Row
        g.db.execute("PRAGMA foreign_keys=ON")
        g.db.execute("PRAGMA busy_timeout=10000")
    return g.db


def allrows(sql, params=()):
    return [dict(r) for r in db().execute(sql, params)]


def one(sql, params=()):
    row = db().execute(sql, params).fetchone()
    if row is None:
        raise DomainError("记录不存在", 404)
    return dict(row)


def textfield(data, key, maximum=200, minimum=1):
    value = data.get(key, "")
    if not isinstance(value, str) or not minimum <= len(value.strip()) <= maximum:
        raise DomainError(f"{key} 请填写 {minimum}—{maximum} 个字符")
    return value.strip()


def integer(data, key, low, high):
    value = data.get(key)
    if isinstance(value, bool) or not isinstance(value, int) or not low <= value <= high:
        raise DomainError(f"{key} 超出允许范围")
    return value


def location(data, key):
    value = data.get(key)
    if not isinstance(value, str) or value not in POINTS:
        raise DomainError("请选择校园内的地点")
    return value


def user():
    uid = session.get("uid")
    if uid is None:
        raise DomainError("请先选择演示角色或登录", 401)
    return one("SELECT id,username,name,role FROM users WHERE id=?", (uid,))


def require_staff():
    u = user()
    if u["role"] != "station":
        raise DomainError("仅服务站工作人员可以操作", 403)
    return u


def event(task_id, uid, action):
    db().execute("INSERT INTO events(task_id,actor,action,created) VALUES(?,?,?,?)",
                 (task_id, uid, action, now()))


def fee(reward):
    return min((reward + 5) // 10, 200)


def distance(a, b):
    return math.hypot(POINTS[a]["x"] - POINTS[b]["x"], POINTS[a]["y"] - POINTS[b]["y"])


def match_task(task, origin, destination, minutes, max_detour, clock=None):
    """Explainable single-task insertion. Distances are synthetic demo meters."""
    clock = clock or now()
    route = distance(origin, task["pickup"]) + distance(task["pickup"], task["destination"]) + distance(task["destination"], destination)
    detour = max(0, route - distance(origin, destination))
    delivery_minutes = (distance(origin, task["pickup"]) + distance(task["pickup"], task["destination"])) / 70 + 2
    if detour > max_detour or route / 70 + 2 > minutes or clock + delivery_minutes * 60 > task["deadline"]:
        return None
    score = max(0, min(100, round(100 - detour / 12)))
    return {"score": score, "detour_m": round(detour), "extra_minutes": round(detour / 70 + 2, 1),
            "reason": f"顺路度 {score} · 额外绕行 {round(detour)} 米 · 时间可达"}


def seed(conn):
    if conn.execute("SELECT count(*) FROM users").fetchone()[0]:
        return
    # Demo identities have unguessable passwords; entry exists only in explicit demo mode.
    for username, name, role in [("demo_lin", "林同学", "student"), ("demo_chen", "陈同学", "student"), ("demo_zhou", "周同学", "student"), ("demo_hub", "服务站管理员", "station")]:
        conn.execute("INSERT INTO users(username,password,name,role) VALUES(?,?,?,?)", (username, generate_password_hash(secrets.token_urlsafe(32)), name, role))
    tasks = [
        ("帮我取一份快递", "一个小包裹，宿舍区门口交接即可。", "快递代拿", "gate", "dorm", 500, 2),
        ("图书馆的书送到教学楼", "两本教材，已经装好袋子。", "物品代送", "library", "teaching", 400, 3),
        ("资料送到顺手帮服务站", "一份普通活动报名材料，服务站当面交接。", "日常代办", "canteen", "hub", 300, 2),
        ("宿舍区小纸箱送往南门", "空纸箱一只，轻便不易碎。", "物品代送", "dorm", "gate", 600, 3),
    ]
    for title, desc, cat, pickup, dest, reward, publisher in tasks:
        cur = conn.execute("INSERT INTO tasks(title,description,category,pickup,destination,reward,deadline,publisher,created) VALUES(?,?,?,?,?,?,?,?,?)", (title, desc, cat, pickup, dest, reward, now()+7200, publisher, now()))
        conn.execute("INSERT INTO events(task_id,actor,action,created) VALUES(?,?,?,?)", (cur.lastrowid, publisher, "发布委托", now()))
    for row in [(2,"九成新阅读灯","三档色温，适合宿舍阅读。","一本小说或桌面收纳","dorm","数码"), (3,"高等数学笔记","个人整理的复习笔记，干净完整。","闲置文具","library","书籍"), (1,"桌面收纳盒","白色分格收纳，已清洁。","阅读灯或书籍","dorm","生活"), (1,"旅行随行杯","未使用的礼品杯。","文具或植物","dorm","生活")]:
        conn.execute("INSERT INTO items(owner,title,description,wants,location,category) VALUES(?,?,?,?,?,?)", row)
    conn.execute("INSERT INTO found(finder,title,description,location,created) VALUES(?,?,?,?,?)", (2,"捡到一把折叠伞","昨晚在图书馆入口拾到，请描述颜色及特殊标记。","library",now()))
    conn.execute("INSERT INTO found(finder,title,description,location,created) VALUES(?,?,?,?,?)", (3,"一串钥匙等待主人","已送到顺手帮服务站。请说明钥匙数量和挂件。","hub",now()))


def create_app(config=None):
    app = Flask(__name__, static_folder="web", static_url_path="/static")
    app.config.update(DATABASE=str(ROOT / "data" / "campus.sqlite"), DEMO_MODE=False,
                      MAX_CONTENT_LENGTH=32*1024, SESSION_COOKIE_HTTPONLY=True,
                      SESSION_COOKIE_SAMESITE="Strict", PERMANENT_SESSION_LIFETIME=3600)
    if config:
        app.config.update(config)
    Path(app.config["DATABASE"]).parent.mkdir(parents=True, exist_ok=True)
    if not app.config.get("SECRET_KEY"):
        key_path = Path(app.config["DATABASE"]).parent / ".session-key"
        if not key_path.exists():
            key_path.write_text(secrets.token_hex(32), encoding="utf-8")
        app.config["SECRET_KEY"] = key_path.read_text(encoding="utf-8").strip()
    with closing(sqlite3.connect(app.config["DATABASE"])) as conn:
        with conn:
            conn.executescript(SCHEMA)
            if app.config["DEMO_MODE"]:
                seed(conn)

    @app.teardown_appcontext
    def close_db(_error=None):
        conn = g.pop("db", None)
        if conn:
            conn.close()

    @app.errorhandler(DomainError)
    def domain_error(exc):
        return jsonify(error=exc.message), exc.status

    @app.errorhandler(413)
    def too_large(_exc):
        return jsonify(error="提交内容过大"), 413

    @app.before_request
    def guard():
        if request.method == "POST" and request.path.startswith("/api/"):
            # The custom header prevents cross-origin form posts; no CORS is enabled.
            if request.headers.get("X-Campus-Client") != "web":
                raise DomainError("请求来源校验失败", 403)
            origin = request.headers.get("Origin")
            if origin and origin != request.host_url.rstrip("/"):
                raise DomainError("不允许跨站提交", 403)
            if not request.is_json or not isinstance(request.get_json(silent=True), dict):
                raise DomainError("需要 JSON 对象")

    @app.after_request
    def headers(response):
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["Referrer-Policy"] = "same-origin"
        response.headers["Content-Security-Policy"] = "default-src 'self'; script-src 'self'; style-src 'self'; img-src 'self' data:; connect-src 'self'; frame-ancestors 'none'; base-uri 'self'; form-action 'self'"
        if request.path.startswith("/api/"):
            response.headers["Cache-Control"] = "no-store"
        return response

    @app.get("/")
    def index():
        return app.send_static_file("index.html")

    @app.get("/api/bootstrap")
    def bootstrap():
        me = user() if session.get("uid") else None
        demos = allrows("SELECT id,name,role FROM users WHERE username LIKE 'demo_%' ORDER BY id") if app.config["DEMO_MODE"] else []
        return jsonify(me=me, demo=app.config["DEMO_MODE"], demo_users=demos, locations=LOCATIONS, categories=CATEGORIES)

    @app.post("/api/auth/demo")
    def demo_login():
        if not app.config["DEMO_MODE"]:
            raise DomainError("演示入口未启用", 404)
        uid = integer(request.json, "user_id", 1, 1000000)
        u = one("SELECT id FROM users WHERE id=? AND username LIKE 'demo_%'", (uid,))
        session.clear()
        session["uid"] = u["id"]
        session.permanent = True
        return jsonify(ok=True)

    @app.post("/api/auth/register")
    def register():
        data = request.json
        username = textfield(data, "username", 30, 3)
        password = textfield(data, "password", 128, 10)
        name = textfield(data, "name", 20)
        if not username.isascii() or not username.replace("_", "").isalnum() or username.startswith("demo_"):
            raise DomainError("用户名请使用英文字母、数字或下划线")
        try:
            with db():
                cur = db().execute("INSERT INTO users(username,password,name) VALUES(?,?,?)", (username, generate_password_hash(password), name))
                session.clear()
                session["uid"] = cur.lastrowid
                session.permanent = True
        except sqlite3.IntegrityError:
            raise DomainError("用户名已存在", 409)
        return jsonify(ok=True), 201

    @app.post("/api/auth/login")
    def login():
        data = request.json
        rows = allrows("SELECT * FROM users WHERE username=?", (textfield(data,"username",30),))
        password = textfield(data,"password",128)
        if not rows or not check_password_hash(rows[0]["password"], password):
            raise DomainError("用户名或密码错误", 401)
        session.clear()
        session["uid"] = rows[0]["id"]
        session.permanent = True
        return jsonify(ok=True)

    @app.post("/api/auth/logout")
    def logout():
        session.clear()
        return jsonify(ok=True)

    @app.get("/api/tasks")
    def tasks():
        u = user()
        rows = allrows("SELECT t.*,p.name publisher_name,r.name runner_name FROM tasks t JOIN users p ON t.publisher=p.id LEFT JOIN users r ON t.runner=r.id ORDER BY t.id DESC")
        for row in rows:
            row.update(fee=fee(row["reward"]), runner_income=row["reward"]-fee(row["reward"]), expired=row["deadline"]<=now())
        return jsonify(tasks=rows, me=u["id"])

    @app.post("/api/tasks")
    def publish():
        u, d = user(), request.json
        title, desc = textfield(d,"title",60), textfield(d,"description",500)
        cat = d.get("category")
        if cat not in CATEGORIES[:-1]:
            raise DomainError("请选择委托分类")
        pickup, dest = location(d,"pickup"), location(d,"destination")
        if pickup == dest:
            raise DomainError("起点和终点不能相同")
        reward = integer(d,"reward",100,20000)
        duration = integer(d,"minutes",10,1440)
        with db():
            cur = db().execute("INSERT INTO tasks(title,description,category,pickup,destination,reward,deadline,publisher,created) VALUES(?,?,?,?,?,?,?,?,?)",(title,desc,cat,pickup,dest,reward,now()+duration*60,u["id"],now()))
            event(cur.lastrowid,u["id"],"发布委托")
        return jsonify(id=cur.lastrowid),201

    @app.get("/api/matches")
    def matches():
        u=user()
        origin,dest=location(request.args,"origin"),location(request.args,"destination")
        try:
            minutes=int(request.args.get("minutes","30")); limit=int(request.args.get("max_detour","800"))
        except ValueError:
            raise DomainError("时间和绕路距离必须为整数")
        if not 5<=minutes<=240 or not 0<=limit<=3000:
            raise DomainError("路线参数超出范围")
        active=db().execute("SELECT count(*) FROM tasks WHERE runner=? AND status IN ('accepted','delivered')",(u["id"],)).fetchone()[0]
        result=[]
        if active<3:
            for t in allrows("SELECT * FROM tasks WHERE status='open' AND category<>'易物配送' AND publisher<>?",(u["id"],)):
                match=match_task(t,origin,dest,minutes,limit)
                if match:
                    result.append(dict(t,**match))
        result.sort(key=lambda t:(-t["score"],t["deadline"]))
        return jsonify(matches=result,active=active,capacity=3,method="单单插入路线 · 校园示意距离")

    @app.post("/api/tasks/<int:tid>/<action>")
    def task_action(tid,action):
        u=user()
        with db():
            db().execute("BEGIN IMMEDIATE")
            t=one("SELECT * FROM tasks WHERE id=?",(tid,))
            if action=="accept":
                if t["publisher"]==u["id"]:
                    raise DomainError("不能接自己发布的委托",403)
                if t["status"]!="open" or t["deadline"]<=now():
                    raise DomainError("订单已被接取或已过期",409)
                active=db().execute("SELECT count(*) FROM tasks WHERE runner=? AND status IN ('accepted','delivered')",(u["id"],)).fetchone()[0]
                if active>=3:
                    raise DomainError("最多同时接取 3 单，请先完成已有委托",409)
                db().execute("UPDATE tasks SET runner=?,status='accepted' WHERE id=?",(u["id"],tid))
                label="接取委托"
            elif action=="deliver":
                if t["runner"]!=u["id"]:
                    raise DomainError("仅接单者可确认送达",403)
                if t["status"]!="accepted":
                    raise DomainError("当前状态不能确认送达",409)
                db().execute("UPDATE tasks SET status='delivered' WHERE id=?",(tid,)); label="已送达，等待委托人确认"
            elif action=="complete":
                if t["publisher"]!=u["id"]:
                    raise DomainError("仅委托人可确认完成",403)
                if t["status"]!="delivered":
                    raise DomainError("请等待接单者确认送达",409)
                db().execute("UPDATE tasks SET status='completed' WHERE id=?",(tid,)); label="确认完成（费用仅模拟）"
            elif action=="cancel":
                if t["publisher"]!=u["id"]:
                    raise DomainError("仅委托人可取消",403)
                if t["status"]!="open":
                    raise DomainError("接单后不能单方面取消，请协商处理",409)
                db().execute("UPDATE tasks SET status='cancelled' WHERE id=?",(tid,)); label="取消委托"
            else:
                raise DomainError("未知操作",404)
            event(tid,u["id"],label)
        return jsonify(ok=True)

    @app.get("/api/tasks/<int:tid>/events")
    def events(tid):
        u=user(); t=one("SELECT * FROM tasks WHERE id=?",(tid,))
        if u["id"] not in (t["publisher"],t["runner"]) and u["role"]!="station":
            raise DomainError("仅订单参与者可查看记录",403)
        return jsonify(events=allrows("SELECT e.action,e.created,u.name FROM events e LEFT JOIN users u ON e.actor=u.id WHERE task_id=? ORDER BY e.id",(tid,)))

    @app.get("/api/items")
    def items():
        user()
        return jsonify(items=allrows("SELECT i.*,u.name owner_name FROM items i JOIN users u ON i.owner=u.id ORDER BY i.id DESC"))

    @app.post("/api/items")
    def add_item():
        u,d=user(),request.json
        values=(u["id"],textfield(d,"title",60),textfield(d,"description",300),textfield(d,"wants",100),location(d,"location"),textfield(d,"category",20))
        with db():
            cur=db().execute("INSERT INTO items(owner,title,description,wants,location,category) VALUES(?,?,?,?,?,?)",values)
        return jsonify(id=cur.lastrowid),201

    @app.get("/api/exchanges")
    def exchanges():
        u=user()
        return jsonify(exchanges=allrows("SELECT e.*,a.title offered_title,b.title wanted_title,p.name proposer_name,r.name recipient_name FROM exchanges e JOIN items a ON e.offered=a.id JOIN items b ON e.wanted=b.id JOIN users p ON e.proposer=p.id JOIN users r ON e.recipient=r.id WHERE e.proposer=? OR e.recipient=? ORDER BY e.id DESC",(u["id"],u["id"])))

    @app.post("/api/exchanges")
    def propose():
        u,d=user(),request.json
        offered,wanted=integer(d,"offered",1,10**9),integer(d,"wanted",1,10**9)
        with db():
            db().execute("BEGIN IMMEDIATE")
            a,b=one("SELECT * FROM items WHERE id=?",(offered,)),one("SELECT * FROM items WHERE id=?",(wanted,))
            if a["owner"]!=u["id"] or b["owner"]==u["id"]:
                raise DomainError("请选择自己的物品与其他同学交换",403)
            if a["status"]!="available" or b["status"]!="available":
                raise DomainError("物品已被预留",409)
            if db().execute("SELECT 1 FROM exchanges WHERE offered=? AND wanted=? AND status='pending'",(offered,wanted)).fetchone():
                raise DomainError("已提交交换申请",409)
            cur=db().execute("INSERT INTO exchanges(offered,wanted,proposer,recipient,created) VALUES(?,?,?,?,?)",(offered,wanted,u["id"],b["owner"],now()))
        return jsonify(id=cur.lastrowid),201

    @app.post("/api/exchanges/<int:eid>/<action>")
    def exchange_action(eid,action):
        u=user()
        with db():
            db().execute("BEGIN IMMEDIATE")
            e=one("SELECT * FROM exchanges WHERE id=?",(eid,))
            if u["id"] not in (e["proposer"],e["recipient"]):
                raise DomainError("仅交换双方可以操作",403)
            if action in ("accept","reject"):
                if u["id"]!=e["recipient"] or e["status"]!="pending":
                    raise DomainError("当前状态不能处理申请",409)
                if action=="reject":
                    db().execute("UPDATE exchanges SET status='rejected' WHERE id=?",(eid,))
                else:
                    a=one("SELECT * FROM items WHERE id=?",(e["offered"],)); b=one("SELECT * FROM items WHERE id=?",(e["wanted"],))
                    if a["status"]!="available" or b["status"]!="available":
                        raise DomainError("其中一件物品已被交换",409)
                    db().execute("UPDATE items SET status='reserved' WHERE id IN (?,?)",(e["offered"],e["wanted"]))
                    db().execute("UPDATE exchanges SET status='accepted' WHERE id=?",(eid,))
                    db().execute("UPDATE exchanges SET status='rejected' WHERE id<>? AND status='pending' AND (offered IN (?,?) OR wanted IN (?,?))",(eid,e["offered"],e["wanted"],e["offered"],e["wanted"]))
            elif action=="delivery":
                if e["status"]!="accepted" or u["id"]!=e["proposer"]:
                    raise DomainError("由发起人在双方同意后创建配送",409)
                if e["delivery_task_id"]:
                    return jsonify(ok=True,task_id=e["delivery_task_id"])
                a=one("SELECT * FROM items WHERE id=?",(e["offered"],)); b=one("SELECT * FROM items WHERE id=?",(e["wanted"],))
                cur=db().execute("INSERT INTO tasks(title,description,category,pickup,destination,reward,deadline,publisher,created) VALUES(?,?,?,?,?,?,?,?,?)",(f"易物交接：{a['title']} ↔ {b['title']}","双向交换：到起点取第一件物品，送至对方地点并带回交换物。5 元为演示报价，发布前双方已确认。","易物配送",a["location"],b["location"],500,now()+7200,u["id"],now()))
                # A round-trip exchange cannot use the single-trip route matcher (see matches filter).
                db().execute("UPDATE exchanges SET delivery_task_id=? WHERE id=?",(cur.lastrowid,eid))
                event(cur.lastrowid,u["id"],"由易物申请生成双向配送单")
            elif action=="confirm":
                if e["status"]!="accepted":
                    raise DomainError("当前状态不能确认交接",409)
                if e["delivery_task_id"]:
                    t=one("SELECT status FROM tasks WHERE id=?",(e["delivery_task_id"],))
                    if t["status"] not in ("completed","cancelled"):
                        raise DomainError("请先完成关联配送单",409)
                column="proposer_ok" if u["id"]==e["proposer"] else "recipient_ok"
                db().execute(f"UPDATE exchanges SET {column}=1 WHERE id=?",(eid,))
                latest=one("SELECT * FROM exchanges WHERE id=?",(eid,))
                if latest["proposer_ok"] and latest["recipient_ok"]:
                    db().execute("UPDATE exchanges SET status='completed' WHERE id=?",(eid,))
                    db().execute("UPDATE items SET status='exchanged' WHERE id IN (?,?)",(e["offered"],e["wanted"]))
            else:
                raise DomainError("未知操作",404)
        return jsonify(ok=True)

    @app.get("/api/found")
    def found():
        u=user()
        entries=allrows("SELECT f.*,u.name finder_name FROM found f JOIN users u ON f.finder=u.id ORDER BY f.id DESC")
        claims=allrows("SELECT c.*,u.name claimant_name,f.title FROM claims c JOIN found f ON c.found_id=f.id JOIN users u ON c.claimant=u.id WHERE c.claimant=? OR f.finder=? OR ?='station' ORDER BY c.id DESC",(u["id"],u["id"],u["role"]))
        return jsonify(found=entries,claims=claims)

    @app.post("/api/found")
    def add_found():
        u,d=user(),request.json
        with db():
            cur=db().execute("INSERT INTO found(finder,title,description,location,created) VALUES(?,?,?,?,?)",(u["id"],textfield(d,"title",60),textfield(d,"description",300),location(d,"location"),now()))
        return jsonify(id=cur.lastrowid),201

    @app.post("/api/found/<int:fid>/claim")
    def claim(fid):
        u=user(); evidence=textfield(request.json,"evidence",300,5)
        with db():
            db().execute("BEGIN IMMEDIATE")
            f=one("SELECT * FROM found WHERE id=?",(fid,))
            if f["status"]!="open" or f["finder"]==u["id"]:
                raise DomainError("当前物品不能认领",409)
            try:
                db().execute("INSERT INTO claims(found_id,claimant,evidence,created) VALUES(?,?,?,?)",(fid,u["id"],evidence,now()))
            except sqlite3.IntegrityError:
                raise DomainError("已提交认领线索，请等待核验",409)
        return jsonify(ok=True),201

    @app.post("/api/claims/<int:cid>/return")
    def return_found(cid):
        u=user()
        with db():
            db().execute("BEGIN IMMEDIATE")
            c=one("SELECT c.*,f.finder,f.status item_status FROM claims c JOIN found f ON c.found_id=f.id WHERE c.id=?",(cid,))
            if u["id"]!=c["finder"] and u["role"]!="station":
                raise DomainError("仅拾获者或服务站可以核验归还",403)
            if c["status"]!="pending" or c["item_status"]!="open":
                raise DomainError("物品已归还",409)
            db().execute("UPDATE claims SET status=CASE WHEN id=? THEN 'returned' ELSE 'closed' END WHERE found_id=?",(cid,c["found_id"]))
            db().execute("UPDATE found SET status='returned' WHERE id=?",(c["found_id"],))
        return jsonify(ok=True)

    @app.get("/api/station")
    def station():
        u=user()
        return jsonify(name="顺手帮 · 梧桐服务站",hours="演示开放时间 08:00—22:00",capacity=20,
                       checked_in=allrows("SELECT u.id,u.name,c.expires FROM checkins c JOIN users u ON c.user_id=u.id WHERE expires>?",(now(),)),
                       occupancy=db().execute("SELECT count(*) FROM parcels WHERE status='stored'").fetchone()[0],
                       parcels=allrows("SELECT p.id,p.owner,p.label,p.status,p.created,p.collected,u.name owner_name FROM parcels p JOIN users u ON p.owner=u.id WHERE p.owner=? OR ?='station' ORDER BY p.id DESC",(u["id"],u["role"])))

    @app.post("/api/station/checkin")
    def checkin():
        u=user()
        with db():
            db().execute("INSERT INTO checkins(user_id,expires) VALUES(?,?) ON CONFLICT(user_id) DO UPDATE SET expires=excluded.expires",(u["id"],now()+3600))
        return jsonify(ok=True)

    @app.post("/api/station/parcels")
    def parcel():
        u=user(); label=textfield(request.json,"label",80)
        code=secrets.token_hex(4).upper()
        with db():
            cur=db().execute("INSERT INTO parcels(owner,label,code_hash,created) VALUES(?,?,?,?)",(u["id"],label,hashlib.sha256(code.encode()).hexdigest(),now()))
        return jsonify(id=cur.lastrowid,pickup_code=code),201

    @app.post("/api/station/parcels/<int:pid>/<action>")
    def parcel_action(pid,action):
        require_staff(); failure=None
        with db():
            db().execute("BEGIN IMMEDIATE")
            p=one("SELECT * FROM parcels WHERE id=?",(pid,))
            if action=="store":
                if p["status"]!="pending":
                    raise DomainError("仅待入库物品可操作",409)
                if db().execute("SELECT count(*) FROM parcels WHERE status='stored'").fetchone()[0]>=20:
                    raise DomainError("服务站存放位置已满",409)
                db().execute("UPDATE parcels SET status='stored' WHERE id=?",(pid,))
            elif action=="collect":
                code=textfield(request.json,"code",16).upper()
                if p["status"]!="stored" or p["attempts"]>=5:
                    raise DomainError("物品不在库或验证次数超限，请人工核验",409)
                if not secrets.compare_digest(hashlib.sha256(code.encode()).hexdigest(),p["code_hash"]):
                    db().execute("UPDATE parcels SET attempts=attempts+1 WHERE id=?",(pid,)); failure="取件码错误"
                else:
                    db().execute("UPDATE parcels SET status='collected',collected=? WHERE id=?",(now(),pid))
            else:
                raise DomainError("未知操作",404)
        if failure:
            raise DomainError(failure)
        return jsonify(ok=True)

    return app


if __name__ == "__main__":
    parser=argparse.ArgumentParser(description="顺手校园 · 本地原型")
    parser.add_argument("--demo",action="store_true",help="启用虚构数据和演示角色切换")
    parser.add_argument("--port",type=int,default=8000)
    parser.add_argument("--database",default=str(ROOT / "data" / "campus.sqlite"),help="SQLite 数据库文件路径")
    args=parser.parse_args()
    app=create_app({"DEMO_MODE":args.demo,"DATABASE":args.database})
    print(f"Open http://127.0.0.1:{args.port} (demo={args.demo})",flush=True)
    app.run(host="127.0.0.1",port=args.port,debug=False)
