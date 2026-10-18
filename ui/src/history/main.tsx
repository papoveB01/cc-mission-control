import { StrictMode } from "react";
import { createRoot } from "react-dom/client";
import "../styles.css";
import "./history.css";
import { HistoryApp } from "./HistoryApp";

const root = document.getElementById("root");
if (root) {
  createRoot(root).render(
    <StrictMode>
      <HistoryApp />
    </StrictMode>,
  );
}
