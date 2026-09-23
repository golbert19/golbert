from fastapi import FastAPI, HTTPException, Header, Depends
from fastapi.middleware.cors import CORSMiddleware
import libvirt, os, uuid, subprocess, psutil, sqlite3, hashlib, jwt, datetime
from pydantic import BaseModel

app = FastAPI(title="Golbert PRO")
app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_methods=["*"], allow_headers=["*"])

SECRET = "golbert19-secret-key-cambialo"
conn = libvirt.open('qemu:///system')
DB = "/opt/golbert/golbert.db"

def db():
    con = sqlite3.connect(DB)
    con.row_factory = sqlite3.Row
    return con

# Init DB
def init_db():
    c = db()
    c.execute("CREATE TABLE IF NOT EXISTS users (id INTEGER PRIMARY KEY, email TEXT UNIQUE, password TEXT, plan TEXT DEFAULT 'free', vps_limit INTEGER DEFAULT 1)")
    c.execute("CREATE TABLE IF NOT EXISTS vps (name TEXT PRIMARY KEY, user_id INTEGER, vcpu INTEGER, ram INTEGER, disk INTEGER)")
    c.execute("INSERT OR IGNORE INTO users (id, email, password, plan, vps_limit) VALUES (1, 'admin@golbert.com',?, 'pro', 100)", (hashlib.sha256(b"admin123").hexdigest(),))
    c.commit()
init_db()

class UserCreate(BaseModel): email: str; password: str
class VMCreate(BaseModel): vcpu: int = 1; ram: int = 1024; disk: int = 20; name: str = None; plan: str = "basic"

PLANS = {
    "basic": {"vcpu": 1, "ram": 1024, "disk": 20, "price": 5},
    "medium": {"vcpu": 2, "ram": 2048, "disk": 40, "price": 10},
    "pro": {"vcpu": 4, "ram": 4096, "disk": 80, "price": 20}
}

def get_user(token: str = Header(None, alias="Authorization")):
    if not token: raise HTTPException(401, "No token")
    try:
        token = token.replace("Bearer ", "")
        data = jwt.decode(token, SECRET, algorithms=["HS256"])
        return data
    except: raise HTTPException(401, "Token inválido")

@app.post("/auth/register")
def register(u: UserCreate):
    c = db();
    try:
        c.execute("INSERT INTO users (email, password) VALUES (?,?)", (u.email, hashlib.sha256(u.password.encode()).hexdigest()))
        c.commit(); return {"status":"ok"}
    except: raise HTTPException(400, "Usuario ya existe")

@app.post("/auth/login")
def login(u: UserCreate):
    c = db()
    row = c.execute("SELECT * FROM users WHERE email=? AND password=?", (u.email, hashlib.sha256(u.password.encode()).hexdigest())).fetchone()
    if not row: raise HTTPException(401, "Credenciales malas")
    token = jwt.encode({"id": row["id"], "email": row["email"], "exp": datetime.datetime.utcnow() + datetime.timedelta(days=7)}, SECRET)
    return {"token": token, "email": row["email"], "plan": row["plan"]}

@app.get("/plans")
def plans(): return PLANS

@app.get("/vms")
def list_vms(user=Depends(get_user)):
    c = db()
    my_vps = c.execute("SELECT * FROM vps WHERE user_id=?", (user["id"],)).fetchall()
    result = []
    for r in my_vps:
        try:
            dom = conn.lookupByName(r["name"])
            state = dom.getInfo()[0]
        except: state = 0
        result.append(dict(r) | {"state": state})
    return result

@app.post("/vms")
def create_vm(data: VMCreate, user=Depends(get_user)):
    c = db()
    count = c.execute("SELECT COUNT(*) FROM vps WHERE user_id=?", (user["id"],)).fetchone()[0]
    limit_row = c.execute("SELECT vps_limit FROM users WHERE id=?", (user["id"],)).fetchone()
    if count >= limit_row[0]: raise HTTPException(403, f"Límite de {limit_row[0]} VPS alcanzado. Actualiza tu plan.")

    plan_cfg = PLANS.get(data.plan, PLANS["basic"])
    name = data.name or f"golbert-{user['id']}-{uuid.uuid4().hex[:4]}"
    disk_path = f"/var/lib/libvirt/images/{name}.qcow2"
    subprocess.run(["qemu-img", "create", "-f", "qcow2", disk_path, f"{plan_cfg['disk']}G"], check=True)
    xml = f"""<domain type='kvm'><name>{name}</name><memory unit='MiB'>{plan_cfg['ram']}</memory><vcpu>{plan_cfg['vcpu']}</vcpu><os><type>hvm</type><boot dev='hd'/></os><devices><disk type='file' device='disk'><driver name='qemu' type='qcow2'/><source file='{disk_path}'/><target dev='vda' bus='virtio'/></disk><interface type='network'><source network='default'/><model type='virtio'/></interface><graphics type='vnc' listen='0.0.0.0' port='-1'/></devices></domain>"""
    dom = conn.defineXML(xml); dom.create()
    c.execute("INSERT INTO vps VALUES (?,?,?,?,?)", (name, user["id"], plan_cfg["vcpu"], plan_cfg["ram"], plan_cfg["disk"])); c.commit()
    return {"status":"created", "name": name, "plan": plan_cfg}

@app.post("/vms/{name}/{action}")
def action_vm(name: str, action: str, user=Depends(get_user)):
    c = db()
    if not c.execute("SELECT * FROM vps WHERE name=? AND user_id=?", (name, user["id"])).fetchone() and user["email"]!="admin@golbert.com":
        raise HTTPException(403, "No es tu VPS")
    dom = conn.lookupByName(name)
    if action=="start": dom.create()
    elif action=="stop": dom.shutdown()
    elif action=="reboot": dom.reboot()
    elif action=="delete":
        try: dom.destroy()
        except: pass
        dom.undefine(); c.execute("DELETE FROM vps WHERE name=?", (name,)); c.commit()
    return {"status":"ok"}

@app.get("/stats")
def stats(): return {"cpu": psutil.cpu_percent(), "ram": psutil.virtual_memory().percent}
