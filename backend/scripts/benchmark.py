"""AgentOS 后端性能基线测量脚本。

对运行中的 AgentOS 后端测量核心接口的响应时间，输出 Markdown 表格。
测量前自动造代表性数据量（perf_ 前缀，便于识别与复测）：

- 10 名成员（1 负责人 + 9 普通成员，幂等：已存在则复用）
- 指定数量的工作项（默认 100，均指定主执行人并发布）

用法（容器内执行，与运行中后端同网络）：

    docker compose run --rm --no-deps -v "$PWD/backend:/app" backend \
        python scripts/benchmark.py --base-url http://backend:8000

参数：--items 工作项数量（默认 100）--samples 每接口采样数（默认 30）
"""

import argparse
import asyncio
import statistics
import time
import uuid

import httpx

# 各接口采样数（命令/文件类操作采样少一些，读接口多一些）
LOGIN_SAMPLES = 20
CMD_SAMPLES = 15
FILE_SAMPLES = 10


async def login(client: httpx.AsyncClient, username: str, password: str) -> dict[str, str]:
    resp = await client.post("/api/v1/auth/login", json={"username": username, "password": password})
    resp.raise_for_status()
    return {"Authorization": f"Bearer {resp.json()['access_token']}"}


async def measure(name: str, samples: int, fn, results: list[dict]) -> None:
    """调用 fn() 采样 samples 次，并记录以毫秒为单位的耗时统计。"""
    times: list[float] = []
    for _ in range(samples):
        start = time.perf_counter()
        await fn()
        times.append((time.perf_counter() - start) * 1000)
    times.sort()
    results.append(
        {
            "name": name,
            "samples": samples,
            "avg": statistics.fmean(times),
            "p50": times[len(times) // 2],
            "p95": times[min(int(len(times) * 0.95), len(times) - 1)],
            "max": times[-1],
        }
    )


async def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--base-url", default="http://backend:8000")
    parser.add_argument("--admin-user", default="admin")
    parser.add_argument("--admin-password", default="admin123")
    parser.add_argument("--items", type=int, default=100)
    parser.add_argument("--samples", type=int, default=30)
    args = parser.parse_args()

    limits = httpx.Limits(max_connections=10)
    async with httpx.AsyncClient(base_url=args.base_url, timeout=30.0, limits=limits) as client:
        admin_headers = await login(client, args.admin_user, args.admin_password)

        # 准备成员数据，已存在的成员直接复用
        resp = await client.get("/api/v1/members", headers=admin_headers)
        resp.raise_for_status()
        members = resp.json()
        by_name = {m["username"]: m for m in members}
        perf_members = []
        for i in range(9):
            username = f"perf_member_{i}"
            if username not in by_name:
                resp = await client.post(
                    "/api/v1/members",
                    json={"username": username, "password": username, "display_name": f"基线成员{i}", "role": "member"},
                    headers=admin_headers,
                )
                if resp.status_code not in (200, 201):
                    print(f"成员 {username} 创建失败：{resp.status_code} {resp.text}，尝试登录复用")
                else:
                    by_name[username] = resp.json()
            perf_members.append(by_name.get(username))
        perf_members = [m for m in perf_members if m]
        member_headers = {}
        for i, m in enumerate(perf_members):
            username = f"perf_member_{i}"
            # 新成员初始密码即用户名（创建接口返回的一次性密码策略可能不同，
            # 登录失败时该成员的用例降级为只用负责人视角）
            try:
                member_headers[username] = await login(client, username, username)
            except httpx.HTTPStatusError:
                pass

        # 准备工作项数据，通过 perf_ 标题前缀统计并复用已有数据
        resp = await client.get("/api/v1/work-items?limit=500", headers=admin_headers)
        resp.raise_for_status()
        existing = resp.json()
        existing_items = existing if isinstance(existing, list) else existing.get("items", [])
        perf_count = sum(1 for w in existing_items if str(w.get("title", "")).startswith("perf_"))
        to_create = max(0, args.items - perf_count)
        print(f"已有 perf_ 工作项 {perf_count} 个，本次新建 {to_create} 个")
        for i in range(to_create):
            assignee = perf_members[i % len(perf_members)]
            resp = await client.post(
                "/api/v1/work-items",
                json={
                    "title": f"perf_工作项_{uuid.uuid4().hex[:8]}",
                    "description": "性能基线种子数据",
                    "priority": "medium",
                    "assignee_id": assignee["id"],
                },
                headers={**admin_headers, "Idempotency-Key": f"perf-seed-{uuid.uuid4()}"},
            )
            resp.raise_for_status()

        results: list[dict] = []

        # 登录接口
        async def do_login() -> None:
            await login(client, args.admin_user, args.admin_password)

        await measure("POST /auth/login", LOGIN_SAMPLES, do_login, results)

        # 读取接口
        async def get_items() -> None:
            (await client.get("/api/v1/work-items?limit=100", headers=admin_headers)).raise_for_status()

        await measure("GET /work-items（列表）", args.samples, get_items, results)

        async def get_members() -> None:
            (await client.get("/api/v1/members", headers=admin_headers)).raise_for_status()

        await measure("GET /members（负载汇总）", args.samples, get_members, results)

        async def get_approvals() -> None:
            (await client.get("/api/v1/approvals", headers=admin_headers)).raise_for_status()

        await measure("GET /approvals（审批聚合）", args.samples, get_approvals, results)

        async def get_notifications() -> None:
            (await client.get("/api/v1/notifications", headers=admin_headers)).raise_for_status()

        await measure("GET /notifications", args.samples, get_notifications, results)

        # 命令接口：创建协作和工作项，覆盖幂等键与乐观锁场景
        # 建一个专用工作项做命令测量
        assignee = perf_members[0]
        resp = await client.post(
            "/api/v1/work-items",
            json={"title": f"perf_cmd_{uuid.uuid4().hex[:8]}", "description": "命令测量",
                  "priority": "low", "assignee_id": assignee["id"]},
            headers={**admin_headers, "Idempotency-Key": f"perf-cmd-{uuid.uuid4()}"},
        )
        resp.raise_for_status()
        cmd_item = resp.json()
        resp = await client.post(f"/api/v1/work-items/{cmd_item['id']}/publish",
                                 json={"version": 1}, headers=admin_headers)
        resp.raise_for_status()

        collab_counter = 0
        # 协作请求只能由主执行人发起（权限规则），用主执行人身份；登录失败则降级跳过
        requester_headers = member_headers.get("perf_member_0")

        async def create_collab() -> None:
            nonlocal collab_counter
            collab_counter += 1
            resp = await client.post(
                f"/api/v1/work-items/{cmd_item['id']}/collaboration-requests",
                json={"assignee_id": perf_members[1]["id"], "title": f"perf协作{collab_counter}",
                      "goal": "基线测量"},
                headers=requester_headers,
            )
            resp.raise_for_status()

        if requester_headers:
            await measure("POST /collaboration-requests（协作命令）", CMD_SAMPLES, create_collab, results)
        else:
            print("警告：perf_member_0 登录失败，跳过协作命令测量")

        async def create_item_cmd() -> None:
            resp = await client.post(
                "/api/v1/work-items",
                json={"title": f"perf_cmd_create_{uuid.uuid4().hex[:8]}", "description": "基线",
                      "priority": "low", "assignee_id": assignee["id"]},
                headers={**admin_headers, "Idempotency-Key": f"perf-create-{uuid.uuid4()}"},
            )
            resp.raise_for_status()

        await measure("POST /work-items（创建+幂等键）", CMD_SAMPLES, create_item_cmd, results)

        # 文件上传与下载接口
        payload = b"perf-benchmark-payload\n" * 40000  # 约 880KB

        file_ids: list[str] = []

        async def upload() -> None:
            resp = await client.post(
                "/api/v1/files",
                files={"file": ("perf_bench.txt", payload, "text/plain")},
                data={"work_item_id": cmd_item["id"]},
                headers=admin_headers,
            )
            resp.raise_for_status()
            file_ids.append(resp.json()["id"])

        await measure("POST /files（上传 ~880KB）", FILE_SAMPLES, upload, results)

        if file_ids:
            fid = file_ids[0]

            async def download() -> None:
                (await client.get(f"/api/v1/files/{fid}/download", headers=admin_headers)).raise_for_status()

            await measure("GET /files/{id}/download", FILE_SAMPLES, download, results)

        # SSE 连接建立
        async def sse_connect() -> None:
            async with client.stream("GET", "/api/v1/events/stream", headers=admin_headers) as resp:
                resp.raise_for_status()
                async for _ in resp.aiter_bytes():
                    break  # 收到首帧即断开，只测建立时间

        await measure("GET /events/stream（SSE 建立+首帧）", FILE_SAMPLES, sse_connect, results)

    # 输出 Markdown 统计表
    print("\n| 接口 | 样本 | 平均(ms) | p50(ms) | p95(ms) | 最大(ms) |")
    print("| --- | --- | --- | --- | --- | --- |")
    for r in results:
        print(f"| {r['name']} | {r['samples']} | {r['avg']:.1f} | {r['p50']:.1f} | {r['p95']:.1f} | {r['max']:.1f} |")


if __name__ == "__main__":
    asyncio.run(main())
