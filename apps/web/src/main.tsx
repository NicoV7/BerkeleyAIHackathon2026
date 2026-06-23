import React from "react";
import ReactDOM from "react-dom/client";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { GTProvider } from "gt-react";
import App from "./App";
import "./index.css";

const queryClient = new QueryClient();

// VITE_GT_PROJECT_ID lives only in .env (never committed). With no project id
// configured we still render — gt-react falls back to the default locale (en).
const gtProjectId = import.meta.env.VITE_GT_PROJECT_ID as string | undefined;

ReactDOM.createRoot(document.getElementById("root")!).render(
  <React.StrictMode>
    <QueryClientProvider client={queryClient}>
      <GTProvider projectId={gtProjectId} defaultLocale="en" locales={["es"]}>
        <App />
      </GTProvider>
    </QueryClientProvider>
  </React.StrictMode>,
);
