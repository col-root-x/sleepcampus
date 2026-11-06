import os, sys, tempfile, uuid, unittest, threading
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ["SC_DB"] = os.path.join(tempfile.mkdtemp(), "t.db")
import server as S

def K(): return str(uuid.uuid4())
def C(dev, act, role="facility_manager", v=1, val=None, key=None): return dict(key=key or K(), device=dev, action=act, role=role, version=v, value=val)

class T(unittest.TestCase):
    def setUp(self): S.reset(11)
    def test_monitoring_and_ghost(self):
        s = S.state(); a = {d["id"]: d for d in s["devices"]}
        self.assertEqual(len(s["devices"]), 24); self.assertEqual(a["A204"]["anomaly"]["level"], "confirmed")
        self.assertIn("baseline", a["A204"]["anomaly"]["reason"])
    def test_schedule_awareness(self):
        a = {d["id"]: d for d in S.state()["devices"]}
        self.assertIsNone(a["A101"]["anomaly"])                      # scheduled+occupied -> fine
        self.assertEqual(a["A301"]["anomaly"]["level"], "suspected")  # scheduled, low occ -> review not shutdown
        self.assertNotIn("est_kwh", a["A301"]["anomaly"])
        self.assertEqual(a["A205"]["status"], "class CANCELLED")
    def test_forecast(self):
        f = S.forecast(); self.assertEqual(len(f["points"]), 12); self.assertIn("FORECAST", f["kind"])
    def test_critical_protected(self):
        r = S.cmd(C("I1", "off", "energy_manager")); self.assertEqual(r["status"], "rejected"); self.assertIn("PROTECTED", r["reason"])
        self.assertEqual(S.dev_row("I1")["state"], "on")
    def test_observer_cannot_act(self):
        self.assertEqual(S.cmd(C("A204", "off", "observer"))["status"], "rejected")
    def test_setpoint_limit(self):
        self.assertEqual(S.cmd(C("A204", "setpoint", val=10))["status"], "rejected")
    def test_before_after_savings(self):
        r = S.cmd(C("A204", "off")); self.assertEqual(r["status"], "accepted")
        st = S.state()["totals"]; self.assertEqual(st["interventions"], 1); self.assertGreater(st["est_kwh"], 0)
        self.assertNotEqual(st["est_kwh"], st["measured_kwh_simulated"])  # estimated vs measured are separate
    def test_idempotent_key(self):
        k = K(); r1 = S.cmd(C("A204", "off", key=k)); r2 = S.cmd(C("A204", "off", key=k))
        self.assertTrue(r2["idempotent_replay"]); self.assertEqual(r1["new_version"], r2["new_version"])
    def test_ten_duplicates(self):
        d = S.demo("duplicate"); self.assertEqual(d["successful_operations"], 1); self.assertEqual(d["idempotent_duplicates"], 9)
        self.assertEqual(d["device_state_transitions"], 1); self.assertEqual(d["savings_events"], 1)
        self.assertEqual(d["operation_records"], 1); self.assertEqual(d["audit_entries"], 1)
    def test_conflict(self):
        for _ in range(5):
            d = S.demo("conflict"); self.assertEqual(sum(r["status"] == "accepted" for r in d["results"]), 1)
            self.assertEqual(sum(r["status"] == "conflict" for r in d["results"]), 1); self.assertEqual(d["final_state"]["version"], 2)
    def test_stale(self):
        d = S.demo("stale"); self.assertEqual(d["step2_stale_request_with_v1"]["status"], "conflict"); self.assertEqual(d["final_version"], 2)
    def test_unsafe_demo(self): self.assertEqual(S.demo("unsafe")["result"]["status"], "rejected")
    def test_audit_has_rejections(self):
        S.cmd(C("I1", "off")); S.cmd(C("A204", "off", v=1)); S.cmd(C("A204", "off", "lab_admin", v=1))
        o = [x["outcome"] for x in S.api_get("/api/audit")["audit"]]; self.assertIn("REJECTED", o); self.assertIn("ACCEPTED", o); self.assertIn("CONFLICT", o)
    def test_privacy(self):
        S.cmd(C("A204", "off")); p = S.privacy(); self.assertTrue(p["all_passed"], p)
    def test_no_pii_in_state(self):
        import json; self.assertNotRegex(json.dumps(S.state()), r"[\w.]+@[\w.]+\.\w+|(?:[0-9A-F]{2}:){5}[0-9A-F]{2}")
if __name__ == "__main__": unittest.main(verbosity=2)
