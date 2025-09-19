# Mesh Dash Makefile
# Virtual environment and Docker management

# Variables
VENV_DIR = .venv
PYTHON = python3
PIP = $(VENV_DIR)/bin/pip
PYTHON_VENV = $(VENV_DIR)/bin/python
DOCKER_IMAGE = bgulla/mesh-dash
DOCKER_TAG = latest

# Default target
.DEFAULT_GOAL := help

# Help target
.PHONY: help
help: ## Show this help message
	@echo "Mesh Dash - Available targets:"
	@grep -E '^[a-zA-Z_-]+:.*?## .*$$' $(MAKEFILE_LIST) | sort | awk 'BEGIN {FS = ":.*?## "}; {printf "  \033[36m%-20s\033[0m %s\n", $$1, $$2}'

# Virtual Environment Management
.PHONY: venv
venv: ## Create virtual environment
	@echo "Creating virtual environment..."
	$(PYTHON) -m venv $(VENV_DIR)
	@echo "Virtual environment created in $(VENV_DIR)"

.PHONY: install
install: venv ## Install dependencies in virtual environment
	@echo "Installing dependencies..."
	$(PIP) install --upgrade pip
	$(PIP) install -r requirements.txt
	@echo "Dependencies installed"

.PHONY: clean-venv
clean-venv: ## Remove virtual environment
	@echo "Removing virtual environment..."
	rm -rf $(VENV_DIR)
	@echo "Virtual environment removed"

# Application Management
.PHONY: run
run: install ## Run the application in virtual environment
	@echo "Starting Mesh Dash..."
	$(PYTHON_VENV) mesh_listen.py

.PHONY: dev
dev: install ## Run in development mode (alias for run)
	@$(MAKE) run

# Docker Management
.PHONY: docker-build
docker-build: ## Build Docker image (multi-architecture)
	@echo "Building Docker image for multiple architectures..."
	docker buildx build --platform linux/amd64,linux/arm64 -t $(DOCKER_IMAGE):$(DOCKER_TAG) .
	@echo "Docker image built: $(DOCKER_IMAGE):$(DOCKER_TAG)"

.PHONY: docker-build-local
docker-build-local: ## Build Docker image for local architecture only
	@echo "Building Docker image for local architecture..."
	docker build -t $(DOCKER_IMAGE):$(DOCKER_TAG) .
	@echo "Docker image built: $(DOCKER_IMAGE):$(DOCKER_TAG)"

.PHONY: docker-push
docker-push: ## Push Docker image to registry
	@echo "Pushing Docker image..."
	docker buildx build --platform linux/amd64,linux/arm64 -t $(DOCKER_IMAGE):$(DOCKER_TAG) --push .
	@echo "Docker image pushed: $(DOCKER_IMAGE):$(DOCKER_TAG)"

.PHONY: docker-run
docker-run: ## Run Docker container
	@echo "Running Docker container..."
	docker run -d \
		--name mesh-dash \
		-p 8080:8080 \
		-e MESH_HOST=${MESH_HOST:-192.168.0.91} \
		-e API_HOST=0.0.0.0 \
		-e API_PORT=8080 \
		$(DOCKER_IMAGE):$(DOCKER_TAG)
	@echo "Container started. Access the dashboard at http://localhost:8080"

.PHONY: docker-run-it
docker-run-it: ## Run Docker container interactively
	@echo "Running Docker container interactively..."
	docker run -it --rm \
		-p 8080:8080 \
		-e MESH_HOST=${MESH_HOST:-192.168.0.91} \
		-e API_HOST=0.0.0.0 \
		-e API_PORT=8080 \
		$(DOCKER_IMAGE):$(DOCKER_TAG)

.PHONY: docker-stop
docker-stop: ## Stop and remove the Docker container
	@echo "Stopping Docker container..."
	-docker stop mesh-dash
	-docker rm mesh-dash
	@echo "Container stopped and removed"

.PHONY: docker-logs
docker-logs: ## Show Docker container logs
	docker logs -f mesh-dash

# Combined Operations
.PHONY: build-and-push
build-and-push: docker-build docker-push ## Build and push Docker image

.PHONY: deploy
deploy: docker-stop docker-run ## Stop existing container and run new one

# Cleanup
.PHONY: clean
clean: clean-venv docker-stop ## Clean up virtual environment and stop containers
	@echo "Cleanup completed"

.PHONY: clean-docker
clean-docker: ## Remove Docker images
	@echo "Removing Docker images..."
	-docker rmi $(DOCKER_IMAGE):$(DOCKER_TAG)
	@echo "Docker images removed"

# Setup buildx for multi-architecture builds (run once)
.PHONY: docker-setup-buildx
docker-setup-buildx: ## Setup Docker buildx for multi-architecture builds
	@echo "Setting up Docker buildx..."
	docker buildx create --name mesh-dash-builder --use || true
	docker buildx inspect --bootstrap
	@echo "Docker buildx setup completed"

# Environment activation helper
.PHONY: activate
activate: venv ## Show command to activate virtual environment
	@echo "To activate the virtual environment, run:"
	@echo "source $(VENV_DIR)/bin/activate"