#!/bin/bash

# Interactive Git-to-K8s Deployer
# Automates deployment of GitHub repositories to Kind clusters

set -euo pipefail

# Colors for output
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
BLUE='\033[0;34m'
NC='\033[0m' # No Color

# Logging functions
log_info() { echo -e "${BLUE}[INFO]${NC} $1"; }
log_success() { echo -e "${GREEN}[SUCCESS]${NC} $1"; }
log_warn() { echo -e "${YELLOW}[WARN]${NC} $1"; }
log_error() { echo -e "${RED}[ERROR]${NC} $1"; }

# Utility functions
sanitize_name() {
    echo "$1" | tr '[:upper:]' '[:lower:]' | sed -E 's/[^a-z0-9-]+/-/g; s/^-+//; s/-+$//'
}

require_cmd() {
    command -v "$1" >/dev/null 2>&1 || {
        log_error "Missing required command: $1"
        exit 1
    }
}

# Array to collect exactly what we built
declare -a BUILT_IMAGES=()

# Check required tools
for cmd in git docker kind kubectl; do
    require_cmd "$cmd"
done

# --- 1. Gather User Input ---

echo -e "${GREEN}Interactive Git-to-K8s Deployer${NC}"
echo "----------------------------------------"

# GitHub repository
read -p "Enter GitHub repository URL: " REPO_URL
if [[ -z "$REPO_URL" ]]; then
    log_error "Repository URL is required"
    exit 1
fi

# Extract and sanitize repository name
REPO_NAME_RAW=$(basename "$REPO_URL" .git)
REPO_NAME=$(sanitize_name "$REPO_NAME_RAW")
NAMESPACE="$REPO_NAME"
log_info "Repository name: $REPO_NAME_RAW"
log_info "Kubernetes namespace: $NAMESPACE"

# Public/private repo handling
echo ""
read -p "Is the repository public? (y/n) [default: y]: " IS_PUBLIC
IS_PUBLIC=${IS_PUBLIC:-y}

if [[ "$IS_PUBLIC" =~ ^[Nn]$ ]]; then
    read -sp "Enter GitHub personal access token: " GH_TOKEN
    echo ""
else
    GH_TOKEN=""
    log_info "Repository is public — no token required"
fi

# Clone directory with timestamp for easy cleanup
TIMESTAMP=$(date +%Y%m%d-%H%M%S)
CLONE_DIR="/tmp/keypouch-deploy-${TIMESTAMP}"
MANIFESTS_DIR="$CLONE_DIR/manifest-k8s"

# Branch selection
echo ""
log_info "Fetching remote branches..."
git ls-remote --heads "$REPO_URL" 2>/dev/null | awk -F'/' '{print $NF}' | sort -u > /tmp/branches.txt || {
    log_error "Failed to fetch branches. Check repository URL and token."
    exit 1
}

echo "Available branches:"
awk '{print NR". "$1}' /tmp/branches.txt
echo ""

read -p "Select branch number or enter branch name [default: main]: " BRANCH_CHOICE
BRANCH_CHOICE=${BRANCH_CHOICE:-main}

if [[ "$BRANCH_CHOICE" =~ ^[0-9]+$ ]]; then
    SELECTED_BRANCH=$(sed -n "${BRANCH_CHOICE}p" /tmp/branches.txt)
else
    SELECTED_BRANCH="$BRANCH_CHOICE"
fi

if [[ -z "$SELECTED_BRANCH" ]]; then
    log_error "Invalid branch selection"
    exit 1
fi

log_info "Selected branch: $SELECTED_BRANCH"

# PVC size
read -p "Enter PVC size (e.g., 1Gi, 5Gi) [default: 1Gi]: " PVC_SIZE
PVC_SIZE=${PVC_SIZE:-1Gi}

# Image tagging
echo ""
echo "Image tagging options:"
echo "1. Random 5-digit number (default)"
echo "2. Timestamp"
echo "3. Custom tag"
read -p "Choose option [1-3]: " TAG_OPTION
TAG_OPTION=${TAG_OPTION:-1}

case "$TAG_OPTION" in
    1)
        IMAGE_TAG=$(shuf -i 10000-99999 -n 1)
        ;;
    2)
        IMAGE_TAG=$(date +%s)
        ;;
    3)
        read -p "Enter custom tag: " IMAGE_TAG
        if [[ -z "$IMAGE_TAG" ]]; then
            log_error "Custom tag cannot be empty"
            exit 1
        fi
        ;;
    *)
        log_error "Invalid tagging option"
        exit 1
        ;;
esac

log_info "Using image tag: $IMAGE_TAG"

# --- 2. Clone Repository ---

log_info "Cloning repository branch '$SELECTED_BRANCH' into $CLONE_DIR..."

if [ -d "$CLONE_DIR" ]; then
    log_warn "Directory $CLONE_DIR already exists. Removing..."
    rm -rf "$CLONE_DIR"
fi

# Set up authentication for private repos
if [ -n "$GH_TOKEN" ]; then
    AUTH_URL="https://${GH_TOKEN}@github.com/"
    git clone --branch "$SELECTED_BRANCH" "${AUTH_URL}${REPO_URL#https://github.com/}" "$CLONE_DIR"
else
    git clone --branch "$SELECTED_BRANCH" "$REPO_URL" "$CLONE_DIR"
fi

cd "$CLONE_DIR"
log_success "Repository cloned successfully"

# --- 3. Environment Variables ---

log_info "Scanning for .env files..."
ENV_FILES=$(find . -name "*.env" -type f)

CONFIGMAP_DATA=""
SECRET_DATA=""
DATABASE_HOST=""
DB_TYPE=""
DB_IMAGE=""
DB_PORT=""
DB_DATA_PATH=""

if [ -n "$ENV_FILES" ]; then
    log_info "Found .env files, extracting environment variables..."
    
    while read -r envfile; do
        log_info "Processing: $envfile"
        while IFS='=' read -r key val; do
            # Skip empty lines and comments
            [[ -z "$key" || "$key" =~ ^[[:space:]]*# ]] && continue
            
            # Remove quotes from value
            val=$(echo "$val" | sed 's/^["'\'']//' | sed 's/["'\'']$//')
            
            # Detect database type from DATABASE_HOST
            case "$key" in
                DATABASE_HOST|DB_HOST|POSTGRES_HOST|MYSQL_HOST)
                    DATABASE_HOST="$val"
                    export DATABASE_HOST
                    ;;
            esac
            
            # Separate sensitive data
            if [[ "$key" =~ PASSWORD|TOKEN|KEY|SECRET|API ]]; then
                SECRET_DATA="${SECRET_DATA}  ${key}: $(echo -n "$val" | base64 -w 0)\n"
            else
                # Escape backslashes and double quotes for YAML compatibility within echo -e
                val_safe=$(echo "$val" | sed 's/\\/\\\\\\\\/g; s/"/\\"/g')
                CONFIGMAP_DATA="${CONFIGMAP_DATA}  ${key}: \"${val_safe}\"\n"
            fi
        done < "$envfile"
    done <<< "$ENV_FILES"
    
    # Create ConfigMap if data exists
    if [ -n "$CONFIGMAP_DATA" ]; then
        mkdir -p "$MANIFESTS_DIR"
        cat > "$MANIFESTS_DIR/02-configmap.yaml" <<EOF
apiVersion: v1
kind: ConfigMap
metadata:
  name: app-config
  namespace: ${NAMESPACE}
  labels:
    app: ${REPO_NAME}
data:
$(echo -e "$CONFIGMAP_DATA")
EOF
        log_info "Created ConfigMap manifest: 02-configmap.yaml"
    fi
    
    # Create Secret if data exists
    if [ -n "$SECRET_DATA" ]; then
        mkdir -p "$MANIFESTS_DIR"
        cat > "$MANIFESTS_DIR/01-secrets.yaml" <<EOF
apiVersion: v1
kind: Secret
metadata:
  name: app-secrets
  namespace: ${NAMESPACE}
  labels:
    app: ${REPO_NAME}
type: Opaque
data:
$(echo -e "$SECRET_DATA")
EOF
        log_info "Created Secret manifest: 01-secrets.yaml"
    fi
    
    # Determine database type
    if [ -n "$DATABASE_HOST" ]; then
        case "$DATABASE_HOST" in
            *postgres*|*psql*)
                DB_TYPE="postgres"
                DB_IMAGE="postgres:15"
                DB_PORT="5432"
                DB_DATA_PATH="/var/lib/postgresql/data"
                DB_INIT_PATH="/docker-entrypoint-initdb.d"
                ;;
            *mysql*)
                DB_TYPE="mysql"
                DB_IMAGE="mysql:8.0"
                DB_PORT="3306"
                DB_DATA_PATH="/var/lib/mysql"
                DB_INIT_PATH="/docker-entrypoint-initdb.d"
                ;;
            *mongo*|*mongodb*)
                DB_TYPE="mongodb"
                DB_IMAGE="mongo:7.0"
                DB_PORT="27017"
                DB_DATA_PATH="/data/db"
                DB_INIT_PATH="/docker-entrypoint-initdb.d"
                ;;
            *)
                log_warn "Unknown database type from DATABASE_HOST: $DATABASE_HOST"
                DB_TYPE="postgres"
                DB_IMAGE="postgres:15"
                DB_PORT="5432"
                DB_DATA_PATH="/var/lib/postgresql/data"
                DB_INIT_PATH="/docker-entrypoint-initdb.d"
                ;;
        esac
        log_info "Detected database type: $DB_TYPE"
    fi
fi

# --- 4. Database Resources ---

log_info "Scanning for SQL/JS initialization files..."
SQL_FILES=$(find . -name "*.sql")
JS_FILES=$(find . -name "*.js")

if [ -n "$SQL_FILES" ] && [ -n "$DB_TYPE" ] && [ "$DB_TYPE" != "mongodb" ]; then
    log_info "Found SQL files and database type: $DB_TYPE"
    mkdir -p "$MANIFESTS_DIR"
    DB_INIT_FILES="$SQL_FILES"
elif [ -n "$JS_FILES" ] && [ -n "$DB_TYPE" ] && [ "$DB_TYPE" = "mongodb" ]; then
    log_info "Found JS files and MongoDB database type: $DB_TYPE"
    mkdir -p "$MANIFESTS_DIR"
    DB_INIT_FILES="$JS_FILES"
elif [ -n "$SQL_FILES" ] || [ -n "$JS_FILES" ]; then
    log_warn "Found initialization files but no DATABASE_HOST in .env - skipping database creation"
    DB_INIT_FILES=""
else
    DB_INIT_FILES=""
fi

if [ -n "$DB_INIT_FILES" ] && [ -n "$DB_TYPE" ]; then
    
    # Database PVC
    cat > "$MANIFESTS_DIR/${DB_TYPE}-pvc.yaml" <<EOF
apiVersion: v1
kind: PersistentVolumeClaim
metadata:
  name: ${DB_TYPE}-pvc
  namespace: ${NAMESPACE}
  labels:
    app: ${REPO_NAME}
    component: database
spec:
  accessModes:
    - ReadWriteOnce
  resources:
    requests:
      storage: ${PVC_SIZE}
EOF

    # Database Deployment
    cat > "$MANIFESTS_DIR/${DB_TYPE}.yaml" <<EOF
apiVersion: apps/v1
kind: Deployment
metadata:
  name: ${DB_TYPE}
  namespace: ${NAMESPACE}
  labels:
    app: ${REPO_NAME}
    component: database
spec:
  replicas: 1
  selector:
    matchLabels:
      app: ${REPO_NAME}
      component: database
  template:
    metadata:
      labels:
        app: ${REPO_NAME}
        component: database
    spec:
      containers:
      - name: ${DB_TYPE}
        image: ${DB_IMAGE}
        ports:
        - containerPort: ${DB_PORT}
        envFrom:
        - secretRef:
            name: app-secrets
        - configMapRef:
            name: app-config
        env:
        # Map standard database env vars for container images
EOF

        # Add database-specific environment variables
        case "$DB_TYPE" in
            "postgres")
                cat >> "$MANIFESTS_DIR/${DB_TYPE}.yaml" <<EOF
        - name: POSTGRES_PASSWORD
          valueFrom:
            secretKeyRef:
              name: app-secrets
              key: DATABASE_PASSWORD
              optional: true
        - name: POSTGRES_USER
          valueFrom:
            secretKeyRef:
              name: app-secrets
              key: DATABASE_USER
              optional: true
        - name: POSTGRES_DB
          valueFrom:
            configMapKeyRef:
              name: app-config
              key: DATABASE_NAME
              optional: true
EOF
                ;;
            "mysql")
                cat >> "$MANIFESTS_DIR/${DB_TYPE}.yaml" <<EOF
        - name: MYSQL_ROOT_PASSWORD
          valueFrom:
            secretKeyRef:
              name: app-secrets
              key: DATABASE_PASSWORD
              optional: true
        - name: MYSQL_USER
          valueFrom:
            secretKeyRef:
              name: app-secrets
              key: DATABASE_USER
              optional: true
        - name: MYSQL_DATABASE
          valueFrom:
            configMapKeyRef:
              name: app-config
              key: DATABASE_NAME
              optional: true
EOF
                ;;
            "mongodb")
                cat >> "$MANIFESTS_DIR/${DB_TYPE}.yaml" <<EOF
        - name: MONGO_INITDB_ROOT_USERNAME
          valueFrom:
            secretKeyRef:
              name: app-secrets
              key: DATABASE_USER
              optional: true
        - name: MONGO_INITDB_ROOT_PASSWORD
          valueFrom:
            secretKeyRef:
              name: app-secrets
              key: DATABASE_PASSWORD
              optional: true
        - name: MONGO_INITDB_DATABASE
          valueFrom:
            configMapKeyRef:
              name: app-config
              key: DATABASE_NAME
              optional: true
EOF
                ;;
        esac
        cat >> "$MANIFESTS_DIR/${DB_TYPE}.yaml" <<EOF
        volumeMounts:
        - name: db-storage
          mountPath: ${DB_DATA_PATH}
        - name: init-scripts
          mountPath: ${DB_INIT_PATH}
        resources:
          requests:
            memory: "256Mi"
            cpu: "100m"
          limits:
            memory: "512Mi"
            cpu: "200m"
        readinessProbe:
          exec:
            command:
EOF

        # Add database-specific readiness probe
        case "$DB_TYPE" in
            "postgres")
                cat >> "$MANIFESTS_DIR/${DB_TYPE}.yaml" <<EOF
            - /bin/sh
            - -c
            - pg_isready -U \${POSTGRES_USER:-postgres}
EOF
                ;;
            "mysql")
                cat >> "$MANIFESTS_DIR/${DB_TYPE}.yaml" <<EOF
            - /bin/sh
            - -c
            - mysqladmin ping -h localhost -u \${MYSQL_USER:-root} -p\${MYSQL_ROOT_PASSWORD:-}
EOF
                ;;
            "mongodb")
                cat >> "$MANIFESTS_DIR/${DB_TYPE}.yaml" <<EOF
            - mongosh
            - --eval
            - "db.adminCommand('ping')"
EOF
                ;;
        esac

        cat >> "$MANIFESTS_DIR/${DB_TYPE}.yaml" <<EOF
          initialDelaySeconds: 5
          periodSeconds: 5
        livenessProbe:
          exec:
            command:
EOF

        # Add database-specific liveness probe
        case "$DB_TYPE" in
            "postgres")
                cat >> "$MANIFESTS_DIR/${DB_TYPE}.yaml" <<EOF
            - /bin/sh
            - -c
            - pg_isready -U \${POSTGRES_USER:-postgres}
EOF
                ;;
            "mysql")
                cat >> "$MANIFESTS_DIR/${DB_TYPE}.yaml" <<EOF
            - /bin/sh
            - -c
            - mysqladmin ping -h localhost -u \${MYSQL_USER:-root} -p\${MYSQL_ROOT_PASSWORD:-}
EOF
                ;;
            "mongodb")
                cat >> "$MANIFESTS_DIR/${DB_TYPE}.yaml" <<EOF
            - mongosh
            - --eval
            - "db.adminCommand('ping')"
EOF
                ;;
        esac

        cat >> "$MANIFESTS_DIR/${DB_TYPE}.yaml" <<EOF
          initialDelaySeconds: 10
          periodSeconds: 10
      volumes:
      - name: db-storage
        persistentVolumeClaim:
          claimName: ${DB_TYPE}-pvc
      - name: init-scripts
        configMap:
          name: ${REPO_NAME}-db-init
EOF

    # Database Service
    cat > "$MANIFESTS_DIR/${DB_TYPE}-service.yaml" <<EOF
apiVersion: v1
kind: Service
metadata:
  name: ${DB_TYPE}
  namespace: ${NAMESPACE}
  labels:
    app: ${REPO_NAME}
    component: database
spec:
  selector:
    app: ${REPO_NAME}
    component: database
  ports:
  - protocol: TCP
    port: ${DB_PORT}
    targetPort: ${DB_PORT}
  type: ClusterIP
EOF

    # SQL/JS Init ConfigMap
    INIT_CONTENT=""
    if [ "$DB_TYPE" = "mongodb" ]; then
        # For MongoDB, look for .js files
        INIT_FILES=$(find . -name "*.js")
        while read -r initfile; do
            FILENAME=$(basename "$initfile")
            INIT_CONTENT="${INIT_CONTENT}  ${FILENAME}: |\n$(sed 's/^/    /' "$initfile")\n"
        done <<< "$INIT_FILES"
    else
        # For SQL databases, look for .sql files
        while read -r sqlfile; do
            FILENAME=$(basename "$sqlfile")
            INIT_CONTENT="${INIT_CONTENT}  ${FILENAME}: |\n$(sed 's/^/    /' "$sqlfile")\n"
        done <<< "$SQL_FILES"
    fi
    
    cat > "$MANIFESTS_DIR/${DB_TYPE}-init.yaml" <<EOF
apiVersion: v1
kind: ConfigMap
metadata:
  name: ${REPO_NAME}-db-init
  namespace: ${NAMESPACE}
  labels:
    app: ${REPO_NAME}
    component: database
data:
$(echo -e "$INIT_CONTENT")
EOF
    
    log_success "Database manifests created"
elif [ -n "$SQL_FILES" ]; then
    log_warn "Found SQL files but no DATABASE_HOST in .env - skipping database creation"
fi

# --- 5. Application Build & Manifests ---

log_info "Scanning for Dockerfiles..."
DOCKERFILES=$(find . -name "Dockerfile" -type f)

MANIFESTS_CREATED=false

if [ -z "$DOCKERFILES" ]; then
    log_warn "No Dockerfiles found in repository"
else
    mkdir -p "$MANIFESTS_DIR"
    while read -r dockerfile; do
        DOCKERFILE_DIR=$(dirname "$dockerfile")
        
        # Create image and deployment names
        if [ "$DOCKERFILE_DIR" = "." ]; then
            IMAGE_NAME="${REPO_NAME}:${IMAGE_TAG}"
            DEPLOYMENT_NAME="${REPO_NAME}"
        else
            SUBDIR=$(basename "$DOCKERFILE_DIR")
            SUBDIR_SAFE=$(sanitize_name "$SUBDIR")
            IMAGE_NAME="${REPO_NAME}-${SUBDIR_SAFE}:${IMAGE_TAG}"
            DEPLOYMENT_NAME="${REPO_NAME}-${SUBDIR_SAFE}"
        fi
        
        # Build Docker image
        log_info "Building Docker image: $IMAGE_NAME from $dockerfile"
        docker build -t "$IMAGE_NAME" -f "$dockerfile" "$DOCKERFILE_DIR" || {
            log_error "Failed to build $IMAGE_NAME"
            continue
        }
        
        # Track successfully built images
        BUILT_IMAGES+=("$IMAGE_NAME")
        log_success "Built and tagged: $IMAGE_NAME"
        
        # Determine component type and service name
        if [[ "$DEPLOYMENT_NAME" =~ backend ]]; then
            COMPONENT="backend"
            SERVICE_NAME="backend"
            REPLICAS=2
            HEALTH_PATH="/health"
            HEALTH_PORT="httpGet"
        elif [[ "$DEPLOYMENT_NAME" =~ frontend ]]; then
            COMPONENT="frontend"
            SERVICE_NAME="frontend"
            REPLICAS=1
            HEALTH_PATH=""
            HEALTH_PORT="tcpSocket"
        else
            COMPONENT="app"
            SERVICE_NAME="$DEPLOYMENT_NAME"
            REPLICAS=1
            HEALTH_PATH=""
            HEALTH_PORT="tcpSocket"
        fi
        
        # Extract exposed port from Dockerfile (handle multiple EXPOSE lines)
        EXPOSED_PORT=$(grep -Ei "^\s*EXPOSE\s+" "$dockerfile" | head -n 1 | awk '{print $2}' | cut -d/ -f1 | tr -d '"' | tr -d "'")
        if [[ ! "$EXPOSED_PORT" =~ ^[0-9]+$ ]]; then
            log_warn "Could not detect valid port in $dockerfile. Defaulting to 8080."
            EXPOSED_PORT=8080
        fi
        
        # Generate deployment manifest with proper naming
        if [ "$DOCKERFILE_DIR" = "." ]; then
            DEPLOYMENT_FILE="${REPO_NAME}-deployment.yaml"
            SERVICE_FILE="${REPO_NAME}-service.yaml"
        else
            SUBDIR=$(basename "$DOCKERFILE_DIR")
            SUBDIR_SAFE=$(sanitize_name "$SUBDIR")
            DEPLOYMENT_FILE="${REPO_NAME}-${SUBDIR_SAFE}-deployment.yaml"
            SERVICE_FILE="${REPO_NAME}-${SUBDIR_SAFE}-service.yaml"
        fi
        
        cat > "$MANIFESTS_DIR/$DEPLOYMENT_FILE" <<EOF
apiVersion: apps/v1
kind: Deployment
metadata:
  name: ${SERVICE_NAME}
  namespace: ${NAMESPACE}
  labels:
    app: ${REPO_NAME}
    component: ${COMPONENT}
spec:
  replicas: ${REPLICAS}
  selector:
    matchLabels:
      app: ${REPO_NAME}
      component: ${COMPONENT}
  template:
    metadata:
      labels:
        app: ${REPO_NAME}
        component: ${COMPONENT}
    spec:
      containers:
      - name: ${SERVICE_NAME}
        image: ${IMAGE_NAME}
        imagePullPolicy: IfNotPresent
        ports:
        - containerPort: ${EXPOSED_PORT}
EOF

        # Add environment variables for backend
        if [[ "$COMPONENT" == "backend" ]]; then
            cat >> "$MANIFESTS_DIR/$DEPLOYMENT_FILE" <<EOF
        env:
          - name: DB_HOST
            value: "postgres"
          - name: DB_PORT
            value: "${DB_PORT:-5432}"
          - name: DB_NAME
            valueFrom:
              configMapKeyRef:
                name: app-config
                key: DATABASE_NAME
                optional: true
          - name: DB_USER
            valueFrom:
              secretKeyRef:
                name: app-secrets
                key: DATABASE_USER
                optional: true
          - name: DB_PASSWORD
            valueFrom:
              secretKeyRef:
                name: app-secrets
                key: DATABASE_PASSWORD
                optional: true
        envFrom:
          - secretRef:
              name: app-secrets
              optional: true
          - configMapRef:
              name: app-config
              optional: true
EOF
        else
            cat >> "$MANIFESTS_DIR/$DEPLOYMENT_FILE" <<EOF
        envFrom:
          - secretRef:
              name: app-secrets
              optional: true
          - configMapRef:
              name: app-config
              optional: true
EOF
        fi

        # Add resources and probes
        cat >> "$MANIFESTS_DIR/$DEPLOYMENT_FILE" <<EOF
        resources:
          requests:
            memory: "$([ "$COMPONENT" == "backend" ] && echo "128Mi" || echo "64Mi")"
            cpu: "$([ "$COMPONENT" == "backend" ] && echo "100m" || echo "50m")"
          limits:
            memory: "$([ "$COMPONENT" == "backend" ] && echo "256Mi" || echo "128Mi")"
            cpu: "$([ "$COMPONENT" == "backend" ] && echo "200m" || echo "100m")"
EOF

        # Add health probes
        if [ -n "$HEALTH_PATH" ]; then
            cat >> "$MANIFESTS_DIR/$DEPLOYMENT_FILE" <<EOF
        readinessProbe:
          ${HEALTH_PORT}:
            path: ${HEALTH_PATH}
            port: ${EXPOSED_PORT}
          initialDelaySeconds: 10
          periodSeconds: 5
        livenessProbe:
          ${HEALTH_PORT}:
            path: ${HEALTH_PATH}
            port: ${EXPOSED_PORT}
          initialDelaySeconds: 15
          periodSeconds: 10
EOF
        else
            cat >> "$MANIFESTS_DIR/$DEPLOYMENT_FILE" <<EOF
        readinessProbe:
          ${HEALTH_PORT}:
            port: ${EXPOSED_PORT}
          initialDelaySeconds: 5
          periodSeconds: 5
        livenessProbe:
          ${HEALTH_PORT}:
            port: ${EXPOSED_PORT}
          initialDelaySeconds: 10
          periodSeconds: 10
EOF
        fi

        # Close deployment manifest
        cat >> "$MANIFESTS_DIR/$DEPLOYMENT_FILE" <<EOF
EOF

        # Generate service manifest
        cat > "$MANIFESTS_DIR/$SERVICE_FILE" <<EOF
---
apiVersion: v1
kind: Service
metadata:
  name: ${SERVICE_NAME}
  namespace: ${NAMESPACE}
  labels:
    app: ${REPO_NAME}
    component: ${COMPONENT}
spec:
  type: $([ "$COMPONENT" == "frontend" ] && echo "LoadBalancer" || echo "ClusterIP")
  selector:
    app: ${REPO_NAME}
    component: ${COMPONENT}
  ports:
  - port: ${EXPOSED_PORT}
    targetPort: ${EXPOSED_PORT}
EOF
        
        log_info "Created manifests: $DEPLOYMENT_FILE and $SERVICE_FILE"
        MANIFESTS_CREATED=true
    done <<< "$DOCKERFILES"
fi

# --- 6. Kind Cluster Management ---

log_info "Checking for kind clusters..."
ALL_CLUSTERS=$(kind get clusters 2>/dev/null || true)

if [ -z "$ALL_CLUSTERS" ]; then
    log_warn "No kind clusters found"
    read -p "Create a new kind cluster? (y/n): " CREATE_CLUSTER
    
    if [[ "$CREATE_CLUSTER" =~ ^[Yy]$ ]]; then
        read -p "Enter cluster name [default: kind]: " CLUSTER_NAME
        CLUSTER_NAME=${CLUSTER_NAME:-kind}
        
        log_info "Creating kind cluster: $CLUSTER_NAME"
        kind create cluster --name "$CLUSTER_NAME"
        KIND_CLUSTER="$CLUSTER_NAME"
    else
        log_error "No cluster available for deployment"
        exit 1
    fi
else
    echo ""
    log_info "Found kind clusters:"
    echo "$ALL_CLUSTERS" | awk '{print NR". "$1}'
    echo ""
    
    read -p "Select cluster number or name [default: first]: " CLUSTER_CHOICE
    
    if [[ -z "$CLUSTER_CHOICE" ]]; then
        KIND_CLUSTER=$(echo "$ALL_CLUSTERS" | head -n 1)
    elif [[ "$CLUSTER_CHOICE" =~ ^[0-9]+$ ]]; then
        KIND_CLUSTER=$(echo "$ALL_CLUSTERS" | sed -n "${CLUSTER_CHOICE}p")
    else
        KIND_CLUSTER="$CLUSTER_CHOICE"
    fi
    
    if [ -z "$KIND_CLUSTER" ]; then
        log_error "Invalid cluster selection"
        exit 1
    fi
    
    log_info "Selected cluster: $KIND_CLUSTER"
    
    # Verify cluster health
    log_info "Verifying cluster health for: $KIND_CLUSTER"
    
    # Check if kind recognizes the cluster
    if ! kind get clusters | grep -q "^${KIND_CLUSTER}$"; then
        log_error "Kind does not recognize cluster '$KIND_CLUSTER'"
        read -p "Recreate cluster '$KIND_CLUSTER'? (y/n): " RECREATE
        if [[ "$RECREATE" =~ ^[Yy]$ ]]; then
            kind delete cluster --name "$KIND_CLUSTER" 2>/dev/null || true
            kind create cluster --name "$KIND_CLUSTER"
        else
            log_error "Cannot proceed with corrupted cluster"
            exit 1
        fi
    fi
    
    # Get list of nodes
    mapfile -t KIND_NODES < <(kind get nodes --name "$KIND_CLUSTER" 2>/dev/null)
    
    # Fallback: use docker labels
    if [ ${#KIND_NODES[@]} -eq 0 ]; then
        mapfile -t KIND_NODES < <(docker ps --filter "label=io.x-k8s.kind.cluster=${KIND_CLUSTER}" --format "{{.Names}}")
    fi
    
    if [ ${#KIND_NODES[@]} -eq 0 ]; then
        log_error "No kind nodes found for cluster '$KIND_CLUSTER'"
        log_info "This usually means the cluster is broken"
        read -p "Recreate cluster '$KIND_CLUSTER'? (y/n): " RECREATE
        if [[ "$RECREATE" =~ ^[Yy]$ ]]; then
            kind delete cluster --name "$KIND_CLUSTER" 2>/dev/null || true
            kind create cluster --name "$KIND_CLUSTER"
            mapfile -t KIND_NODES < <(kind get nodes --name "$KIND_CLUSTER" 2>/dev/null)
        else
            log_error "Cannot proceed with corrupted cluster"
            exit 1
        fi
    fi
    
    log_success "Found ${#KIND_NODES[@]} node(s): ${KIND_NODES[*]}"
    
    # Verify kubectl connectivity
    if ! kubectl cluster-info --request-timeout=5s >/dev/null 2>&1; then
        log_warn "kubectl cannot connect to cluster API server"
        log_info "Attempting to switch context to kind-$KIND_CLUSTER"
        kubectl config use-context "kind-$KIND_CLUSTER" || {
            log_error "Failed to set kubectl context"
            exit 1
        }
    fi
fi

# --- 7. Load Images into Kind ---

log_info "Preparing to load images into kind cluster: $KIND_CLUSTER"

# Verify cluster and nodes exist
if ! kind get clusters | grep -q "^${KIND_CLUSTER}$"; then
    log_error "Kind does not recognize cluster '$KIND_CLUSTER' anymore"
    exit 1
fi

# Get list of nodes
mapfile -t KIND_NODES < <(kind get nodes --name "$KIND_CLUSTER" 2>/dev/null)

# Fallback: use docker labels
if [ ${#KIND_NODES[@]} -eq 0 ]; then
    mapfile -t KIND_NODES < <(docker ps --filter "label=io.x-k8s.kind.cluster=${KIND_CLUSTER}" --format "{{.Names}}")
fi

if [ ${#KIND_NODES[@]} -eq 0 ]; then
    log_error "No kind nodes found for cluster '$KIND_CLUSTER'"
    exit 1
else
    log_info "Found ${#KIND_NODES[@]} node(s): ${KIND_NODES[*]}"
fi

# Load images
if [ ${#BUILT_IMAGES[@]} -eq 0 ]; then
    log_warn "No images were successfully built — nothing to load"
else
    log_info "Loading ${#BUILT_IMAGES[@]} image(s) into cluster $KIND_CLUSTER"

    for image in "${BUILT_IMAGES[@]}"; do
        log_info "Loading image → $image"

        # Try normal load first
        if kind load docker-image "$image" --name "$KIND_CLUSTER" 2>/dev/null; then
            log_success "Loaded $image (auto node discovery)"
            continue
        fi

        log_warn "Auto load failed — falling back to explicit node(s)"

        # Fallback: load to each known node individually
        loaded=false
        for node in "${KIND_NODES[@]}"; do
            if [[ -z "$node" ]]; then continue; fi
            log_info "  Trying node: $node"
            if kind load docker-image "$image" --name "$KIND_CLUSTER" --nodes "$node" 2>/dev/null; then
                log_success "  → Loaded to $node"
                loaded=true
            else
                log_warn "  → Failed on $node"
            fi
        done

        if ! $loaded; then
            log_error "Failed to load $image on any node"
        fi
    done
fi

# --- 8. Deploy to Kubernetes ---

# Create namespace manifest
mkdir -p "$MANIFESTS_DIR"
cat > "$MANIFESTS_DIR/namespace.yaml" <<EOF
apiVersion: v1
kind: Namespace
metadata:
  name: ${NAMESPACE}
EOF
log_info "Created namespace manifest: namespace.yaml"

# Create namespace
log_info "Creating Kubernetes namespace: $NAMESPACE"
kubectl create namespace "$NAMESPACE" --dry-run=client -o yaml | kubectl apply -f -

# Apply manifests
MANIFESTS_APPLIED=false
if [ "$MANIFESTS_CREATED" = true ]; then
    echo ""
    log_info "Manifests generated in: $MANIFESTS_DIR"
    log_info "Generated files:"
    ls -la "$MANIFESTS_DIR"/*.yaml 2>/dev/null || true
    echo ""
    read -p "Apply these manifests to cluster '$KIND_CLUSTER'? (y/n) [default: y]: " APPLY_MANIFESTS
    APPLY_MANIFESTS=${APPLY_MANIFESTS:-y}
    
    if [[ "$APPLY_MANIFESTS" =~ ^[Yy]$ ]]; then
        log_info "Applying Kubernetes manifests..."
        kubectl apply -f "$MANIFESTS_DIR/" || {
            log_error "Failed to apply manifests"
            exit 1
        }
        log_success "Manifests applied successfully"
        MANIFESTS_APPLIED=true
    else
        log_warn "Skipping manifest application"
        log_info "You can apply them later with:"
        echo "  kubectl apply -f $MANIFESTS_DIR/"
        echo ""
        log_info "Cleanup command when done: rm -rf $CLONE_DIR"
    fi
else
    log_warn "No manifests to apply"
fi

# --- 9. Verify Database Initialization ---

if [ "$MANIFESTS_APPLIED" = true ] && [ -n "$DB_INIT_FILES" ] && [ -n "$DB_TYPE" ]; then
    log_info "Waiting for database pod to be ready..."
    if kubectl wait --for=condition=ready pod -l component=database -n "$NAMESPACE" --timeout=120s; then
        log_success "Database pod is ready"
        
        # Give database time to initialize
        sleep 5
        
        # Check logs for initialization with SQL execution verification
        DB_POD=$(kubectl get pods -n "$NAMESPACE" -l component=database -o jsonpath='{.items[0].metadata.name}' 2>/dev/null)
        if [ -n "$DB_POD" ]; then
            # Wait a bit more for initialization scripts to run
            sleep 10
            
            LOGS=$(kubectl logs "$DB_POD" -n "$NAMESPACE" 2>/dev/null)
            INIT_SUCCESS=false
            
            case "$DB_TYPE" in
                "postgres")
                    # Check for database ready AND SQL execution
                    if echo "$LOGS" | grep -qi "database system is ready\|ready for connections"; then
                        # Check for SQL file execution
                        if echo "$LOGS" | grep -qi "CREATE TABLE\|INSERT INTO\|ALTER TABLE" || echo "$LOGS" | grep -qi "running initialization script\|executing.*\.sql"; then
                            log_success "PostgreSQL initialization and SQL execution successful"
                            INIT_SUCCESS=true
                        else
                            log_warn "PostgreSQL is ready but SQL execution not detected. Checking for init files..."
                            # Count SQL files (guaranteed > 0 by outer check)
                            SQL_COUNT=$(echo "$DB_INIT_FILES" | wc -l)
                            log_warn "Found $SQL_COUNT SQL file(s) but no execution evidence. Reapplying database deployment..."
                            INIT_SUCCESS=false
                        fi
                    else
                        log_warn "PostgreSQL not ready yet"
                    fi
                    ;;
                "mysql")
                    if echo "$LOGS" | grep -qi "mysqld: ready for connections\|ready for start up"; then
                        # Check for SQL execution
                        if echo "$LOGS" | grep -qi "CREATE TABLE\|INSERT INTO\|ALTER TABLE" || echo "$LOGS" | grep -qi "running.*\.sql\|executing.*\.sql"; then
                            log_success "MySQL initialization and SQL execution successful"
                            INIT_SUCCESS=true
                        else
                            log_warn "MySQL is ready but SQL execution not detected. Checking for init files..."
                            SQL_COUNT=$(echo "$DB_INIT_FILES" | wc -l)
                            log_warn "Found $SQL_COUNT SQL file(s) but no execution evidence. Reapplying database deployment..."
                            INIT_SUCCESS=false
                        fi
                    else
                        log_warn "MySQL not ready yet"
                    fi
                    ;;
                "mongodb")
                    if echo "$LOGS" | grep -qi "waiting for connections\|MongoDB starting"; then
                        # Check for JS execution
                        if echo "$LOGS" | grep -qi "executing.*\.js\|running.*\.js" || echo "$LOGS" | grep -qi "db\.createCollection\|db\.insert"; then
                            log_success "MongoDB initialization and JS execution successful"
                            INIT_SUCCESS=true
                        else
                            log_warn "MongoDB is ready but JS execution not detected. Checking for init files..."
                            JS_COUNT=$(echo "$DB_INIT_FILES" | wc -l)
                            log_warn "Found $JS_COUNT JS file(s) but no execution evidence. Reapplying database deployment..."
                            INIT_SUCCESS=false
                        fi
                    else
                        log_warn "MongoDB not ready yet"
                    fi
                    ;;
            esac
            
            # If initialization failed, retry by redeploying database
            if [ "$INIT_SUCCESS" = false ]; then
                log_warn "Database initialization incomplete. Redeploying database resources..."
                kubectl delete -f "$MANIFESTS_DIR/${DB_TYPE}.yaml" --ignore-not-found
                kubectl delete -f "$MANIFESTS_DIR/${DB_TYPE}-init.yaml" --ignore-not-found
                sleep 5
                kubectl apply -f "$MANIFESTS_DIR/${DB_TYPE}-init.yaml"
                kubectl apply -f "$MANIFESTS_DIR/${DB_TYPE}.yaml"
                
                log_info "Waiting for database redeployment to complete..."
                if kubectl wait --for=condition=ready pod -l component=database -n "$NAMESPACE" --timeout=120s; then
                    sleep 15  # Give more time for init scripts
                    REDO_LOGS=$(kubectl logs "$DB_POD" -n "$NAMESPACE" 2>/dev/null | tail -30)
                    case "$DB_TYPE" in
                        "postgres")
                            if echo "$REDO_LOGS" | grep -qi "CREATE TABLE\|INSERT INTO\|ALTER TABLE"; then
                                log_success "PostgreSQL SQL execution successful after redeployment"
                            else
                                log_warn "PostgreSQL SQL execution still not detected. Manual check may be needed."
                            fi
                            ;;
                        "mysql")
                            if echo "$REDO_LOGS" | grep -qi "CREATE TABLE\|INSERT INTO\|ALTER TABLE"; then
                                log_success "MySQL SQL execution successful after redeployment"
                            else
                                log_warn "MySQL SQL execution still not detected. Manual check may be needed."
                            fi
                            ;;
                        "mongodb")
                            if echo "$REDO_LOGS" | grep -qi "db\.createCollection\|db\.insert"; then
                                log_success "MongoDB JS execution successful after redeployment"
                            else
                                log_warn "MongoDB JS execution still not detected. Manual check may be needed."
                            fi
                            ;;
                    esac
                else
                    log_error "Database redeployment failed"
                fi
            fi
        fi
    else
        log_warn "Database pod not ready within timeout. Attempting redeployment..."
        kubectl delete -f "$MANIFESTS_DIR/${DB_TYPE}.yaml" --ignore-not-found
        sleep 5
        kubectl apply -f "$MANIFESTS_DIR/${DB_TYPE}.yaml"
        kubectl wait --for=condition=ready pod -l component=database -n "$NAMESPACE" --timeout=120s || {
            log_error "Database redeployment failed"
        }
    fi
fi

# --- 10. Final Status ---

echo ""
log_success "=== Deployment Complete ==="
log_info "Namespace: $NAMESPACE"
log_info "Cluster: $KIND_CLUSTER"
log_info "Images built: ${#BUILT_IMAGES[@]}"
log_info "Manifests directory: $MANIFESTS_DIR"
log_info "Cleanup command: rm -rf $CLONE_DIR"

if [ "$MANIFESTS_CREATED" = true ]; then
    echo ""
    log_info "Useful commands:"
    echo "  kubectl get pods -n $NAMESPACE"
    echo "  kubectl get services -n $NAMESPACE"
    echo "  kubectl logs -n $NAMESPACE -l component=database"
    echo "  kubectl port-forward -n $NAMESPACE service/frontend 8080:80"
fi

echo ""
log_success "Done!"