"""六网联动关键链调度引擎。

纯函数式计算，输入不可变快照，输出完整排程结论：

- Tarjan 强连通分量识别硬循环（阻断）、依赖当前豁免放行的潜在循环、可替代边构成的弱循环；
- 前向计算（FS+lag、可替代“或”组、许可门、豁免到期翻转、延误里程碑）；
- 资源日历识别同一共享资源的超额占用，并按确定性优先级做串行化平衡；
- 后向计算总时差，输出跨网关键链；
- 增量重算：只对触发点的下游闭包与被触及资源做前向重算，边界节点冻结；
- 表面正常研判（局部视图 vs 全链视图）与多方案牺牲对比。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date
from typing import Any

from .model import (
    Commitment,
    Dependency,
    DependencyKind,
    Milestone,
    MilestoneState,
    PermitStatus,
    ResourceBooking,
    SharedResource,
)

MAX_LEVEL_ITERATIONS = 60


# --------------------------------------------------------------------------- 结果对象


@dataclass(frozen=True, slots=True)
class MilestoneSchedule:
    code: str
    project_code: str
    start: date | None
    finish: date | None
    late_finish: date | None
    slack_days: int | None
    critical: bool
    completed: bool
    driven_by: str | None  # 决定最早开始的约束：边编码 / PERMIT:xxx / RESOURCE / PROJECT_START / OVERDUE
    chosen_alternative: str | None
    blocked_reasons: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class CommitmentForecast:
    code: str
    project_code: str
    title: str
    promised_date: date
    weight: int
    target_milestone: str | None
    forecast_date: date | None
    delay_days: int | None
    status: str  # MET / BREACHED / BLOCKED


@dataclass(frozen=True, slots=True)
class OverloadEvent:
    resource_code: str
    day: date
    demand: float
    capacity: float
    booking_codes: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class LeveledBooking:
    code: str
    resource_code: str
    milestone_code: str
    project_code: str
    start: date
    finish: date
    displaced_days: int


@dataclass(frozen=True, slots=True)
class CycleReport:
    blocking: tuple[tuple[str, ...], ...] = ()  # 硬循环：当前即阻断
    latent: tuple[tuple[str, ...], ...] = ()  # 潜在循环：豁免到期后将变成硬循环
    alternative: tuple[tuple[str, ...], ...] = ()  # 含可替代边的弱循环（提示）


@dataclass(frozen=True, slots=True)
class ExemptionStatus:
    dependency_code: str
    upstream: str
    downstream: str
    exempt_until: date
    active_now: bool  # True=豁免生效中（临时放行）；False=已到期恢复硬阻断
    days_to_expiry: int


@dataclass(frozen=True, slots=True)
class RecomputeReport:
    trigger_milestones: tuple[str, ...]
    trigger_resources: tuple[str, ...]
    scope_size: int
    total_milestones: int
    frozen_boundary: tuple[str, ...]
    changed_forecasts: tuple[str, ...]
    leveled_resources: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class ScheduleResult:
    revision: int
    as_of: date
    milestones: dict[str, MilestoneSchedule]
    project_forecasts: dict[str, date | None]
    commitments: list[CommitmentForecast]
    overloads: list[OverloadEvent]
    leveled_bookings: list[LeveledBooking]
    cycles: CycleReport
    exemptions: list[ExemptionStatus]
    critical_chains: dict[str, list[str]]
    program_chain: list[str]
    warnings: tuple[str, ...]
    recompute: RecomputeReport | None = None

    def to_dict(self) -> dict[str, Any]:
        def _d(value: Any) -> Any:
            if isinstance(value, date):
                return value.isoformat()
            if isinstance(value, dict):
                return {k: _d(v) for k, v in value.items()}
            if isinstance(value, (list, tuple)):
                return [_d(v) for v in value]
            if hasattr(value, "__dataclass_fields__"):
                return {k: _d(getattr(value, k)) for k in value.__dataclass_fields__}
            return value

        return _d(self)


# --------------------------------------------------------------------------- 快照


@dataclass(frozen=True, slots=True)
class Snapshot:
    revision: int
    entities_fingerprint: str
    projects: dict[str, Any]
    milestones: dict[str, Milestone]
    dependencies: dict[str, Dependency]
    resources: dict[str, SharedResource]
    bookings: dict[str, ResourceBooking]
    permits: dict[str, Any]
    commitments: dict[str, Commitment]


# --------------------------------------------------------------------------- 图工具


def _tarjan_scc(nodes: set[str], edges: dict[str, list[str]]) -> list[list[str]]:
    index = 0
    stack: list[str] = []
    on_stack: set[str] = set()
    indices: dict[str, int] = {}
    low: dict[str, int] = {}
    result: list[list[str]] = []

    def strong(v: str) -> None:
        nonlocal index

        def walk(start: str) -> None:
            nonlocal index
            work: list[tuple[str, int]] = [(start, 0)]
            indices[start] = low[start] = index
            index += 1
            stack.append(start)
            on_stack.add(start)
            while work:
                u, pi = work[-1]
                neighbors = edges.get(u, [])
                if pi < len(neighbors):
                    work[-1] = (u, pi + 1)
                    w = neighbors[pi]
                    if w not in indices:
                        indices[w] = low[w] = index
                        index += 1
                        stack.append(w)
                        on_stack.add(w)
                        work.append((w, 0))
                    elif w in on_stack:
                        low[u] = min(low[u], indices[w])
                else:
                    work.pop()
                    if low[u] == indices[u]:
                        comp: list[str] = []
                        while True:
                            w = stack.pop()
                            on_stack.discard(w)
                            comp.append(w)
                            if w == u:
                                break
                        result.append(comp)
                    if work:
                        p = work[-1][0]
                        low[p] = min(low[p], low[u])

        walk(v)

    for v in sorted(nodes):
        if v not in indices:
            strong(v)
    return [sorted(c) for c in result if len(c) > 1 or (len(c) == 1 and c[0] in edges.get(c[0], []))]


def _downstream_closure(seeds: set[str], succ: dict[str, list[str]]) -> set[str]:
    seen: set[str] = set()
    queue = list(seeds)
    while queue:
        node = queue.pop()
        if node in seen:
            continue
        seen.add(node)
        queue.extend(succ.get(node, []))
    return seen


# --------------------------------------------------------------------------- 主计算


def compute_schedule(snapshot: Snapshot, as_of: date) -> ScheduleResult:
    return _compute(snapshot, as_of)


def _compute(
    snapshot: Snapshot,
    as_of: date,
    *,
    scope: set[str] | None = None,
    frozen: dict[str, MilestoneSchedule] | None = None,
    frozen_booking_starts: dict[str, date] | None = None,
    level_resources: set[str] | None = None,
) -> ScheduleResult:
    milestones = snapshot.milestones
    deps = snapshot.dependencies
    frozen = frozen or {}
    frozen_booking_starts = frozen_booking_starts or {}
    if level_resources is None:
        level_resources = set(snapshot.resources)
    if scope is None:
        scope = set(milestones)

    # --- 边分类（按 as_of 决定豁免是否放行）---
    and_preds: dict[str, list[Dependency]] = {}
    alt_preds: dict[str, dict[str, list[Dependency]]] = {}
    waived_exemptions: list[Dependency] = []
    expired_exemptions: list[Dependency] = []
    active_succ: dict[str, list[str]] = {}
    and_succ: dict[str, list[str]] = {}
    hard_exempt_succ: dict[str, list[str]] = {}
    all_succ: dict[str, list[str]] = {}

    for dep in sorted(deps.values(), key=lambda d: d.code):
        all_succ.setdefault(dep.upstream, []).append(dep.downstream)
        if dep.kind is DependencyKind.EXEMPTION:
            hard_exempt_succ.setdefault(dep.upstream, []).append(dep.downstream)
        active = True
        if dep.kind is DependencyKind.EXEMPTION:
            if as_of <= dep.exempt_until:
                active = False
                waived_exemptions.append(dep)
            else:
                expired_exemptions.append(dep)
        if active:
            active_succ.setdefault(dep.upstream, []).append(dep.downstream)
            if dep.kind is DependencyKind.ALTERNATIVE:
                alt_preds.setdefault(dep.downstream, {}).setdefault(
                    dep.alternative_group, []
                ).append(dep)
            else:
                and_preds.setdefault(dep.downstream, []).append(dep)
                and_succ.setdefault(dep.upstream, []).append(dep.downstream)
        # 非豁免的硬边在“全部豁免到期”假想图中同样存在
        if dep.kind is DependencyKind.HARD:
            hard_exempt_succ.setdefault(dep.upstream, []).append(dep.downstream)

    # --- 循环检测 ---
    cycles = _detect_cycles(set(milestones), and_succ, hard_exempt_succ, all_succ, deps)

    # --- 许可门（驳回为硬阻断，待批为日期门）---
    permit_gate: dict[str, date] = {}
    permit_reject: dict[str, str] = {}
    permit_late_warning: list[str] = []
    for permit in snapshot.permits.values():
        targets: list[str] = []
        if permit.milestone_code:
            targets.append(permit.milestone_code)
        else:
            targets = [
                m.code for m in milestones.values() if m.project_code == permit.project_code
            ]
        for mc in targets:
            if mc not in milestones:
                continue
            if permit.status is PermitStatus.REJECTED:
                permit_reject[mc] = f"监管许可 {permit.code} 已驳回"
            elif permit.status is PermitStatus.PENDING:
                prev = permit_gate.get(mc)
                if prev is None or permit.planned_date > prev:
                    permit_gate[mc] = permit.planned_date
                if as_of > permit.planned_date:
                    permit_late_warning.append(permit.code)

    # 阻断集合：硬循环节点 + 被驳回许可的里程碑，再统一沿硬边传播
    blocked: dict[str, str] = {}
    blocking_nodes = {n for comp in cycles.blocking for n in comp}
    for node in blocking_nodes:
        blocked[node] = "处于硬循环依赖"
    for node, reason in permit_reject.items():
        blocked.setdefault(node, reason)
    queue = list(blocked)
    while queue:
        u = queue.pop()
        # 阻断只沿硬阻断/已到期豁免边传播；可替代边存在其他路径，不扩散阻断
        for v in and_succ.get(u, []):
            if v not in blocked:
                blocked[v] = f"上游 {u} 被阻断"
                queue.append(v)

    # --- 前向 + 资源平衡不动点 ---
    starts: dict[str, date] = {}
    finishes: dict[str, date] = {}
    net_starts: dict[str, date] = {}
    driven: dict[str, str] = {}
    chosen_alt: dict[str, str] = {}
    resource_gate: dict[str, date] = {}
    overloads: list[OverloadEvent] = []
    leveled: list[LeveledBooking] = []

    # 冻结节点直接沿用
    for code, fs in frozen.items():
        if code in scope and fs.start is not None and fs.finish is not None:
            starts[code] = fs.start
            finishes[code] = fs.finish
            net_starts[code] = fs.start

    for iteration in range(MAX_LEVEL_ITERATIONS + 1):
        prev_starts = dict(starts)
        starts.clear()
        finishes.clear()
        net_starts.clear()
        driven.clear()
        chosen_alt.clear()
        for code, fs in frozen.items():
            if code in scope and fs.start is not None and fs.finish is not None:
                starts[code] = fs.start
                finishes[code] = fs.finish
                net_starts[code] = fs.start

        order = _topo_order(scope, blocked, active_succ, and_preds, alt_preds, frozen)
        for code in order:
            m = milestones[code]
            project = snapshot.projects[m.project_code]
            if m.state is MilestoneState.COMPLETED:
                finishes[code] = m.actual_finish
                starts[code] = date.fromordinal(
                    m.actual_finish.toordinal() - m.duration_days
                )
                net_starts[code] = starts[code]
                driven[code] = "ACTUAL"
                continue
            es = project.start_date
            why = "PROJECT_START"
            for dep in and_preds.get(code, []):
                if dep.upstream not in finishes:
                    continue  # 上游被阻断，当前节点也会在阻断传播中
                candidate = date.fromordinal(finishes[dep.upstream].toordinal() + dep.lag_days)
                if candidate > es:
                    es, why = candidate, dep.code
            for group, edges in alt_preds.get(code, {}).items():
                ready = [
                    (date.fromordinal(finishes[e.upstream].toordinal() + e.lag_days), e)
                    for e in edges
                    if e.upstream in finishes
                ]
                if ready:
                    candidate, chosen = min(ready, key=lambda x: (x[0], x[1].code))
                    chosen_alt[f"{code}|{group}"] = chosen.code
                    if candidate > es:
                        es, why = candidate, chosen.code
            if code in permit_gate and permit_gate[code] > es:
                es, why = permit_gate[code], f"PERMIT:{code}"
            # 网络最早开始（不含资源门），供资源排队作为意图
            net_es = es
            net_starts[code] = net_es
            gate = resource_gate.get(code)
            if gate is not None and gate > es:
                es, why = gate, "RESOURCE"
            # 延误里程碑：早应完工却未完成，按完整剩余工期从 as_of 重新预测
            earliest_finish_ord = es.toordinal() + m.duration_days
            if earliest_finish_ord < as_of.toordinal():
                es, why = as_of, "OVERDUE"
            starts[code] = es
            finishes[code] = date.fromordinal(es.toordinal() + m.duration_days)
            driven[code] = why

        new_gate: dict[str, date] = {}
        new_overloads: list[OverloadEvent] = []
        new_leveled: list[LeveledBooking] = []
        for res_code, resource in sorted(snapshot.resources.items()):
            res_bookings = [b for b in snapshot.bookings.values() if b.resource_code == res_code]
            if not res_bookings:
                continue
            # 预约意图对齐“无资源门”的网络最早开始；冻结预约沿用既有排程
            intent_start: dict[str, date] = {}
            for b in res_bookings:
                if b.code in frozen_booking_starts:
                    intent_start[b.code] = frozen_booking_starts[b.code]
                elif b.milestone_code in net_starts:
                    intent_start[b.code] = net_starts[b.milestone_code]
            if res_code in level_resources:
                # 超额占用：平衡前的原始日历
                raw_load: dict[int, float] = {}
                raw_members: dict[int, list[str]] = {}
                for b in res_bookings:
                    s = intent_start.get(b.code)
                    if s is None:
                        continue
                    for off in range(b.duration_days):
                        day = s.toordinal() + off
                        raw_load[day] = raw_load.get(day, 0.0) + b.demand
                        raw_members.setdefault(day, []).append(b.code)
                for day_ord, demand in sorted(raw_load.items()):
                    if demand > resource.capacity_per_day + 1e-9:
                        new_overloads.append(
                            OverloadEvent(
                                resource_code=res_code,
                                day=date.fromordinal(day_ord),
                                demand=round(demand, 3),
                                capacity=resource.capacity_per_day,
                                booking_codes=tuple(sorted(raw_members[day_ord])),
                            )
                        )
                # 确定性优先级串行化
                load: dict[int, float] = {}
                ordered = sorted(
                    res_bookings,
                    key=lambda b: (
                        intent_start.get(b.code, date.max),
                        b.priority,
                        b.code,
                    ),
                )
                for b in ordered:
                    es_b = intent_start.get(b.code)
                    if es_b is None:
                        continue
                    candidate = es_b.toordinal()
                    while not _window_fits(load, candidate, b.duration_days, b.demand, resource.capacity_per_day):
                        candidate += 1
                    for off in range(b.duration_days):
                        day = candidate + off
                        load[day] = load.get(day, 0.0) + b.demand
                    placed = date.fromordinal(candidate)
                    displaced = candidate - es_b.toordinal()
                    new_leveled.append(
                        LeveledBooking(
                            code=b.code,
                            resource_code=res_code,
                            milestone_code=b.milestone_code,
                            project_code=b.project_code,
                            start=placed,
                            finish=date.fromordinal(candidate + b.duration_days),
                            displaced_days=displaced,
                        )
                    )
                    if displaced > 0:
                        ms = b.milestone_code
                        if ms in scope and (ms not in new_gate or placed > new_gate[ms]):
                            new_gate[ms] = placed
            else:
                # 未触及资源：冻结既有排程
                for b in res_bookings:
                    s = frozen_booking_starts.get(b.code) or intent_start.get(b.code)
                    if s is None:
                        continue
                    new_leveled.append(
                        LeveledBooking(
                            code=b.code,
                            resource_code=res_code,
                            milestone_code=b.milestone_code,
                            project_code=b.project_code,
                            start=s,
                            finish=date.fromordinal(s.toordinal() + b.duration_days),
                            displaced_days=0,
                        )
                    )
        converged = resource_gate == new_gate and all(
            prev_starts.get(c) == starts.get(c) for c in scope if c in starts
        )
        # 门控单调取最大值：资源排队只能向后推，保证不动点必然收敛
        merged_gate = dict(resource_gate)
        for key, value in new_gate.items():
            if value > merged_gate.get(key, date.min):
                merged_gate[key] = value
        resource_gate, overloads, leveled = merged_gate, new_overloads, new_leveled
        if converged:
            break
    else:
        raise RuntimeError("资源平衡未能在限定迭代内收敛")

    # --- 后向计算总时差 ---
    late_finish = {c: f for c, f in finishes.items()}
    project_end_for_backward: dict[str, str] = {}
    _end_by_project: dict[str, tuple[date, str]] = {}
    for code in finishes:
        pc = milestones[code].project_code
        pair = (finishes[code], code)
        if pc not in _end_by_project or pair > _end_by_project[pc]:
            _end_by_project[pc] = pair
    project_end_for_backward = {pc: pair[1] for pc, pair in _end_by_project.items()}
    order_codes = [c for c in _topo_order(scope, blocked, active_succ, and_preds, alt_preds, frozen)]
    for commitment in snapshot.commitments.values():
        target = commitment.milestone_code or project_end_for_backward.get(
            commitment.project_code
        )
        if target in late_finish:
            if commitment.promised_date < late_finish[target]:
                late_finish[target] = commitment.promised_date
    # 虚拟项目汇点：无活动后继的叶子节点最迟不晚于项目预测完工，
    # 使其支线总时差相对于项目整体终点计算
    for code, finish in list(finishes.items()):
        if active_succ.get(code):
            continue
        end_code = project_end_for_backward.get(milestones[code].project_code)
        if end_code is not None and end_code in finishes:
            bound = late_finish.get(end_code, finishes[end_code])
            if bound > late_finish[code]:
                late_finish[code] = bound
    for code in reversed(order_codes):
        m = milestones[code]
        ls_ord = late_finish[code].toordinal() - m.duration_days
        for dep in and_preds.get(code, []):
            if dep.upstream in late_finish:
                bound = date.fromordinal(ls_ord - dep.lag_days)
                if bound < late_finish[dep.upstream]:
                    late_finish[dep.upstream] = bound
        for group, edges in alt_preds.get(code, {}).items():
            chosen_code = chosen_alt.get(f"{code}|{group}")
            for dep in edges:
                if dep.code != chosen_code or dep.upstream not in late_finish:
                    continue
                bound = date.fromordinal(ls_ord - dep.lag_days)
                if bound < late_finish[dep.upstream]:
                    late_finish[dep.upstream] = bound

    schedules: dict[str, MilestoneSchedule] = {}
    for code, m in milestones.items():
        if code in frozen:
            schedules[code] = frozen[code]
        elif code in blocked:
            schedules[code] = MilestoneSchedule(
                code=code,
                project_code=m.project_code,
                start=None,
                finish=None,
                late_finish=None,
                slack_days=None,
                critical=False,
                completed=m.state is MilestoneState.COMPLETED,
                driven_by=None,
                chosen_alternative=None,
                blocked_reasons=(blocked[code],),
            )
        elif code in finishes:
            slack = late_finish[code].toordinal() - finishes[code].toordinal()
            schedules[code] = MilestoneSchedule(
                code=code,
                project_code=m.project_code,
                start=starts[code],
                finish=finishes[code],
                late_finish=late_finish[code],
                slack_days=slack,
                critical=slack <= 0,
                completed=m.state is MilestoneState.COMPLETED,
                driven_by=driven.get(code),
                chosen_alternative=_chosen_alt_for(chosen_alt, code),
            )
        else:
            schedules[code] = MilestoneSchedule(
                code=code,
                project_code=m.project_code,
                start=None,
                finish=None,
                late_finish=None,
                slack_days=None,
                critical=False,
                completed=False,
                driven_by=None,
                chosen_alternative=None,
                blocked_reasons=("上游被阻断",),
            )

    return _assemble(
        snapshot,
        as_of,
        schedules,
        overloads,
        leveled,
        cycles,
        waived_exemptions,
        expired_exemptions,
        and_preds,
        alt_preds,
        chosen_alt,
        permit_late_warning,
    )


def _window_fits(load: dict[int, float], start_ord: int, duration: int, demand: float, cap: float) -> bool:
    return all(load.get(start_ord + off, 0.0) + demand <= cap + 1e-9 for off in range(duration))


def _topo_order(
    scope: set[str],
    blocked: dict[str, str],
    active_succ: dict[str, list[str]],
    and_preds: dict[str, list[Dependency]],
    alt_preds: dict[str, dict[str, list[Dependency]]],
    frozen: dict[str, MilestoneSchedule],
) -> list[str]:
    remaining = {n for n in scope if n not in blocked}

    def resolved(pred: str) -> bool:
        if pred in blocked:
            return False  # 被阻断的前置不提供就绪
        # 已处理（离开 remaining）或冻结边界有确定完工日
        return pred not in remaining or (
            pred in frozen and frozen[pred].finish is not None
        )

    def ready(node: str) -> bool:
        for dep in and_preds.get(node, []):
            if dep.upstream in remaining and not resolved(dep.upstream):
                return False
        for edges in alt_preds.get(node, {}).values():
            if not any(resolved(e.upstream) for e in edges):
                return False
        return True

    queue = sorted(n for n in remaining if ready(n))
    order: list[str] = []
    while queue:
        node = queue.pop(0)
        if node not in remaining:
            continue
        order.append(node)
        remaining.discard(node)
        newly = [v for v in active_succ.get(node, []) if v in remaining and ready(v)]
        queue.extend(newly)
        queue.sort()
    # 仍留在 remaining 的节点（如可替代组全部上游被阻断）不在序中，最终按阻断处理
    return order


def _detect_cycles(
    nodes: set[str],
    and_succ: dict[str, list[str]],
    hard_exempt_succ: dict[str, list[str]],
    all_succ: dict[str, list[str]],
    deps: dict[str, Dependency],
) -> CycleReport:
    # 硬循环：仅当前生效的硬阻断/到期豁免边
    blocking = tuple(tuple(c) for c in _tarjan_scc(nodes, and_succ))
    blocking_flat = {n for comp in blocking for n in comp}
    # 潜在循环：假设全部豁免到期后的硬边图中出现、当前尚不存在的环
    latent: list[tuple[str, ...]] = []
    for comp in _tarjan_scc(nodes, hard_exempt_succ):
        if any(n in blocking_flat for n in comp):
            continue
        latent.append(tuple(comp))
    # 弱循环：仅靠可替代边才闭合的环（同组“或”关系不会真正阻断，但提示建模风险）
    alt_edges = {
        (d.upstream, d.downstream)
        for d in deps.values()
        if d.kind is DependencyKind.ALTERNATIVE
    }
    latent_flat = {n for comp in latent for n in comp}
    alternative: list[tuple[str, ...]] = []
    for comp in _tarjan_scc(nodes, all_succ):
        comp_set = set(comp)
        if any(n in blocking_flat or n in latent_flat for n in comp):
            continue
        uses_alt = any(
            (u, v) in alt_edges for u in comp_set for v in all_succ.get(u, []) if v in comp_set
        )
        if uses_alt:
            alternative.append(tuple(comp))
    return CycleReport(
        blocking=tuple(sorted(blocking)),
        latent=tuple(sorted(latent)),
        alternative=tuple(sorted(alternative)),
    )


def _chosen_alt_for(chosen_alt: dict[str, str], code: str) -> str | None:
    picks = [v for k, v in chosen_alt.items() if k.startswith(f"{code}|")]
    return picks[0] if len(picks) == 1 else (picks[0] if picks else None)


def _assemble(
    snapshot: Snapshot,
    as_of: date,
    schedules: dict[str, MilestoneSchedule],
    overloads: list[OverloadEvent],
    leveled: list[LeveledBooking],
    cycles: CycleReport,
    waived: list[Dependency],
    expired: list[Dependency],
    and_preds: dict[str, list[Dependency]],
    alt_preds: dict[str, dict[str, list[Dependency]]],
    chosen_alt: dict[str, str],
    permit_late_warning: list[str],
) -> ScheduleResult:
    # 项目预测完工
    project_finish: dict[str, date | None] = {}
    project_end_node: dict[str, str] = {}
    by_project: dict[str, list[str]] = {}
    for code, ms in schedules.items():
        by_project.setdefault(ms.project_code, []).append(code)
    for project_code, codes in by_project.items():
        datable = [(schedules[c].finish, c) for c in codes if schedules[c].finish is not None]
        if not datable:
            project_finish[project_code] = None
            continue
        finish, end_code = max(datable, key=lambda x: (x[0], x[1]))
        project_finish[project_code] = finish
        project_end_node[project_code] = end_code

    # 承诺预测
    forecasts: list[CommitmentForecast] = []
    for commitment in sorted(snapshot.commitments.values(), key=lambda c: c.code):
        if commitment.milestone_code:
            target = commitment.milestone_code
            ms = schedules.get(target)
            forecast = ms.finish if ms else None
        else:
            target = project_end_node.get(commitment.project_code)
            forecast = project_finish.get(commitment.project_code)
        if forecast is None:
            status, delay = "BLOCKED", None
        elif forecast <= commitment.promised_date:
            status, delay = "MET", 0
        else:
            status, delay = "BREACHED", (forecast - commitment.promised_date).days
        forecasts.append(
            CommitmentForecast(
                code=commitment.code,
                project_code=commitment.project_code,
                title=commitment.title,
                promised_date=commitment.promised_date,
                weight=commitment.weight,
                target_milestone=target,
                forecast_date=forecast,
                delay_days=delay,
                status=status,
            )
        )

    # 关键链：零时差节点上沿“实际驱动边”求最长路径
    critical_chains, program_chain = _critical_chains(
        snapshot, schedules, and_preds, alt_preds, chosen_alt, project_end_node
    )

    exemptions: list[ExemptionStatus] = []
    for dep in sorted(waived + expired, key=lambda d: d.code):
        active_now = dep in waived
        exemptions.append(
            ExemptionStatus(
                dependency_code=dep.code,
                upstream=dep.upstream,
                downstream=dep.downstream,
                exempt_until=dep.exempt_until,
                active_now=active_now,
                days_to_expiry=(dep.exempt_until - as_of).days,
            )
        )

    warnings = list(permit_late_warning and [f"许可超期未获批: {c}" for c in sorted(set(permit_late_warning))])

    return ScheduleResult(
        revision=snapshot.revision,
        as_of=as_of,
        milestones=schedules,
        project_forecasts=project_finish,
        commitments=forecasts,
        overloads=sorted(overloads, key=lambda o: (o.resource_code, o.day)),
        leveled_bookings=sorted(leveled, key=lambda b: (b.resource_code, b.start, b.code)),
        cycles=cycles,
        exemptions=exemptions,
        critical_chains=critical_chains,
        program_chain=program_chain,
        warnings=tuple(warnings),
    )


def _critical_chains(
    snapshot: Snapshot,
    schedules: dict[str, MilestoneSchedule],
    and_preds: dict[str, list[Dependency]],
    alt_preds: dict[str, dict[str, list[Dependency]]],
    chosen_alt: dict[str, str],
    project_end_node: dict[str, str],
) -> tuple[dict[str, list[str]], list[str]]:
    # 仅保留两端零时差的驱动边
    pred_edge: dict[str, tuple[str, str]] = {}  # node -> (pred, edge_code)
    for node, ms in schedules.items():
        if not ms.critical or ms.driven_by in (None, "PROJECT_START", "OVERDUE", "ACTUAL", "RESOURCE"):
            continue
        if ms.driven_by and ms.driven_by.startswith("PERMIT:"):
            continue
        dep = snapshot.dependencies.get(ms.driven_by)
        if dep is None:
            continue
        pred_ms = schedules.get(dep.upstream)
        if pred_ms is not None and pred_ms.critical:
            pred_edge[node] = (dep.upstream, dep.code)

    succ_map: dict[str, list[str]] = {}
    for node, (pred, _edge) in pred_edge.items():
        succ_map.setdefault(pred, []).append(node)

    def chain_to(node: str) -> list[str]:
        pred = pred_edge.get(node, (None,))[0]
        if pred is None:
            return [node]
        return chain_to(pred) + [node]

    chains: dict[str, list[str]] = {}
    best_global: list[str] = []
    commitment_targets = {c.milestone_code for c in snapshot.commitments.values() if c.milestone_code}
    for project_code, end in sorted(project_end_node.items()):
        ms = schedules.get(end)
        if ms is not None and ms.critical:
            chains[project_code] = chain_to(end)
    candidates = list(chains.values())
    for target in commitment_targets:
        ms = schedules.get(target)
        if ms is not None and ms.critical:
            candidates.append(chain_to(target))
    if candidates:
        def weight(chain: list[str]) -> int:
            return sum(snapshot.milestones[c].duration_days for c in chain if c in snapshot.milestones)

        best_global = max(candidates, key=lambda ch: (weight(ch), len(ch), ch))
    return chains, best_global


# --------------------------------------------------------------------------- 增量重算


@dataclass(frozen=True, slots=True)
class Trigger:
    milestone_codes: frozenset[str] = frozenset()
    resource_codes: frozenset[str] = frozenset()
    dependency_codes: frozenset[str] = frozenset()
    reasons: tuple[str, ...] = ()


def derive_triggers(
    old: Snapshot,
    new: Snapshot,
    old_as_of: date | None = None,
    new_as_of: date | None = None,
) -> Trigger:
    """对比两个快照与两个 as_of，自动派生需要重算的触发点。"""
    seeds: set[str] = set()
    resources: set[str] = set()
    reasons: list[str] = []

    for code, m in new.milestones.items():
        old_m = old.milestones.get(code)
        if old_m is None or old_m.fingerprint() != m.fingerprint():
            seeds.add(code)
            reasons.append(f"里程碑变更: {code}")
    for code in set(old.milestones) - set(new.milestones):
        reasons.append(f"里程碑删除: {code}")
        seeds.update(old.milestones[code].project_code and [code])

    for code, dep in new.dependencies.items():
        old_dep = old.dependencies.get(code)
        flipped = False
        if old_dep is None or old_dep.fingerprint() != dep.fingerprint():
            reasons.append(f"前置条件变更: {code}")
            flipped = True
        if (
            old_as_of
            and new_as_of
            and dep.kind is DependencyKind.EXEMPTION
            and (old_as_of <= dep.exempt_until) != (new_as_of <= dep.exempt_until)
        ):
            reasons.append(f"豁免状态翻转: {code}（到期日 {dep.exempt_until}）")
            flipped = True
        if flipped:
            # 豁免恢复为硬阻断会同时改变下游约束与上游排序，两端都纳入触发域
            seeds.add(dep.downstream)
            seeds.add(dep.upstream)
    for code, dep in old.dependencies.items():
        if code not in new.dependencies:
            seeds.add(dep.downstream)
            reasons.append(f"前置条件删除: {code}")

    for code, project in new.projects.items():
        old_project = old.projects.get(code)
        if old_project is None or old_project.fingerprint() != project.fingerprint():
            seeds.update(m.code for m in new.milestones.values() if m.project_code == code)
            reasons.append(f"项目锚点变更: {code}")

    for code, permit in new.permits.items():
        old_permit = old.permits.get(code)
        if old_permit is None or old_permit.fingerprint() != permit.fingerprint():
            if permit.milestone_code:
                seeds.add(permit.milestone_code)
            else:
                seeds.update(m.code for m in new.milestones.values() if m.project_code == permit.project_code)
            reasons.append(f"监管许可变更: {code}")

    for code, booking in new.bookings.items():
        old_booking = old.bookings.get(code)
        if old_booking is None or old_booking.fingerprint() != booking.fingerprint():
            resources.add(booking.resource_code)
            seeds.add(booking.milestone_code)
            reasons.append(f"资源预约变更: {code}")
    for code, resource in new.resources.items():
        old_resource = old.resources.get(code)
        if old_resource is None or old_resource.fingerprint() != resource.fingerprint():
            resources.add(code)
            reasons.append(f"共享资源变更: {code}")

    return Trigger(
        milestone_codes=frozenset(seeds),
        resource_codes=frozenset(resources),
        dependency_codes=frozenset(),
        reasons=tuple(sorted(set(reasons))),
    )


def affected_scope(snapshot: Snapshot, trigger: Trigger) -> tuple[set[str], set[str]]:
    """返回（受影响里程碑闭包, 受影响资源）。

    影响传播沿三类通道做不动点扩张：
    1. 任意前置边（含可替代边）的下游；
    2. 触发资源上全部预约所属里程碑；
    3. 受影响里程碑所持预约落在的其他资源（排队可跨资源级联）。
    """
    succ: dict[str, list[str]] = {}
    for dep in snapshot.dependencies.values():
        succ.setdefault(dep.upstream, []).append(dep.downstream)

    resource_to_milestones: dict[str, set[str]] = {}
    milestone_to_resources: dict[str, set[str]] = {}
    for b in snapshot.bookings.values():
        resource_to_milestones.setdefault(b.resource_code, set()).add(b.milestone_code)
        milestone_to_resources.setdefault(b.milestone_code, set()).add(b.resource_code)

    scope: set[str] = set()
    touched: set[str] = set(trigger.resource_codes)
    seeds = (set(trigger.milestone_codes) | set(trigger.dependency_codes)) & set(snapshot.milestones)
    while True:
        expanded = _downstream_closure(
            seeds | {m for r in touched for m in resource_to_milestones.get(r, set())}, succ
        )
        new_resources = {
            r for m in expanded for r in milestone_to_resources.get(m, set())
        } | set(trigger.resource_codes)
        if expanded == scope and new_resources <= touched:
            scope = expanded
            break
        scope, seeds, touched = expanded, expanded, new_resources
    # 只有确实含受影响预约的资源才需要重新平衡
    level_resources = {
        r for r in touched if resource_to_milestones.get(r, set()) & scope
    }
    return scope, level_resources


def recompute_incremental(
    snapshot: Snapshot,
    as_of: date,
    previous: ScheduleResult,
    trigger: Trigger,
) -> ScheduleResult:
    """只重算触发点下游闭包；闭包外节点沿用上次结果（边界冻结）。"""
    scope, touched_resources = affected_scope(snapshot, trigger)
    frozen = {
        code: ms for code, ms in previous.milestones.items() if code not in scope
    }
    frozen_booking_starts = {
        b.code: b.start for b in previous.leveled_bookings if b.resource_code not in touched_resources
    }
    result = _compute(
        snapshot,
        as_of,
        scope=scope,
        frozen=frozen,
        frozen_booking_starts=frozen_booking_starts,
        level_resources=touched_resources,
    )
    # 未触及资源的既有超载结论原样继承
    inherited_overloads = [o for o in previous.overloads if o.resource_code not in touched_resources]
    overloads = sorted(result.overloads + inherited_overloads, key=lambda o: (o.resource_code, o.day))
    result = ScheduleResult(
        revision=result.revision,
        as_of=result.as_of,
        milestones=result.milestones,
        project_forecasts=result.project_forecasts,
        commitments=result.commitments,
        overloads=overloads,
        leveled_bookings=result.leveled_bookings,
        cycles=result.cycles,
        exemptions=result.exemptions,
        critical_chains=result.critical_chains,
        program_chain=result.program_chain,
        warnings=result.warnings,
    )
    boundary = sorted(
        {
            dep.upstream
            for dep in snapshot.dependencies.values()
            if dep.downstream in scope and dep.upstream not in scope
        }
    )
    changed = sorted(
        code
        for code in scope
        if (old := previous.milestones.get(code)) is not None
        and old.finish != result.milestones[code].finish
    )
    report = RecomputeReport(
        trigger_milestones=tuple(sorted(trigger.milestone_codes)),
        trigger_resources=tuple(sorted(touched_resources)),
        scope_size=len(scope),
        total_milestones=len(snapshot.milestones),
        frozen_boundary=tuple(boundary),
        changed_forecasts=tuple(changed),
        leveled_resources=tuple(sorted(touched_resources)),
    )
    return ScheduleResult(
        revision=result.revision,
        as_of=result.as_of,
        milestones=result.milestones,
        project_forecasts=result.project_forecasts,
        commitments=result.commitments,
        overloads=result.overloads,
        leveled_bookings=result.leveled_bookings,
        cycles=result.cycles,
        exemptions=result.exemptions,
        critical_chains=result.critical_chains,
        program_chain=result.program_chain,
        warnings=result.warnings,
        recompute=report,
    )


# --------------------------------------------------------------------------- 表面正常研判


@dataclass(frozen=True, slots=True)
class HiddenDelay:
    project_code: str
    commitment_code: str | None
    local_forecast: date | None
    full_forecast: date | None
    promised_date: date | None
    slip_days: int
    surface_status: str  # LOCAL_MET_FULL_BREACHED / LOCAL_MET_FULL_SLIPPED
    causes: tuple[str, ...]


def hidden_delay_analysis(snapshot: Snapshot, as_of: date, full: ScheduleResult) -> list[HiddenDelay]:
    """局部视图（忽略跨网前置与他网资源排队）对比全链视图。"""
    local_deps = {
        code: dep
        for code, dep in snapshot.dependencies.items()
        if snapshot.milestones[dep.upstream].project_code
        == snapshot.milestones[dep.downstream].project_code
    }
    local_bookings = {
        code: b
        for code, b in snapshot.bookings.items()
        if all(
            ob.project_code == b.project_code
            for ob in snapshot.bookings.values()
            if ob.resource_code == b.resource_code
        )
    }
    local_snapshot = Snapshot(
        revision=snapshot.revision,
        entities_fingerprint=snapshot.entities_fingerprint,
        projects=snapshot.projects,
        milestones=snapshot.milestones,
        dependencies=local_deps,
        resources=snapshot.resources,
        bookings=local_bookings,
        permits=snapshot.permits,
        commitments=snapshot.commitments,
    )
    local = compute_schedule(local_snapshot, as_of)

    results: list[HiddenDelay] = []
    for project in sorted(snapshot.projects):
        local_finish = local.project_forecasts.get(project)
        full_finish = full.project_forecasts.get(project)
        if local_finish is None or full_finish is None or full_finish <= local_finish:
            continue
        slip = (full_finish - local_finish).days
        project_commitments = [c for c in snapshot.commitments.values() if c.project_code == project]
        causes = _external_causes(snapshot, full, project, local, full_finish, local_finish)
        if project_commitments:
            for c in project_commitments:
                local_met = local_finish <= c.promised_date
                full_breached = full_finish > c.promised_date
                status = "LOCAL_MET_FULL_BREACHED" if local_met and full_breached else "LOCAL_MET_FULL_SLIPPED"
                if local_met:
                    results.append(
                        HiddenDelay(
                            project_code=project,
                            commitment_code=c.code,
                            local_forecast=local_finish,
                            full_forecast=full_finish,
                            promised_date=c.promised_date,
                            slip_days=slip,
                            surface_status=status,
                            causes=tuple(causes),
                        )
                    )
        else:
            results.append(
                HiddenDelay(
                    project_code=project,
                    commitment_code=None,
                    local_forecast=local_finish,
                    full_forecast=full_finish,
                    promised_date=None,
                    slip_days=slip,
                    surface_status="LOCAL_MET_FULL_SLIPPED",
                    causes=tuple(causes),
                )
            )
    return sorted(results, key=lambda h: (h.project_code, h.commitment_code or ""))


def _external_causes(
    snapshot: Snapshot,
    full: ScheduleResult,
    project: str,
    local: ScheduleResult,
    full_finish: date,
    local_finish: date,
) -> list[str]:
    causes: list[str] = []
    own = {m.code for m in snapshot.milestones.values() if m.project_code == project}
    # 跨网前置边
    for dep in snapshot.dependencies.values():
        if dep.downstream in own and dep.upstream not in own:
            up = full.milestones.get(dep.upstream)
            if up and up.finish is not None:
                up_project = snapshot.milestones[dep.upstream].project_code
                causes.append(
                    f"跨网前置 {dep.code}: {up_project}.{dep.upstream} 预计 {up.finish} 完成"
                    + ("（当前豁免放行中）" if dep.kind is DependencyKind.EXEMPTION else "")
                )
    # 共享资源被他网预约挤占
    local_starts = {b.code: b.start for b in local.leveled_bookings}
    for booking in full.leveled_bookings:
        if booking.project_code != project or booking.displaced_days <= 0:
            continue
        resource = snapshot.resources[booking.resource_code]
        blockers = [
            ob.code
            for ob in full.leveled_bookings
            if ob.resource_code == booking.resource_code
            and ob.project_code != project
            and ob.start <= booking.start
            and ob.finish > booking.start
        ]
        causes.append(
            f"共享资源 {resource.name}（{booking.resource_code}）排队 {booking.displaced_days} 天"
            + (f"，挤占方: {', '.join(sorted(blockers))}" if blockers else "")
        )
    return sorted(set(causes))


# --------------------------------------------------------------------------- 方案评估


@dataclass(frozen=True, slots=True)
class Scenario:
    scenario_id: str
    name: str
    description: str = ""
    forced_alternatives: dict[str, str] = field(default_factory=dict)  # downstream -> dependency_code
    booking_priorities: dict[str, int] = field(default_factory=dict)
    booking_durations: dict[str, int] = field(default_factory=dict)  # booking -> 压缩后占用天数
    extend_exemptions: dict[str, date] = field(default_factory=dict)  # dep_code -> 新到期日
    crash_durations: dict[str, int] = field(default_factory=dict)  # milestone -> 压缩后工期
    permit_assumed_granted: frozenset[str] = frozenset()


@dataclass(frozen=True, slots=True)
class ScenarioEvaluation:
    scenario_id: str
    name: str
    program_finish: date | None
    weighted_sacrifice: int
    sacrificed_commitments: tuple[CommitmentForecast, ...]
    commitments: tuple[CommitmentForecast, ...]
    overload_count: int
    program_chain: tuple[str, ...]
    schedule: ScheduleResult


def apply_scenario(snapshot: Snapshot, scenario: Scenario) -> Snapshot:
    import dataclasses

    dependencies = dict(snapshot.dependencies)
    for dep_code, new_until in scenario.extend_exemptions.items():
        dep = dependencies[dep_code]
        dependencies[dep_code] = dataclasses.replace(dep, exempt_until=new_until)

    milestones = dict(snapshot.milestones)
    for code, duration in scenario.crash_durations.items():
        milestones[code] = dataclasses.replace(milestones[code], duration_days=duration)

    bookings = dict(snapshot.bookings)
    for code, priority in scenario.booking_priorities.items():
        bookings[code] = dataclasses.replace(bookings[code], priority=priority)
    for code, duration in scenario.booking_durations.items():
        bookings[code] = dataclasses.replace(bookings[code], duration_days=duration)

    # 强制选择某条可替代边：把同组其他边临时改为“不参与”——用 lag 极大不可行，
    # 这里通过将同组其它边的下游临时断开实现：构造替代依赖集合
    deps = dict(dependencies)
    for downstream, chosen_code in scenario.forced_alternatives.items():
        chosen = deps[chosen_code]
        group = chosen.alternative_group
        for code, dep in list(deps.items()):
            if (
                dep.kind is DependencyKind.ALTERNATIVE
                and dep.downstream == downstream
                and dep.alternative_group == group
                and code != chosen_code
            ):
                deps.pop(code)

    permits = dict(snapshot.permits)
    for code in scenario.permit_assumed_granted:
        permit = permits[code]
        permits[code] = dataclasses.replace(
            permit, status=PermitStatus.GRANTED, granted_date=permit.planned_date
        )

    return Snapshot(
        revision=snapshot.revision,
        entities_fingerprint=snapshot.entities_fingerprint + ":scenario:" + scenario.scenario_id,
        projects=snapshot.projects,
        milestones=milestones,
        dependencies=deps,
        resources=snapshot.resources,
        bookings=bookings,
        permits=permits,
        commitments=snapshot.commitments,
    )


def evaluate_scenario(snapshot: Snapshot, as_of: date, scenario: Scenario) -> ScenarioEvaluation:
    scoped = apply_scenario(snapshot, scenario)
    result = compute_schedule(scoped, as_of)
    sacrificed = tuple(c for c in result.commitments if c.status == "BREACHED")
    cost = sum(c.weight * (c.delay_days or 0) for c in sacrificed)
    program_finish = max((f for f in result.project_forecasts.values() if f is not None), default=None)
    return ScenarioEvaluation(
        scenario_id=scenario.scenario_id,
        name=scenario.name,
        program_finish=program_finish,
        weighted_sacrifice=cost,
        sacrificed_commitments=sacrificed,
        commitments=tuple(result.commitments),
        overload_count=len(result.overloads),
        program_chain=tuple(result.program_chain),
        schedule=result,
    )


def compare_scenarios(
    snapshot: Snapshot, as_of: date, scenarios: list[Scenario]
) -> list[ScenarioEvaluation]:
    evaluations = [evaluate_scenario(snapshot, as_of, s) for s in scenarios]
    return sorted(
        evaluations,
        key=lambda e: (
            len(e.sacrificed_commitments),
            e.weighted_sacrifice,
            e.program_finish or date.max,
        ),
    )
