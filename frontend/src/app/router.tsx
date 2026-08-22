import { createBrowserRouter } from "react-router-dom";
import AdminOnly from "../components/AdminOnly";
import AppLayout from "../components/AppLayout";
import ProjectGate from "../components/ProjectGate";
import RequireAuth from "../components/RequireAuth";
import AdminConsolePage from "../features/admin/AdminConsolePage";
import LoginPage from "../features/auth/LoginPage";
import ProjectPickerPage from "../features/auth/ProjectPickerPage";
import DashboardPage from "../features/dashboard/DashboardPage";
import TeamOverviewPage from "../features/dashboard/TeamOverviewPage";
import MembersPage from "../features/members/MembersPage";
import WorkItemsPage from "../features/work-items/WorkItemsPage";
import WorkItemDetailPage from "../features/work-items/WorkItemDetailPage";
import ApprovalsPage from "../features/approvals/ApprovalsPage";
import DeliverablesPage from "../features/deliverables/DeliverablesPage";
import AgentAssistantPage from "../features/agent-assistant/AgentAssistantPage";
import DocumentsPage from "../features/knowledge/DocumentsPage";
import CoreMemoryPage from "../features/knowledge/CoreMemoryPage";

export const router = createBrowserRouter([
  { path: "/login", element: <LoginPage /> },
  {
    path: "/console",
    element: (
      <RequireAuth>
        <AdminOnly>
          <AdminConsolePage />
        </AdminOnly>
      </RequireAuth>
    ),
  },
  {
    path: "/projects",
    element: (
      <RequireAuth>
        <ProjectPickerPage />
      </RequireAuth>
    ),
  },
  {
    path: "/",
    element: (
      <RequireAuth>
        <ProjectGate>
          <AppLayout />
        </ProjectGate>
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
      { path: "documents", element: <DocumentsPage /> },
      { path: "core-memory", element: <CoreMemoryPage /> },
    ],
  },
]);
