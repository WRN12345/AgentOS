import { createBrowserRouter } from "react-router-dom";
import AppLayout from "../components/AppLayout";
import RequireAuth from "../components/RequireAuth";
import LoginPage from "../features/auth/LoginPage";
import DashboardPage from "../features/dashboard/DashboardPage";
import TeamOverviewPage from "../features/dashboard/TeamOverviewPage";
import MembersPage from "../features/members/MembersPage";
import WorkItemsPage from "../features/work-items/WorkItemsPage";
import WorkItemDetailPage from "../features/work-items/WorkItemDetailPage";
import ApprovalsPage from "../features/approvals/ApprovalsPage";
import DeliverablesPage from "../features/deliverables/DeliverablesPage";
import AgentAssistantPage from "../features/agent-assistant/AgentAssistantPage";

export const router = createBrowserRouter([
  { path: "/login", element: <LoginPage /> },
  {
    path: "/",
    element: (
      <RequireAuth>
        <AppLayout />
      </RequireAuth>
    ),
    children: [
      { index: true, element: <DashboardPage /> },
      { path: "team-overview", element: <TeamOverviewPage /> },
      { path: "members", element: <MembersPage /> },
      { path: "work-items", element: <WorkItemsPage /> },
      { path: "work-items/:id", element: <WorkItemDetailPage /> },
      { path: "approvals", element: <ApprovalsPage /> },
      { path: "deliverables", element: <DeliverablesPage /> },
      { path: "agent-assistant", element: <AgentAssistantPage /> },
    ],
  },
]);
