"""六网项目联动排程：题述场景端到端演示（内存运行，无需启动服务）。

演示链路：
  1. 登记三网项目、里程碑、硬阻断/限期豁免前置、共享道路窗口、监管许可与交付承诺；
  2. 基于已批准基线计算关键链，输出表面正常但被上游拖延的项目；
  3. 对比"维持现状 / 压缩变电站工期 / 道路窗口让位"三种解法牺牲的承诺；
  4. 推进到豁免到期日之后，只重算受影响范围并观察变电站被推迟。
"""

from __future__ import annotations

import json
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent / "src"))

from datetime import date

from program_scheduling.engine import Scenario
from program_scheduling.model import (
    Change,
    Commitment,
    Dependency,
    DependencyKind,
    Milestone,
    Permit,
    PermitStatus,
    Project,
    ResourceBooking,
    SharedResource,
)
from program_scheduling.service import ProgramService


def upsert(entity: str, obj) -> Change:
    return Change(op="UPSERT", entity=entity, data=obj.to_dict())


def build_changes() -> list[Change]:
    projects = [
        Project(code="P-POWER", name="变电站扩容", network="电网", owner_unit="电力公司",
                responsible_party="电力责任人", start_date=date(2026, 10, 1)),
        Project(code="P-COMP", name="算力通道", network="算力网", owner_unit="算力公司",
                responsible_party="算力责任人", start_date=date(2026, 10, 1)),
        Project(code="P-GALL", name="地下管廊迁改", network="管廊网", owner_unit="管廊公司",
                responsible_party="管廊责任人", start_date=date(2026, 10, 1)),
    ]
    milestones = [
        Milestone(code="SUB", project_code="P-POWER", name="变电站投运", duration_days=55),
        Milestone(code="COMP", project_code="P-COMP", name="算力通道贯通", duration_days=10),
        Milestone(code="GALL", project_code="P-GALL", name="管廊迁改完成", duration_days=20),
    ]
    dependencies = [
        Dependency(code="D-SUB-COMP", upstream="SUB", downstream="COMP", kind=DependencyKind.HARD),
        Dependency(code="D-GALL-SUB", upstream="GALL", downstream="SUB",
                   kind=DependencyKind.EXEMPTION, exempt_until=date(2026, 10, 15),
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
        Permit(code="L-COMP", project_code="P-COMP", authority="通信管理局",
               milestone_code="COMP", status=PermitStatus.PENDING,
               planned_date=date(2026, 11, 20)),
    ]
    commitments = [
        Commitment(code="C-COMP", project_code="P-COMP", title="算力通道投用",
                   promised_date=date(2026, 11, 30), weight=5),
        Commitment(code="C-SUB", project_code="P-POWER", title="变电站送电",
                   promised_date=date(2026, 11, 15), weight=3),
        Commitment(code="C-GALL", project_code="P-GALL", title="管廊回迁",
                   promised_date=date(2026, 12, 31), weight=2),
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


def print_step(title: str) -> None:
    print("\n" + "=" * 72)
    print(title)
    print("=" * 72)


def main() -> None:
    data_dir = Path(tempfile.mkdtemp(prefix="sixnet-demo-"))
    svc = ProgramService(str(data_dir))
    svc.establish_initial_baseline(approver="省级专班", changes=build_changes(),
                                   comment="六网联动初始联合基线")

    print_step("1. 基于已批准基线（修订版 1）的关键链排程  as_of=2026-10-01")
    result = svc.full_schedule(date(2026, 10, 1))
    for code in ("SUB", "COMP", "GALL"):
        ms = result.milestones[code]
        print(f"  {code:5s} {ms.start} → {ms.finish}  关键={ms.critical!s:5s} 驱动={ms.driven_by}")
    print(f"  道路窗口原始超载天数: {len(result.overloads)}")
    for booking in result.leveled_bookings:
        print(f"  平衡后预约 {booking.code}: {booking.start} → {booking.finish}"
              f"（排队 {booking.displaced_days} 天）")
    print(f"  全计划关键链: {' → '.join(result.program_chain)}")

    print_step("2. 表面正常、实际被上游拖延的项目（局部视图 vs 全链视图）")
    for item in svc.hidden_delays(date(2026, 10, 1)):
        print(f"  [{item.surface_status}] 项目 {item.project_code}"
              f"（承诺 {item.commitment_code}）")
        print(f"    局部预测={item.local_forecast}  全链预测={item.full_forecast}"
              f"  承诺日={item.promised_date}  滑移 {item.slip_days} 天")
        for cause in item.causes:
            print(f"    - {cause}")

    print_step("3. 多解法牺牲的承诺对比")
    scenarios = [
        Scenario(scenario_id="S0", name="维持现状"),
        Scenario(scenario_id="S1", name="压缩变电站工期至 35 天",
                 crash_durations={"SUB": 35}, booking_durations={"BK-SUB": 35}),
        Scenario(scenario_id="S2", name="道路窗口让位给管廊",
                 booking_priorities={"BK-SUB": 50, "BK-GALL": 10}),
    ]
    for rank, evaluation in enumerate(svc.evaluate_scenarios(date(2026, 10, 1), scenarios), 1):
        sacrificed = [f"{c.code}(延 {c.delay_days} 天)" for c in evaluation.sacrificed_commitments]
        print(f"  #{rank} {evaluation.scenario_id} {evaluation.name}")
        print(f"      全计划完工={evaluation.program_finish}  "
              f"牺牲={sacrificed or '无'}  加权代价={evaluation.weighted_sacrifice}")

    print_step("4. 豁免到期（2026-10-16）：只重算受影响范围")
    svc.full_schedule(date(2026, 10, 1))
    after = svc.schedule(date(2026, 10, 16))
    report = after.recompute
    print(f"  触发点={report.trigger_milestones}  重算节点数={report.scope_size}"
          f"/总节点 {report.total_milestones}")
    print(f"  预测发生变化的里程碑={report.changed_forecasts}")
    for code in ("GALL", "SUB", "COMP"):
        ms = after.milestones[code]
        print(f"  {code:5s} {ms.start} → {ms.finish}")

    print_step("5. 发布不可变月度结论并回溯批准过程")
    publication = svc.publish_monthly(
        month="2026-10", publisher="省级专班", as_of=date(2026, 10, 31)
    )
    print(f"  发布编号={publication.publication_id}  内容指纹={publication.content_fingerprint[:16]}…")
    print(f"  违约承诺数={len(publication.document['summary']['commitments_breached'])}")
    trail = svc.approval_trail()
    print(f"  批准链: {[t['proposal_id'] for t in trail]}")
    print(json.dumps({"demo": "ok", "baseline_revision": svc.repo.revision},
                     ensure_ascii=False))


if __name__ == "__main__":
    main()
