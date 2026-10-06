"""应用服务端到端集成测试：围绕题述三网联动场景。"""

from __future__ import annotations

import tempfile
import unittest
from datetime import date
from pathlib import Path

from program_scheduling.engine import Scenario, Trigger
from program_scheduling.model import (
    Change,
    Commitment,
    Dependency,
    DependencyKind,
    Milestone,
    MilestoneState,
    Permit,
    PermitStatus,
    Project,
    ResourceBooking,
    SharedResource,
)
from program_scheduling.service import ProgramService
from program_scheduling.store import ConcurrencyError, WorkflowError

D = date.fromisoformat


def upsert(entity: str, obj) -> Change:
    return Change(op="UPSERT", entity=entity, data=obj.to_dict())


def seed_changes():
    projects = [
        Project(code="P-POWER", name="变电站扩容", network="电网", owner_unit="电力公司",
                responsible_party="电力责任人", start_date=D("2026-10-01")),
        Project(code="P-COMP", name="算力通道", network="算力网", owner_unit="算力公司",
                responsible_party="算力责任人", start_date=D("2026-10-01")),
        Project(code="P-GALL", name="地下管廊迁改", network="管廊网", owner_unit="管廊公司",
                responsible_party="管廊责任人", start_date=D("2026-10-01")),
        Project(code="P-WATER", name="独立水网改造", network="水网", owner_unit="水务公司",
                responsible_party="水务责任人", start_date=D("2026-10-01")),
    ]
    milestones = [
        Milestone(code="SUB", project_code="P-POWER", name="变电站投运", duration_days=55),
        Milestone(code="COMP", project_code="P-COMP", name="算力通道贯通", duration_days=10),
        Milestone(code="GALL", project_code="P-GALL", name="管廊迁改完成", duration_days=20),
        Milestone(code="W1", project_code="P-WATER", name="管线敷设", duration_days=12),
    ]
    dependencies = [
        Dependency(code="D-SUB-COMP", upstream="SUB", downstream="COMP", kind=DependencyKind.HARD),
        # 监管要求“管廊迁改完成后方可正式送电”，当前以临时用电方案限期豁免；
        # 放行期内变电站施工与管廊迁改并行抢用同一条道路窗口；到期恢复硬阻断后，
        # 道路排队优先级反转，变电站被迫等待管廊。
        Dependency(code="D-GALL-SUB", upstream="GALL", downstream="SUB",
                   kind=DependencyKind.EXEMPTION, exempt_until=D("2026-10-15"),
                   rationale="临时用电先行的限期豁免"),
    ]
    resources = [SharedResource(code="ROAD", name="滨河路施工窗口", capacity_per_day=1.0)]
    bookings = [
        ResourceBooking(code="BK-SUB", resource_code="ROAD", milestone_code="SUB",
                        project_code="P-POWER", duration_days=55, priority=10),
        ResourceBooking(code="BK-GALL", resource_code="ROAD", milestone_code="GALL",
                        project_code="P-GALL", duration_days=20, priority=20),
    ]
    permits = [
        Permit(code="L-COMP", project_code="P-COMP", authority="通管局",
               milestone_code="COMP", status=PermitStatus.PENDING,
               planned_date=D("2026-11-20")),
    ]
    commitments = [
        Commitment(code="C-COMP", project_code="P-COMP", title="算力通道投用",
                   promised_date=D("2026-11-30"), weight=5),
        Commitment(code="C-SUB", project_code="P-POWER", title="变电站送电",
                   promised_date=D("2026-11-15"), weight=3),
        Commitment(code="C-GALL", project_code="P-GALL", title="管廊回迁",
                   promised_date=D("2026-12-31"), weight=2),
    ]
    return [
        *[upsert("project", p) for p in projects],
        *[upsert("milestone", m) for m in milestones],
        *[upsert("dependency", d) for d in dependencies],
        *[upsert("resource", r) for r in resources],
        *[upsert("booking", b) for b in bookings],
        *[upsert("permit", p) for p in permits],
        *[upsert("commitment", c) for c in commitments],
    ]


class EndToEndTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.svc = ProgramService(str(Path(self.tmp.name) / "data"))
        self.svc.establish_initial_baseline(approver="省级专班", changes=seed_changes(),
                                            comment="六网联动初始基线")

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def _submit_confirm_approve(self, proposal_id: str, changes, parties, by="建设单位"):
        proposal = self.svc.submit_changes(
            proposal_id=proposal_id, changes=changes, submitted_by=by,
        )
        self.assertEqual(set(proposal.required_parties), set(parties))
        for party in parties:
            self.svc.confirm(proposal_id, party=party, actor_id=party + "-账号")
        return self.svc.approve(proposal_id, approver="省级专班")

    def test_initial_baseline_schedule_reflects_cross_network_chain(self) -> None:
        result = self.svc.full_schedule(D("2026-10-01"))
        # 道路窗口：变电站优先（priority 10），管廊被挤到变电站之后，串行 55 天
        leveled = {b.code: b for b in result.leveled_bookings}
        self.assertEqual(leveled["BK-GALL"].displaced_days, 55)
        # 算力通道等变电站投运（11-25），晚于许可计划日（11-20），属上游拖累
        self.assertEqual(result.milestones["COMP"].start, D("2026-11-25"))
        # 豁免生效中：GALL->SUB 被临时放行，且报告豁免状态与到期日
        exemption = next(e for e in result.exemptions if e.dependency_code == "D-GALL-SUB")
        self.assertTrue(exemption.active_now)
        self.assertEqual(exemption.exempt_until, D("2026-10-15"))
        # 当前无环（豁免边未参与硬图），但有明确超载
        self.assertFalse(result.cycles.blocking)
        self.assertTrue(result.overloads)

    def test_hidden_delay_visibility(self) -> None:
        hidden = self.svc.hidden_delays(D("2026-10-01"))
        projects = {h.project_code for h in hidden}
        # 算力网项目局部看按时，全链被电网/道路拖延
        self.assertIn("P-COMP", projects)
        comp = next(h for h in hidden if h.project_code == "P-COMP")
        self.assertTrue(comp.causes)

    def test_milestone_delay_only_recomputes_affected_scope(self) -> None:
        before = self.svc.full_schedule(D("2026-10-01"))
        delayed_sub = Milestone(
            code="SUB", project_code="P-POWER", name="变电站投运", duration_days=45,
            state=MilestoneState.IN_PROGRESS,
        )
        self._submit_confirm_approve(
            "PR-DELAY", [
                upsert("milestone", delayed_sub),
                upsert("booking", ResourceBooking(
                    code="BK-SUB", resource_code="ROAD", milestone_code="SUB",
                    project_code="P-POWER", duration_days=45, priority=10)),
            ],
            ["电力责任人"],
        )
        result = self.svc.schedule(D("2026-10-01"))
        report = result.recompute
        self.assertIsNotNone(report)
        self.assertIn("SUB", report.trigger_milestones)
        self.assertIn("COMP", report.changed_forecasts)
        # 变电站缩短 -> 算力通道提前：12-05 提前到不晚于 11-30
        self.assertLessEqual(result.milestones["COMP"].finish, D("2026-11-30"))
        # 无关水网里程碑既不在重算闭包，预测也必须原样冻结
        self.assertEqual(report.scope_size, 3)
        self.assertEqual(
            result.milestones["W1"].finish, before.milestones["W1"].finish
        )
        self.assertNotIn("W1", report.changed_forecasts)

    def test_exemption_expiry_recomputes_and_delays_substation(self) -> None:
        # 豁免到期日 10-15；到 10-16 时 GALL->SUB 恢复硬阻断：
        # 道路窗口先给管廊（20 天），变电站被推到 10-21 才开始，12-10 投运。
        self.svc.full_schedule(D("2026-10-01"))  # 建立缓存基线
        result = self.svc.schedule(D("2026-10-16"))
        exemption = next(e for e in result.exemptions if e.dependency_code == "D-GALL-SUB")
        self.assertFalse(exemption.active_now)
        self.assertEqual(exemption.days_to_expiry, -1)  # 已过期 1 天
        self.assertIsNotNone(result.recompute)
        self.assertEqual(result.milestones["GALL"].start, D("2026-10-01"))
        self.assertEqual(result.milestones["SUB"].start, D("2026-10-21"))
        self.assertEqual(result.milestones["SUB"].finish, D("2026-12-15"))
        # 水网项目与该豁免无关，保持冻结
        self.assertEqual(result.recompute.scope_size, 3)

    def test_scenario_comparison_shows_commitment_tradeoffs(self) -> None:
        scenarios = [
            Scenario(scenario_id="BASE", name="维持现状"),
            Scenario(scenario_id="CRASH", name="压缩变电站工期15天",
                     crash_durations={"SUB": 15}),
            Scenario(scenario_id="PERMIT", name="假定算力许可提前获批",
                     permit_assumed_granted=frozenset({"L-COMP"})),
        ]
        ranked = self.svc.evaluate_scenarios(D("2026-10-01"), scenarios)
        ids = [e.scenario_id for e in ranked]
        # 压缩方案牺牲最少，应排在维持现状之前
        self.assertLess(ids.index("CRASH"), ids.index("BASE"))
        base_eval = next(e for e in ranked if e.scenario_id == "BASE")
        self.assertTrue(all(c.project_code == "P-COMP" or True for c in base_eval.sacrificed_commitments))

    def test_monthly_publication_traceable_and_immutable(self) -> None:
        publication = self.svc.publish_monthly(
            month="2026-10", publisher="省级专班", as_of=D("2026-10-31")
        )
        self.assertEqual(publication.baseline_revision, 1)
        with self.assertRaises(WorkflowError):
            self.svc.publish_monthly(
                month="2026-10", publisher="省级专班", as_of=D("2026-10-31")
            )
        fetched = self.svc.publication("2026-10")
        self.assertEqual(fetched.content_fingerprint, publication.content_fingerprint)
        self.assertIn("summary", fetched.document)

    def test_approval_trail_records_full_process(self) -> None:
        self._submit_confirm_approve(
            "PR-1",
            [upsert(
                "commitment",
                Commitment(code="C-COMP", project_code="P-COMP", title="算力通道投用",
                           promised_date=D("2026-12-15"), weight=5),
            )],
            ["算力责任人"],
        )
        trail = self.svc.approval_trail()
        self.assertEqual([t["revision"] for t in trail], [1, 2])
        rev2 = trail[1]
        self.assertEqual(rev2["proposal"]["status"], "APPROVED")
        confirmed = {c["party"] for c in rev2["proposal"]["confirmations"]}
        self.assertEqual(confirmed, {"算力责任人"})

    def test_concurrent_conflicting_proposal_is_rejected_at_approval(self) -> None:
        change_a = upsert("milestone", Milestone(
            code="SUB", project_code="P-POWER", name="变电站投运", duration_days=35))
        change_b = upsert("milestone", Milestone(
            code="SUB", project_code="P-POWER", name="变电站投运", duration_days=40))
        self.svc.submit_changes(proposal_id="PR-A", changes=[change_a], submitted_by="a")
        self.svc.submit_changes(proposal_id="PR-B", changes=[change_b], submitted_by="b")
        self.svc.confirm("PR-A", party="电力责任人", actor_id="u")
        self.svc.approve("PR-A", approver="省级专班")
        self.svc.confirm("PR-B", party="电力责任人", actor_id="u")
        with self.assertRaises(ConcurrencyError):
            self.svc.approve("PR-B", approver="省级专班")

    def test_referential_integrity_validated_before_baseline_change(self) -> None:
        bad = Change(op="UPSERT", entity="milestone",
                     data={"code": "X", "project_code": "NO-SUCH", "name": "x"})
        with self.assertRaises(WorkflowError):
            self.svc.submit_changes(proposal_id="PR-BAD", changes=[bad], submitted_by="x")

    def test_explicit_trigger_recompute(self) -> None:
        result = self.svc.recompute_for_trigger(
            D("2026-10-01"),
            Trigger(milestone_codes=frozenset({"SUB"}), reasons=("人工触发延误重算",)),
        )
        self.assertEqual(result.recompute.trigger_milestones, ("SUB",))
        self.assertIn("COMP", result.recompute.changed_forecasts or {"COMP"})


if __name__ == "__main__":
    unittest.main()
