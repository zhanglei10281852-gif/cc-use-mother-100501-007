"""领域模型校验测试。"""

from __future__ import annotations

import unittest
from datetime import date

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
    stable_identity,
)


class ModelTests(unittest.TestCase):
    def _project(self, **overrides):
        values = dict(
            code="P1",
            name="变电站",
            network="电网",
            owner_unit="电力公司",
            responsible_party="电力责任人",
            start_date=date(2026, 10, 1),
        )
        values.update(overrides)
        return Project(**values)

    def test_project_requires_fields_and_parses_dates(self) -> None:
        project = self._project(start_date="2026-10-01")
        self.assertEqual(project.start_date, date(2026, 10, 1))
        with self.assertRaises(ValueError):
            self._project(name=" ")

    def test_milestone_completed_requires_actual_finish(self) -> None:
        Milestone(
            code="M1", project_code="P1", name="投运",
            state=MilestoneState.COMPLETED, actual_finish=date(2026, 11, 1),
        )
        with self.assertRaises(ValueError):
            Milestone(
                code="M2", project_code="P1", name="投运",
                state=MilestoneState.COMPLETED,
            )

    def test_dependency_rules(self) -> None:
        with self.assertRaises(ValueError):
            Dependency(code="D", upstream="M1", downstream="M1", kind=DependencyKind.HARD)
        with self.assertRaises(ValueError):
            Dependency(
                code="D", upstream="M1", downstream="M2",
                kind=DependencyKind.ALTERNATIVE,
            )
        with self.assertRaises(ValueError):
            Dependency(
                code="D", upstream="M1", downstream="M2",
                kind=DependencyKind.EXEMPTION,
            )
        dep = Dependency(
            code="D", upstream="M1", downstream="M2",
            kind=DependencyKind.EXEMPTION, exempt_until="2026-12-31",
        )
        self.assertEqual(dep.exempt_until, date(2026, 12, 31))
        with self.assertRaises(ValueError):
            Dependency(
                code="D2", upstream="M1", downstream="M2",
                kind=DependencyKind.HARD, exempt_until=date(2026, 12, 31),
            )

    def test_resource_and_booking_validation(self) -> None:
        SharedResource(code="R", name="道路窗口", capacity_per_day=2)
        with self.assertRaises(ValueError):
            SharedResource(code="R", name="道路窗口", capacity_per_day=0)
        ResourceBooking(
            code="B", resource_code="R", milestone_code="M1",
            project_code="P1", duration_days=5,
        )
        with self.assertRaises(ValueError):
            ResourceBooking(
                code="B", resource_code="R", milestone_code="M1",
                project_code="P1", duration_days=0,
            )

    def test_permit_status_rules(self) -> None:
        with self.assertRaises(ValueError):
            Permit(code="L", project_code="P1", authority="监管局", status=PermitStatus.PENDING)
        permit = Permit(
            code="L", project_code="P1", authority="监管局",
            status=PermitStatus.GRANTED, granted_date=date(2026, 10, 5),
        )
        self.assertEqual(permit.granted_date, date(2026, 10, 5))

    def test_commitment_weight(self) -> None:
        commitment = Commitment(
            code="C", project_code="P1", title="送电",
            promised_date="2027-01-01", weight=3,
        )
        self.assertEqual(commitment.promised_date, date(2027, 1, 1))
        with self.assertRaises(ValueError):
            Commitment(
                code="C", project_code="P1", title="送电",
                promised_date="2027-01-01", weight=0,
            )

    def test_immutability_and_fingerprint(self) -> None:
        project = self._project()
        with self.assertRaises(Exception):
            project.name = "改名"  # type: ignore[misc]
        again = self._project()
        self.assertEqual(project.fingerprint(), again.fingerprint())

    def test_stable_identity_conflict(self) -> None:
        a = self._project()
        b = self._project(name="不同名称")
        with self.assertRaises(ValueError):
            stable_identity([a, b])


if __name__ == "__main__":
    unittest.main()
