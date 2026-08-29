"""Agent 使用的 LangGraph 基础图。

流程：加载授权后的项目上下文 → 路由到所需辅助能力 → 结构校验与安全规则
→ 保存 AgentSuggestion → 通知相关人员查看。

CAPABILITIES 注册 echo、各专用 Agent 和 requirement_pipeline。所有输出必须经过统一
Schema 校验；失败诊断写入 agent_runs.error，不保存建议也不发送通知。建议只能通过
write_suggestion 保存。检查点由调用方注入，仅用于中断恢复；业务事实以 agent_runs
和 agent_suggestions 为准。
"""

import inspect
import uuid
from typing import Any, Required, TypedDict

from langgraph.graph import END, StateGraph
from sqlalchemy import select

from app.agents.schemas.suggestion import AgentSuggestionEnvelope, parse_suggestion_output
from app.agents.specialists.assignment import assignment_advisor_capability
from app.agents.specialists.dev_doc_review import dev_doc_review_capability
from app.agents.specialists.pipeline import requirement_pipeline_capability
from app.agents.specialists.planning import planning_advisor_capability
from app.agents.specialists.requirement import requirement_analyst_capability
from app.agents.specialists.review import deliverable_review_capability
from app.agents.specialists.risk import workflow_risk_capability
from app.agents.specialists.summary import summary_agent_capability
from app.agents.tools import write_suggestion
from app.core.logging import setup_logging
from app.domains.notifications.service import notify
from app.domains.project.models import Project, ProjectMember
from app.domains.work_items.models import WorkItem
from app.infrastructure.database.engine import async_session_factory

logger = setup_logging("agent-graph")

ECHO_PROMPT_VERSION = "echo.v1"

NOTIFICATION_TYPE = "agent.suggestion_ready"


class AgentGraphState(TypedDict, total=False):
    """可供检查点持久化的 JSON 状态。

    Required 字段由 initial_state 提供，其余字段由图节点按执行顺序写入。
    """

    run_id: Required[str]
    agent_type: Required[str]
    trigger_source: Required[str]
    # worker 无请求头，项目归属必须由 agent_runs.project_id 经队列载荷传入。
    project_id: Required[str | None]
    work_item_id: Required[str | None]
    request_id: Required[str | None]
    prompt: Required[str]
    # 授权后的项目上下文只保留分析所需的最小字段。
    context: dict[str, Any]
    capability: str
    suggestion: dict[str, Any]
    suggestion_id: str
    notification_recipient_id: str
    notification_title: str
    notification_body: str
    notification_link: str | None


def _echo_capability(state: AgentGraphState) -> dict[str, Any]:
    """最小 echo/健康探针能力：不回模型，回显输入与上下文摘要，验证全链路。"""
    context = state.get("context", {})
    work_item = context.get("work_item") or {}
    return {
        "suggestion_type": state["agent_type"],
        "content": {
            "summary": f"echo 占位能力已接收输入：{state.get('prompt', '')[:200] or '(空)'}",
            "rationale": "T5.2 基础图链路自检：上下文加载、路由、校验、保存、通知全链路连通。",
            "echo": {
                "project": (context.get("project") or {}).get("name"),
                "work_item_title": work_item.get("title"),
            },
        },
        "confidence": 1.0,
        "risks": "占位能力，未调用模型，内容不构成正式建议。",
        "fact_refs": {
            "work_item_ids": [state["work_item_id"]] if state.get("work_item_id") else [],
        },
        "prompt_version": ECHO_PROMPT_VERSION,
    }


#: 能力可同步返回 suggestion dict，也可异步返回模型 JSON，结果统一由 validate_output 校验。
CAPABILITIES = {
    "echo": _echo_capability,
    "requirement_analyst": requirement_analyst_capability,
    "assignment_advisor": assignment_advisor_capability,
    "planning_advisor": planning_advisor_capability,
    "workflow_risk": workflow_risk_capability,
    "deliverable_review": deliverable_review_capability,
    "summary_agent": summary_agent_capability,
    "requirement_pipeline": requirement_pipeline_capability,
    "dev_doc_review": dev_doc_review_capability,
}

#: agent_type 到能力名的路由；未识别类型回退到 echo。
AGENT_ROUTES: dict[str, str] = {
    "echo": "echo",
    "requirement_analyst": "requirement_analyst",
    "assignment_advisor": "assignment_advisor",
    "planning_advisor": "planning_advisor",
    "workflow_risk": "workflow_risk",
    "deliverable_review": "deliverable_review",
    "summary_agent": "summary_agent",
    "requirement_pipeline": "requirement_pipeline",
    "dev_doc_review": "dev_doc_review",
}


async def load_context(state: AgentGraphState) -> dict[str, Any]:
    """加载授权后的最小项目上下文，仅包含项目名和目标工作项标题、状态。

    优先使用 state.project_id；缺失时从 work_item 推导。两者不一致表示项目归属链路
    被破坏，必须立即终止，避免跨项目读取。
    """
    async with async_session_factory() as session:
        project = None
        work_item = None
        if state.get("work_item_id"):
            work_item = await session.get(WorkItem, uuid.UUID(state["work_item_id"]))

        explicit_project_id = state.get("project_id")
        if explicit_project_id:
            project = await session.get(Project, uuid.UUID(explicit_project_id))
        elif work_item is not None:
            project = await session.get(Project, work_item.project_id)

        context: dict[str, Any] = {
            "project": {"id": str(project.id), "name": project.name} if project else None,
            "work_item": None,
            "leader_id": None,
        }
        if work_item is not None:
            if project is not None and work_item.project_id != project.id:
                logger.error(
                    "work_item project mismatch: work_item=%s item_project=%s run_project=%s",
                    work_item.id,
                    work_item.project_id,
                    explicit_project_id,
                )
                raise RuntimeError("work_item.project_id != run.project_id，项目归属链路被破坏")
            context["work_item"] = {
                "id": str(work_item.id),
                "title": work_item.title,
                "status": work_item.status,
            }
        if project is not None:
            leader = (
                await session.execute(
                    select(ProjectMember)
                    .where(ProjectMember.project_id == project.id, ProjectMember.role == "leader")
                    .order_by(ProjectMember.created_at)
                    .limit(1)
                )
            ).scalar_one_or_none()
            context["leader_id"] = str(leader.id) if leader else None
    return {"context": context}


async def route_capability(state: AgentGraphState) -> dict[str, Any]:
    """路由到所需能力，未注册的 agent_type 回退到 echo。"""
    capability = AGENT_ROUTES.get(state["agent_type"], "echo")
    return {"capability": capability}


async def run_capability(state: AgentGraphState) -> dict[str, Any]:
    """执行命中的能力节点，产出结构化建议。

    能力函数可以同步或 async 执行。模型错误直接冒泡，由 worker 统一标记 run failed。
    """
    # route_capability 按图顺序先写入 capability；缺省回退与路由规则保持一致。
    capability = CAPABILITIES[state.get("capability", "echo")]
    result = capability(state)
    if inspect.isawaitable(result):
        result = await result
    return {"suggestion": result}


async def validate_output(state: AgentGraphState) -> dict[str, Any]:
    """执行统一结构校验和安全规则。

    非法 JSON、缺字段或类型错误会抛出带诊断信息的 SuggestionValidationError。
    worker 随后将 run 标记为 failed；失败输出不得保存为建议或触发通知。
    """
    output = parse_suggestion_output(state.get("suggestion"), run_id=state["run_id"])
    return {"suggestion": output.model_dump(mode="json")}


async def save_suggestion(state: AgentGraphState) -> dict[str, Any]:
    """在同一事务中保存 AgentSuggestion 并通知负责人。

    写入只能走 write_suggestion。系统在此补充 run_id，模型名以 agent_runs.model 为准。
    """
    # 图路由保证 validate_output 先写入 suggestion。
    suggestion = state.get("suggestion")
    assert suggestion is not None, "validate_output must run before save_suggestion"
    context = state.get("context", {})
    leader_id = context.get("leader_id")
    work_item_id = state.get("work_item_id")

    title = "Agent 分析完成"
    body = f"{state['agent_type']} 已生成建议，请在建议中心查看"
    link = f"/work-items/{work_item_id}" if work_item_id else None

    envelope = AgentSuggestionEnvelope(run_id=uuid.UUID(state["run_id"]), **suggestion)

    async with async_session_factory() as session:
        record = await write_suggestion(session, envelope=envelope)
        recipient_id: str | None = None
        # 通知的 project_id 必须取自已授权的 load_context 结果。
        project_id = (context.get("project") or {}).get("id")
        if leader_id is not None and project_id is not None:
            await notify(
                session,
                project_id=uuid.UUID(project_id),
                recipient_id=uuid.UUID(leader_id),
                type=NOTIFICATION_TYPE,
                title=title,
                body=body,
                link=link,
            )
            recipient_id = leader_id
        else:
            logger.warning("no project leader found, skip suggestion notification")
        await session.commit()

    return {
        "suggestion_id": str(record.id),
        "notification_recipient_id": recipient_id or "",
        "notification_project_id": project_id or "",
        "notification_title": title,
        "notification_body": body,
        "notification_link": link,
    }


def build_agent_graph(checkpointer: Any = None) -> Any:
    """组装基础图；checkpointer 由 worker 注入 AsyncPostgresSaver。"""
    graph = StateGraph(AgentGraphState)
    graph.add_node("load_context", load_context)
    graph.add_node("route_capability", route_capability)
    graph.add_node("run_capability", run_capability)
    graph.add_node("validate_output", validate_output)
    graph.add_node("save_suggestion", save_suggestion)

    graph.set_entry_point("load_context")
    graph.add_edge("load_context", "route_capability")
    graph.add_edge("route_capability", "run_capability")
    graph.add_edge("run_capability", "validate_output")
    graph.add_edge("validate_output", "save_suggestion")
    graph.add_edge("save_suggestion", END)

    return graph.compile(checkpointer=checkpointer)


def initial_state(
    *,
    run_id: uuid.UUID,
    agent_type: str,
    trigger_source: str,
    project_id: uuid.UUID | None,
    work_item_id: uuid.UUID | None,
    request_id: str | None,
    prompt: str,
) -> AgentGraphState:
    """构造图输入。"""
    return AgentGraphState(
        run_id=str(run_id),
        agent_type=agent_type,
        trigger_source=trigger_source,
        project_id=str(project_id) if project_id else None,
        work_item_id=str(work_item_id) if work_item_id else None,
        request_id=request_id,
        prompt=prompt,
    )
