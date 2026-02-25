# k8s-deploy.py - Automated Kubernetes Deployment Script

A powerful Python script that automatically deploys GitHub repositories to local Kubernetes clusters (kind, minikube, k3d) with intelligent service discovery, environment variable handling, and automatic frontend-backend connectivity fixes.

## 🚀 Features

- **Multi-Cluster Support**: Works with kind, minikube, and k3d
- **Automatic Service Discovery**: Detects and configures backend services
- **Smart Port Detection**: Reads port mappings from docker-compose.yml
- **Environment Variable Handling**: Manages ConfigMaps and Secrets
- **Frontend-Backend Connectivity**: Automatically patches Vite proxy configurations
- **Database Integration**: PostgreSQL setup with proper initialization
- **Ingress Configuration**: Automatic HTTP/HTTPS routing setup
- **SSL Certificate Generation**: Optional SSL cert creation

## 📋 Prerequisites

- Python 3.8+
- Docker
- Local Kubernetes cluster (kind/minikube/k3d)
- kubectl configured

## 🛠️ Installation

```bash
git clone https://github.com/regmishreeshesh-hub/kind-cluster.git
cd kind-cluster
chmod +x k8s-deploy.py
```

## 🎯 Quick Start

### Deploy a Repository
```bash
# Interactive mode
python3 k8s-deploy.py --url https://github.com/user/repo.git

# Non-interactive mode
python3 k8s-deploy.py --url https://github.com/user/repo.git --non-interactive --apply
```

### Specify Cluster
```bash
python3 k8s-deploy.py --url https://github.com/user/repo.git --cluster my-cluster
```

## 📖 Usage Examples

### Example 1: React + Node.js App
```bash
python3 k8s-deploy.py --url https://github.com/regmishreeshesh-hub/Keypouch.git --non-interactive --apply
```

### Example 2: Python Flask + Static Frontend
```bash
python3 k8s-deploy.py --url https://github.com/regmishreeshesh-hub/telephone-secrets.git --non-interactive --apply
```

## ⚙️ Command Line Options

| Option | Description |
|--------|-------------|
| `--url` | GitHub repository URL |
| `--branch` | Branch to deploy (default: main) |
| `--cluster` | Specific cluster name |
| `--non-interactive` | Skip prompts, use defaults |
| `--apply` | Auto-apply manifests |
| `--db-pvc-size` | Database PVC size (default: 1Gi) |
| `--no-db-pvc` | Disable DB PVC creation |

## 🔧 What the Script Does

### 1. Repository Analysis
- Clones the GitHub repository
- Scans for Dockerfiles, docker-compose.yml, .env files
- Detects frontend/backend components

### 2. Environment Processing
- Reads .env files and docker-compose environment variables
- Creates Kubernetes ConfigMaps and Secrets
- Fixes database host references

### 3. Frontend-Backend Connectivity
- **Vite Apps**: Patches vite.config.ts with correct service names
- **Environment Files**: Sets VITE_API_URL for proper routing
- **ConfigMaps**: Ensures empty VITE_API_URL for Ingress

### 4. Docker Image Management
- Builds Docker images for each component
- Loads images into local cluster registry
- Handles port mapping from docker-compose

### 5. Kubernetes Deployment
- Creates namespace, ConfigMaps, Secrets
- Deploys applications with proper service discovery
- Sets up Ingress for HTTP/HTTPS access
- Initializes databases with init.sql

## 🌐 Accessing Deployed Applications

### HTTP Access
```bash
kubectl port-forward -n ingress-nginx svc/ingress-nginx-controller 8080:80 &
# Access: http://localhost:8080
```

### HTTPS Access
```bash
kubectl port-forward -n ingress-nginx svc/ingress-nginx-controller 8443:443 &
# Access: https://localhost:8443
```

## 📁 Repository Structure Support

The script supports various repository structures:

### React/Vite + Node.js
```
repo/
├── backend/
│   ├── Dockerfile
│   └── package.json
├── web/
│   ├── vite.config.ts
│   ├── package.json
│   └── .env.example
├── docker-compose.yml
└── init.sql
```

### Python Flask + Static Frontend
```
repo/
├── backend/
│   ├── Dockerfile
│   └── app.py
├── frontend/
│   ├── Dockerfile
│   └── index.html
├── docker-compose.yml
└── init.sql
```

## 🔍 Troubleshooting

### Common Issues

**1. Cluster Not Detected**
```bash
# Check available clusters
kind get clusters
minikube profile list
k3d cluster list
```

**2. Port Already in Use**
```bash
# Kill existing port forwards
pkill -f "kubectl port-forward"
```

**3. Database Connection Issues**
- Check if database name matches in ConfigMap
- Verify database service is running
- Review init.sql execution

### Debug Commands
```bash
# Check pod status
kubectl get pods -n <namespace>

# View logs
kubectl logs -n <namespace> <pod-name>

# Check ConfigMap
kubectl get configmap <config-name> -n <namespace> -o yaml

# Port forward to service
kubectl port-forward -n <namespace> svc/<service-name> <local-port>:<container-port>
```

## 🧪 Tested Repositories

The script has been tested with:

- **Keypouch**: React/Vite + Node.js + PostgreSQL
- **telephone-secrets**: Python Flask + Static Frontend + PostgreSQL

## 🤝 Contributing

1. Fork the repository
2. Create a feature branch
3. Test with different repository structures
4. Submit a pull request

## 📝 License

This project is open source and available under the MIT License.
