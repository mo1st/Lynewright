import React from "react";
import ReactDOM from "react-dom/client";
import { ConfirmProvider } from "./context/ConfirmContext";
import { ThemeProvider } from "./context/ThemeContext";
import "./styles/tokens.css";
import "./styles/base.css";
import legacyDashboardStyles from "./components/ui/legacy-dashboard.module.css";
import App from "./App";
const rootElement = document.getElementById("root") as HTMLElement;
rootElement.classList.add(legacyDashboardStyles.legacyDashboardRoot);
ReactDOM.createRoot(rootElement).render(
  <React.StrictMode><ThemeProvider><ConfirmProvider><App /></ConfirmProvider></ThemeProvider></React.StrictMode>,
);
