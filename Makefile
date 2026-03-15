###############################################################################
# Makefile - Manage Zabbix Monitoring Stack
# Sử dụng: make <target>
###############################################################################

.PHONY: help up down restart logs build clean status db-shell zabbix-shell set-zabbix-password db-migrate

COMPOSE = docker compose
PROJECT_NAME = zabbix-monitoring

help: ## Show list of commands
	@grep -E '^[a-zA-Z_-]+:.*?## .*$$' $(MAKEFILE_LIST) | sort | \
		awk 'BEGIN {FS = ":.*?## "}; {printf "\033[36m%-20s\033[0m %s\n", $$1, $$2}'

build: ## Build all images
	$(COMPOSE) -p $(PROJECT_NAME) build --no-cache

up: ## Start all services
	$(COMPOSE) -p $(PROJECT_NAME) up -d
	@echo "✅ Stack started. Zabbix Web: http://localhost:8081"

down: ## Stop all services (cleans both default and zabbix-monitoring project to avoid network subnet conflict)
	-$(COMPOSE) -p $(PROJECT_NAME) down
	-$(COMPOSE) down

restart: ## Restart all services
	$(COMPOSE) -p $(PROJECT_NAME) restart

logs: ## View logs of all services
	$(COMPOSE) -p $(PROJECT_NAME) logs -f --tail=100

logs-discovery: ## View logs of tenant discovery service
	$(COMPOSE) -p $(PROJECT_NAME) logs -f tenant-discovery

logs-zabbix: ## View logs of Zabbix server
	$(COMPOSE) -p $(PROJECT_NAME) logs -f zabbix-server

status: ## View status of all services
	$(COMPOSE) -p $(PROJECT_NAME) ps

db-shell: ## Open PostgreSQL shell
	$(COMPOSE) -p $(PROJECT_NAME) exec postgres psql -U zabbix -d zabbix

zabbix-shell: ## Open shell into Zabbix server container
	$(COMPOSE) -p $(PROJECT_NAME) exec zabbix-server /bin/sh

clean: ## Delete all data (WARNING: all data will be lost!)
	$(COMPOSE) -p $(PROJECT_NAME) down -v
	@echo "⚠️  All volumes removed."

uptime-report: ## View uptime report of last 30 days
	$(COMPOSE) -p $(PROJECT_NAME) exec postgres \
		psql -U zabbix -d zabbix -c "SELECT * FROM daily_uptime_report LIMIT 50;"

MIGRATIONS_DIR = config/postgres/migrations
MIGRATIONS = $(sort $(wildcard $(MIGRATIONS_DIR)/*.sql))

db-migrate: ## Run all migrations in config/postgres/migrations/ (add columns, optimize lengths, etc.). Safe for existing data.
	@for f in $(MIGRATIONS); do echo "Running $$f..."; cat "$$f" | $(COMPOSE) -p $(PROJECT_NAME) exec -T postgres psql -U zabbix -d zabbix; done
