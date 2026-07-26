import { Layout, Menu, Typography } from "antd";
import { Link, Outlet, useLocation } from "react-router-dom";

const { Header, Sider, Content } = Layout;

const menuItems = [
  { key: "/", label: <Link to="/">工作台</Link> },
  { key: "/members", label: <Link to="/members">成员</Link> },
  { key: "/work-items", label: <Link to="/work-items">工作项</Link> },
  { key: "/approvals", label: <Link to="/approvals">审批中心</Link> },
  { key: "/deliverables", label: <Link to="/deliverables">交付物</Link> },
  { key: "/agent-assistant", label: <Link to="/agent-assistant">Agent 助手</Link> },
];

export default function AppLayout() {
  const location = useLocation();
  return (
    <Layout style={{ minHeight: "100vh" }}>
      <Header style={{ display: "flex", alignItems: "center" }}>
        <Typography.Title level={4} style={{ color: "#fff", margin: 0 }}>
          AgentOS
        </Typography.Title>
      </Header>
      <Layout>
        <Sider width={200}>
          <Menu
            mode="inline"
            selectedKeys={[location.pathname]}
            items={menuItems}
            style={{ height: "100%" }}
          />
        </Sider>
        <Content style={{ padding: 24 }}>
          <Outlet />
        </Content>
      </Layout>
    </Layout>
  );
}
