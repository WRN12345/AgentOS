"""更换 1024 维 embedding 模型后全量重建记忆索引。

1. 清空 memory_chunks（旧模型向量全部作废——检索本来就只命中当前模型版本，
   此步只是腾出空间并让重建结果可预期）；
   不能用于变更 embedding 维度：memory_chunks 的 vector 列固定为 1024 维，
   维度变更必须先通过专门的数据库迁移调整列类型；
2. 当前版本文档重置 index_status 为 pending 并重投 memory.index 任务
   （worker 从文件原文重新提取→切块→转向量）；
3. 成员档案按现内容重投纯文本索引任务；
4. 已完成的拆解/分配运行与已完成工作项重投历史索引任务
   （worker 从 run/工作项记录现取文本）；
5. active 核心记忆条目重投 core_memory 索引任务，Worker 按当前状态重建或作废。

重建期间知识库不可用，Worker 必须保持运行以消费任务。

用法（容器内执行）：
    docker compose exec backend python -m scripts.rebuild_memory_index --yes
不加 --yes 只打印将要重建的数量，不做任何修改。
"""

import argparse
import asyncio

from sqlalchemy import delete, select, update

from app.agents.models import AgentRun
from app.domains.files.models import StoredFile
from app.domains.memory.history import HISTORY_RUN_AGENT_TYPES
from app.domains.memory.indexer import MEMORY_INDEX_TASK_TYPE
from app.domains.memory.models import CoreMemoryEntry, MemberProfile, MemoryChunk
from app.domains.work_items.models import WorkItem
from app.domains.work_items.state_machine import WorkItemStatus
from app.infrastructure.cache.redis import create_redis_client
from app.infrastructure.database.engine import async_session_factory
from app.infrastructure.queue.queue import enqueue


async def main() -> None:
    parser = argparse.ArgumentParser(description="全量重建记忆索引（16.4）")
    parser.add_argument("--yes", action="store_true", help="确认执行（不加则只预览数量）")
    args = parser.parse_args()

    async with async_session_factory() as session:
        files = (
            (
                await session.execute(
                    select(StoredFile).where(
                        StoredFile.superseded_by.is_(None),
                        StoredFile.index_status.in_(["indexed", "failed"]),
                    )
                )
            )
            .scalars()
            .all()
        )
        profiles = (await session.execute(select(MemberProfile))).scalars().all()
        runs = (
            (
                await session.execute(
                    select(AgentRun).where(
                        AgentRun.status == "succeeded",
                        AgentRun.agent_type.in_(HISTORY_RUN_AGENT_TYPES),
                    )
                )
            )
            .scalars()
            .all()
        )
        work_items = (
            (
                await session.execute(
                    select(WorkItem).where(
                        WorkItem.status == WorkItemStatus.COMPLETED.value
                    )
                )
            )
            .scalars()
            .all()
        )
        core_entries = (
            (
                await session.execute(
                    select(CoreMemoryEntry).where(CoreMemoryEntry.status == "active")
                )
            )
            .scalars()
            .all()
        )
        chunk_count = (
            await session.execute(select(MemoryChunk.id))
        ).scalars().all()

    total = len(files) + len(profiles) + len(runs) + len(work_items) + len(core_entries)
    print(
        f"将清空 {len(chunk_count)} 个向量块，重建 {total} 个来源："
        f"文档 {len(files)}、档案 {len(profiles)}、拆解/分配运行 {len(runs)}、"
        f"已完成工作项 {len(work_items)}、核心记忆 {len(core_entries)}"
    )
    if not args.yes:
        print("预览模式：未做任何修改。加 --yes 执行重建。")
        return

    redis_client = create_redis_client()
    try:
        async with async_session_factory() as session:
            await session.execute(delete(MemoryChunk))
            await session.execute(
                update(StoredFile)
                .where(StoredFile.id.in_([f.id for f in files]))
                .values(index_status="pending")
            )
            await session.commit()

        for f in files:
            await enqueue(
                redis_client,
                MEMORY_INDEX_TASK_TYPE,
                {
                    "project_id": str(f.project_id),
                    "source_type": "document",
                    "source_id": str(f.id),
                    "stored_file_id": str(f.id),
                },
            )
        for p in profiles:
            await enqueue(
                redis_client,
                MEMORY_INDEX_TASK_TYPE,
                {
                    "project_id": None,
                    "source_type": "profile",
                    "source_id": str(p.id),
                },
            )
        for r in runs:
            await enqueue(
                redis_client,
                MEMORY_INDEX_TASK_TYPE,
                {
                    "project_id": str(r.project_id),
                    "source_type": "history",
                    "source_id": str(r.id),
                    "history_kind": "run",
                },
            )
        for w in work_items:
            await enqueue(
                redis_client,
                MEMORY_INDEX_TASK_TYPE,
                {
                    "project_id": str(w.project_id),
                    "source_type": "history",
                    "source_id": str(w.id),
                    "history_kind": "work_item",
                },
            )
        for e in core_entries:
            await enqueue(
                redis_client,
                MEMORY_INDEX_TASK_TYPE,
                {
                    "project_id": str(e.project_id),
                    "source_type": "core_memory",
                    "source_id": str(e.id),
                },
            )
    finally:
        await redis_client.aclose()
    print(f"已投递 {total} 个索引任务，worker 将逐个重建。可用 docker compose logs -f worker 观察进度。")


if __name__ == "__main__":
    asyncio.run(main())
