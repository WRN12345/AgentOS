"""模型输出清洗（strip_model_noise）与推理模型链路回归。

背景：MiniMax M2.x 等推理模型 content 前部固定带 <think>...</think>
（thinking 无法关闭），导致 Agent 结构化输出 json_parse 失败（17.3 节）。
清洗在 call_model_json 入口统一完成。
"""

import json

import httpx

from app.agents.specialists.common import strip_model_noise
from app.domains.project.models import Project
from tests.helpers_e2e import (
    VALID_REQUIREMENT_OUTPUT,
    StubModelProvider,
    drive_agent_run,
    get_suggestions_for_run,
    stub_provider,  # noqa: F401 - fixture 注入
)
from tests.helpers_t6a import make_ctx


class TestStripModelNoise:
    """清洗函数单元用例。"""

    def test_pure_json_passthrough(self) -> None:
        assert strip_model_noise('{"a": 1}') == '{"a": 1}'

    def test_think_block_removed(self) -> None:
        raw = '<think>\n让我分析一下...\n</think>\n{"a": 1}'
        assert json.loads(strip_model_noise(raw)) == {"a": 1}

    def test_unclosed_think_removed(self) -> None:
        # 输出被截断导致 think 未闭合：全部剥掉（解析失败走诊断路径）
        assert strip_model_noise("<think>还没想完") == ""

    def test_json_fence_removed(self) -> None:
        raw = '```json\n{"a": 1}\n```'
        assert json.loads(strip_model_noise(raw)) == {"a": 1}

    def test_think_plus_fence(self) -> None:
        raw = '<think>思考</think>\n```json\n{"a": 1}\n```'
        assert json.loads(strip_model_noise(raw)) == {"a": 1}


async def test_agent_run_with_think_wrapped_output(
    client: httpx.AsyncClient,  # noqa: ARG001 - 保持与其他场景一致的依赖
    project: Project,  # noqa: ARG001
    stub_provider: StubModelProvider,
) -> None:
    """集成回归：模型返回 <think> 包装的合法 JSON，Agent 运行应成功并产出建议。"""
    await make_ctx(client, project)
    stub_provider.set_script(f"<think>\n先分析需求再输出。\n</think>\n{VALID_REQUIREMENT_OUTPUT}")
    run = await drive_agent_run(agent_type="requirement_analyst", prompt="测试 think 包装")
    assert run.status == "succeeded", run.error
    suggestions = await get_suggestions_for_run(run.id)
    assert len(suggestions) == 1
    assert suggestions[0].suggestion_type == "requirement"
