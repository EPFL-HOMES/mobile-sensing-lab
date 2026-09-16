import { Alert, Button, CircularProgress, Stack } from "@mui/material";
import { Component, lazy, Suspense } from "react";
import type { ReactNode } from "react";
import { Navigate, Route, Routes } from "react-router-dom";
import { AppShell } from "../components/AppShell";

const ProjectPage = lazy(() => import("../features/ProjectPage").then((module) => ({ default: module.ProjectPage })));
const EnvironmentPage = lazy(() => import("../features/EnvironmentPage").then((module) => ({ default: module.EnvironmentPage })));
const FleetPage = lazy(() => import("../features/FleetPage").then((module) => ({ default: module.FleetPage })));
const SimulationPage = lazy(() => import("../features/SimulationPage").then((module) => ({ default: module.SimulationPage })));
const PortfolioPage = lazy(() => import("../features/PortfolioPage").then((module) => ({ default: module.PortfolioPage })));
const DataPage = lazy(() => import("../features/DataPage").then((module) => ({ default: module.DataPage })));
const ResultsPage = lazy(() => import("../features/results/ResultsPage").then((module) => ({ default: module.ResultsPage })));
const ExamplesPage = lazy(() => import("../features/SecondaryPages").then((module) => ({ default: module.ExamplesPage })));

class PageLoadBoundary extends Component<{ children: ReactNode }, { failed: boolean }> {
  state = { failed: false };
  static getDerivedStateFromError() { return { failed: true }; }
  render() {
    if (this.state.failed) return <Stack gap={2} sx={{ m: 4 }}><Alert severity="error">This page could not be loaded. Reload the application to restore the saved project and local draft.</Alert><Button onClick={() => window.location.reload()}>Reload application</Button></Stack>;
    return this.props.children;
  }
}

export function App() {
  return (
    <PageLoadBoundary><Suspense fallback={<CircularProgress aria-label="Loading page" sx={{ m: 4 }} />}>
      <Routes>
        <Route element={<AppShell />}>
          <Route index element={<Navigate to="/project" replace />} />
          <Route path="project" element={<ProjectPage />} />
          <Route path="environment" element={<EnvironmentPage />} />
          <Route path="fleets" element={<FleetPage />} />
          <Route path="simulation" element={<SimulationPage />} />
          <Route path="portfolio" element={<PortfolioPage />} />
          <Route path="results" element={<ResultsPage />} />
          <Route path="examples" element={<ExamplesPage />} />
          <Route path="data" element={<DataPage />} />
          <Route path="*" element={<Navigate to="/project" replace />} />
        </Route>
      </Routes>
    </Suspense></PageLoadBoundary>
  );
}
