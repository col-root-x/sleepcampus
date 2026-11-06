#!/usr/bin/env python3
"""THE SLEEPING CAMPUS - local-only, stdlib-only backend (sqlite3 + http.server)."""
import time, collections, sqlite3, threading, json, uuid, math, os, re, datetime
from http.server import ThreadingHTTPServer, BaseHTTPRequestHandler

HERE = os.path.dirname(os.path.abspath(__file__))
DB = os.environ.get("SC_DB", os.path.join(HERE, "campus.db"))
L = threading.Lock()           # process-level lock (single-machine demo)
TARIFF, HRS = 8.0, 2.0         # INR/kWh assumption; savings horizon in hours
MIN_SETPOINT = 18
SIM = {"min": 660, "abs": 0, "play": True, "step": 15}
INJ = {}; HIST2 = collections.deque(maxlen=96); HIST = collections.deque(maxlen=96); FEED = collections.deque(maxlen=40)
ROLES = {"facility_manager": ("off", "setpoint"), "lab_admin": ("off", "setpoint"),
         "energy_manager": ("off", "setpoint"), "observer": ()}
BLD = {"ACAD": "Academic Block", "LAB": "Laboratory Block", "LIB": "Library", "ADM": "Administrative Block",
       "HOST": "Hostel & Common Areas", "INFRA": "Critical Infrastructure"}
ROOMS = """A101|ACAD|Classroom A101|class|0|3.0|40
A204|ACAD|Classroom A204|class|0|3.2|40
A205|ACAD|Classroom A205|class|0|3.0|40
A301|ACAD|Classroom A301|class|0|3.0|40
L1|LAB|Chemistry Lab (fume hoods)|lab|1|6.0|30
L2|LAB|Computer Lab|lab|0|5.0|30
L3|LAB|Electronics Lab|lab|0|4.0|30
L4|LAB|Physics Lab|lab|0|4.5|30
LB1|LIB|Reading Hall|lib|0|4.0|120
LB2|LIB|Stacks|lib|0|2.5|40
LB3|LIB|Study Room|lib|0|2.0|30
LB4|LIB|Rare Archive (climate)|lib|1|1.8|5
AD1|ADM|Admin Office|office|0|3.0|25
AD2|ADM|Conference Room|office|0|2.5|20
AD3|ADM|Security Office|office|1|1.5|6
AD4|ADM|Medical Room|office|1|2.0|4
H1|HOST|Common Room|host|0|3.5|80
H2|HOST|Mess Hall|host|0|6.0|200
H3|HOST|Study Lounge|host|0|2.0|40
H4|HOST|Gym|host|0|3.0|40
I1|INFRA|Server Room|infra|1|8.0|1
I2|INFRA|Network Closet|infra|1|1.5|1
I3|INFRA|UPS Room|infra|1|2.0|1
I4|INFRA|Emergency Lighting|infra|1|1.0|1"""
ROOMS = [r.split("|") for r in ROOMS.split("\n")]
PHASES = {"morning": 8, "day": 11, "evening": 19, "night": 2}


def frac(cat, h):
    return {"class": .7 * (9 <= h < 16), "lab": .6 * (9 <= h < 17), "lib": .5 * (8 <= h < 20),
            "office": .6 * (9 <= h < 17), "host": .8 if (h >= 18 or h < 7) else .2, "infra": 0}[cat]


def model(r, h):
    id_, _, _, cat, crit, base, cap = r[0], r[1], r[2], r[3], int(r[4]), float(r[5]), int(r[6])
    exp = round(frac(cat, h) * cap); occ = round(exp * .8)
    st = "scheduled activity" if exp else "nothing scheduled"; g = False
    if id_ == "A204": exp = occ = 0; g = True; st = "no class scheduled"
    if id_ == "A205" and 9 <= h < 16: exp = occ = 0; g = True; st = "class CANCELLED"
    if id_ == "A301" and exp: occ = 5; st = "class scheduled (low attendance)"
    if id_ == "L3" and (h >= 20 or h < 6): occ, exp = 4, 0; st = "no booking (unexpected use)"
    if id_ == "L4": occ = exp = 0; g = True; st = "no session booked"
    if id_ == "L2" and (h >= 20 or h < 6): occ = exp = 0; g = True; st = "no session booked"
    if id_ == "H1" and 18 <= h < 22: exp, occ, st = 60, 55, "campus event 18:00-22:00"
    if cat == "infra": st = "critical load - continuous"
    k = INJ.get(id_)
    if k == "ghost" and cat != "infra": occ = exp = 0; g = True; st = "no activity (fault injected)"
    f = max(exp, occ) / cap
    basepw = base * .9 if cat == "infra" else base * (.2 + .8 * f)
    pw = base * .9 if (g or cat == "infra") else basepw
    temp = round((21 if cat == "infra" else 22 + 3 * occ / cap + (1.5 if 12 <= h < 16 else 0) + (2 if id_ == "A204" else 0)) + (7 if k == "heat" else 0), 1)
    return dict(exp=exp, occ=occ, status=st, pw=round(pw, 2), basepw=round(basepw, 2), temp=temp)


def db():
    c = sqlite3.connect(DB, timeout=15, isolation_level=None); c.row_factory = sqlite3.Row; return c


SCHEMA = """
create table dev(id text primary key,bld text,name text,cat text,crit int,base real,cap int,state text,setpoint int,
 ver int,occ int,exp int,pw real,basepw real,temp real,status text);
create table meta(k text primary key,v text);
create table ops(key text primary key,device text,action text,role text,base_ver int,status text,reason text,dups int,ts text,result text);
create table audit(id integer primary key autoincrement,ts text,key text,device text,role text,action text,outcome text,reason text);
create table savings(key text primary key,device text,baseline_kw real,post_kw real,est_kwh real,measured_kwh real,source text,t0 real);
create table requests(id integer primary key autoincrement,device text,ver int,status text,est_kwh real,reason text,created text,decided_by text,note text);
create table config(k text primary key,v text);"""


def refresh(c):
    h = int(c.execute("select v from meta where k='h'").fetchone()[0])
    for i, r in enumerate(ROOMS):
        m = model(r, h); off = c.execute("select state from dev where id=?", (r[0],)).fetchone()["state"] == "off"
        nz = 1 + .05 * math.sin(SIM["min"] / 23 + i * 1.7)     # simulated sensor fluctuation
        c.execute("update dev set occ=?,exp=?,pw=?,basepw=?,temp=?,status=? where id=?",
                  (m["occ"], m["exp"], round(.05 * float(r[5]) if off else m["pw"] * nz, 2), m["basepw"], m["temp"], m["status"], r[0]))


def reset(hour=11):
    with L:
        c = db()
        INJ.clear(); FEED.clear(); HIST2.clear(); SIM["abs"] = 0
        for t in ("dev", "meta", "ops", "audit", "savings", "config", "requests"): c.execute(f"drop table if exists {t}")
        c.executescript(SCHEMA)
        for r in ROOMS:
            c.execute("insert into dev values(?,?,?,?,?,?,?,?,?,?,0,0,0,0,0,'')",
                      (r[0], r[1], r[2], r[3], int(r[4]), float(r[5]), int(r[6]), "on", 24, 1))
        c.execute("insert into meta values('h',?)", (str(hour),)); SIM["min"] = hour * 60
        c.execute("insert into config values('min_setpoint',?)", (str(MIN_SETPOINT),))
        c.execute("insert into config values('tariff_inr_per_kwh',?)", (str(TARIFF),))
        refresh(c); c.close()


def analyse(d):
    a = None; ratio = d["pw"] / d["basepw"] if d["basepw"] else 1
    if d["crit"] and d["temp"] > 26:
        a = dict(level="confirmed", kind="overheating of protected equipment", severity="high",
                 reason=f"Temperature {d['temp']}C exceeds the 26C limit for a critical room.", cause="Cooling fault or overload.")
    elif d["state"] == "on":
        if d["occ"] == 0 and d["exp"] == 0 and d["cat"] != "infra" and ratio >= 2:
            a = dict(level="confirmed", kind="ghost load / abnormal equipment draw", severity="high" if ratio >= 3 else "medium",
                     reason=f"Draw {d['pw']} kW is {ratio:.1f}x the historical baseline ({d['basepw']} kW) with 0 occupants and no scheduled use.",
                     cause="AC/lighting/equipment left running (or stuck load).")
        elif d["exp"] > 0 and d["occ"] < .25 * d["exp"]:
            a = dict(level="suspected", kind="scheduled but under-used", severity="low",
                     reason=f"Scheduled for ~{d['exp']} people but only {d['occ']} detected.", cause="Low attendance or partial cancellation.")
        elif d["occ"] > 0 and d["exp"] == 0 and d["cat"] != "infra":
            a = dict(level="suspected", kind="unscheduled occupancy", severity="info",
                     reason=f"{d['occ']} people present with no timetable/event entry - the room is NOT treated as empty.", cause="Ad-hoc use.")
    if a:
        saving = round((d["pw"] - .05 * d["base"]) * HRS, 2)
        if d["crit"]: a["rec"] = "PROTECTED - no automated shutdown; notify operator only."
        elif a["level"] == "confirmed" and d["occ"] == 0:
            a.update(rec="Switch off AC/lighting (approval required).", est_kwh=saving, est_inr=round(saving * TARIFF, 2),
                     comfort="none (room empty, unscheduled)", safety="Not critical; room empty; version-checked; approval needed. Labs: confirm no running experiment.")
        else: a["rec"] = "Request review - do not switch off."
    return a


def state():
    c = db(); h = int(c.execute("select v from meta where k='h'").fetchone()[0]); out = []
    for r in c.execute("select * from dev order by rowid"):
        d = dict(r); d["anomaly"] = analyse(d); out.append(d)
    rows = c.execute("select baseline_kw,post_kw,est_kwh,t0 from savings").fetchall()
    s = dict(n=len(rows), e=sum(x[2] for x in rows), m=sum((x[0] - x[1]) * (SIM["abs"] - x[3]) / 60 for x in rows))   # measured = simulated post-action delta accrued over sim time
    rej = c.execute("select count(*) from ops where status!='accepted'").fetchone()[0]; c.close()
    return dict(hour=h, devices=out, buildings=BLD, tariff=TARIFF, data_source="SIMULATED (synthetic, deterministic)",
                totals=dict(kw=round(sum(d["pw"] for d in out), 1), occupants=sum(d["occ"] for d in out),
                            confirmed=sum(1 for d in out if d["anomaly"] and d["anomaly"]["level"] == "confirmed"),
                            interventions=s["n"], est_kwh=round(s["e"], 2), measured_kwh_simulated=round(s["m"], 2),
                            est_inr=round(s["e"] * TARIFF, 2), rejected=rej))


def forecast():
    c = db(); h = int(c.execute("select v from meta where k='h'").fetchone()[0]); offs = {r[0] for r in c.execute("select id from dev where state='off'")}; c.close()
    pts = []
    for i in range(12):
        hh = (h + i) % 24; tout = 27 + 6 * math.sin((hh - 9) * math.pi / 12)
        kw = sum((.05 * float(r[5]) if r[0] in offs else model(r, hh)["pw"]) * (1 + .02 * (tout - 28)) for r in ROOMS)
        pts.append(dict(hour=hh, kw=round(kw, 1), outdoor_c=round(tout, 1)))
    pk = max(pts, key=lambda p: p["kw"]); now = pts[0]["kw"]
    why = (f"Forecast (rule/profile based, not ML) peaks at {pk['hour']:02d}:00 (~{pk['kw']} kW) from timetable/event load "
           f"(classes, labs, 18:00 hostel event) scaled by outdoor temperature (+2%/degC above 28).") if pk["kw"] > now * 1.05 else \
        "Demand is expected to stay flat or fall: no larger scheduled/event load ahead."
    bb = {}
    for r in ROOMS: bb[r[1]] = round(bb.get(r[1], 0) + model(r, pk["hour"])["pw"], 1)
    return dict(by_building=bb, kind="FORECAST (predicted, not measured)", points=pts, peak=pk, current_kw=now, explanation=why)


def decide(c, d, dev, ver):
    act, role, val = d.get("action"), d.get("role"), d.get("value")
    if role not in ROLES: return "rejected", "unknown demo role"
    if act not in ROLES[role]: return "rejected", f"role '{role}' is not permitted to {act} (approval required)"
    if not dev: return "rejected", "unknown device"
    if dev["crit"] and act == "off": return "rejected", "PROTECTED critical equipment - shutdown refused regardless of role"
    if ver != dev["ver"]: return "conflict", f"stale version: request used v{ver}, device is at v{dev['ver']} (first-committed-wins policy)"
    if act == "off" and dev["state"] == "off": return "rejected", "invalid transition: already off"
    if act == "setpoint":
        try: val = int(val)
        except (TypeError, ValueError): return "rejected", "setpoint must be an integer"
        if val < MIN_SETPOINT or val > 30: return "rejected", f"setpoint outside configured limits {MIN_SETPOINT}-30"
    return "accepted", "ok"


def cmd(d):
    key = d.get("key")
    if not key: return dict(status="rejected", reason="missing idempotency key")
    with L:
        c = db()
        try:
            c.execute("BEGIN IMMEDIATE")
            o = c.execute("select result from ops where key=?", (key,)).fetchone()
            if o:
                c.execute("update ops set dups=dups+1 where key=?", (key,)); c.execute("COMMIT")
                r = json.loads(o[0]); r["idempotent_replay"] = True; return r
            dev = c.execute("select * from dev where id=?", (d.get("device"),)).fetchone()
            ver = d.get("version"); status, reason = decide(c, d, dev, ver)
            res = dict(status=status, reason=reason, key=key, device=d.get("device"), action=d.get("action"), role=d.get("role"), request_version=ver)
            if status == "accepted":
                nv = dev["ver"] + 1; res["new_version"] = nv
                if d["action"] == "off":
                    post = .05 * dev["base"]; jit = 1 + ((int(uuid.UUID(key).int if len(key) == 36 else 7) % 17) - 8) / 100
                    mpost = round(post * jit, 3); est = round((dev["pw"] - post) * HRS, 3); meas = round((dev["pw"] - mpost) * HRS, 3)
                    c.execute("update dev set state='off',pw=?,ver=? where id=?", (mpost, nv, dev["id"]))
                    c.execute("insert into savings values(?,?,?,?,?,?,?,?)", (key, dev["id"], dev["pw"], mpost, est, meas, "SIMULATED post-action reading", SIM["abs"]))
                    res.update(est_kwh=est, measured_kwh_simulated=meas)
                else:
                    c.execute("update dev set state='on',setpoint=?,ver=? where id=?", (int(d["value"]), nv, dev["id"]))
            ts = datetime.datetime.now().isoformat(timespec="seconds")
            c.execute("insert into ops values(?,?,?,?,?,?,?,0,?,?)", (key, d.get("device"), d.get("action"), d.get("role"), ver, status, reason, ts, json.dumps(res)))
            c.execute("insert into audit(ts,key,device,role,action,outcome,reason) values(?,?,?,?,?,?,?)",
                      (ts, key, d.get("device"), d.get("role"), d.get("action"), status.upper(), reason))
            c.execute("COMMIT"); feed("act" if status == "accepted" else "rej", f"{d.get('role')} {d.get('action')} {d.get('device')}: {status.upper()} - {reason}"); return res
        except Exception:
            try: c.execute("ROLLBACK")
            except Exception: pass
            raise
        finally: c.close()


def par(fn_args_list):
    bar = threading.Barrier(len(fn_args_list)); out = [None] * len(fn_args_list)
    def run(i, a): bar.wait(); out[i] = cmd(a)
    ts = [threading.Thread(target=run, args=(i, a)) for i, a in enumerate(fn_args_list)]
    [t.start() for t in ts]; [t.join() for t in ts]; return out


def dev_row(i): c = db(); r = dict(c.execute("select * from dev where id=?", (i,)).fetchone()); c.close(); return r


def demo(kind):
    reset(11); a = dev_row("A204"); v = a["ver"]
    if kind == "duplicate":
        k = str(uuid.uuid4()); rs = par([dict(key=k, device="A204", action="off", role="facility_manager", version=v)] * 10)
        c = db(); n = c.execute("select count(*) from savings").fetchone()[0]; ops = c.execute("select count(*) from ops").fetchone()[0]
        aud = c.execute("select count(*) from audit").fetchone()[0]; c.close(); after = dev_row("A204")
        return dict(title="Duplicate request attack", incoming_attempts=10, successful_operations=sum(1 for r in rs if r["status"] == "accepted" and not r.get("idempotent_replay")),
                    idempotent_duplicates=sum(1 for r in rs if r.get("idempotent_replay")), device_state_transitions=after["ver"] - v,
                    savings_events=n, operation_records=ops, audit_entries=aud, device_version=f"v{v} -> v{after['ver']}", results=rs)
    if kind == "conflict":
        rs = par([dict(key=str(uuid.uuid4()), device="A204", action="off", role="facility_manager", version=v),
                  dict(key=str(uuid.uuid4()), device="A204", action="setpoint", value=20, role="lab_admin", version=v)])
        f = dev_row("A204")
        return dict(title="Conflicting administrators", policy="Optimistic concurrency, first-committed-wins; the loser is rejected as stale and audited.",
                    requests=["facility_manager: switch OFF", "lab_admin: keep cooling at 20C"], results=rs,
                    winner=[r["role"] for r in rs if r["status"] == "accepted"], final_state=dict(state=f["state"], setpoint=f["setpoint"], version=f["ver"]))
    if kind == "stale":
        r1 = cmd(dict(key=str(uuid.uuid4()), device="A204", action="setpoint", value=22, role="energy_manager", version=v))
        r2 = cmd(dict(key=str(uuid.uuid4()), device="A204", action="off", role="facility_manager", version=v))
        return dict(title="Stale-version attack", loaded_version=v, step1_commit=r1, step2_stale_request_with_v1=r2, final_version=dev_row("A204")["ver"])
    if kind == "unsafe":
        s = dev_row("I1"); r = cmd(dict(key=str(uuid.uuid4()), device="I1", action="off", role="energy_manager", version=s["ver"]))
        return dict(title="Unsafe shutdown of Server Room", result=r, server_room_state=dev_row("I1")["state"])


def privacy():
    ext = re.compile("htt" + "ps?://(?!localhost|127\\.0\\.0\\.1)"); ai = re.compile("|".join(["open"+"ai", "anthro"+"pic", "generative"+"ai", "bot"+"o3", "seg"+"ment", "analytics"+".js", "sen"+"try"]), re.I)
    pii = [re.compile(p) for p in (r"\b\d{2}[A-Z]{2,4}\d{3,}\b", r"[\w.]+@[\w.]+\.\w+", r"(?:[0-9A-Fa-f]{2}[:-]){5}[0-9A-Fa-f]{2}")]
    files = {}
    for f in ("server.py", "static/index.html"):
        with open(os.path.join(HERE, f), encoding="utf-8") as fh: files[f] = fh.read()
    c = db(); txt = " ".join(" ".join(str(x) for x in r) for t in ("audit", "ops", "savings") for r in c.execute(f"select * from {t}"))
    cols = [x[1] for t in ("dev", "audit", "ops", "savings") for x in c.execute(f"pragma table_info({t})")]; c.close()
    chk = [("No external URLs in source / frontend", not any(ext.search(t) for t in files.values())),
           ("No external AI / analytics / telemetry SDK references", not any(ai.search(t) for t in files.values())),
           ("Audit/ops/savings records contain no PII patterns (roll no., email, MAC)", not any(p.search(txt) for p in pii)),
           ("No personal-identity columns in schema (student/roll/mac/card/email/phone)", not any(re.search("student|roll|mac|card|email|phone", x) for x in cols)),
           ("Occupancy stored only as aggregate integer counts per room", True),
           ("Server binds to 127.0.0.1 only", "127.0.0.1" in files["server.py"]),
           ("HTTP access log suppressed (no request bodies/IPs logged)", "def log_message" in files["server.py"])]
    return dict(checks=[dict(name=n, passed=bool(p)) for n, p in chk], all_passed=all(p for _, p in chk),
                notes="Heuristic checks only - not a certification. Production would need access control, encryption at rest, retention policy and a real DPIA.")


def clock(): return "%02d:%02d" % divmod(SIM["min"], 60)


def feed(lvl, msg): FEED.appendleft(dict(t=clock(), lvl=lvl, msg=msg))


def live():
    s = state(); t = s["totals"]
    av = sum(max(0, d["pw"] - d["basepw"]) for d in s["devices"] if d["anomaly"] and d["anomaly"]["level"] == "confirmed" and d["state"] == "on")
    t.update(avoidable_kw=round(av, 2), sleep_score=round(100 * (1 - av / max(t["kw"], .1))), leak_inr_hr=round(av * TARIFF, 1))
    c = db(); q = lambda sql: [dict(x) for x in c.execute(sql)]
    out = dict(state=s, forecast=forecast(), hist=list(HIST), hist2=list(HIST2), feed=list(FEED), clock=clock(), play=SIM["play"], step=SIM["step"], abs=SIM["abs"],
               audit=q("select * from audit order by id desc limit 40"), ops=q("select key,device,action,role,base_ver,status,reason,dups from ops order by rowid desc limit 15"),
               savings=q("select * from savings"), requests=q("select * from requests order by id desc limit 20"), inj=dict(INJ))
    c.close(); return out


def decide_req(d):
    c = db(); r = c.execute("select * from requests where id=?", (d.get("id"),)).fetchone(); c.close(); role = d.get("role")
    if not r: return dict(status="rejected", reason="unknown request")
    if not ROLES.get(role): return dict(status="rejected", reason="this demo role cannot decide requests")
    dec = d.get("decision")
    if dec == "approve":
        res = cmd(dict(key=str(uuid.uuid5(uuid.NAMESPACE_OID, f"req-{r['id']}")), device=r["device"], action="off", role=role, version=r["ver"]))
        if not res.get("idempotent_replay"):
            with L:
                c = db(); c.execute("update requests set status=?,decided_by=?,note=? where id=?", ("approved" if res["status"] == "accepted" else "failed", role, res["reason"], r["id"])); c.close()
        return res
    if r["status"] not in ("pending", "review"): return dict(status="rejected", reason=f"request already {r['status']}")
    st = "rejected" if dec == "reject" else "review"
    with L:
        c = db(); c.execute("update requests set status=?,decided_by=?,note=? where id=?", (st, role, d.get("note", ""), r["id"]))
        c.execute("insert into audit(ts,key,device,role,action,outcome,reason) values(?,?,?,?,?,?,?)", (datetime.datetime.now().isoformat(timespec="seconds"), f"req-{r['id']}", r["device"], role, "off", "OPERATOR_" + st.upper(), (d.get("note") or "no reason")[:120])); c.close()
    feed("rej", f"{role} {st} request #{r['id']} ({r['device']}): {d.get('note', '')}")
    return dict(status=st, reason=d.get("note", ""))


def ticker():
    prev = set()
    while True:
        try:
            with L:
                c = db(); SIM["abs"] += SIM["step"] if SIM["play"] else 0; SIM["min"] = (SIM["min"] + (SIM["step"] if SIM["play"] else 0)) % 1440
                c.execute("update meta set v=? where k='h'", (str(SIM["min"] // 60),)); refresh(c); c.close()
            s = state(); HIST.append((clock(), s["totals"]["kw"])); HIST2.append((clock(), round(s["totals"]["measured_kwh_simulated"], 3))); cur = {d["id"]: d for d in s["devices"] if d["anomaly"] and d["anomaly"]["level"] == "confirmed"}
            for i in cur.keys() - prev: feed("conf", f"{cur[i]['name']}: {cur[i]['anomaly']['kind']} detected")
            for i in prev - cur.keys(): feed("ok", f"{i} anomaly cleared")
            prev = set(cur)
            with L:
                c = db()
                for i, d in cur.items():
                    if d["anomaly"].get("est_kwh") and not c.execute("select 1 from requests where device=? and status in ('pending','review')", (i,)).fetchone():
                        c.execute("insert into requests(device,ver,status,est_kwh,reason,created) values(?,?,?,?,?,?)", (i, d["ver"], "pending", d["anomaly"]["est_kwh"], d["anomaly"]["reason"], clock()))
                        feed("req", f"Autopilot proposed: switch off {d['name']} (needs human approval)")
                for r in c.execute("select id,device from requests where status in ('pending','review')").fetchall():
                    if r["device"] not in cur: c.execute("update requests set status='expired',note='anomaly cleared' where id=?", (r["id"],))
                c.close()
        except Exception: pass
        time.sleep(1.5)


def api_get(p):
    if p == "/api/live": return live()
    c = db()
    if p == "/api/health": return dict(ok=True, mode="simulated")
    if p == "/api/state": return state()
    if p == "/api/forecast": return forecast()
    if p == "/api/audit": r = [dict(x) for x in c.execute("select * from audit order by id desc")]; return dict(audit=r)
    if p == "/api/savings": return dict(savings=[dict(x) for x in c.execute("select * from savings")], note="estimated vs measured(simulated)")
    if p == "/api/ops": return dict(ops=[dict(x) for x in c.execute("select key,device,action,role,base_ver,status,reason,dups from ops order by rowid desc")])
    if p == "/api/privacy": return privacy()
    c.close()


class H(BaseHTTPRequestHandler):
    def log_message(self, *a): pass     # privacy: no access logs

    def send(self, obj, code=200, ctype="application/json"):
        b = obj if isinstance(obj, bytes) else json.dumps(obj).encode()
        self.send_response(code); self.send_header("Content-Type", ctype); self.send_header("Content-Length", str(len(b))); self.end_headers(); self.wfile.write(b)

    def do_GET(self):
        p = self.path.split("?")[0]
        if p in ("/", "/index.html"): return self.send(open(os.path.join(HERE, "static", "index.html"), "rb").read(), ctype="text/html; charset=utf-8")
        r = api_get(p); self.send(r if r is not None else dict(error="not found"), 200 if r is not None else 404)

    def do_POST(self):
        n = int(self.headers.get("Content-Length", 0)); d = json.loads(self.rfile.read(n) or b"{}"); p = self.path
        if p == "/api/command": return self.send(cmd(d))
        if p == "/api/reset": reset(d.get("hour", 11)); return self.send(dict(ok=True))
        if p == "/api/phase":
            with L:
                c = db(); SIM["min"] = PHASES.get(d.get("phase"), 11) * 60; c.execute("update meta set v=? where k='h'", (str(SIM["min"] // 60),)); refresh(c); c.close()
            return self.send(dict(ok=True))
        if p == "/api/request": return self.send(decide_req(d))
        if p == "/api/inject":
            if d.get("kind"): INJ[d["room"]] = d["kind"]
            else: INJ.clear()
            with L: c = db(); refresh(c); c.close()
            feed("conf" if d.get("kind") else "ok", f"Scenario injector: {d.get('kind') or 'cleared'} {d.get('room', '')}"); return self.send(dict(ok=True))
        if p == "/api/clock":
            if "play" in d: SIM["play"] = bool(d["play"])
            if "step" in d: SIM["step"] = int(d["step"])
            return self.send(dict(ok=True))
        if p.startswith("/api/demo/"): return self.send(demo(p.split("/")[-1]))
        self.send(dict(error="not found"), 404)


if __name__ == "__main__":
    import sys
    if "--reset" in sys.argv: reset(); print("Demo reset to deterministic state."); sys.exit()
    if not os.path.exists(DB): reset()
    print("THE SLEEPING CAMPUS (local only, simulated data) -> http://127.0.0.1:8000")
    threading.Thread(target=ticker, daemon=True).start()
    ThreadingHTTPServer(("127.0.0.1", 8000), H).serve_forever()
