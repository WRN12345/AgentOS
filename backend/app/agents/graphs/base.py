"""LangGraph 基础图（10.2 节，T5.2/T5.3）。

流程：加载授权后的项目上下文 → 路由到所需辅助能力 → 结构校验与安全规则
→ 保存 AgentSuggestion → 通知相关人员查看。

- 能力注册表 CAPABILITIES 含 echo（最小健康探针型能力，不调用模型，便于在
  无 Ollama 的环境下端到端验证链路）与 10.1 节六个具体 Agent
  （T5.4：requirement_analyst / assignment_advisor / planning_advisor；
  T5.5：workflow_risk / deliverable_review / summary_agent，
  实现在 app.agents.specialists，提示词在 app.agents.prompts）；
- 所有能力输出统一走 T5.3 的 Schema（app.agents.schemas）与校验节点；
- 校验失败的诊断信息由 SuggestionValidationError 携带，worker 落入
  agent_runs.error，不保存正式建议、不发通知（17.3 节）；
- 保存建议走工具注册表中的 write_suggestion（app.agents.tools，10.3 节）。

检查点由调用方注入（worker 用 AsyncPostgresSaver 持久化到 PostgreSQL，
17.3 节）；检查点只做中断恢复，业务记录以 agent_runs/agent_suggestions 为准。
"""

import inspect
import uuid
from typing import Any, TypedDict

from langgraph.graph import END, StateGraph
from sqlalchemy import select

from app.agents.schemas.suggestion import AgentSuggestionEnvelope, parse_suggestion_output
from app.agents.specialists.assignment import assignment_advisor_capability
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

# echo 占位能力的提示词版本（T5.4 起各 Agent 维护自己的版本号）
ECHO_PROMPT_VERSION = "echo.v1"

NOTIFICATION_TYPE = "agent.suggestion_ready"


class AgentGraphState(TypedDict, total=False):
    """基础图状态（全部可 JSON 序列化，供检查点持久化）。

    T5.4/T5.5 接入具体 Agent 时在此扩展上下文字段（最小上下文原则，16 节）。
    """

    run_id: str
    agent_type: str
    trigger_source: str
    work_item_id: str | None
    request_id: str | None
    # 人工触发时携带的自然语言输入（如需求描述）
    prompt: str
    # 授权后的项目上下文（只含完成分析所需的最小字段）
    context: dict[str, Any]
    # 路由结果：命中的能力名
    capability: str
    # 能力产出的结构化建议（10.2 节统一字段）
    suggestion: dict[str, Any]
    # 保存/通知结果
    suggestion_id: str
    notification_recipient_id: str
    notification_title: str
    notification_body: str
    notification_link: str | None


# ---------- 能力注册表（占位路由；T5.4/T5.5 注册六个具体 Agent） ----------


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
                "project": context.get("project", {}).get("name"),
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


#: 能力名 → 能力函数（输入 state；输出 suggestion dict 或模型返回的 JSON
#: 字符串，二者都由 validate_output 统一按 Schema 校验）。
#: 支持同步（如 echo）与 async（需要查库/调模型的具体 Agent）两种形态。
CAPABILITIES = {
    "echo": _echo_capability,
    "requirement_analyst": requirement_analyst_capability,
    "assignment_advisor": assignment_advisor_capability,
    "planning_advisor": planning_advisor_capability,
    "workflow_risk": workflow_risk_capability,
    "deliverable_review": deliverable_review_capability,
    "summary_agent": summary_agent_capability,
}

#: agent_type → 能力名。T5.4/T5.5 六个具体 Agent（10.1 节）挂在同名能力上；
#: 未识别的 agent_type 一律落到 echo 占位能力。
AGENT_ROUTES: dict[str, str] = {
    "echo": "echo",
    "requirement_analyst": "requirement_analyst",
    "assignment_advisor": "assignment_advisor",
    "planning_advisor": "planning_advisor",
    "workflow_risk": "workflow_risk",
    "deliverable_review": "deliverable_review",
    "summary_agent": "summary_agent",
}


# ---------- 图节点 ----------


async def load_context(state: AgentGraphState) -> dict[str, Any]:
    """加载授权后的项目上下文（最小上下文原则：仅项目名 + 目标工作项标题/状态）。"""
    async with async_session_factory() as session:
        project = (
            await session.execute(select(Project).order_by(Project.created_at).limit(1))
        ).scalar_one_or_none()
        context: dict[str, Any] = {
            "project": {"id": str(project.id), "name": project.name} if project else None,
            "work_item": None,
            "leader_id": None,
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
        if state.get("work_item_id"):
            item = await session.get(WorkItem, uuid.UUID(state["work_item_id"]))
            if item is not None:
                context["work_item"] = {
                    "id": str(item.id),
                    "title": item.title,
                    "status": item.status,
                }
    return {"context": context}


async def route_capability(state: AgentGraphState) -> dict[str, Any]:
    """路由到所需辅助能力（占位：未注册的 agent_type 一律走 echo）。"""
    capability = AGENT_ROUTES.get(state["agent_type"], "echo")
    return {"capability": capability}


async def run_capability(state: AgentGraphState) -> dict[str, Any]:
    """执行命中的能力节点，产出结构化建议。

    能力函数可为同步（echo）或 async（具体 Agent 需查库/调模型）；
    模型不可用等错误直接冒泡，由 worker 统一标记 run failed（17.3 节）。
    """
    capability = CAPABILITIES[state["capability"]]
    result = capability(state)
    if inspect.isawaitable(result):
        result = await result
    return {"suggestion": result}


async def validate_output(state: AgentGraphState) -> dict[str, Any]:
    """结构校验与安全规则（10.2 节统一 Schema，T5.3）。

    能力产出（dict 或模型返回的 JSON 字符串）经 parse_suggestion_output 严格
    校验；非法 JSON / 缺字段 / 类型错误抛 SuggestionValidationError（携带诊断
    信息），worker 捕获后 run 标记 failed、诊断落 agent_runs.error，
    不保存正式建议、不发通知（17.3 节）。
    校验通过后以规范化 dict 回写 state，供 save_suggestion 使用。
    """
    output = parse_suggestion_output(state.get("suggestion"), run_id=state["run_id"])
    return {"suggestion": output.model_dump(mode="json")}


async def save_suggestion(state: AgentGraphState) -> dict[str, Any]:
    """保存 AgentSuggestion 并通知负责人查看（同一事务，复用 T3.5 notify）。

    写入走工具注册表中的 write_suggestion（10.3 节唯一写工具）；系统侧在
    此处补信封字段（run_id；模型名以 agent_runs.model 为准，不冗余存储）。
    """
    suggestion = state["suggestion"]
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
        if leader_id is not None:
            await notify(
                session,
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
        "notification_title": title,
        "notification_body": body,
        "notification_link": link,
    }


# ---------- 图组装 ----------


def build_agent_graph(checkpointer: Any = None) -> Any:
    """组装基础图（10.2 节）。checkpointer 由 worker 注入 AsyncPostgresSaver。"""
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
    work_item_id: uuid.UUID | None,
    request_id: str | None,
    prompt: str,
) -> AgentGraphState:
    """构造图输入（模型名取自当前配置，记录在 agent_runs 上）。"""
    return AgentGraphState(
        run_id=str(run_id),
        agent_type=agent_type,
        trigger_source=trigger_source,
        work_item_id=str(work_item_id) if work_item_id else None,
        request_id=request_id,
        prompt=prompt,
    )
