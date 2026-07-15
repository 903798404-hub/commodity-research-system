import { StrictMode } from "react";
import { createRoot } from "react-dom/client";
import App from "./App";
import PresentationApp from "./presentation/PresentationApp";
import { isPresentationRoute } from "./presentation/config";
import "./styles.css";

createRoot(document.getElementById("root")!).render(
  <StrictMode>
    {isPresentationRoute(window.location.pathname) ? <PresentationApp /> : <App />}
  </StrictMode>,
);
