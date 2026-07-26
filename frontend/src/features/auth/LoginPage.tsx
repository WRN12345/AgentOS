import { Button, Card, Form, Input, Typography, message } from "antd";
import { api, ApiError } from "../../services/api";

/** 登录占位页：认证在阶段 2（T2.1）落地。 */
export default function LoginPage() {
  const [form] = Form.useForm();

  const onFinish = async (values: { username: string; password: string }) => {
    try {
      await api.post("/auth/login", values);
    } catch (error) {
      if (error instanceof ApiError) {
        message.info(`后端占位响应：${error.code} - ${error.message}`);
      } else {
        message.error("网络错误");
      }
    }
  };

  return (
    <div style={{ display: "flex", justifyContent: "center", marginTop: 120 }}>
      <Card style={{ width: 360 }}>
        <Typography.Title level={3}>AgentOS 登录</Typography.Title>
        <Form form={form} layout="vertical" onFinish={onFinish}>
          <Form.Item name="username" label="用户名" rules={[{ required: true }]}>
            <Input autoComplete="username" />
          </Form.Item>
          <Form.Item name="password" label="密码" rules={[{ required: true }]}>
            <Input.Password autoComplete="current-password" />
          </Form.Item>
          <Button type="primary" htmlType="submit" block>
            登录
          </Button>
        </Form>
      </Card>
    </div>
  );
}
