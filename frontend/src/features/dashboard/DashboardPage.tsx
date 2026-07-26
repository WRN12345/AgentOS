import { useQuery } from "@tanstack/react-query";
import { Card, Descriptions, Typography } from "antd";
import { api } from "../../services/api";

interface V1Root {
  service: string;
  api: string;
  status: string;
}

/** 工作台占位页：通过 TanStack Query 验证 API 客户端链路。 */
export default function DashboardPage() {
  const { data, isLoading, error } = useQuery({
    queryKey: ["v1-root"],
    queryFn: () => api.get<V1Root>("/"),
    retry: false,
  });

  return (
    <Card loading={isLoading}>
      <Typography.Title level={3}>工作台</Typography.Title>
      <Typography.Paragraph type="secondary">
        项目总览、状态分布与即将到期事项将在阶段 2/3 实现。
      </Typography.Paragraph>
      {data && (
        <Descriptions title="后端连通性" size="small" column={1}>
          <Descriptions.Item label="service">{data.service}</Descriptions.Item>
          <Descriptions.Item label="api">{data.api}</Descriptions.Item>
          <Descriptions.Item label="status">{data.status}</Descriptions.Item>
        </Descriptions>
      )}
      {error && (
        <Typography.Text type="danger">后端暂不可达：{String(error)}</Typography.Text>
      )}
    </Card>
  );
}
