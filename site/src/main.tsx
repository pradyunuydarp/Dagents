import React from "react";
import { createRoot } from "react-dom/client";

import App from "./App";
import { installCodeCopy } from "./codeCopy";
import { installGlow } from "./glow";
import "./design-system.css";
import "./styles.css";
import "./futuristic.css";

installGlow();
installCodeCopy();

createRoot(document.getElementById("root")!).render(
  <React.StrictMode>
    <App />
  </React.StrictMode>
);
