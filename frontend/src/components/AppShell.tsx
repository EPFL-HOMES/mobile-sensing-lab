import AnalyticsRounded from "@mui/icons-material/AnalyticsRounded";
import CloudUploadRounded from "@mui/icons-material/CloudUploadRounded";
import DatasetRounded from "@mui/icons-material/DatasetRounded";
import DirectionsCarRounded from "@mui/icons-material/DirectionsCarRounded";
import FolderRounded from "@mui/icons-material/FolderRounded";
import HelpOutlineRounded from "@mui/icons-material/HelpOutlineRounded";
import MapRounded from "@mui/icons-material/MapRounded";
import PlayCircleRounded from "@mui/icons-material/PlayCircleRounded";
import SaveRounded from "@mui/icons-material/SaveRounded";
import ScienceRounded from "@mui/icons-material/ScienceRounded";
import {
  AppBar,
  Box,
  Button,
  Chip,
  Dialog,
  DialogContent,
  DialogTitle,
  Divider,
  Drawer,
  IconButton,
  List,
  ListItemButton,
  ListItemIcon,
  ListItemText,
  Stack,
  Toolbar,
  Tooltip,
  Typography,
  Alert,
} from "@mui/material";
import { useState } from "react";
import { Link, NavLink, Outlet } from "react-router-dom";
import { useWorkspace } from "../app/workspace";

const drawerWidth = 232;
const navigation = [
  ["/project", "Project", FolderRounded],
  ["/data", "Data", DatasetRounded],
  ["/environment", "Environment", MapRounded],
  ["/fleets", "Fleet Configuration", DirectionsCarRounded],
  ["/simulation", "Simulation", PlayCircleRounded],
  ["/portfolio", "Portfolio", AnalyticsRounded],
  ["/results", "Results", ScienceRounded],
] as const;

export function AppShell() {
  const { project, revision, dirty, draft, saveRevision, busy } = useWorkspace();
  const [helpOpen, setHelpOpen] = useState(false);
  const exportConfig = () => {
    const url = URL.createObjectURL(
      new Blob([JSON.stringify(draft.authoring, null, 2)], { type: "application/json" }),
    );
    const anchor = document.createElement("a");
    anchor.href = url;
    anchor.download = `${project?.name ?? "mobile-sensing"}-draft.json`;
    anchor.click();
    URL.revokeObjectURL(url);
  };
  return (
    <Box sx={{ display: "flex", minHeight: "100vh" }}>
      <AppBar
        position="fixed"
        color="inherit"
        elevation={0}
        sx={{ ml: `${drawerWidth}px`, width: `calc(100% - ${drawerWidth}px)`, borderBottom: 1, borderColor: "divider" }}
      >
        <Toolbar sx={{ minHeight: "56px !important", gap: 2 }}>
          <Box sx={{ flex: 1, minWidth: 0 }}>
            <Typography fontWeight={750} noWrap>{project?.name ?? "No project open"}</Typography>
            <Typography variant="caption" color="text.secondary" className="mono" noWrap>
              {revision ? "Saved project configuration" : project ? "Draft configuration · saved automatically when you run" : "Create or open a project to begin"}
            </Typography>
          </Box>
          <Chip
            label={!revision ? "Not saved" : dirty ? "Unsaved draft" : "Saved"}
            color={!revision ? "default" : dirty ? "warning" : "success"}
            size="small"
            variant={revision && dirty ? "filled" : "outlined"}
          />
          <Button
            startIcon={<SaveRounded />}
            variant="contained"
            disabled={!project || !dirty || busy || Boolean(draft.authoring?.read_only)}
            onClick={() => void saveRevision().catch(() => undefined)}
          >
            Save
          </Button>
          <Button onClick={exportConfig} disabled={!project}>Export config</Button>
          <Tooltip title="Workflow and scientific terminology">
            <IconButton aria-label="Help" onClick={() => setHelpOpen(true)}><HelpOutlineRounded /></IconButton>
          </Tooltip>
        </Toolbar>
      </AppBar>
      <Drawer
        variant="permanent"
        sx={{ width: drawerWidth, flexShrink: 0, "& .MuiDrawer-paper": { width: drawerWidth, bgcolor: "#10253d", color: "#dce8f2", border: 0 } }}
      >
        <Toolbar sx={{ minHeight: "56px !important", px: 2.25 }}>
          <CloudUploadRounded sx={{ color: "#61c5df", mr: 1.25 }} />
          <Typography fontWeight={800} letterSpacing="-0.02em">Mobile Sensing Simulator</Typography>
        </Toolbar>
        <Divider sx={{ borderColor: "rgba(255,255,255,.1)" }} />
        <List aria-label="Main navigation" sx={{ px: 1, py: 1.5 }}>
          {navigation.map(([to, label, Icon]) => (
            <ListItemButton
              key={to}
              component={NavLink}
              to={to}
              sx={{ borderRadius: 1.5, mb: 0.5, color: "#b9c9d8", "&.active": { bgcolor: "rgba(97,197,223,.16)", color: "#ffffff", "& .MuiListItemIcon-root": { color: "#61c5df" } }, "&:hover": { bgcolor: "rgba(255,255,255,.08)" } }}
            >
              <ListItemIcon sx={{ minWidth: 38, color: "inherit" }}><Icon fontSize="small" /></ListItemIcon>
              <ListItemText primary={label} slotProps={{ primary: { fontSize: 14, fontWeight: 650 } }} />
            </ListItemButton>
          ))}
        </List>
      </Drawer>
      <Box sx={{ flexGrow: 1, minWidth: 0, pt: "56px" }}>
        <Box sx={{ px: 3, pt: 2 }}>
          <Alert severity={draft.authoring?.prepared_environment ? "success" : "info"}>
            {!project ? <Link to="/project">Create or open a project to begin.</Link> : !draft.authoring?.prepared_environment ? <>Project open · Environment needs preparation. <Link to="/data">Register global data</Link> or <Link to="/environment">prepare the study area</Link>.</> : !draft.authoring?.fleets?.length ? <>Environment prepared · <Link to="/fleets">Add the first fleet</Link>.</> : <>Environment prepared · {draft.authoring.fleets.length} fleets configured · <Link to="/simulation">Run validates and saves the current settings</Link>.</>}
          </Alert>
        </Box>
        <Outlet />
      </Box>
      <Dialog open={helpOpen} onClose={() => setHelpOpen(false)} maxWidth="sm" fullWidth>
        <DialogTitle>Scientific workflow</DialogTitle>
        <DialogContent>
          <Stack gap={1.5}>
            <ol>
              <li><strong>Project:</strong> Open the example results, duplicate a project to edit it, or create a new project. Use Files to access settings, inputs and exports.</li>
              <li><strong>Data:</strong> Register boundary, grid, road network, speed and optional feature files. Features such as population are aggregated onto the grid.</li>
              <li><strong>Environment:</strong> Select or search the study region and prepare its grid, roads and speeds. Inspect the prepared environment and optional spatial features on the maps.</li>
              <li><strong>Fleet Configuration:</strong> Add each fleet; configure Demand, Supply, Dispatch, Routing and sensing movements.</li>
              <li><strong>Simulation:</strong> Choose the day, observation hours, simulation replications, reporting resolution and worker processes. Run simulation saves, validates and computes movement and exposure.</li>
              <li><strong>Portfolio:</strong> Select a completed run; set sensor counts, costs, budgets, fleet sampling runs and utility. Run portfolio analysis reuses its exposure.</li>
              <li><strong>Results:</strong> Inspect mean operations and sensing in Fleet results, then compare count portfolios and their utility/risk frontier. Export results for further analysis.</li>
            </ol>
            <Typography>Supply fixes the physical vehicle population. Portfolio sampling selects which vehicles carry sensors.</Typography>
          </Stack>
        </DialogContent>
      </Dialog>
    </Box>
  );
}
