import { Card, Typography } from "antd";

interface Props {
  title: string;
  description: string;
}

/** 占位页面：阶段 1 仅建立路由与目录结构，业务界面在后续阶段实现。 */
export default function PlaceholderPage({ title, description }: Props) {
  return (
    <Card>
      <Typography.Title level={3}>{title}</Typography.Title>
      <Typography.Text type="secondary">{description}</Typography.Text>
    </Card>
  );
}
