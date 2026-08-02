import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";

export default defineConfig({
	plugins: [
		react(),
	],
	server: {
		host: true, // listen on 0.0.0.0 (useful if you dev on a remote server)
		port: 5173,
		strictPort: true,
		proxy: {
			// Adjust the key if your backend uses a different base path
			"/api": {
				target: "http://localhost:8000",
				changeOrigin: true,
				// If FastAPI is not under /api in production, you can rewrite here:
				// rewrite: (path) => path.replace(/^\/api/, ""),
			},
		},
	},
});
