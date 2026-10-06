"""事件存储与联合基线协同工作流测试。"""

from __future__ import annotations

import tempfile
import unittest
from datetime import date
from pathlib import Path

from program_scheduling.model import Change, Milestone, Project
from program_scheduling.store import ConcurrencyError, EventStore, ProgramRepository, WorkflowError


def project_change(code: str = "P1", party: str = "电力责任人", start: str = "2026-10-01") -> Change:
    return Change(
        op="UPSERT",
        entity="project",
        data={
            "code": code, "name": code, "network": "电网",
            "owner_unit": "电力公司", "responsible_party": party,
            "start_date": start,
        },
    )


def milestone_change(code: str = "M1", project: str = "P1", duration: int = 5) -> Change:
    return Change(
        op="UPSERT",
        entity="milestone",
        data={"code": code, "project_code": project, "name": code, "duration_days": duration},
    )


class StoreWorkflowTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.repo = ProgramRepository(EventStore(Path(self.tmp.name) / "store"))

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def test_initial_baseline_then_approve_requires_confirmations(self) -> None:
        self.repo.establish_initial(approver="专班", changes=[project_change(), milestone_change()])
        self.assertEqual(self.repo.revision, 1)

        proposal = self.repo.submit_proposal(
            proposal_id="PR-1",
            changes=[milestone_change(duration=9)],
            submitted_by="电力公司",
            affected_projects=["P1"],
            required_parties=["电力责任人"],
        )
        self.assertEqual(proposal.base_revision, 1)
        with self.assertRaises(WorkflowError):
            self.repo.approve_proposal("PR-1", approver="专班")
        self.repo.confirm_proposal("PR-1", party="电力责任人", actor_id="u1")
        _proposal, revision = self.repo.approve_proposal("PR-1", approver="专班")
        self.assertEqual(revision.revision, 2)
        self.assertEqual(self.repo.get_entity("milestone", "M1").duration_days, 9)

    def test_unapproved_changes_never_reach_baseline(self) -> None:
        self.repo.establish_initial(approver="专班", changes=[project_change(), milestone_change()])
        self.repo.submit_proposal(
            proposal_id="PR-X",
            changes=[milestone_change(duration=99)],
            submitted_by="x",
            affected_projects=["P1"],
            required_parties=["电力责任人"],
        )
        self.repo.reject_proposal("PR-X", decider="专班", comment="资料不全")
        self.assertEqual(self.repo.get_entity("milestone", "M1").duration_days, 5)
        self.assertEqual(self.repo.proposal("PR-X").status.value, "REJECTED")

    def test_wrong_party_cannot_confirm(self) -> None:
        self.repo.establish_initial(approver="专班", changes=[project_change(), milestone_change()])
        self.repo.submit_proposal(
            proposal_id="PR-2",
            changes=[milestone_change(duration=9)],
            submitted_by="x",
            affected_projects=["P1"],
            required_parties=["电力责任人"],
        )
        with self.assertRaises(WorkflowError):
            self.repo.confirm_proposal("PR-2", party="无关单位", actor_id="u")
        # 确认幂等
        self.repo.confirm_proposal("PR-2", party="电力责任人", actor_id="u1")
        again = self.repo.confirm_proposal("PR-2", party="电力责任人", actor_id="u1")
        self.assertEqual(len(again.confirmations), 1)

    def test_idempotency_key_returns_same_proposal(self) -> None:
        first = self.repo.submit_proposal(
            proposal_id="PR-A",
            changes=[project_change("P9")],
            submitted_by="x",
            affected_projects=["P9"],
            required_parties=["p"],
            idempotency_key="KEY-1",
        )
        second = self.repo.submit_proposal(
            proposal_id="PR-DIFFERENT",
            changes=[project_change("P9")],
            submitted_by="x",
            affected_projects=["P9"],
            required_parties=["p"],
            idempotency_key="KEY-1",
        )
        self.assertEqual(first.proposal_id, second.proposal_id)

    def test_concurrent_change_to_same_entity_marks_superseded(self) -> None:
        self.repo.establish_initial(approver="专班", changes=[project_change(), milestone_change()])
        # 两个并发提议都改 M1
        self.repo.submit_proposal(
            proposal_id="PR-C1", changes=[milestone_change(duration=9)],
            submitted_by="a", affected_projects=["P1"], required_parties=["电力责任人"],
        )
        self.repo.submit_proposal(
            proposal_id="PR-C2", changes=[milestone_change(duration=12)],
            submitted_by="b", affected_projects=["P1"], required_parties=["电力责任人"],
        )
        self.repo.confirm_proposal("PR-C1", party="电力责任人", actor_id="u")
        self.repo.approve_proposal("PR-C1", approver="专班")
        self.repo.confirm_proposal("PR-C2", party="电力责任人", actor_id="u")
        with self.assertRaises(ConcurrencyError):
            self.repo.approve_proposal("PR-C2", approver="专班")
        self.assertEqual(self.repo.proposal("PR-C2").status.value, "SUPERSEDED")
        self.assertEqual(self.repo.get_entity("milestone", "M1").duration_days, 9)

    def test_change_to_unrelated_entity_can_still_approve(self) -> None:
        self.repo.establish_initial(approver="专班", changes=[project_change(), milestone_change()])
        self.repo.submit_proposal(
            proposal_id="PR-U1", changes=[milestone_change(duration=9)],
            submitted_by="a", affected_projects=["P1"], required_parties=["电力责任人"],
        )
        self.repo.submit_proposal(
            proposal_id="PR-U2", changes=[project_change("P2", "算力责任人")],
            submitted_by="b", affected_projects=["P2"], required_parties=["算力责任人"],
        )
        self.repo.confirm_proposal("PR-U1", party="电力责任人", actor_id="u")
        self.repo.approve_proposal("PR-U1", approver="专班")
        self.repo.confirm_proposal("PR-U2", party="算力责任人", actor_id="u")
        _p, revision = self.repo.approve_proposal("PR-U2", approver="专班")
        self.assertEqual(revision.revision, 3)

    def test_event_log_rebuilds_state(self) -> None:
        self.repo.establish_initial(approver="专班", changes=[project_change(), milestone_change()])
        rebuilt = ProgramRepository(self.repo.store)
        self.assertEqual(rebuilt.revision, 1)
        self.assertEqual(rebuilt.get_entity("project", "P1").responsible_party, "电力责任人")
        self.assertEqual(rebuilt.get_entity("milestone", "M1").duration_days, 5)

    def test_idempotency_mapping_survives_rebuild(self) -> None:
        self.repo.submit_proposal(
            proposal_id="PR-R",
            changes=[project_change("PRJ")],
            submitted_by="x", affected_projects=["PRJ"], required_parties=["p"],
            idempotency_key="REBUILD-KEY",
        )
        rebuilt = ProgramRepository(self.repo.store)
        again = rebuilt.submit_proposal(
            proposal_id="PR-OTHER",
            changes=[project_change("PRJ")],
            submitted_by="x", affected_projects=["PRJ"], required_parties=["p"],
            idempotency_key="REBUILD-KEY",
        )
        self.assertEqual(again.proposal_id, "PR-R")

    def test_monthly_publication_is_immutable(self) -> None:
        self.repo.establish_initial(approver="专班", changes=[project_change()])
        self.repo.publish_monthly(
            month="2026-10", publisher="专班", as_of=date(2026, 10, 31),
            document={"结论": "总体可控"},
        )
        with self.assertRaises(WorkflowError):
            self.repo.publish_monthly(
                month="2026-10", publisher="专班", as_of=date(2026, 10, 31),
                document={"结论": "被篡改"},
            )
        fetched = self.repo.publication("2026-10")
        self.assertEqual(fetched.document["结论"], "总体可控")
        self.assertTrue(fetched.content_fingerprint)

    def test_approval_trail_covers_every_revision(self) -> None:
        self.repo.establish_initial(approver="专班", changes=[project_change(), milestone_change()])
        self.repo.submit_proposal(
            proposal_id="PR-T", changes=[milestone_change(duration=8)],
            submitted_by="a", affected_projects=["P1"], required_parties=["电力责任人"],
        )
        self.repo.confirm_proposal("PR-T", party="电力责任人", actor_id="u")
        self.repo.approve_proposal("PR-T", approver="专班", comment="同意调整")
        trail = self.repo.approval_trail()
        self.assertEqual([t["revision"] for t in trail], [1, 2])
        self.assertEqual(trail[0]["proposal_id"], "INITIAL")
        self.assertEqual(trail[1]["proposal"]["proposal_id"], "PR-T")
        self.assertEqual(trail[1]["proposal"]["status"], "APPROVED")
        self.assertTrue(trail[1]["proposal"]["confirmations"])


if __name__ == "__main__":
    unittest.main()
