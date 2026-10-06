"""关键链调度引擎测试。"""

from __future__ import annotations

import unittest
from datetime import date

from program_scheduling.engine import (
    Scenario,
    Snapshot,
    Trigger,
    compute_schedule,
    derive_triggers,
    hidden_delay_analysis,
    recompute_incremental,
    compare_scenarios,
)
from program_scheduling.model import (
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

D = date.fromisoformat
AS_OF = D("2026-10-01")


def make_project(code: str, party: str | None = None, start: str = "2026-10-01") -> Project:
    return Project(
        code=code, name=code, network="电网", owner_unit=code + "-单位",
        responsible_party=party or code + "-责任方", start_date=D(start),
    )


def make_milestone(code: str, project: str, duration: int, **kw) -> Milestone:
    return Milestone(code=code, project_code=project, name=code, duration_days=duration, **kw)


def snapshot(projects, milestones, dependencies=(), resources=(), bookings=(),
             permits=(), commitments=()) -> Snapshot:
    return Snapshot(
        revision=1,
        entities_fingerprint="test",
        projects={p.code: p for p in projects},
        milestones={m.code: m for m in milestones},
        dependencies={d.code: d for d in dependencies},
        resources={r.code: r for r in resources},
        bookings={b.code: b for b in bookings},
        permits={p.code: p for p in permits},
        commitments={c.code: c for c in commitments},
    )


class CriticalChainTests(unittest.TestCase):
    def test_hard_dependency_forward_and_critical_chain(self) -> None:
        snap = snapshot(
            [make_project("P1")],
            [make_milestone("A", "P1", 10), make_milestone("B", "P1", 5)],
            [Dependency(code="D1", upstream="A", downstream="B", kind=DependencyKind.HARD)],
        )
        result = compute_schedule(snap, AS_OF)
        self.assertEqual(result.milestones["A"].finish, D("2026-10-11"))
        self.assertEqual(result.milestones["B"].start, D("2026-10-11"))
        self.assertEqual(result.milestones["B"].finish, D("2026-10-16"))
        self.assertTrue(result.milestones["A"].critical)
        self.assertTrue(result.milestones["B"].critical)
        self.assertEqual(result.critical_chains["P1"], ["A", "B"])

    def test_slack_on_non_critical_branch(self) -> None:
        # A(10)->B(2) 与 A->C(10)；B 有正时差
        snap = snapshot(
            [make_project("P1")],
            [make_milestone("A", "P1", 10), make_milestone("B", "P1", 2),
             make_milestone("C", "P1", 10)],
            [
                Dependency(code="D1", upstream="A", downstream="B", kind=DependencyKind.HARD),
                Dependency(code="D2", upstream="A", downstream="C", kind=DependencyKind.HARD),
            ],
            commitments=[Commitment(code="K", project_code="P1", title="交付",
                                    promised_date=D("2026-10-21"))],
        )
        result = compute_schedule(snap, AS_OF)
        self.assertGreater(result.milestones["B"].slack_days, 0)
        self.assertFalse(result.milestones["B"].critical)
        self.assertEqual(result.milestones["C"].slack_days, 0)

    def test_lag_days_shift_successor(self) -> None:
        snap = snapshot(
            [make_project("P1")],
            [make_milestone("A", "P1", 10), make_milestone("B", "P1", 1)],
            [Dependency(code="D1", upstream="A", downstream="B",
                        kind=DependencyKind.HARD, lag_days=7)],
        )
        result = compute_schedule(snap, AS_OF)
        self.assertEqual(result.milestones["B"].start, D("2026-10-18"))


class CycleTests(unittest.TestCase):
    def test_hard_cycle_is_detected_and_blocks(self) -> None:
        snap = snapshot(
            [make_project("P1")],
            [make_milestone("A", "P1", 5), make_milestone("B", "P1", 5),
             make_milestone("C", "P1", 5)],
            [
                Dependency(code="D1", upstream="A", downstream="B", kind=DependencyKind.HARD),
                Dependency(code="D2", upstream="B", downstream="A", kind=DependencyKind.HARD),
                Dependency(code="D3", upstream="B", downstream="C", kind=DependencyKind.HARD),
            ],
        )
        result = compute_schedule(snap, AS_OF)
        self.assertEqual([tuple(sorted(c)) for c in result.cycles.blocking], [("A", "B")])
        self.assertIsNone(result.milestones["A"].finish)
        self.assertIsNone(result.milestones["C"].finish)
        self.assertTrue(result.milestones["A"].blocked_reasons)

    def test_exemption_active_latent_cycle_then_expires_to_blocking(self) -> None:
        deps = [
            Dependency(code="D1", upstream="A", downstream="B", kind=DependencyKind.HARD),
            Dependency(code="D2", upstream="B", downstream="A",
                       kind=DependencyKind.EXEMPTION, exempt_until=D("2026-12-31")),
        ]
        snap = snapshot(
            [make_project("P1")],
            [make_milestone("A", "P1", 5), make_milestone("B", "P1", 5)],
            deps,
        )
        waived = compute_schedule(snap, D("2026-10-01"))
        self.assertEqual(waived.cycles.blocking, ())
        self.assertEqual([tuple(sorted(c)) for c in waived.cycles.latent], [("A", "B")])
        self.assertTrue(waived.exemptions[0].active_now)
        expired = compute_schedule(snap, D("2027-01-01"))
        self.assertEqual([tuple(sorted(c)) for c in expired.cycles.blocking], [("A", "B")])
        self.assertFalse(expired.exemptions[0].active_now)
        self.assertIsNone(expired.milestones["A"].finish)

    def test_alternative_cycle_is_weak_only(self) -> None:
        snap = snapshot(
            [make_project("P1")],
            [make_milestone("A", "P1", 5), make_milestone("B", "P1", 5)],
            [
                Dependency(code="D1", upstream="A", downstream="B", kind=DependencyKind.HARD),
                Dependency(code="D2", upstream="B", downstream="A",
                           kind=DependencyKind.ALTERNATIVE, alternative_group="G1"),
            ],
        )
        result = compute_schedule(snap, AS_OF)
        self.assertEqual(result.cycles.blocking, ())
        self.assertEqual([tuple(sorted(c)) for c in result.cycles.alternative], [("A", "B")])


class AlternativeTests(unittest.TestCase):
    def test_or_group_uses_earliest_ready_predecessor(self) -> None:
        snap = snapshot(
            [make_project("P1")],
            [make_milestone("A", "P1", 2), make_milestone("B", "P1", 20),
             make_milestone("C", "P1", 1)],
            [
                Dependency(code="D1", upstream="A", downstream="C",
                           kind=DependencyKind.ALTERNATIVE, alternative_group="G"),
                Dependency(code="D2", upstream="B", downstream="C",
                           kind=DependencyKind.ALTERNATIVE, alternative_group="G"),
            ],
        )
        result = compute_schedule(snap, AS_OF)
        # C 选择 10-03 就绪的 A，而不是 10-21 的 B
        self.assertEqual(result.milestones["C"].start, D("2026-10-03"))
        self.assertEqual(result.milestones["C"].chosen_alternative, "D1")

    def test_node_blocked_when_entire_or_group_is_blocked(self) -> None:
        # A<->B 构成硬循环，C 仅以 A、B 为可替代前置；两条路都断，C 也阻断
        snap = snapshot(
            [make_project("P1")],
            [make_milestone("A", "P1", 2), make_milestone("B", "P1", 2),
             make_milestone("C", "P1", 1)],
            [
                Dependency(code="D1", upstream="A", downstream="B", kind=DependencyKind.HARD),
                Dependency(code="D2", upstream="B", downstream="A", kind=DependencyKind.HARD),
                Dependency(code="D3", upstream="A", downstream="C",
                           kind=DependencyKind.ALTERNATIVE, alternative_group="G"),
                Dependency(code="D4", upstream="B", downstream="C",
                           kind=DependencyKind.ALTERNATIVE, alternative_group="G"),
            ],
        )
        result = compute_schedule(snap, AS_OF)
        self.assertIsNone(result.milestones["C"].finish)
        self.assertTrue(result.milestones["C"].blocked_reasons)


class PermitAndProgressTests(unittest.TestCase):
    def test_pending_permit_gates_milestone(self) -> None:
        snap = snapshot(
            [make_project("P1")],
            [make_milestone("A", "P1", 5)],
            permits=[
                Permit(code="L1", project_code="P1", authority="监管局",
                       milestone_code="A", status=PermitStatus.PENDING,
                       planned_date=D("2026-10-10")),
            ],
        )
        result = compute_schedule(snap, AS_OF)
        self.assertEqual(result.milestones["A"].start, D("2026-10-10"))
        self.assertTrue(any("许可超期" in w for w in result.warnings) is False)

    def test_rejected_permit_blocks(self) -> None:
        snap = snapshot(
            [make_project("P1")],
            [make_milestone("A", "P1", 5)],
            permits=[
                Permit(code="L1", project_code="P1", authority="监管局",
                       status=PermitStatus.REJECTED),
            ],
        )
        result = compute_schedule(snap, AS_OF)
        self.assertIsNone(result.milestones["A"].finish)

    def test_completed_milestone_uses_actual_date(self) -> None:
        snap = snapshot(
            [make_project("P1")],
            [
                make_milestone("A", "P1", 5,
                               state=MilestoneState.COMPLETED, actual_finish=D("2026-10-02")),
                make_milestone("B", "P1", 1),
            ],
            [Dependency(code="D1", upstream="A", downstream="B", kind=DependencyKind.HARD)],
        )
        result = compute_schedule(snap, AS_OF)
        self.assertEqual(result.milestones["B"].start, D("2026-10-02"))

    def test_overdue_milestone_is_reforecast_from_as_of(self) -> None:
        snap = snapshot(
            [make_project("P1", start="2026-01-01")],
            [make_milestone("A", "P1", 10)],
        )
        result = compute_schedule(snap, D("2026-11-01"))
        self.assertEqual(result.milestones["A"].start, D("2026-11-01"))
        self.assertEqual(result.milestones["A"].driven_by, "OVERDUE")


class ResourceTests(unittest.TestCase):
    def _road_snapshot(self):
        return snapshot(
            [make_project("P1"), make_project("P2")],
            [make_milestone("A", "P1", 10), make_milestone("B", "P2", 10)],
            resources=[SharedResource(code="R", name="道路窗口")],
            bookings=[
                ResourceBooking(code="BK1", resource_code="R", milestone_code="A",
                                project_code="P1", duration_days=10, priority=10),
                ResourceBooking(code="BK2", resource_code="R", milestone_code="B",
                                project_code="P2", duration_days=10, priority=20),
            ],
        )

    def test_overload_detected_and_serialized_by_priority(self) -> None:
        result = compute_schedule(self._road_snapshot(), AS_OF)
        self.assertTrue(result.overloads)
        leveled = {b.code: b for b in result.leveled_bookings}
        self.assertEqual(leveled["BK1"].displaced_days, 0)
        self.assertEqual(leveled["BK2"].displaced_days, 10)
        self.assertEqual(result.milestones["B"].start, D("2026-10-11"))

    def test_capacity_two_allows_parallel(self) -> None:
        snap = snapshot(
            [make_project("P1"), make_project("P2")],
            [make_milestone("A", "P1", 10), make_milestone("B", "P2", 10)],
            resources=[SharedResource(code="R", name="通道", capacity_per_day=2)],
            bookings=[
                ResourceBooking(code="BK1", resource_code="R", milestone_code="A",
                                project_code="P1", duration_days=10),
                ResourceBooking(code="BK2", resource_code="R", milestone_code="B",
                                project_code="P2", duration_days=10),
            ],
        )
        result = compute_schedule(snap, AS_OF)
        self.assertEqual(result.overloads, [])
        self.assertEqual(sum(b.displaced_days for b in result.leveled_bookings), 0)


class CommitmentTests(unittest.TestCase):
    def test_commitment_met_and_breached(self) -> None:
        snap = snapshot(
            [make_project("P1")],
            [make_milestone("A", "P1", 10)],
            commitments=[
                Commitment(code="K1", project_code="P1", title="宽松承诺",
                           promised_date=D("2026-12-01"), weight=1),
                Commitment(code="K2", project_code="P1", title="紧张承诺",
                           promised_date=D("2026-10-05"), weight=5),
            ],
        )
        result = compute_schedule(snap, AS_OF)
        statuses = {c.code: c for c in result.commitments}
        self.assertEqual(statuses["K1"].status, "MET")
        self.assertEqual(statuses["K2"].status, "BREACHED")
        self.assertEqual(statuses["K2"].delay_days, 6)


class IncrementalTests(unittest.TestCase):
    def _build(self):
        projects = [make_project("P-POWER"), make_project("P-COMP"), make_project("P-GALL")]
        milestones = [
            make_milestone("SUB", "P-POWER", 30),
            make_milestone("COMP", "P-COMP", 10),
            make_milestone("GALL", "P-GALL", 20),
        ]
        return projects, milestones

    def test_only_downstream_scope_recomputed(self) -> None:
        projects, milestones = self._build()
        old = snapshot(
            projects, milestones,
            [Dependency(code="D1", upstream="SUB", downstream="COMP", kind=DependencyKind.HARD)],
        )
        previous = compute_schedule(old, AS_OF)
        new = snapshot(
            projects,
            [make_milestone("SUB", "P-POWER", 40)] + milestones[1:],
            [Dependency(code="D1", upstream="SUB", downstream="COMP", kind=DependencyKind.HARD)],
        )
        trigger = derive_triggers(old, new)
        self.assertIn("SUB", trigger.milestone_codes)
        result = recompute_incremental(new, AS_OF, previous, trigger)
        report = result.recompute
        self.assertIn("SUB", report.trigger_milestones)
        self.assertIn("COMP", report.changed_forecasts)
        # GALL 与触发点无依赖关系，必须冻结在闭包外
        self.assertNotIn("GALL", report.frozen_boundary)
        self.assertEqual(report.scope_size, 2)
        self.assertEqual(result.milestones["COMP"].finish, D("2026-11-20"))
        self.assertEqual(result.milestones["GALL"].finish, previous.milestones["GALL"].finish)

    def test_resource_trigger_expands_scope_to_co_bookings(self) -> None:
        projects, milestones = self._build()
        resources = [SharedResource(code="R", name="道路窗口")]
        bookings = [
            ResourceBooking(code="B1", resource_code="R", milestone_code="SUB",
                            project_code="P-POWER", duration_days=30),
            ResourceBooking(code="B2", resource_code="R", milestone_code="GALL",
                            project_code="P-GALL", duration_days=20),
        ]
        old = snapshot(projects, milestones, resources=resources, bookings=bookings)
        previous = compute_schedule(old, AS_OF)
        new_bookings = [
            ResourceBooking(code="B1", resource_code="R", milestone_code="SUB",
                            project_code="P-POWER", duration_days=35),
            bookings[1],
        ]
        new = snapshot(projects, milestones, resources=resources, bookings=new_bookings)
        trigger = derive_triggers(old, new)
        self.assertEqual(trigger.resource_codes, frozenset({"R"}))
        result = recompute_incremental(new, AS_OF, previous, trigger)
        self.assertIn("R", result.recompute.leveled_resources)
        self.assertLess(result.recompute.scope_size, 3)  # COMP 不使用该资源且无依赖路径
        self.assertGreaterEqual(
            result.milestones["GALL"].finish, previous.milestones["GALL"].finish
        )

    def test_exemption_expiry_triggers_recompute(self) -> None:
        projects = [make_project("P1")]
        milestones = [make_milestone("A", "P1", 5), make_milestone("B", "P1", 5)]
        deps = [
            Dependency(code="D1", upstream="A", downstream="B", kind=DependencyKind.HARD),
            Dependency(code="D2", upstream="B", downstream="A",
                       kind=DependencyKind.EXEMPTION, exempt_until=D("2026-12-31")),
        ]
        snap = snapshot(projects, milestones, deps)
        previous = compute_schedule(snap, D("2026-10-01"))
        trigger = derive_triggers(snap, snap, D("2026-10-01"), D("2027-01-01"))
        self.assertTrue(any("豁免状态翻转" in r for r in trigger.reasons))
        result = recompute_incremental(snap, D("2027-01-01"), previous, trigger)
        self.assertTrue(result.cycles.blocking)


class HiddenDelayTests(unittest.TestCase):
    def test_local_on_time_but_full_chain_delayed(self) -> None:
        snap = snapshot(
            [make_project("P1"), make_project("P2")],
            [make_milestone("A", "P1", 30), make_milestone("B", "P2", 5)],
            [Dependency(code="D1", upstream="A", downstream="B", kind=DependencyKind.HARD)],
            commitments=[
                Commitment(code="K2", project_code="P2", title="P2交付",
                           promised_date=D("2026-10-20")),
            ],
        )
        full = compute_schedule(snap, AS_OF)
        hidden = hidden_delay_analysis(snap, AS_OF, full)
        self.assertEqual(len(hidden), 1)
        item = hidden[0]
        self.assertEqual(item.project_code, "P2")
        self.assertEqual(item.commitment_code, "K2")
        self.assertEqual(item.surface_status, "LOCAL_MET_FULL_BREACHED")
        self.assertTrue(any("跨网前置" in c for c in item.causes))


class ScenarioTests(unittest.TestCase):
    def test_scenarios_ranked_by_sacrifice(self) -> None:
        snap = snapshot(
            [make_project("P1"), make_project("P2")],
            [make_milestone("A", "P1", 30), make_milestone("B", "P2", 10)],
            [Dependency(code="D1", upstream="A", downstream="B", kind=DependencyKind.HARD)],
            commitments=[
                Commitment(code="KB", project_code="P2", title="B承诺",
                           promised_date=D("2026-11-05"), weight=4),
            ],
        )
        do_nothing = Scenario(scenario_id="S0", name="维持现状")
        crash = Scenario(
            scenario_id="S1", name="压缩变电站工期",
            crash_durations={"A": 20},
        )
        ranked = compare_scenarios(snap, AS_OF, [do_nothing, crash])
        self.assertEqual(ranked[0].scenario_id, "S1")
        self.assertEqual(ranked[0].sacrificed_commitments, ())
        self.assertEqual(len(ranked[1].sacrificed_commitments), 1)


if __name__ == "__main__":
    unittest.main()
