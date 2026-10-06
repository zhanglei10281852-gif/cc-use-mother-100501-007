"""应用服务：把协同存储与调度引擎组合为完整用例。"""

from __future__ import annotations

from dataclasses import asdict, dataclass
from datetime import date
from typing import Any

from .engine import (
    ScheduleResult,
    Scenario,
    Snapshot,
    Trigger,
    affected_scope,
    compare_scenarios,
    compute_schedule,
    derive_triggers,
    hidden_delay_analysis,
    recompute_incremental,
)
from .model import (
    Change,
    MonthlyConclusion,
    coerce_date,
)
from .store import ConcurrencyError, EventStore, ProgramRepository, WorkflowError  # noqa: F401


ENTITY_KINDS = ("project", "milestone", "dependency", "resource", "booking", "permit", "commitment")


def _dataclass_to_dict(value: Any) -> Any:
    from .model import to_primitive

    return to_primitive(asdict(value))


def validate_referential_integrity(entities: dict[str, dict[str, Any]]) -> None:
    """批准前校验：所有引用必须闭合（循环依赖允许存在，由调度引擎识别上报）。"""
    errors: list[str] = []
    projects = entities["project"]
    milestones = entities["milestone"]
    resources = entities["resource"]

    for m in milestones.values():
        if m.project_code not in projects:
            errors.append(f"里程碑 {m.code} 引用了不存在的项目 {m.project_code}")
    for dep in entities["dependency"].values():
        if dep.upstream not in milestones:
            errors.append(f"前置条件 {dep.code} 的上游里程碑 {dep.upstream} 不存在")
        if dep.downstream not in milestones:
            errors.append(f"前置条件 {dep.code} 的下游里程碑 {dep.downstream} 不存在")
    for booking in entities["booking"].values():
        if booking.resource_code not in resources:
            errors.append(f"预约 {booking.code} 引用了不存在的共享资源 {booking.resource_code}")
        milestone = milestones.get(booking.milestone_code)
        if milestone is None:
            errors.append(f"预约 {booking.code} 引用了不存在的里程碑 {booking.milestone_code}")
        elif milestone.project_code != booking.project_code:
            errors.append(
                f"预约 {booking.code} 的项目 {booking.project_code} 与里程碑所属项目不一致"
            )
    for permit in entities["permit"].values():
        if permit.project_code not in projects:
            errors.append(f"许可 {permit.code} 引用了不存在的项目 {permit.project_code}")
        if permit.milestone_code and permit.milestone_code not in milestones:
            errors.append(f"许可 {permit.code} 引用了不存在的里程碑 {permit.milestone_code}")
    for commitment in entities["commitment"].values():
        if commitment.project_code not in projects:
            errors.append(f"承诺 {commitment.code} 引用了不存在的项目 {commitment.project_code}")
        if commitment.milestone_code and commitment.milestone_code not in milestones:
            errors.append(f"承诺 {commitment.code} 引用了不存在的里程碑 {commitment.milestone_code}")
    if errors:
        raise WorkflowError("引用完整性校验失败: " + "；".join(sorted(errors)))


def build_snapshot(repo: ProgramRepository) -> Snapshot:
    return Snapshot(
        revision=repo.revision,
        entities_fingerprint=repo.entities_fingerprint(),
        projects=dict(repo.entities["project"]),
        milestones=dict(repo.entities["milestone"]),
        dependencies=dict(repo.entities["dependency"]),
        resources=dict(repo.entities["resource"]),
        bookings=dict(repo.entities["booking"]),
        permits=dict(repo.entities["permit"]),
        commitments=dict(repo.entities["commitment"]),
    )


@dataclass(slots=True)
class _CachedSchedule:
    entities_fingerprint: str
    as_of: date
    snapshot: Snapshot
    result: ScheduleResult


class ProgramService:
    """对外统一入口：登记提议、确认批准、调度研判、月度发布、审计。"""

    def __init__(self, path: str) -> None:
        self.repo = ProgramRepository(EventStore(path))
        self._cache: _CachedSchedule | None = None

    # ----- 基线 -----

    def establish_initial_baseline(self, *, approver: str, changes: list[Change], comment: str = "") -> Any:
        return self.repo.establish_initial(
            approver=approver,
            changes=changes,
            comment=comment,
            validator=validate_referential_integrity,
        )

    def snapshot(self) -> Snapshot:
        return build_snapshot(self.repo)

    # ----- 提议：自动推断受影响项目与责任方 -----

    def _involvement(
        self, changes: list[Change], preview: dict[str, dict[str, Any]]
    ) -> tuple[list[str], list[str]]:
        projects: set[str] = set()
        parties: set[str] = set()
        for change in changes:
            table = preview[change.entity]
            obj = table.get(change.data["code"])
            if change.entity == "project" and obj is not None:
                projects.add(obj.code)
                parties.add(obj.responsible_party)
            elif change.entity == "milestone" and obj is not None:
                projects.add(obj.project_code)
            elif change.entity in {"dependency", "booking", "permit", "commitment"} and obj is not None:
                projects.add(obj.project_code)
            elif change.entity == "resource":
                # 共享资源变更涉及资源上全部预约方
                for booking in preview["booking"].values():
                    if booking.resource_code == change.data["code"]:
                        projects.add(booking.project_code)
        # REMOVE 时从 change.data 推断项目
        for change in changes:
            if change.op == "REMOVE":
                code = change.data["code"]
                if change.entity == "project":
                    projects.add(code)
                elif change.entity == "milestone":
                    current = self.repo.get_entity("milestone", code)
                    if current:
                        projects.add(current.project_code)
                elif change.entity in {"dependency", "booking", "permit", "commitment"}:
                    current = self.repo.get_entity(change.entity, code)
                    if current:
                        projects.add(current.project_code)
        for project_code in projects:
            project = preview["project"].get(project_code) or self.repo.get_entity("project", project_code)
            if project is not None:
                parties.add(project.responsible_party)
        return sorted(projects), sorted(parties)

    def submit_changes(
        self,
        *,
        proposal_id: str,
        changes: list[Change],
        submitted_by: str,
        idempotency_key: str | None = None,
        selected_scenario_id: str | None = None,
        rationale: str = "",
    ) -> Any:
        preview = self.repo.preview_changes(changes)
        validate_referential_integrity(preview)
        projects, parties = self._involvement(changes, preview)
        return self.repo.submit_proposal(
            proposal_id=proposal_id,
            changes=changes,
            submitted_by=submitted_by,
            affected_projects=projects,
            required_parties=parties,
            idempotency_key=idempotency_key,
            selected_scenario_id=selected_scenario_id,
            rationale=rationale,
        )

    def confirm(self, proposal_id: str, *, party: str, actor_id: str, comment: str = "") -> Any:
        return self.repo.confirm_proposal(
            proposal_id, party=party, actor_id=actor_id, comment=comment
        )

    def approve(self, proposal_id: str, *, approver: str, comment: str = "") -> Any:
        proposal, revision = self.repo.approve_proposal(
            proposal_id,
            approver=approver,
            comment=comment,
            validator=validate_referential_integrity,
        )
        return proposal, revision

    def reject(self, proposal_id: str, *, decider: str, comment: str = "") -> Any:
        return self.repo.reject_proposal(proposal_id, decider=decider, comment=comment)

    # ----- 调度 -----

    def schedule(self, as_of: date | str | None = None, *, incremental: bool = True) -> ScheduleResult:
        as_of = coerce_date(as_of) or date.today()
        snapshot = build_snapshot(self.repo)
        cached = self._cache
        if (
            incremental
            and cached is not None
            and cached.entities_fingerprint == snapshot.entities_fingerprint
            and cached.as_of == as_of
        ):
            return cached.result
        if (
            incremental
            and cached is not None
            and cached.snapshot.revision <= snapshot.revision
            and (
                cached.entities_fingerprint != snapshot.entities_fingerprint
                or cached.as_of != as_of
            )
        ):
            trigger = derive_triggers(cached.snapshot, snapshot, cached.as_of, as_of)
            scope, _ = affected_scope(snapshot, trigger)
            # 数据未变且无豁免翻转（例如仅换了研判日期）时，全量重算以正确处理延误外推
            if scope or cached.entities_fingerprint != snapshot.entities_fingerprint:
                result = recompute_incremental(snapshot, as_of, cached.result, trigger)
            else:
                result = compute_schedule(snapshot, as_of)
        else:
            result = compute_schedule(snapshot, as_of)
        self._cache = _CachedSchedule(snapshot.entities_fingerprint, as_of, snapshot, result)
        return result

    def full_schedule(self, as_of: date | str | None = None) -> ScheduleResult:
        return self.schedule(as_of, incremental=False)

    def recompute_for_trigger(
        self, as_of: date | str, trigger: Trigger
    ) -> ScheduleResult:
        """显式触发：豁免到期、里程碑延误登记等场景的定向重算入口。"""
        as_of = coerce_date(as_of)
        snapshot = build_snapshot(self.repo)
        base = (
            self._cache.result
            if self._cache is not None and self._cache.snapshot.revision == snapshot.revision
            else compute_schedule(snapshot, as_of)
        )
        result = recompute_incremental(snapshot, as_of, base, trigger)
        self._cache = _CachedSchedule(snapshot.entities_fingerprint, as_of, snapshot, result)
        return result

    # ----- 研判 -----

    def hidden_delays(self, as_of: date | str | None = None) -> Any:
        as_of = coerce_date(as_of) or date.today()
        full = self.full_schedule(as_of)
        return hidden_delay_analysis(build_snapshot(self.repo), as_of, full)

    def evaluate_scenarios(self, as_of: date | str | None, scenarios: list[Scenario]) -> Any:
        as_of = coerce_date(as_of) or date.today()
        return compare_scenarios(build_snapshot(self.repo), as_of, scenarios)

    def affected(self, trigger: Trigger) -> Any:
        return affected_scope(build_snapshot(self.repo), trigger)

    # ----- 月度结论 -----

    def publish_monthly(
        self, *, month: str, publisher: str, as_of: date | str
    ) -> MonthlyConclusion:
        as_of = coerce_date(as_of)
        result = self.full_schedule(as_of)
        result_dict = result.to_dict()
        snapshot = build_snapshot(self.repo)
        hidden = hidden_delay_analysis(snapshot, as_of, result)
        document = {
            "month": month,
            "as_of": as_of.isoformat(),
            "revision": self.repo.revision,
            "entities_fingerprint": snapshot.entities_fingerprint,
            "summary": {
                "project_count": len(snapshot.projects),
                "milestone_count": len(snapshot.milestones),
                "projects_blocked": sorted(
                    p for p, f in result.project_forecasts.items() if f is None
                ),
                "commitments_breached": [
                    c for c in result_dict["commitments"] if c["status"] == "BREACHED"
                ],
                "overload_count": len(result.overloads),
                "blocking_cycles": [list(c) for c in result.cycles.blocking],
                "latent_cycles": [list(c) for c in result.cycles.latent],
                "hidden_delays": [_dataclass_to_dict(h) for h in hidden],
                "active_exemptions": [
                    _dataclass_to_dict(e) for e in result.exemptions if e.active_now
                ],
            },
            "projects": {
                code: {
                    "forecast_finish": result.project_forecasts.get(code).isoformat()
                    if result.project_forecasts.get(code)
                    else None,
                }
                for code in sorted(snapshot.projects)
            },
            "commitments": result_dict["commitments"],
            "program_critical_chain": result.program_chain,
        }
        return self.repo.publish_monthly(
            month=month, publisher=publisher, as_of=as_of, document=document
        )

    def publication(self, month: str) -> MonthlyConclusion:
        return self.repo.publication(month)

    def publications(self) -> list[MonthlyConclusion]:
        return self.repo.list_publications()

    # ----- 审计 -----

    def approval_trail(self) -> list[dict[str, Any]]:
        return self.repo.approval_trail()
