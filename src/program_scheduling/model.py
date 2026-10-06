"""六网项目联动排程领域模型。

所有领域对象均为不可变值对象，携带稳定内容指纹：

- 联合基线只能通过追加新版本演进，历史对象不被就地修改；
- 指纹同时用于幂等提交、乐观并发冲突检测与审计校对；
- 日期统一使用 :class:`datetime.date`，序列化时转为 ISO 字符串。
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from datetime import date, datetime, timezone
from enum import Enum
import json
from hashlib import sha256
from typing import Any, Iterable


# --------------------------------------------------------------------------- 基础工具


def utc_now() -> datetime:
    return datetime.now(timezone.utc).replace(microsecond=0)


def now_iso() -> str:
    return utc_now().isoformat()


def coerce_date(value: Any) -> date | None:
    if value is None or isinstance(value, date):
        return value
    if isinstance(value, str) and value.strip():
        return date.fromisoformat(value)
    raise TypeError(f"无法解析日期: {value!r}")


def _json_default(value: Any) -> Any:
    if isinstance(value, Enum):
        return value.value
    if isinstance(value, date):
        return value.isoformat()
    raise TypeError(f"不可序列化的对象: {type(value)!r}")


def canonical_dumps(value: Any) -> str:
    """稳定规范化 JSON，供指纹与幂等键使用。"""
    return json.dumps(
        value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), default=_json_default
    )


def to_primitive(value: Any) -> Any:
    """把领域对象转换为只含 JSON 原生类型的结构。"""
    return json.loads(json.dumps(value, default=_json_default))


def require_text(value: str, field_name: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{field_name} 不能为空")
    return value.strip()


@dataclass(frozen=True, slots=True)
class DomainObject:
    """不可变领域对象基类，提供指纹、快照与安全演进。"""

    def fingerprint(self) -> str:
        return sha256(canonical_dumps(asdict(self)).encode("utf-8")).hexdigest()

    def to_dict(self) -> dict[str, Any]:
        return to_primitive(asdict(self))


# --------------------------------------------------------------------------- 枚举


class NetworkKind(str, Enum):
    """六网标识（允许扩展，不在此枚举内的自定义网络也可登记）。"""

    POWER = "电网"
    COMPUTING = "算力网"
    PIPE_GALLERY = "管廊网"
    ROAD = "路网"
    WATER = "水网"
    COMMUNICATION = "通信网"


class DependencyKind(str, Enum):
    HARD = "HARD"  # 硬阻断：前置不完成，后续不得开始
    ALTERNATIVE = "ALTERNATIVE"  # 可替代：同组前置满足其一即可
    EXEMPTION = "EXEMPTION"  # 带期限的临时豁免：到期自动恢复为硬阻断


class MilestoneState(str, Enum):
    PLANNED = "PLANNED"  # 计划中
    IN_PROGRESS = "IN_PROGRESS"  # 实施中
    COMPLETED = "COMPLETED"  # 已完成（以实际完成日期为准）


class PermitStatus(str, Enum):
    PENDING = "PENDING"  # 待批
    GRANTED = "GRANTED"  # 已许可
    REJECTED = "REJECTED"  # 被驳回（硬阻断，无预计解除日期）


class ProposalStatus(str, Enum):
    OPEN = "OPEN"  # 待责任方确认
    APPROVED = "APPROVED"  # 已批准并并入联合基线
    REJECTED = "REJECTED"  # 被驳回
    SUPERSEDED = "SUPERSEDED"  # 确认期间被并发更新覆盖，需重新提交


# --------------------------------------------------------------------------- 登记对象


@dataclass(frozen=True, slots=True)
class Project(DomainObject):
    """建设单位在某一张网上的项目，start_date 为该项目进度计算锚点。"""

    code: str
    name: str
    network: str
    owner_unit: str  # 建设单位
    responsible_party: str  # 责任方（更新联合基线前必须由其确认）
    start_date: date

    def __post_init__(self) -> None:
        require_text(self.code, "code")
        require_text(self.name, "name")
        require_text(self.network, "network")
        require_text(self.owner_unit, "owner_unit")
        require_text(self.responsible_party, "responsible_party")
        object.__setattr__(self, "start_date", coerce_date(self.start_date))
        if self.start_date is None:
            raise ValueError("start_date 不能为空")

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "Project":
        return cls(
            code=data["code"],
            name=data["name"],
            network=data["network"],
            owner_unit=data["owner_unit"],
            responsible_party=data["responsible_party"],
            start_date=coerce_date(data["start_date"]),
        )


@dataclass(frozen=True, slots=True)
class Milestone(DomainObject):
    """里程碑（同时作为进度网络中的作业节点，duration_days 为持续时间）。"""

    code: str
    project_code: str
    name: str
    duration_days: int = 0
    state: MilestoneState = MilestoneState.PLANNED
    actual_finish: date | None = None

    def __post_init__(self) -> None:
        require_text(self.code, "code")
        require_text(self.project_code, "project_code")
        require_text(self.name, "name")
        if not isinstance(self.duration_days, int) or self.duration_days < 0:
            raise ValueError("duration_days 必须为非负整数")
        object.__setattr__(self, "state", MilestoneState(self.state))
        object.__setattr__(self, "actual_finish", coerce_date(self.actual_finish))
        if self.state is MilestoneState.COMPLETED and self.actual_finish is None:
            raise ValueError("已完成里程碑必须提供 actual_finish")

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "Milestone":
        return cls(
            code=data["code"],
            project_code=data["project_code"],
            name=data["name"],
            duration_days=int(data.get("duration_days", 0)),
            state=MilestoneState(data.get("state", MilestoneState.PLANNED.value)),
            actual_finish=coerce_date(data.get("actual_finish")),
        )


@dataclass(frozen=True, slots=True)
class Dependency(DomainObject):
    """前置条件。

    - HARD：downstream 必须等待 upstream 完成（FS + lag_days）；
    - ALTERNATIVE：同 downstream 下相同 alternative_group 的边为“或”关系，满足最早一条即可；
    - EXEMPTION：as_of 晚于 exempt_until 时恢复为硬阻断，此前临时放行。
    """

    code: str
    upstream: str
    downstream: str
    kind: DependencyKind
    lag_days: int = 0
    alternative_group: str | None = None
    exempt_until: date | None = None
    rationale: str = ""

    def __post_init__(self) -> None:
        require_text(self.code, "code")
        require_text(self.upstream, "upstream")
        require_text(self.downstream, "downstream")
        object.__setattr__(self, "kind", DependencyKind(self.kind))
        if self.upstream == self.downstream:
            raise ValueError("前置条件不能自引用")
        if not isinstance(self.lag_days, int) or self.lag_days < 0:
            raise ValueError("lag_days 必须为非负整数")
        if self.kind is DependencyKind.ALTERNATIVE:
            require_text(self.alternative_group or "", "alternative_group")
        else:
            if self.alternative_group:
                raise ValueError("只有可替代前置才能设置 alternative_group")
        if self.kind is DependencyKind.EXEMPTION:
            object.__setattr__(self, "exempt_until", coerce_date(self.exempt_until))
            if self.exempt_until is None:
                raise ValueError("临时豁免必须设置 exempt_until")
        else:
            if self.exempt_until is not None:
                raise ValueError("只有临时豁免才能设置 exempt_until")

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "Dependency":
        return cls(
            code=data["code"],
            upstream=data["upstream"],
            downstream=data["downstream"],
            kind=DependencyKind(data["kind"]),
            lag_days=int(data.get("lag_days", 0)),
            alternative_group=data.get("alternative_group"),
            exempt_until=coerce_date(data.get("exempt_until")),
            rationale=data.get("rationale", ""),
        )


@dataclass(frozen=True, slots=True)
class SharedResource(DomainObject):
    """跨网共享资源，如同一条道路的施工窗口、共用作业面。"""

    code: str
    name: str
    capacity_per_day: float = 1.0  # 每日可并行占用的容量（1.0 表示同一时间只能一家占用）
    category: str = ""

    def __post_init__(self) -> None:
        require_text(self.code, "code")
        require_text(self.name, "name")
        if not isinstance(self.capacity_per_day, (int, float)) or self.capacity_per_day <= 0:
            raise ValueError("capacity_per_day 必须为正数")

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "SharedResource":
        return cls(
            code=data["code"],
            name=data["name"],
            capacity_per_day=float(data.get("capacity_per_day", 1.0)),
            category=data.get("category", ""),
        )


@dataclass(frozen=True, slots=True)
class ResourceBooking(DomainObject):
    """某里程碑对共享资源的占用预约（窗口长度 duration_days，需求 demand）。"""

    code: str
    resource_code: str
    milestone_code: str
    project_code: str
    duration_days: int
    demand: float = 1.0
    preferred_start: date | None = None
    priority: int = 100

    def __post_init__(self) -> None:
        require_text(self.code, "code")
        require_text(self.resource_code, "resource_code")
        require_text(self.milestone_code, "milestone_code")
        require_text(self.project_code, "project_code")
        if not isinstance(self.duration_days, int) or self.duration_days <= 0:
            raise ValueError("duration_days 必须为正整数")
        if not isinstance(self.demand, (int, float)) or self.demand <= 0:
            raise ValueError("demand 必须为正数")
        object.__setattr__(self, "preferred_start", coerce_date(self.preferred_start))

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "ResourceBooking":
        return cls(
            code=data["code"],
            resource_code=data["resource_code"],
            milestone_code=data["milestone_code"],
            project_code=data["project_code"],
            duration_days=int(data["duration_days"]),
            demand=float(data.get("demand", 1.0)),
            preferred_start=coerce_date(data.get("preferred_start")),
            priority=int(data.get("priority", 100)),
        )


@dataclass(frozen=True, slots=True)
class Permit(DomainObject):
    """监管许可：未获许可时按监管门控处理（待批用预计日期，驳回则永久阻断）。"""

    code: str
    project_code: str
    authority: str
    milestone_code: str | None = None  # 为空表示项目级许可，门控该项目全部里程碑
    status: PermitStatus = PermitStatus.PENDING
    planned_date: date | None = None
    granted_date: date | None = None

    def __post_init__(self) -> None:
        require_text(self.code, "code")
        require_text(self.project_code, "project_code")
        require_text(self.authority, "authority")
        object.__setattr__(self, "status", PermitStatus(self.status))
        object.__setattr__(self, "planned_date", coerce_date(self.planned_date))
        object.__setattr__(self, "granted_date", coerce_date(self.granted_date))
        if self.status is PermitStatus.PENDING and self.planned_date is None:
            raise ValueError("待批许可必须提供 planned_date")
        if self.status is PermitStatus.GRANTED and self.granted_date is None:
            raise ValueError("已许可必须提供 granted_date")

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "Permit":
        return cls(
            code=data["code"],
            project_code=data["project_code"],
            authority=data["authority"],
            milestone_code=data.get("milestone_code"),
            status=PermitStatus(data.get("status", PermitStatus.PENDING.value)),
            planned_date=coerce_date(data.get("planned_date")),
            granted_date=coerce_date(data.get("granted_date")),
        )


@dataclass(frozen=True, slots=True)
class Commitment(DomainObject):
    """对外部作出的交付承诺，带承诺日期与权重（研判时用于衡量牺牲代价）。"""

    code: str
    project_code: str
    title: str
    promised_date: date
    milestone_code: str | None = None  # 为空表示以项目最终完工为准
    weight: int = 1

    def __post_init__(self) -> None:
        require_text(self.code, "code")
        require_text(self.project_code, "project_code")
        require_text(self.title, "title")
        object.__setattr__(self, "promised_date", coerce_date(self.promised_date))
        if self.promised_date is None:
            raise ValueError("promised_date 不能为空")
        if not isinstance(self.weight, int) or self.weight < 1:
            raise ValueError("weight 必须为正整数")

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "Commitment":
        return cls(
            code=data["code"],
            project_code=data["project_code"],
            title=data["title"],
            promised_date=coerce_date(data["promised_date"]),
            milestone_code=data.get("milestone_code"),
            weight=int(data.get("weight", 1)),
        )


ENTITY_CLASSES: dict[str, type[DomainObject]] = {
    "project": Project,
    "milestone": Milestone,
    "dependency": Dependency,
    "resource": SharedResource,
    "booking": ResourceBooking,
    "permit": Permit,
    "commitment": Commitment,
}


def parse_entity(entity: str, data: dict[str, Any]) -> DomainObject:
    try:
        cls = ENTITY_CLASSES[entity]
    except KeyError as exc:
        raise ValueError(f"未知实体类型: {entity}") from exc
    return cls.from_dict(data)  # type: ignore[attr-defined]


# --------------------------------------------------------------------------- 协同对象


@dataclass(frozen=True, slots=True)
class Change:
    """对联合基线的一条原子修改。"""

    op: str  # UPSERT / REMOVE
    entity: str
    data: dict[str, Any]

    def __post_init__(self) -> None:
        if self.op not in {"UPSERT", "REMOVE"}:
            raise ValueError("op 只能为 UPSERT 或 REMOVE")
        if self.entity not in ENTITY_CLASSES:
            raise ValueError(f"未知实体类型: {self.entity}")
        if not isinstance(self.data, dict) or "code" not in self.data:
            raise ValueError("change.data 必须是含 code 的对象")

    def fingerprint(self) -> str:
        return sha256(canonical_dumps(asdict(self)).encode("utf-8")).hexdigest()


@dataclass(frozen=True, slots=True)
class Confirmation:
    party: str
    actor_id: str
    confirmed_at: str
    comment: str = ""


@dataclass(frozen=True, slots=True)
class ChangeProposal(DomainObject):
    """联合基线更新提议：责任方全部确认后才能由专班批准。"""

    proposal_id: str
    base_revision: int
    submitted_by: str
    submitted_at: str
    changes: tuple[Change, ...]
    affected_projects: tuple[str, ...]
    required_parties: tuple[str, ...]
    confirmations: tuple[Confirmation, ...] = field(default_factory=tuple)
    status: ProposalStatus = ProposalStatus.OPEN
    idempotency_key: str | None = None
    selected_scenario_id: str | None = None
    rationale: str = ""
    decided_at: str | None = None
    decider: str | None = None
    decision_comment: str = ""
    # 提议时受影响实体的指纹快照，用于批准时发现并发覆盖
    base_fingerprints: tuple[tuple[str, str], ...] = field(default_factory=tuple)


@dataclass(frozen=True, slots=True)
class BaselineRevision(DomainObject):
    revision: int
    approved_at: str
    approver: str
    proposal_id: str  # 初始基线为 "INITIAL"
    entities_fingerprint: str
    comment: str = ""


@dataclass(frozen=True, slots=True)
class MonthlyConclusion(DomainObject):
    """已发布的月度结论：不可变快照，按月份唯一，长期可追溯。"""

    publication_id: str
    month: str  # YYYY-MM
    published_at: str
    publisher: str
    baseline_revision: int
    as_of: str
    content_fingerprint: str
    document: dict[str, Any]


def stable_identity(items: Iterable[DomainObject]) -> list[DomainObject]:
    """按业务标识排序去重；同标识不同内容直接报错（与基础契约一致）。"""
    found: dict[str, DomainObject] = {}
    for item in items:
        key = getattr(item, "code")
        previous = found.get(key)
        if previous is not None and previous.fingerprint() != item.fingerprint():
            raise ValueError(f"业务标识 {key} 对应的内容发生冲突")
        found[key] = item
    return [found[key] for key in sorted(found)]
