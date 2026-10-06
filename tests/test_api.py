"""REST API 端到端测试（真实 HTTP 服务 + 并发线程提交）。"""

from __future__ import annotations

import json
import tempfile
import threading
import unittest
import urllib.error
import urllib.request
from datetime import date
from http.server import ThreadingHTTPServer
from pathlib import Path

from program_scheduling.api import create_server

D = date.fromisoformat


def seed_payload():
    def p(code, name, network, party):
        return {"code": code, "name": name, "network": network, "owner_unit": name,
                "responsible_party": party, "start_date": "2026-10-01"}

    def m(code, project, duration):
        return {"code": code, "project_code": project, "name": code, "duration_days": duration}

    def ch(entity, data, op="UPSERT"):
        return {"op": op, "entity": entity, "data": data}

    return [
        ch("project", p("P-POWER", "变电站扩容", "电网", "电力责任人")),
        ch("project", p("P-COMP", "算力通道", "算力网", "算力责任人")),
        ch("milestone", m("SUB", "P-POWER", 40)),
        ch("milestone", m("COMP", "P-COMP", 10)),
        ch("dependency", {"code": "D1", "upstream": "SUB", "downstream": "COMP", "kind": "HARD"}),
        ch("resource", {"code": "ROAD", "name": "道路窗口", "capacity_per_day": 1.0}),
        ch("booking", {"code": "BK1", "resource_code": "ROAD", "milestone_code": "SUB",
                       "project_code": "P-POWER", "duration_days": 40}),
        ch("commitment", {"code": "C1", "project_code": "P-COMP", "title": "投用",
                          "promised_date": "2026-11-30", "weight": 5}),
    ]


class ApiTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.server: ThreadingHTTPServer = create_server(
            "127.0.0.1", 0, str(Path(self.tmp.name) / "data")
        )
        self.port = self.server.server_address[1]
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()

    def tearDown(self) -> None:
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=5)
        self.tmp.cleanup()

    def _request(self, method: str, path: str, body=None):
        data = json.dumps(body).encode("utf-8") if body is not None else None
        request = urllib.request.Request(
            f"http://127.0.0.1:{self.port}{path}",
            data=data,
            method=method,
            headers={"Content-Type": "application/json"},
        )
        try:
            with urllib.request.urlopen(request, timeout=10) as response:
                return response.status, json.loads(response.read().decode("utf-8"))
        except urllib.error.HTTPError as exc:
            return exc.code, json.loads(exc.read().decode("utf-8"))

    def _seed(self):
        status, _ = self._request("POST", "/api/baseline/initial", {
            "approver": "省级专班", "changes": seed_payload(),
        })
        self.assertEqual(status, 201)

    def test_full_proposal_lifecycle_over_http(self) -> None:
        self._seed()
        status, body = self._request("GET", "/api/health")
        self.assertEqual(body["revision"], 1)

        # 提交里程碑延误更新
        status, body = self._request("POST", "/api/proposals", {
            "proposal_id": "PR-1",
            "submitted_by": "电力公司",
            "rationale": "设备到货延迟",
            "changes": [{
                "op": "UPSERT", "entity": "milestone",
                "data": {"code": "SUB", "project_code": "P-POWER",
                         "name": "变电站投运", "duration_days": 50, "state": "IN_PROGRESS"},
            }],
        })
        self.assertEqual(status, 201)
        self.assertEqual(body["proposal"]["required_parties"], ["电力责任人"])
        self.assertEqual(body["proposal"]["status"], "OPEN")

        # 未确认不能批准
        status, body = self._request("POST", "/api/proposals/PR-1/approve", {})
        self.assertEqual(status, 400)

        # 责任方确认
        status, _ = self._request("POST", "/api/proposals/PR-1/confirmations", {
            "party": "电力责任人", "actor_id": "leader-1",
        })
        self.assertEqual(status, 200)

        # 批准并入基线
        status, body = self._request("POST", "/api/proposals/PR-1/approve", {
            "approver": "省级专班", "comment": "同意",
        })
        self.assertEqual(status, 200)
        self.assertEqual(body["revision"]["revision"], 2)

    def test_schedule_hidden_delays_and_audit(self) -> None:
        self._seed()
        status, body = self._request("GET", "/api/schedule?as_of=2026-10-01")
        self.assertEqual(status, 200)
        self.assertIn("milestones", body)
        self.assertTrue(body["program_chain"])

        status, body = self._request("GET", "/api/analysis/hidden-delays?as_of=2026-10-01")
        self.assertEqual(status, 200)
        self.assertTrue(body["hidden_delays"])
        self.assertEqual(body["hidden_delays"][0]["project_code"], "P-COMP")

        status, body = self._request("POST", "/api/monthly", {
            "month": "2026-10", "as_of": "2026-10-31",
        })
        self.assertEqual(status, 201)
        status, body = self._request("GET", "/api/monthly/2026-10")
        self.assertEqual(body["publication"]["baseline_revision"], 1)
        status, _ = self._request("POST", "/api/monthly", {
            "month": "2026-10", "as_of": "2026-10-31",
        })
        self.assertEqual(status, 400)

        status, body = self._request("GET", "/api/audit/trail")
        self.assertEqual(body["trail"][0]["proposal_id"], "INITIAL")

    def test_concurrent_duplicate_proposals_first_wins(self) -> None:
        self._seed()
        barrier = threading.Barrier(2)
        results: list[tuple[int, dict]] = []

        def submit_then_confirm_and_approve(proposal_id: str, duration: int) -> None:
            self._request("POST", "/api/proposals", {
                "proposal_id": proposal_id,
                "changes": [{
                    "op": "UPSERT", "entity": "milestone",
                    "data": {"code": "SUB", "project_code": "P-POWER",
                             "name": "变电站投运", "duration_days": duration},
                }],
            })
            barrier.wait(timeout=10)
            self._request("POST", f"/api/proposals/{proposal_id}/confirmations", {
                "party": "电力责任人",
            })
            results.append(
                self._request("POST", f"/api/proposals/{proposal_id}/approve", {})
            )

        t1 = threading.Thread(target=submit_then_confirm_and_approve, args=("PA", 45))
        t2 = threading.Thread(target=submit_then_confirm_and_approve, args=("PB", 48))
        t1.start(); t2.start()
        t1.join(); t2.join()
        statuses = sorted(status for status, _ in results)
        self.assertEqual(statuses, [200, 409])  # 一个成功，一个被标记 SUPERSEDED

        status, body = self._request("GET", "/api/baseline")
        self.assertEqual(body["revision"], 2)

    def test_scenario_comparison_endpoint(self) -> None:
        self._seed()
        status, body = self._request("POST", "/api/analysis/scenarios", {
            "as_of": "2026-10-01",
            "scenarios": [
                {"scenario_id": "S0", "name": "维持现状"},
                {"scenario_id": "S1", "name": "压缩工期",
                 "crash_durations": {"SUB": 25}},
            ],
        })
        self.assertEqual(status, 200)
        ranked = body["ranked"]
        self.assertEqual(ranked[0]["scenario_id"], "S1")


if __name__ == "__main__":
    unittest.main()
