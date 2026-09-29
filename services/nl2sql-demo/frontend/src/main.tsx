import React from "react";
import { createRoot } from "react-dom/client";
import App from "./App";
// The shared design system first: it owns the tokens, the base type and the
// `ds-*` primitives. `styles.css` only adds what is specific to this demo.
import "./design-system.css";
import "./styles.css";

createRoot(document.getElementById("root")!).render(
  <React.StrictMode>
    <App />
  </React.StrictMode>
);
