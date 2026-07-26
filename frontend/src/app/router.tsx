import { createBrowserRouter } from "react-router-dom";
import AppLayout from "../components/AppLayout";
import LoginPage from "../features/auth/LoginPage";
import DashboardPage from "../features/dashboard/DashboardPage";
import MembersPage from "../features/members/MembersPage";
import WorkItemsPage from "../features/work-items/WorkItemsPage";
import ApprovalsPage from "../features/approvals/ApprovalsPage";
import DeliverablesPage from "../features/deliverables/DeliverablesPage";
import AgentAssistantPage from "../features/agent-assistant/AgentAssistantPage";

export const router = createBrowserRouter([
  { path: "/login", element: <LoginPage /> },
  {
    path: "/",
    element: <AppLayout />,
    children: [
      { index: true, element: <DashboardPage /> },
      { path: "members", element: <MembersPage /> },
      { path: "work-items", element: <WorkItemsPage /> },
      { path: "approvals", element: <ApprovalsPage /> },
      { path: "deliverables", element: <DeliverablesPage /> },
      { path: "agent-assistant", element: <AgentAssistantPage /> },
    ],
  },
]);
