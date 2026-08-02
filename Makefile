.PHONY: frontend backend reload nginx install-sync mcp

# install deps and build the React app
frontend:
	cd frontend && npm install && npm run build

# start FastAPI backend
backend:
	uvicorn backend.app:app --reload --port 8000

# reload nginx after config or build changes
nginx:
	sudo systemctl reload nginx

# build everything and reload nginx
all: frontend nginx

# start MCP server (HTTP transport on port 8001)
mcp:
	.venv/bin/python riksdagen_mcp/server.py

# install and enable the daily sync timer
install-sync:
	sudo cp etc/riksdagen-sync.service /etc/systemd/system/
	sudo cp etc/riksdagen-sync.timer /etc/systemd/system/
	sudo systemctl daemon-reload
	sudo systemctl enable --now riksdagen-sync.timer
	@echo "Timer installed. Check status with: systemctl list-timers | grep riksdagen"
