"""事件溯源存储：联合基线、提议协同、月度结论的持久化。

设计要点：

- 所有状态变化先追加为不可变事件（JSON Lines），再重建内存状态；
- 基线只以“已批准修订版”存在；责任方未全部确认、专班未批准的更新永不入基线；
- 批准时校验修订版与指纹快照，识别并发覆盖；提议幂等键防重复提交；
- 月度结论一经发布不可修改，重复发布被拒绝；
- 存储使用文件锁（fcntl），支持多进程/多线程并发提交。
"""

from __future__ import annotations

from dataclasses import asdict
from datetime import date
import json
import os
import threading
from pathlib import Path
from typing import Any, Callable

try:  # 生产 Linux/macOS 上使用 fcntl；Windows 退化为进程内锁
    import fcntl

    _HAS_FCNTL = True
except ImportError:  # pragma: no cover
    _HAS_FCNTL = False

from .model import (
    BaselineRevision,
    Change,
    ChangeProposal,
    Confirmation,
    MonthlyConclusion,
    ProposalStatus,
    canonical_dumps,
    now_iso,
    parse_entity,
)

EVENT_LOG = "events.jsonl"
SNAPSHOT_FILE = "snapshot.json"
SNAPSHOT_EVERY = 50


class ConcurrencyError(RuntimeError):
    """并发更新冲突：提议所基于的版本或实体已被他人改变。"""


class WorkflowError(RuntimeError):
    """提议工作流状态不满足操作前提。"""


# --------------------------------------------------------------------------- 事件


def _event(kind: str, payload: dict[str, Any]) -> dict[str, Any]:
    return {"kind": kind, "at": now_iso(), "payload": payload}


def _proposal_to_event(proposal: ChangeProposal, kind: str) -> dict[str, Any]:
    return _event(
        kind,
        {
            "proposal": proposal.to_dict()
            if hasattr(proposal, "to_dict")
            else json.loads(canonical_dumps(asdict(proposal))),
        },
    )


# --------------------------------------------------------------------------- 存储


class EventStore:
    def __init__(self, path: str | Path) -> None:
        self.dir = Path(path)
        self.dir.mkdir(parents=True, exist_ok=True)
        self.log_path = self.dir / EVENT_LOG
        self.snapshot_path = self.dir / SNAPSHOT_FILE
        self._lock = threading.RLock()
        self._file_lock: Any = None

    # ----- 锁与追加 -----

    class _FileGuard:
        def __init__(self, store: "EventStore") -> None:
            self.store = store
            self.fh: Any = None

        def __enter__(self) -> "EventStore._FileGuard":
            self.store._lock.acquire()
            self.fh = open(self.store.log_path, "a", encoding="utf-8")
            if _HAS_FCNTL:
                fcntl.flock(self.fh.fileno(), fcntl.LOCK_EX)
            return self

        def __exit__(self, *exc: Any) -> None:
            try:
                if self.fh is not None:
                    if _HAS_FCNTL:
                        fcntl.flock(self.fh.fileno(), fcntl.LOCK_UN)
                    self.fh.close()
            finally:
                self.store._lock.release()

    def locked(self) -> "_FileGuard":
        return self._FileGuard(self)

    def append(self, event: dict[str, Any]) -> None:
        line = canonical_dumps(event) + "\n"
        with open(self.log_path, "a", encoding="utf-8") as fh:
            fh.write(line)
            fh.flush()
            os.fsync(fh.fileno())

    def read_events(self) -> list[dict[str, Any]]:
        if not self.log_path.exists():
            return []
        events: list[dict[str, Any]] = []
        with open(self.log_path, encoding="utf-8") as fh:
            for line in fh:
                line = line.strip()
                if line:
                    events.append(json.loads(line))
        return events


# --------------------------------------------------------------------------- 应用状态


class ProgramRepository:
    """聚合根：在锁内读取事件 -> 执行操作 -> 追加事件。"""

    def __init__(self, store: EventStore) -> None:
        self.store = store
        self._rebuild()

    # ----- 重建 -----

    def _rebuild(self) -> None:
        self.revision: int = 0
        self.revisions: list[BaselineRevision] = []
        # 已批准基线实体：entity -> code -> 不可变对象
        self.entities: dict[str, dict[str, Any]] = {kind: {} for kind in (
            "project", "milestone", "dependency", "resource", "booking", "permit", "commitment"
        )}
        self.proposals: dict[str, ChangeProposal] = {}
        self.publications: dict[str, MonthlyConclusion] = {}
        self.idempotency: dict[str, str] = {}  # key -> proposal_id
        self.event_count = 0
        self._apply_events(self.store.read_events())

    def _apply_events(self, events: list[dict[str, Any]]) -> None:
        for event in events:
            self._apply(event)
            self.event_count += 1

    def _apply(self, event: dict[str, Any]) -> None:
        kind = event["kind"]
        payload = event["payload"]
        if kind == "BASELINE_APPROVED":
            rev = BaselineRevision(
                revision=payload["revision"],
                approved_at=payload["approved_at"],
                approver=payload["approver"],
                proposal_id=payload["proposal_id"],
                entities_fingerprint=payload["entities_fingerprint"],
                comment=payload.get("comment", ""),
            )
            self.revision = rev.revision
            self.revisions.append(rev)
            for item in payload["changes"]:
                if item["op"] == "REMOVE":
                    self.entities[item["entity"]].pop(item["data"]["code"], None)
                else:
                    obj = parse_entity(item["entity"], item["data"])
                    self.entities[item["entity"]][item["data"]["code"]] = obj
        elif kind == "BASELINE_REMOVED":
            self.entities[payload["entity"]].pop(payload["code"], None)
        elif kind in {
            "PROPOSAL_SUBMITTED",
            "PROPOSAL_CONFIRMED",
            "PROPOSAL_APPROVED",
            "PROPOSAL_REJECTED",
            "PROPOSAL_SUPERSEDED",
        }:
            proposal = self._hydrate_proposal(payload["proposal"])
            self.proposals[proposal.proposal_id] = proposal
            if kind == "PROPOSAL_SUBMITTED" and proposal.idempotency_key:
                # 重建幂等映射：首次提交者占用该键
                self.idempotency.setdefault(proposal.idempotency_key, proposal.proposal_id)
        elif kind == "MONTHLY_PUBLISHED":
            doc = payload["document"]
            conclusion = MonthlyConclusion(
                publication_id=payload["publication_id"],
                month=payload["month"],
                published_at=payload["published_at"],
                publisher=payload["publisher"],
                baseline_revision=payload["baseline_revision"],
                as_of=payload["as_of"],
                content_fingerprint=payload["content_fingerprint"],
                document=doc,
            )
            self.publications[conclusion.month] = conclusion
        else:  # pragma: no cover - 未知事件向前兼容
            pass

    @staticmethod
    def _hydrate_proposal(data: dict[str, Any]) -> ChangeProposal:
        return ChangeProposal(
            proposal_id=data["proposal_id"],
            base_revision=data["base_revision"],
            submitted_by=data["submitted_by"],
            submitted_at=data["submitted_at"],
            changes=tuple(
                Change(op=c["op"], entity=c["entity"], data=c["data"]) for c in data["changes"]
            ),
            affected_projects=tuple(data["affected_projects"]),
            required_parties=tuple(data["required_parties"]),
            confirmations=tuple(
                Confirmation(
                    party=c["party"],
                    actor_id=c["actor_id"],
                    confirmed_at=c["confirmed_at"],
                    comment=c.get("comment", ""),
                )
                for c in data.get("confirmations", [])
            ),
            status=ProposalStatus(data["status"]),
            idempotency_key=data.get("idempotency_key"),
            selected_scenario_id=data.get("selected_scenario_id"),
            rationale=data.get("rationale", ""),
            decided_at=data.get("decided_at"),
            decider=data.get("decider"),
            decision_comment=data.get("decision_comment", ""),
            base_fingerprints=tuple(
                (pair[0], pair[1]) for pair in data.get("base_fingerprints", [])
            ),
        )

    # ----- 查询 -----

    def list_entities(self, entity: str) -> list[Any]:
        return list(self.entities[entity].values())

    def get_entity(self, entity: str, code: str) -> Any:
        return self.entities[entity].get(code)

    def entities_fingerprint(self) -> str:
        return _fingerprint_entities(self.entities)

    def proposal(self, proposal_id: str) -> ChangeProposal:
        try:
            return self.proposals[proposal_id]
        except KeyError as exc:
            raise WorkflowError(f"提议不存在: {proposal_id}") from exc

    def publication(self, month: str) -> MonthlyConclusion:
        try:
            return self.publications[month]
        except KeyError as exc:
            raise WorkflowError(f"月度结论不存在: {month}") from exc

    # ----- 变更预演（不写事件） -----

    def preview_changes(self, changes: list[Change]) -> dict[str, dict[str, Any]]:
        """在已批准基线之上模拟应用变更，返回变更后的实体表（供校验与影响面分析）。"""
        preview: dict[str, dict[str, Any]] = {
            kind: dict(table) for kind, table in self.entities.items()
        }
        for change in changes:
            if change.op == "UPSERT":
                obj = parse_entity(change.entity, change.data)
                preview[change.entity][obj.code] = obj
            else:
                preview[change.entity].pop(change.data["code"], None)
        return preview

    def snapshot_fingerprints(self, changes: list[Change]) -> dict[str, str]:
        """提议涉及实体在当前基线上的指纹（含将被删除的对象）。"""
        fingerprints: dict[str, str] = {}
        for change in changes:
            key = f"{change.entity}:{change.data['code']}"
            current = self.entities[change.entity].get(change.data["code"])
            fingerprints[key] = current.fingerprint() if current else "ABSENT"
        return fingerprints

    # ----- 提议提交 -----

    def submit_proposal(
        self,
        *,
        proposal_id: str,
        changes: list[Change],
        submitted_by: str,
        affected_projects: list[str],
        required_parties: list[str],
        idempotency_key: str | None = None,
        selected_scenario_id: str | None = None,
        rationale: str = "",
    ) -> ChangeProposal:
        with self.store.locked():
            if idempotency_key:
                existing = self.idempotency.get(idempotency_key)
                if existing:
                    return self.proposals[existing]
            if proposal_id in self.proposals:
                raise WorkflowError(f"提议编号已存在: {proposal_id}")
            if not changes:
                raise WorkflowError("提议至少包含一条变更")
            parties = sorted({p.strip() for p in required_parties if p.strip()})
            if not parties:
                raise WorkflowError("必须指定至少一个责任方")
            projects = sorted({p for p in affected_projects})
            proposal = ChangeProposal(
                proposal_id=proposal_id,
                base_revision=self.revision,
                submitted_by=submitted_by,
                submitted_at=now_iso(),
                changes=tuple(changes),
                affected_projects=tuple(projects),
                required_parties=tuple(parties),
                idempotency_key=idempotency_key,
                selected_scenario_id=selected_scenario_id,
                rationale=rationale,
                base_fingerprints=tuple(sorted(self.snapshot_fingerprints(changes).items())),
            )
            event = _proposal_to_event(proposal, "PROPOSAL_SUBMITTED")
            self.store.append(event)
            self._apply(event)
            if idempotency_key:
                self.idempotency[idempotency_key] = proposal_id
            return proposal

    # ----- 责任方确认 -----

    def confirm_proposal(
        self,
        proposal_id: str,
        *,
        party: str,
        actor_id: str,
        comment: str = "",
    ) -> ChangeProposal:
        with self.store.locked():
            proposal = self._require_open(proposal_id)
            if party not in proposal.required_parties:
                raise WorkflowError(f"{party} 不是该提议的责任方: {sorted(proposal.required_parties)}")
            if any(c.party == party for c in proposal.confirmations):
                return proposal  # 幂等
            updated = self._with(
                proposal,
                confirmations=proposal.confirmations
                + (Confirmation(
                    party=party, actor_id=actor_id, confirmed_at=now_iso(), comment=comment
                ),),
            )
            event = _proposal_to_event(updated, "PROPOSAL_CONFIRMED")
            self.store.append(event)
            self._apply(event)
            return updated

    def _require_open(self, proposal_id: str) -> ChangeProposal:
        proposal = self.proposal(proposal_id)
        if proposal.status is not ProposalStatus.OPEN:
            raise WorkflowError(
                f"提议 {proposal_id} 当前状态为 {proposal.status.value}，不能继续操作"
            )
        return proposal

    @staticmethod
    def _with(proposal: ChangeProposal, **changes: Any) -> ChangeProposal:
        from dataclasses import replace

        return replace(proposal, **changes)

    # ----- 专班批准 / 驳回 -----

    def missing_confirmations(self, proposal: ChangeProposal) -> list[str]:
        confirmed = {c.party for c in proposal.confirmations}
        return [p for p in proposal.required_parties if p not in confirmed]

    def approve_proposal(
        self,
        proposal_id: str,
        *,
        approver: str,
        comment: str = "",
        validator: Callable[[dict[str, dict[str, Any]]], None] | None = None,
    ) -> tuple[ChangeProposal, BaselineRevision]:
        with self.store.locked():
            proposal = self._require_open(proposal_id)
            missing = self.missing_confirmations(proposal)
            if missing:
                raise WorkflowError(f"责任方尚未全部确认，缺少: {missing}")
            # 并发覆盖检测：基线修订版或被改实体指纹发生变化即失败
            if proposal.base_revision != self.revision:
                current_fingerprints = self.snapshot_fingerprints(list(proposal.changes))
                changed_now = [
                    key
                    for key, fp in proposal.base_fingerprints
                    if current_fingerprints.get(key) != fp
                ]
                if changed_now:
                    superseded = self._with(
                        proposal,
                        status=ProposalStatus.SUPERSEDED,
                        decided_at=now_iso(),
                        decider=approver,
                        decision_comment="批准时发现并发更新已覆盖受影响实体",
                    )
                    event = _proposal_to_event(superseded, "PROPOSAL_SUPERSEDED")
                    self.store.append(event)
                    self._apply(event)
                    raise ConcurrencyError(
                        f"提议基于修订版 {proposal.base_revision}，"
                        f"当前为 {self.revision}；以下实体已被并发改变: {changed_now}"
                    )
            preview = self.preview_changes(list(proposal.changes))
            if validator is not None:
                validator(preview)
            new_revision = self.revision + 1
            fingerprint = _fingerprint_entities(preview)
            revision = BaselineRevision(
                revision=new_revision,
                approved_at=now_iso(),
                approver=approver,
                proposal_id=proposal.proposal_id,
                entities_fingerprint=fingerprint,
                comment=comment,
            )
            approved = self._with(
                proposal,
                status=ProposalStatus.APPROVED,
                decided_at=revision.approved_at,
                decider=approver,
                decision_comment=comment,
            )
            self.store.append(_proposal_to_event(approved, "PROPOSAL_APPROVED"))
            approve_event = _event(
                "BASELINE_APPROVED",
                {
                    "revision": revision.revision,
                    "approved_at": revision.approved_at,
                    "approver": revision.approver,
                    "proposal_id": revision.proposal_id,
                    "entities_fingerprint": revision.entities_fingerprint,
                    "comment": revision.comment,
                    "changes": [
                        {"op": c.op, "entity": c.entity, "data": c.data}
                        for c in proposal.changes
                    ],
                },
            )
            self.store.append(approve_event)
            self._apply(_proposal_to_event(approved, "PROPOSAL_APPROVED"))
            self._apply(approve_event)
            return approved, revision

    def reject_proposal(
        self, proposal_id: str, *, decider: str, comment: str = ""
    ) -> ChangeProposal:
        with self.store.locked():
            proposal = self._require_open(proposal_id)
            rejected = self._with(
                proposal,
                status=ProposalStatus.REJECTED,
                decided_at=now_iso(),
                decider=decider,
                decision_comment=comment,
            )
            event = _proposal_to_event(rejected, "PROPOSAL_REJECTED")
            self.store.append(event)
            self._apply(event)
            return rejected

    # ----- 初始基线（系统首次建账，proposal_id 固定为 INITIAL） -----

    def establish_initial(
        self,
        *,
        approver: str,
        changes: list[Change],
        comment: str = "建立联合基线",
        validator: Callable[[dict[str, dict[str, Any]]], None] | None = None,
    ) -> BaselineRevision:
        with self.store.locked():
            if self.revision != 0:
                raise WorkflowError("联合基线已建立，初始建账只能执行一次")
            preview = self.preview_changes(changes)
            if validator is not None:
                validator(preview)
            event = _event(
                "BASELINE_APPROVED",
                {
                    "revision": 1,
                    "approved_at": now_iso(),
                    "approver": approver,
                    "proposal_id": "INITIAL",
                    "entities_fingerprint": _fingerprint_entities(preview),
                    "comment": comment,
                    "changes": [
                        {"op": c.op, "entity": c.entity, "data": c.data} for c in changes
                    ],
                },
            )
            self.store.append(event)
            self._apply(event)
            return self.revisions[-1]

    # ----- 月度结论发布 -----

    def publish_monthly(
        self,
        *,
        month: str,
        publisher: str,
        as_of: date,
        document: dict[str, Any],
        publication_id: str | None = None,
    ) -> MonthlyConclusion:
        with self.store.locked():
            _validate_month(month)
            if month in self.publications:
                raise WorkflowError(f"{month} 月度结论已发布，发布结论不可修改")
            from hashlib import sha256

            content_fingerprint = sha256(
                canonical_dumps(
                    {
                        "month": month,
                        "revision": self.revision,
                        "as_of": as_of.isoformat(),
                        "document": document,
                    }
                ).encode("utf-8")
            ).hexdigest()
            conclusion = MonthlyConclusion(
                publication_id=publication_id or f"PUB-{month}-{self.revision}",
                month=month,
                published_at=now_iso(),
                publisher=publisher,
                baseline_revision=self.revision,
                as_of=as_of.isoformat(),
                content_fingerprint=content_fingerprint,
                document=document,
            )
            event = _event("MONTHLY_PUBLISHED", json.loads(canonical_dumps(conclusion.to_dict())))
            self.store.append(event)
            self._apply(event)
            return conclusion

    def list_publications(self) -> list[MonthlyConclusion]:
        return [self.publications[m] for m in sorted(self.publications)]

    # ----- 审计 -----

    def approval_trail(self) -> list[dict[str, Any]]:
        """按修订版顺序给出完整批准过程（初始建账 + 每次提议的全链路）。"""
        events = self.store.read_events()
        proposals_by_revision: dict[str, dict[str, Any]] = {}
        trail: list[dict[str, Any]] = []
        proposal_seen: set[str] = set()
        for event in events:
            kind = event["kind"]
            if kind.startswith("PROPOSAL_"):
                proposal = event["payload"]["proposal"]
                if proposal["proposal_id"] not in proposal_seen:
                    proposal_seen.add(proposal["proposal_id"])
                # 保留最新快照
                proposals_by_revision[proposal["proposal_id"]] = proposal
            elif kind == "BASELINE_APPROVED":
                proposal = proposals_by_revision.get(payload_proposal_id(event))
                trail.append(
                    {
                        "revision": event["payload"]["revision"],
                        "approved_at": event["payload"]["approved_at"],
                        "approver": event["payload"]["approver"],
                        "proposal_id": event["payload"]["proposal_id"],
                        "comment": event["payload"].get("comment", ""),
                        "proposal": proposal,
                    }
                )
        return trail


def payload_proposal_id(event: dict[str, Any]) -> str:
    return event["payload"]["proposal_id"]


def _fingerprint_entities(entities: dict[str, dict[str, Any]]) -> str:
    from hashlib import sha256

    body = {
        kind: {code: obj.fingerprint() for code, obj in sorted(table.items())}
        for kind, table in entities.items()
    }
    return sha256(canonical_dumps(body).encode("utf-8")).hexdigest()


def _validate_month(month: str) -> None:
    try:
        year, mon = month.split("-")
        if len(year) != 4 or not (1 <= int(mon) <= 12):
            raise ValueError
    except ValueError as exc:
        raise WorkflowError("月份必须为 YYYY-MM 格式") from exc
