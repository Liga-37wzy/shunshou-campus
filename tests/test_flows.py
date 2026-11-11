"""Behavioral tests: authorization, concurrent claims and complete user journeys."""
import concurrent.futures
from contextlib import closing
from pathlib import Path
import sqlite3
import tempfile
import unittest

from server import create_app, fee, match_task, now

HEADERS = {"X-Campus-Client": "web"}


class CampusTest(unittest.TestCase):
    def setUp(self):
        self.folder = Path(tempfile.mkdtemp(prefix="shunshou-test-"))
        self.database = self.folder / "test.sqlite"
        self.app = create_app({"TESTING": True, "DATABASE": str(self.database),
                               "SECRET_KEY": "isolated-test-key", "DEMO_MODE": True})
        self.client = self.login(1)

    def login(self, uid):
        client = self.app.test_client()
        self.assertEqual(client.post("/api/auth/demo", json={"user_id": uid}, headers=HEADERS).status_code, 200)
        return client

    def post(self, path, body=None, client=None, status=200):
        result = (client or self.client).post("/api" + path, json=body or {}, headers=HEADERS)
        self.assertEqual(result.status_code, status, result.get_data(as_text=True))
        return result.get_json()

    def update_sql(self, sql, args=()):
        with closing(sqlite3.connect(self.database)) as conn:
            with conn:
                conn.execute(sql, args)

    def test_complete_task_requires_two_different_participants(self):
        self.post("/tasks/1/complete", status=403)
        self.post("/tasks/1/accept")
        self.post("/tasks/1/accept", status=409)
        self.post("/tasks/1/complete", client=self.login(2), status=409)
        self.post("/tasks/1/deliver")
        self.post("/tasks/1/complete", client=self.login(2))
        task = next(t for t in self.client.get("/api/tasks").json["tasks"] if t["id"] == 1)
        self.assertEqual((task["status"],task["fee"],task["runner_income"]),("completed",50,450))
        self.assertEqual(len(self.client.get("/api/tasks/1/events").json["events"]),4)

    def test_same_task_can_only_be_claimed_once_concurrently(self):
        clients = [self.login(1), self.login(3)]
        with concurrent.futures.ThreadPoolExecutor(max_workers=2) as pool:
            statuses = list(pool.map(lambda c: c.post("/api/tasks/1/accept",json={},headers=HEADERS).status_code, clients))
        self.assertEqual(sorted(statuses),[200,409])

    def test_own_order_and_unrelated_updates_are_forbidden(self):
        self.post("/tasks/1/accept",client=self.login(2),status=403)
        self.post("/tasks/1/cancel",status=403)
        self.post("/tasks/1/accept")
        self.post("/tasks/1/deliver",client=self.login(3),status=403)
        self.assertEqual(self.login(3).get("/api/tasks/1/events").status_code,403)

    def test_expired_tasks_cannot_be_accepted(self):
        self.update_sql("UPDATE tasks SET deadline=? WHERE id=1",(now()-1,))
        self.post("/tasks/1/accept",status=409)

    def test_capacity_is_enforced(self):
        for tid in (1,2,3):
            self.post(f"/tasks/{tid}/accept")
        self.post("/tasks/4/accept",status=409)

    def test_task_validation(self):
        data={"title":"代拿","description":"一件物品","category":"快递代拿","pickup":"gate","destination":"dorm","reward":500,"minutes":120}
        self.post("/tasks",dict(data,reward=-5),status=400)
        self.post("/tasks",dict(data,destination="gate"),status=400)
        self.post("/tasks",dict(data,minutes=True),status=400)
        self.post("/tasks",data,status=201)

    def test_fee_rounding_and_cap(self):
        self.assertEqual(fee(505),51)
        self.assertEqual(fee(20000),200)

    def test_route_matching_filters_direction_and_time(self):
        t={"pickup":"gate","destination":"dorm","deadline":now()+3600}
        m=match_task(t,"gate","dorm",30,100)
        self.assertEqual(m["detour_m"],0)
        self.assertIsNone(match_task(t,"dorm","gate",30,100))
        self.assertIsNone(match_task(t,"gate","dorm",5,100))
        self.assertIsNone(match_task(dict(t,deadline=now()-1),"gate","dorm",30,100))

    def test_exchange_delivery_is_idempotent_and_both_confirm(self):
        e=self.post("/exchanges",{"offered":3,"wanted":1},status=201)["id"]
        recipient=self.login(2)
        self.post(f"/exchanges/{e}/accept",client=recipient)
        self.post(f"/exchanges/{e}/delivery")
        tid=self.post(f"/exchanges/{e}/delivery")["task_id"]
        self.post(f"/exchanges/{e}/confirm",status=409)
        # Round-trip jobs are not advertised as single-trip route recommendations.
        matches=self.login(3).get("/api/matches?origin=gate&destination=dorm&minutes=240&max_detour=3000").json["matches"]
        self.assertNotIn(tid,[t["id"] for t in matches])
        runner=self.login(3)
        self.post(f"/tasks/{tid}/accept",client=runner)
        self.post(f"/tasks/{tid}/deliver",client=runner)
        self.post(f"/tasks/{tid}/complete")
        self.post(f"/exchanges/{e}/confirm")
        self.assertEqual(self.client.get("/api/exchanges").json["exchanges"][0]["status"],"accepted")
        self.post(f"/exchanges/{e}/confirm",client=recipient)
        self.assertEqual(self.client.get("/api/exchanges").json["exchanges"][0]["status"],"completed")

    def test_exchange_reserves_both_items_and_closes_competing_offer(self):
        first=self.post("/exchanges",{"offered":3,"wanted":1},status=201)["id"]
        second=self.post("/exchanges",{"offered":4,"wanted":1},status=201)["id"]
        self.post(f"/exchanges/{first}/accept",client=self.login(2))
        states={e["id"]:e["status"] for e in self.client.get("/api/exchanges").json["exchanges"]}
        self.assertEqual(states[second],"rejected")
        self.post("/exchanges",{"offered":4,"wanted":1},status=409)

    def test_exchange_cannot_offer_someone_elses_item(self):
        self.post("/exchanges",{"offered":2,"wanted":1},status=403)

    def test_claim_evidence_is_private_and_return_requires_authority(self):
        self.post("/found/1/claim",{"evidence":"伞柄有一枚绿色星星贴纸"},status=201)
        self.assertEqual(self.login(3).get("/api/found").json["claims"],[])
        claims=self.login(2).get("/api/found").json["claims"]
        cid=claims[0]["id"]
        self.post(f"/claims/{cid}/return",status=403)
        self.post(f"/claims/{cid}/return",client=self.login(2))
        self.assertEqual(self.client.get("/api/found").json["claims"][0]["status"],"returned")

    def test_station_custody_and_pickup_code(self):
        p=self.post("/station/parcels",{"label":"一件书袋"},status=201)
        pid=p["id"]
        self.post(f"/station/parcels/{pid}/store",status=403)
        staff=self.login(4)
        self.post(f"/station/parcels/{pid}/store",client=staff)
        self.assertEqual(self.login(2).get("/api/station").json["parcels"],[])
        self.assertNotIn("code_hash",staff.get("/api/station").json["parcels"][0])
        self.post(f"/station/parcels/{pid}/collect",{"code":"wrong"},client=staff,status=400)
        self.post(f"/station/parcels/{pid}/collect",{"code":p["pickup_code"]},client=staff)
        self.post(f"/station/parcels/{pid}/collect",{"code":p["pickup_code"]},client=staff,status=409)

    def test_pickup_code_failure_count_persists(self):
        p=self.post("/station/parcels",{"label":"包裹"},status=201);staff=self.login(4)
        self.post(f"/station/parcels/{p['id']}/store",client=staff)
        for _ in range(5):
            self.post(f"/station/parcels/{p['id']}/collect",{"code":"bad"},client=staff,status=400)
        self.post(f"/station/parcels/{p['id']}/collect",{"code":p["pickup_code"]},client=staff,status=409)

    def test_csrf_origin_and_anonymous_requests(self):
        self.assertEqual(self.app.test_client().get("/api/tasks").status_code,401)
        self.assertEqual(self.client.post("/api/tasks/1/accept",json={}).status_code,403)
        self.assertEqual(self.client.post("/api/tasks/1/accept",json={},headers={**HEADERS,"Origin":"https://other.example"}).status_code,403)

    def test_registration_cannot_choose_staff_role(self):
        client=self.app.test_client()
        self.post("/auth/register",{"username":"tester","password":"a-long-test-password","name":"测试同学","role":"station"},client=client,status=201)
        self.assertEqual(client.get("/api/bootstrap").json["me"]["role"],"student")
        self.post("/auth/logout",client=client)
        self.post("/auth/login",{"username":"tester","password":"wrong"},client=client,status=401)
        self.post("/auth/login",{"username":"tester","password":"a-long-test-password"},client=client)

    def test_demo_disabled_by_default(self):
        app=create_app({"TESTING":True,"DATABASE":str(self.folder/"empty.sqlite"),"SECRET_KEY":"another-test-key"})
        c=app.test_client()
        self.assertFalse(c.get("/api/bootstrap").json["demo"])
        self.assertEqual(c.post("/api/auth/demo",json={"user_id":1},headers=HEADERS).status_code,404)

    def test_checkin_expiry_and_persistence(self):
        self.post("/station/checkin")
        self.assertEqual(len(self.client.get("/api/station").json["checked_in"]),1)
        self.update_sql("UPDATE checkins SET expires=?",(now()-1,))
        self.assertEqual(self.client.get("/api/station").json["checked_in"],[])
        restarted=create_app({"TESTING":True,"DATABASE":str(self.database),"SECRET_KEY":"isolated-test-key","DEMO_MODE":True})
        c=restarted.test_client();c.post("/api/auth/demo",json={"user_id":1},headers=HEADERS)
        self.assertEqual(len(c.get("/api/tasks").json["tasks"]),4)


if __name__ == "__main__":
    unittest.main()
