"""验证模型输出清洗及推理模型链路回归。

部分推理模型无法关闭 thinking，会在结构化内容前添加 ``<think>`` 块；统一清洗
必须移除该噪声，避免合法 JSON 被误判为解析失败。
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
    """验证模型噪声清洗的边界行为。"""

    def test_pure_json_passthrough(self) -> None:
        assert strip_model_noise('{"a": 1}') == '{"a": 1}'

    def test_think_block_removed(self) -> None:
        raw = '<think>\n让我分析一下...\n</think>\n{"a": 1}'
        assert json.loads(strip_model_noise(raw)) == {"a": 1}

    def test_unclosed_think_removed(self) -> None:
        # 截断的思考块没有可用正文，应清空并交由解析层生成诊断。
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
    """带思考块的合法 JSON 应完成 Agent 运行并产出建议。"""
    await make_ctx(client, project)
    stub_provider.set_script(f"<think>\n先分析需求再输出。\n</think>\n{VALID_REQUIREMENT_OUTPUT}")
    run = await drive_agent_run(
        project_id=project.id, agent_type="requirement_analyst", prompt="测试 think 包装"
    )
    assert run.status == "succeeded", run.error
    suggestions = await get_suggestions_for_run(run.id)
    assert len(suggestions) == 1
    assert suggestions[0].suggestion_type == "requirement"
