import { createTheme } from "@mui/material/styles";

export const theme = createTheme({
  palette: {
    mode: "light",
    primary: { main: "#176d91", dark: "#124c69", light: "#5c9eb9" },
    secondary: { main: "#168c86" },
    success: { main: "#18794e" },
    warning: { main: "#a15c00" },
    error: { main: "#b42318" },
    background: { default: "#edf2f6", paper: "#ffffff" },
    text: { primary: "#132238", secondary: "#506276" },
    divider: "#dce4ec",
  },
  shape: { borderRadius: 10 },
  typography: {
    fontFamily: 'Inter, "IBM Plex Sans", ui-sans-serif, system-ui, sans-serif',
    fontSize: 14,
    h4: { fontWeight: 750, letterSpacing: "-0.025em" },
    h5: { fontWeight: 720, letterSpacing: "-0.018em" },
    h6: { fontWeight: 700, fontSize: "1.08rem", letterSpacing: "-.012em" },
    body2: { lineHeight: 1.55 },
    overline: { fontSize: ".68rem", letterSpacing: ".085em", fontWeight: 700 },
    button: { textTransform: "none", fontWeight: 700 },
  },
  components: {
    MuiButton: { defaultProps: { disableElevation: true }, styleOverrides: { root: { minHeight: 36, paddingInline: 16, whiteSpace: "normal" } } },
    MuiPaper: { styleOverrides: { root: { backgroundImage: "none" } } },
    MuiTextField: { defaultProps: { size: "small" } },
    MuiFormControl: { defaultProps: { size: "small" } },
    MuiFormHelperText: { styleOverrides: { root: { marginLeft: 0, lineHeight: 1.5, marginTop: 6 } } },
    MuiOutlinedInput: { styleOverrides: { root: { backgroundColor: "#fff" }, notchedOutline: { borderColor: "#c6d3df" } } },
    MuiTableCell: { styleOverrides: { head: { backgroundColor: "#f3f6f9", fontSize: 12, fontWeight: 700, color: "#506276" }, root: { borderBottomColor: "#e6edf2" } } },
    MuiAccordion: { defaultProps: { disableGutters: true, elevation: 0 }, styleOverrides: { root: { border: "1px solid #dce4ec", borderRadius: 8, "&:before": { display: "none" } } } },
    MuiAccordionSummary: { styleOverrides: { root: { minHeight: 48 }, content: { fontSize: 13, fontWeight: 600 } } },
    MuiTabs: { styleOverrides: { root: { minHeight: 44, borderBottom: "1px solid #dce4ec" } } },
    MuiTab: { styleOverrides: { root: { textTransform: "none", minHeight: 44, fontWeight: 650 } } },
  },
});
