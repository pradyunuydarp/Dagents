import React from "react";
import { createRoot } from "react-dom/client";

import App from "./App";
import { installGlow } from "./glow";
import "./design-system.css";
import "./styles.css";

installGlow();

createRoot(document.getElementById("root")!).render(
  <React.StrictMode>
    <App />
  </React.StrictMode>
);
