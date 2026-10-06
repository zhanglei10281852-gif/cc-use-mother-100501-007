"""六网联动排程 REST API（仅依赖标准库，可用 ThreadingHTTPServer 直接承载）。

路由：

- GET  /api/health
- POST /api/baseline/initial               建立初始联合基线
- GET  /api/baseline                       当前基线版本与实体清单
- GET  /api/entities/{kind}                按类型查询实体
- POST /api/proposals                      提交变更提议（自动识别责任方）
- GET  /api/proposals[/{id}]               查询提议
- POST /api/proposals/{id}/confirmations   责任方确认
- POST /api/proposals/{id}/approve         专班批准并入基线
- POST /api/proposals/{id}/reject          驳回
- GET  /api/schedule?as_of=&incremental=1  关键链排程结论
- POST /api/schedule/recompute             显式触发定向重算
- GET  /api/analysis/hidden-delays         表面正常却被上游拖延的项目
- POST /api/analysis/scenarios             多方案牺牲对比
- GET  /api/monthly[/{month}]              月度结论查询
- POST /api/monthly                        发布月度结论（不可变）
- GET  /api/audit/trail                    联合基线完整批准过程
"""

from __future__ import annotations

from datetime import date
import json
import re
from typing import Any, Callable
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from .engine import Trigger
from .model import Change
from .service import ENTITY_KINDS, ProgramService
from .store import ConcurrencyError, WorkflowError


def _jsonable(value: Any) -> Any:
    from datetime import datetime

    if isinstance(value, (date, datetime)):
        return value.isoformat()
    if hasattr(value, "to_dict"):
        return value.to_dict()
    if isinstance(value, dict):
        return {k: _jsonable(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_jsonable(v) for v in value]
    if hasattr(value, "__dataclass_fields__"):
        return {k: _jsonable(getattr(value, k)) for k in value.__dataclass_fields__}
    if isinstance(value, frozenset):
        return sorted(value)
    return value


class _Handler(BaseHTTPRequestHandler):
    service: ProgramService = None  # type: ignore[assignment]

    server_version = "SixNetSchedule/1.0"

    def log_message(self, fmt: str, *args: Any) -> None:
        return

    # ----- 基础收发 -----

    def _send(self, status: int, body: Any) -> None:
        data = json.dumps(_jsonable(body), ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def _read_json(self) -> dict[str, Any]:
        length = int(self.headers.get("Content-Length", "0"))
        if length == 0:
            return {}
        raw = self.rfile.read(length)
        try:
            body = json.loads(raw.decode("utf-8"))
        except json.JSONDecodeError as exc:
            raise WorkflowError(f"请求体不是合法 JSON: {exc}") from exc
        if not isinstance(body, dict):
            raise WorkflowError("请求体必须为 JSON 对象")
        return body

    def _query(self) -> dict[str, str]:
        result: dict[str, str] = {}
        if "?" not in self.path:
            return result
        from urllib.parse import parse_qs

        for key, values in parse_qs(self.path.split("?", 1)[1]).items():
            result[key] = values[-1]
        return result

    # ----- 路由 -----

    def do_GET(self) -> None:  # noqa: N802
        self._dispatch("GET")

    def do_POST(self) -> None:  # noqa: N802
        self._dispatch("POST")

    def _dispatch(self, method: str) -> None:
        try:
            path = self.path.split("?", 1)[0].rstrip("/") or "/"
            for pattern, verbs, handler in ROUTES:
                match = pattern.fullmatch(path)
                if match and method in verbs:
                    handler(self, **match.groupdict())
                    return
            self._send(404, {"error": f"未找到路由: {method} {path}"})
        except (WorkflowError, ConcurrencyError) as exc:
            status = 409 if isinstance(exc, ConcurrencyError) else 400
            self._send(status, {"error": str(exc), "type": type(exc).__name__})
        except (KeyError, TypeError, ValueError) as exc:
            self._send(400, {"error": str(exc), "type": type(exc).__name__})

    # ----- 端点实现 -----

    def health(self) -> None:
        self._send(200, {"status": "ok", "revision": self.service.repo.revision})

    def create_initial(self) -> None:
        body = self._read_json()
        changes = [Change(**c) for c in body.get("changes", [])]
        revision = self.service.establish_initial_baseline(
            approver=body.get("approver", "系统"),
            changes=changes,
            comment=body.get("comment", ""),
        )
        self._send(201, {"revision": revision.to_dict()})

    def get_baseline(self) -> None:
        repo = self.service.repo
        self._send(200, {
            "revision": repo.revision,
            "entities_fingerprint": repo.entities_fingerprint(),
            "revisions": [r.to_dict() for r in repo.revisions],
            "counts": {kind: len(repo.entities[kind]) for kind in ENTITY_KINDS},
        })

    def list_entities(self, kind: str) -> None:
        if kind not in ENTITY_KINDS:
            raise WorkflowError(f"未知实体类型: {kind}")
        self._send(200, {kind: [e.to_dict() for e in self.service.repo.list_entities(kind)]})

    def submit_proposal(self) -> None:
        body = self._read_json()
        changes = [Change(**c) for c in body.get("changes", [])]
        proposal = self.service.submit_changes(
            proposal_id=body["proposal_id"],
            changes=changes,
            submitted_by=body.get("submitted_by", "匿名"),
            idempotency_key=body.get("idempotency_key"),
            selected_scenario_id=body.get("selected_scenario_id"),
            rationale=body.get("rationale", ""),
        )
        self._send(201, {"proposal": proposal.to_dict()})

    def list_proposals(self) -> None:
        proposals = sorted(self.service.repo.proposals.values(), key=lambda p: p.submitted_at)
        self._send(200, {"proposals": [p.to_dict() for p in proposals]})

    def get_proposal(self, proposal_id: str) -> None:
        self._send(200, {"proposal": self.service.repo.proposal(proposal_id).to_dict()})

    def confirm(self, proposal_id: str) -> None:
        body = self._read_json()
        proposal = self.service.confirm(
            proposal_id,
            party=body["party"],
            actor_id=body.get("actor_id", body["party"]),
            comment=body.get("comment", ""),
        )
        self._send(200, {"proposal": proposal.to_dict()})

    def approve(self, proposal_id: str) -> None:
        body = self._read_json()
        proposal, revision = self.service.approve(
            proposal_id,
            approver=body.get("approver", "省级专班"),
            comment=body.get("comment", ""),
        )
        self._send(200, {"proposal": proposal.to_dict(), "revision": revision.to_dict()})

    def reject(self, proposal_id: str) -> None:
        body = self._read_json()
        proposal = self.service.reject(
            proposal_id,
            decider=body.get("decider", "省级专班"),
            comment=body.get("comment", ""),
        )
        self._send(200, {"proposal": proposal.to_dict()})

    def get_schedule(self) -> None:
        query = self._query()
        incremental = query.get("incremental", "1") not in {"0", "false", "False"}
        result = self.service.schedule(query.get("as_of"), incremental=incremental)
        self._send(200, result.to_dict())

    def recompute(self) -> None:
        body = self._read_json()
        trigger = Trigger(
            milestone_codes=frozenset(body.get("milestone_codes", [])),
            resource_codes=frozenset(body.get("resource_codes", [])),
            reasons=tuple(body.get("reasons", [])),
        )
        result = self.service.recompute_for_trigger(body["as_of"], trigger)
        self._send(200, result.to_dict())

    def hidden_delays(self) -> None:
        query = self._query()
        items = self.service.hidden_delays(query.get("as_of"))
        self._send(200, {"hidden_delays": [_jsonable(i) for i in items]})

    def scenarios(self) -> None:
        body = self._read_json()
        scenario_list = [_scenario_from_dict(item) for item in body.get("scenarios", [])]
        evaluations = self.service.evaluate_scenarios(body.get("as_of"), scenario_list)
        self._send(200, {"ranked": [_jsonable(e) for e in evaluations]})

    def list_monthly(self) -> None:
        self._send(200, {"publications": [p.to_dict() for p in self.service.publications()]})

    def get_monthly(self, month: str) -> None:
        self._send(200, {"publication": self.service.publication(month).to_dict()})

    def publish_monthly(self) -> None:
        body = self._read_json()
        conclusion = self.service.publish_monthly(
            month=body["month"],
            publisher=body.get("publisher", "省级专班"),
            as_of=body["as_of"],
        )
        self._send(201, {"publication": conclusion.to_dict()})

    def audit_trail(self) -> None:
        self._send(200, {"trail": _jsonable(self.service.approval_trail())})


def _scenario_from_dict(data: dict[str, Any]) -> Any:
    from .engine import Scenario

    return Scenario(
        scenario_id=data["scenario_id"],
        name=data.get("name", data["scenario_id"]),
        description=data.get("description", ""),
        forced_alternatives=data.get("forced_alternatives", {}),
        booking_priorities=data.get("booking_priorities", {}),
        booking_durations={k: int(v) for k, v in data.get("booking_durations", {}).items()},
        extend_exemptions={k: date.fromisoformat(v) for k, v in data.get("extend_exemptions", {}).items()},
        crash_durations={k: int(v) for k, v in data.get("crash_durations", {}).items()},
        permit_assumed_granted=frozenset(data.get("permit_assumed_granted", [])),
    )


def _route(pattern: str, verbs: set[str], attr: str) -> tuple[re.Pattern[str], set[str], Callable]:
    def _call(handler: "_Handler", **groups: str) -> None:
        getattr(handler, attr)(**groups)

    return re.compile(pattern), verbs, _call


ROUTES = [
    _route(r"/api/health", {"GET"}, "health"),
    _route(r"/api/baseline/initial", {"POST"}, "create_initial"),
    _route(r"/api/baseline", {"GET"}, "get_baseline"),
    _route(r"/api/entities/(?P<kind>[a-z]+)", {"GET"}, "list_entities"),
    _route(r"/api/proposals", {"POST"}, "submit_proposal"),
    _route(r"/api/proposals", {"GET"}, "list_proposals"),
    _route(r"/api/proposals/(?P<proposal_id>[^/]+)/confirmations", {"POST"}, "confirm"),
    _route(r"/api/proposals/(?P<proposal_id>[^/]+)/approve", {"POST"}, "approve"),
    _route(r"/api/proposals/(?P<proposal_id>[^/]+)/reject", {"POST"}, "reject"),
    _route(r"/api/proposals/(?P<proposal_id>[^/]+)", {"GET"}, "get_proposal"),
    _route(r"/api/schedule/recompute", {"POST"}, "recompute"),
    _route(r"/api/schedule", {"GET"}, "get_schedule"),
    _route(r"/api/analysis/hidden-delays", {"GET"}, "hidden_delays"),
    _route(r"/api/analysis/scenarios", {"POST"}, "scenarios"),
    _route(r"/api/monthly", {"GET"}, "list_monthly"),
    _route(r"/api/monthly", {"POST"}, "publish_monthly"),
    _route(r"/api/monthly/(?P<month>\d{4}-\d{2})", {"GET"}, "get_monthly"),
    _route(r"/api/audit/trail", {"GET"}, "audit_trail"),
]


def create_server(host: str, port: int, data_dir: str) -> ThreadingHTTPServer:
    service = ProgramService(data_dir)

    handler = type("Handler", (_Handler,), {"service": service})
    server = ThreadingHTTPServer((host, port), handler)
    server.service = service  # type: ignore[attr-defined]
    return server


def main() -> None:
    import argparse

    parser = argparse.ArgumentParser(description="六网项目联动排程后端")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8080)
    parser.add_argument("--data-dir", default="./data/sixnet")
    args = parser.parse_args()

    server = create_server(args.host, args.port, args.data_dir)
    print(f"六网联动排程服务已启动: http://{args.host}:{args.port} 数据目录={args.data_dir}")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        server.shutdown()


if __name__ == "__main__":
    main()
