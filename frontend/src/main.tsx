import React from "react";
import ReactDOM from "react-dom/client";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
// Self-hosted fonts (bundled from node_modules, no external Google Fonts fetch).
import "@fontsource-variable/inter/index.css";
import "@fontsource-variable/eb-garamond/index.css";
import "@fontsource-variable/eb-garamond/wght-italic.css";
import "@fontsource/ibm-plex-mono/400.css";
import "@fontsource/ibm-plex-mono/500.css";
import App from "./App";
import "./styles.css";

const client = new QueryClient();

ReactDOM.createRoot(document.getElementById("root")!).render(
	<React.StrictMode>
		<QueryClientProvider client={client}>
			<App />
		</QueryClientProvider>
	</React.StrictMode>,
);
