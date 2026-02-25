#!/usr/bin/env bash
set -euo pipefail
trap 'echo -e "${RED}Script failed${NC}"; exit 1' ERR

# Colors
GREEN='\033[0;32m'
BLUE='\033[0;34m'
YELLOW='\033[1;33m'
RED='\033[0;31m'
NC='\033[0m'

log_info() { echo -e "${BLUE}[INFO]${NC} $1"; }
log_success() { echo -e "${GREEN}[SUCCESS]${NC} $1"; }
log_warn() { echo -e "${YELLOW}[WARN]${NC} $1"; }
log_error() { echo -e "${RED}[ERROR]${NC} $1"; }

# Configuration
CLUSTER_NAME="staging-cluster"

# --- 1. Gather User Input ---

echo -e "${GREEN}Interactive Git-to-K8s Deployer${NC}"
echo "----------------------------------------"

# Git Info
while true; do
    read -p "Enter GitHub Repository URL: " REPO_URL
    if [[ -z "$REPO_URL" ]]; then
        log_error "Repository URL is required."
        continue
    fi
    
    # Validate GitHub URL format
    if [[ ! "$REPO_URL" =~ ^https://github\.com/[^/]+/[^/]+(\.git)?$ ]]; then
        log_error "Invalid GitHub URL format. Expected: https://github.com/user/repo or https://github.com/user/repo.git"
        continue
    fi
    
    log_success "Valid GitHub URL provided."
    break
done

read -p "Enter Branch Name [main]: " BRANCH
BRANCH=${BRANCH:-main}
log_info "Using branch: $BRANCH"

read -p "Enter GitHub Token (optional for public repos): " GITHUB_TOKEN
if [[ -n "$GITHUB_TOKEN" ]]; then
    echo "Using provided token for authentication."
else
    echo "No token provided - will attempt public clone."
fi

# Configuration
defaults_pvc="5Gi"
while true; do
    read -p "Enter PVC Size [${defaults_pvc}]: " PVC_SIZE
    PVC_SIZE=${PVC_SIZE:-$defaults_pvc}
    
    # Validate PVC size format (e.g., 5Gi, 10Gi, 1Ti)
    if [[ "$PVC_SIZE" =~ ^[0-9]+[KMGT]i$ ]]; then
        log_success "PVC size set to: $PVC_SIZE"
        break
    else
        log_error "Invalid PVC size format. Use format like: 5Gi, 10Gi, 1Ti, 500Mi"
    fi
done

# Tagging
echo ""
echo "Image Tag Options:"
echo "  1) Random 5-digit numeric tag"
echo "  2) Timestamp-based tag (default)"
echo "  3) Custom tag"
read -p "Select tagging option [1-3, default: 2]: " TAG_OPTION
TAG_OPTION=${TAG_OPTION:-2}

case "$TAG_OPTION" in
    1)
        IMG_TAG=$(printf "%05d" $((RANDOM % 100000)))
        log_info "Generated random tag: $IMG_TAG"
        ;;
    2)
        IMG_TAG="$(date +%Y%m%d-%H%M%S)"
        log_info "Using timestamp tag: $IMG_TAG"
        ;;
    3)
        read -p "Enter custom image tag: " CUSTOM_TAG
        if [[ -z "$CUSTOM_TAG" ]]; then
            log_warn "Empty tag provided, using timestamp instead"
            IMG_TAG="$(date +%Y%m%d-%H%M%S)"
        else
            IMG_TAG="$CUSTOM_TAG"
        fi
        log_info "Using custom tag: $IMG_TAG"
        ;;
    *)
        log_warn "Invalid option, using timestamp"
        IMG_TAG="$(date +%Y%m%d-%H%M%S)"
        ;;
esac

# ── Check/create Kind cluster early ──

log_info "Checking/creating Kind cluster '${CLUSTER_NAME}'..."

if ! kind get clusters | grep -q "^${CLUSTER_NAME}$"; then
    read -p "Create Kind cluster '${CLUSTER_NAME}'? [Y/n] " -n 1 -r
    echo
    if [[ ! $REPLY =~ ^[Nn]$ ]]; then
        kind create cluster --name "$CLUSTER_NAME" --wait=90s
        log_success "Kind cluster created."
    else
        log_error "Cannot continue without cluster."
        exit 1
    fi
fi

# --- 2. Clone Repository ---

REPO_NAME=$(basename "$REPO_URL" .git)
NAMESPACE=$(basename "${REPO_URL}" .git | tr '[:upper:]' '[:lower:]' | tr -cd '[:alnum:]-')
TARGET_DIR="${REPO_NAME}"

log_info "Cloning $REPO_URL (branch: $BRANCH) into $TARGET_DIR..."

if [ -d "$TARGET_DIR" ]; then
    log_warn "Directory $TARGET_DIR already exists. Removing..."
    rm -rf "$TARGET_DIR"
fi

if [[ -n "$GITHUB_TOKEN" ]]; then
    # Inject token into URL for authentication
    # Format: https://<token>@github.com/user/repo.git
    AUTH_URL=$(echo "$REPO_URL" | sed "s|https://|https://$GITHUB_TOKEN@|")
    git clone --branch "$BRANCH" "$AUTH_URL" "$TARGET_DIR"
else
    git clone --branch "$BRANCH" "$REPO_URL" "$TARGET_DIR"
fi

cd "$TARGET_DIR"
log_success "Cloned successfully."

# --- 3. Setup Layout ---

MANIFEST_DIR="manifests-k8s"
mkdir -p "$MANIFEST_DIR"

# --- 4. Database Logic ---

DB_DEPLOYED=false

# Scan for .env Files
ENV_FILES=$(find . -maxdepth 1 -name ".env*")
HAS_ENV=false
DETECTED_DB_HOST=""
DETECTED_DB_HOST_KEY=""
DETECTED_DB_USER_KEY=""
DETECTED_DB_PASS_KEY=""
DETECTED_DB_NAME_KEY=""

if [[ -n "$ENV_FILES" ]]; then
    HAS_ENV=true
    log_info "Found .env file(s). Scanning for configuration..."
    
    # Extract keys and values for ConfigMap/Secret
    # We'll put passwords/keys in Secret, others in ConfigMap
    
    # Temporary files for accumulating vars
    touch .env.cats_secrets
    touch .env.cats_config
    
    # Initialize detected keys
    DETECTED_DB_HOST_KEY=""
    DETECTED_DB_USER_KEY=""
    DETECTED_DB_PASS_KEY=""
    DETECTED_DB_NAME_KEY=""

    cat $ENV_FILES | grep -v '^#' | grep '=' | while read -r line; do
        key=$(echo "$line" | cut -d= -f1)
        val=$(echo "$line" | cut -d= -f2-)
        # Remove potential quotes
        val="${val%\"}"
        val="${val#\"}"
        
        if [[ "$key" =~ (PASSWORD|SECRET|KEY|TOKEN|USER) ]]; then
            echo "$key=$val" >> .env.cats_secrets
        else
            echo "$key=$val" >> .env.cats_config
        fi
        
        # Detect DB Keys
        case "$key" in
            DATABASE_HOST|DB_HOST|POSTGRES_HOST|MYSQL_HOST)
                export DETECTED_DB_HOST="$val"
                export DETECTED_DB_HOST_KEY="$key"
                ;;
            DATABASE_USER|DB_USER|POSTGRES_USER|MYSQL_USER)
                export DETECTED_DB_USER_KEY="$key"
                ;;
            DATABASE_PASSWORD|DB_PASSWORD|POSTGRES_PASSWORD|MYSQL_PASSWORD|DB_PASS|DATABASE_PASS)
                export DETECTED_DB_PASS_KEY="$key"
                ;;
            DATABASE_NAME|DB_NAME|POSTGRES_DB|MYSQL_DATABASE|MYSQL_DB|MONGO_INITDB_DATABASE)
                export DETECTED_DB_NAME_KEY="$key"
                ;;
        esac
    done

    # Generate Secret YAML
    if [ -s .env.cats_secrets ]; then
        log_info "Generating 01-secrets.yaml..."
        cat > "$MANIFEST_DIR/01-secrets.yaml" <<EOF
apiVersion: v1
kind: Secret
metadata:
  name: ${NAMESPACE}-secrets
  namespace: $NAMESPACE
type: Opaque
data:
EOF
        while IFS='=' read -r k v; do
            encoded=$(echo -n "$v" | base64)
            echo "  $k: $encoded" >> "$MANIFEST_DIR/01-secrets.yaml"
        done < .env.cats_secrets
    fi

    # Generate ConfigMap YAML
    if [ -s .env.cats_config ]; then
        log_info "Generating 01-configmap.yaml..."
        cat > "$MANIFEST_DIR/01-configmap.yaml" <<EOF
apiVersion: v1
kind: ConfigMap
metadata:
  name: ${NAMESPACE}-config
  namespace: $NAMESPACE
data:
EOF
        while IFS='=' read -r k v; do
            echo "  $k: \"$v\"" >> "$MANIFEST_DIR/01-configmap.yaml"
        done < .env.cats_config
    fi
    
    # Reload detected keys from exported variables if set in subshell loop
    # Actually variables exported in the while loop might not survive if piped.
    # Let's use a temporary file to store detected keys or process differently.
    # To be safe, re-scan the generated .env.cats_* files
    if [[ -f .env.cats_config ]]; then
        line=$(grep -E "^(DATABASE_HOST|DB_HOST|POSTGRES_HOST|MYSQL_HOST)=" .env.cats_config | head -n 1)
        if [[ -n "$line" ]]; then
            DETECTED_DB_HOST_KEY=$(echo "$line" | cut -d= -f1)
            DETECTED_DB_HOST=$(echo "$line" | cut -d= -f2-)
        fi
        line=$(grep -E "^(DATABASE_NAME|DB_NAME|POSTGRES_DB|MYSQL_DATABASE|MYSQL_DB|MONGO_INITDB_DATABASE)=" .env.cats_config | head -n 1)
        [[ -n "$line" ]] && DETECTED_DB_NAME_KEY=$(echo "$line" | cut -d= -f1)
    fi
    if [[ -f .env.cats_secrets ]]; then
        line=$(grep -E "^(DATABASE_USER|DB_USER|POSTGRES_USER|MYSQL_USER)=" .env.cats_secrets | head -n 1)
        [[ -n "$line" ]] && DETECTED_DB_USER_KEY=$(echo "$line" | cut -d= -f1)
        line=$(grep -E "^(DATABASE_PASSWORD|DB_PASSWORD|POSTGRES_PASSWORD|MYSQL_PASSWORD|DB_PASS|DATABASE_PASS)=" .env.cats_secrets | head -n 1)
        [[ -n "$line" ]] && DETECTED_DB_PASS_KEY=$(echo "$line" | cut -d= -f1)
    fi
    
    rm -f .env.cats_secrets .env.cats_config
fi

# Function to verify database SQL execution
verify_db_execution() {
    local db_host=$1
    local namespace=$2
    local max_attempts=3
    local attempt=1
    
    log_info "Verifying database initialization for $db_host..."
    
    # Wait for database pod to be ready
    log_info "Waiting for database pod to be ready (max 120s)..."
    if ! kubectl wait --for=condition=ready pod -l app=$db_host -n $namespace --timeout=120s 2>/dev/null; then
        log_warn "Database pod not ready within timeout. SQL execution may be delayed."
        return 1
    fi
    
    log_success "Database pod is ready."
    
    # Give the database a few seconds to complete initialization
    sleep 5
    
    # Check if SQL files were executed by looking at logs
    log_info "Checking database initialization logs..."
    local db_pod=$(kubectl get pods -n $namespace -l app=$db_host -o jsonpath='{.items[0].metadata.name}' 2>/dev/null)
    
    if [[ -z "$db_pod" ]]; then
        log_error "Could not find database pod"
        return 1
    fi
    
    # Check logs for initialization markers
    local logs=$(kubectl logs $db_pod -n $namespace 2>/dev/null | tail -50)
    
    # Different databases have different success markers
    local init_success=false
    if echo "$logs" | grep -qi "database system is ready to accept connections\|ready for connections\|mysqld: ready for connections\|waiting for connections\|MongoDB init process complete"; then
        init_success=true
    fi
    
    if [[ "$init_success" == "true" ]]; then
        log_success "Database initialization appears successful."
        
        # Additional check: verify initialization file execution markers
        if echo "$logs" | grep -qi "init.sql\|\.sql\|init.js\|\.js\|executed successfully"; then
            log_success "Initialization files were processed."
        else
            log_warn "Could not confirm initialization file execution in logs. Database may need manual verification."
        fi
        return 0
    else
        log_warn "Database initialization status unclear from logs."
        return 1
    fi
}

# Function to reapply database deployment if needed
reapply_db_if_needed() {
    local db_host=$1
    local namespace=$2
    local manifest_dir=$3
    
    log_info "Checking if database redeployment is needed..."
    
    if ! verify_db_execution "$db_host" "$namespace"; then
        log_warn "Database initialization may have failed. Attempting redeployment..."
        
        # Delete and recreate the database deployment
        kubectl delete deployment $db_host -n $namespace --ignore-not-found=true
        sleep 3
        
        # Reapply database manifests
        kubectl apply -f "$manifest_dir/02-db-deployment.yaml"
        
        log_info "Database deployment reapplied. Waiting for pod to be ready..."
        if kubectl wait --for=condition=ready pod -l app=$db_host -n $namespace --timeout=120s 2>/dev/null; then
            log_success "Database redeployment successful."
            verify_db_execution "$db_host" "$namespace"
        else
            log_error "Database redeployment failed. Manual intervention may be required."
            return 1
        fi
    fi
    
    return 0
}

if [[ "$HAS_ENV" == "true" && -n "$DETECTED_DB_HOST" ]]; then
    log_info "Detected DATABASE_HOST: $DETECTED_DB_HOST"
    
    # Improved mapping logic
    DB_IMAGE=""
    DB_PORT=""
    DB_MOUNT_PATH=""
    DB_ENV_PREFIX=""

    case "$DETECTED_DB_HOST" in
      *postgres*|*psql*)
        DB_IMAGE="postgres:16-alpine"
        DB_PORT=5432
        DB_MOUNT_PATH="/var/lib/postgresql/data"
        DB_ENV_PREFIX="POSTGRES"
        ;;
      *mysql*|*mariadb*)
        DB_IMAGE="mysql:9"
        DB_PORT=3306
        DB_MOUNT_PATH="/var/lib/mysql"
        DB_ENV_PREFIX="MYSQL"
        ;;
      *mongo*|*mongodb*)
        DB_IMAGE="mongo:7"
        DB_PORT=27017
        DB_MOUNT_PATH="/data/db"
        DB_ENV_PREFIX="MONGO"
        ;;
    esac

    if [[ -n "$DB_IMAGE" ]]; then
        # Check for any *.sql files in the repo (for PostgreSQL/MySQL) or *.js files (for MongoDB)
        SQL_FILES=$(find . -name "*.sql" -type f)
        JS_FILES=$(find . -name "*.js" -type f)
        
        # Determine initialization files based on database type
        INIT_FILES=""
        INIT_TYPE=""
        
        if [[ "$DB_ENV_PREFIX" == "MONGO" ]]; then
            if [[ -n "$JS_FILES" ]]; then
                INIT_FILES="$JS_FILES"
                INIT_TYPE="JavaScript"
                log_info "Found JavaScript files for MongoDB initialization."
            fi
        else
            if [[ -n "$SQL_FILES" ]]; then
                INIT_FILES="$SQL_FILES"
                INIT_TYPE="SQL"
                log_info "Found SQL files for database initialization."
            fi
        fi
        
        if [[ -n "$INIT_FILES" ]]; then
            log_info "Creating $DB_ENV_PREFIX resources with PVC using $INIT_TYPE files..."
            
            # PVC
            cat > "$MANIFEST_DIR/02-db-pvc.yaml" <<EOF
apiVersion: v1
kind: PersistentVolumeClaim
metadata:
  name: ${DETECTED_DB_HOST}-pvc
  namespace: $NAMESPACE
spec:
  accessModes:
    - ReadWriteOnce
  resources:
    requests:
      storage: $PVC_SIZE
EOF

            # Deployment
            cat > "$MANIFEST_DIR/02-db-deployment.yaml" <<EOF
apiVersion: apps/v1
kind: Deployment
metadata:
  name: $DETECTED_DB_HOST
  namespace: $NAMESPACE
spec:
  replicas: 1
  selector:
    matchLabels:
      app: $DETECTED_DB_HOST
  template:
    metadata:
      labels:
        app: $DETECTED_DB_HOST
    spec:
      containers:
      - name: $DETECTED_DB_HOST
        image: $DB_IMAGE
        ports:
        - containerPort: $DB_PORT
        envFrom:
        - secretRef:
            name: ${NAMESPACE}-secrets
        - configMapRef:
            name: ${NAMESPACE}-config
        env:
          # DB-specific env vars
          - name: ${DB_ENV_PREFIX}_PASSWORD
            valueFrom:
              secretKeyRef:
                name: ${NAMESPACE}-secrets
                key: ${DETECTED_DB_PASS_KEY:-DATABASE_PASSWORD}
                optional: true
          - name: ${DB_ENV_PREFIX}_USER
            valueFrom:
              secretKeyRef:
                name: ${NAMESPACE}-secrets
                key: ${DETECTED_DB_USER_KEY:-DATABASE_USER}
                optional: true
          - name: ${DB_ENV_PREFIX}_DB
            valueFrom:
              configMapKeyRef:
                name: ${NAMESPACE}-config
                key: ${DETECTED_DB_NAME_KEY:-DATABASE_NAME}
                optional: true
        volumeMounts:
        - name: db-data
          mountPath: $DB_MOUNT_PATH
EOF
            
            # Add initialization volume mount for MongoDB (different path)
            if [[ "$DB_ENV_PREFIX" == "MONGO" ]]; then
                cat >> "$MANIFEST_DIR/02-db-deployment.yaml" <<EOF
        - name: init-js
          mountPath: /docker-entrypoint-initdb.d
EOF
            else
                cat >> "$MANIFEST_DIR/02-db-deployment.yaml" <<EOF
        - name: init-sql
          mountPath: /docker-entrypoint-initdb.d
EOF
            fi
            
            cat >> "$MANIFEST_DIR/02-db-deployment.yaml" <<EOF
      volumes:
      - name: db-data
        persistentVolumeClaim:
          claimName: ${DETECTED_DB_HOST}-pvc
EOF
            
            # Add initialization volume for MongoDB or SQL databases
            if [[ "$DB_ENV_PREFIX" == "MONGO" ]]; then
                cat >> "$MANIFEST_DIR/02-db-deployment.yaml" <<EOF
      - name: init-js
        configMap:
          name: ${NAMESPACE}-db-init
EOF
            else
                cat >> "$MANIFEST_DIR/02-db-deployment.yaml" <<EOF
      - name: init-sql
        configMap:
          name: ${NAMESPACE}-db-init
EOF
            fi

            # Service
            cat > "$MANIFEST_DIR/02-db-service.yaml" <<EOF
apiVersion: v1
kind: Service
metadata:
  name: $DETECTED_DB_HOST
  namespace: $NAMESPACE
spec:
  selector:
    app: $DETECTED_DB_HOST
  ports:
  - port: $DB_PORT
    targetPort: $DB_PORT
EOF

            # Init SQL ConfigMap
            cat > "$MANIFEST_DIR/02-db-init-cm.yaml" <<EOF
apiVersion: v1
kind: ConfigMap
metadata:
  name: ${NAMESPACE}-db-init
  namespace: $NAMESPACE
data:
EOF
            # Add all initialization files to ConfigMap (SQL for PostgreSQL/MySQL, JS for MongoDB)
            for f in $INIT_FILES; do
                fname=$(basename "$f")
                echo "  $fname: |" >> "$MANIFEST_DIR/02-db-init-cm.yaml"
                sed 's/^/    /' "$f" >> "$MANIFEST_DIR/02-db-init-cm.yaml"
            done

            log_success "Database manifests created."
            DB_DEPLOYED=true
        else
            log_warn "DATABASE_HOST detected but no initialization files found. Skipping automated DB resource creation."
            log_info "Note: For PostgreSQL/MySQL, expected *.sql files. For MongoDB, expected *.js files."
        fi
    else
        log_warn "Unknown database type for host '$DETECTED_DB_HOST'. Skipping DB creation."
    fi
fi

# --- 5. Namespace Manifest ---

cat > "$MANIFEST_DIR/00-namespace.yaml" <<EOF
apiVersion: v1
kind: Namespace
metadata:
  name: $NAMESPACE
EOF

# --- 6. Application Build & Config ---

# Function to find and process Dockerfiles
process_dockerfile() {
    local dockerfile_path=$1
    local dir_path=$(dirname "$dockerfile_path")
    
    # Name logic: if root, use repo name. If subdir, use reponame-subdir matches requirements
    if [[ "$dir_path" == "." ]]; then
        app_name="${REPO_NAME}"
        sub_name=""  # Empty for root directory
    else
        sub_name=$(basename "$dir_path")
        app_name="${REPO_NAME}-${sub_name}"
    fi
    
    full_image_name="${app_name}:${IMG_TAG}"
    
    log_info "Found Dockerfile in $dir_path. Building $full_image_name..."
    docker build -t "$full_image_name" "$dir_path"
    
    log_info "Loading ${full_image_name} → Kind '${CLUSTER_NAME}'"
    kind load docker-image "${full_image_name}" --name "${CLUSTER_NAME}" || {
        log_error "Failed to load image ${full_image_name}"
        exit 1
    }
    
    # Parse Port - Improved to handle comments and ensure numeric
    # 1. Look for line starting with EXPOSE (case insensitive, allowing whitespace)
    # 2. Convert to lowercase not needed if we just grab args
    # 3. Use awk to get the first argument after EXPOSE
    # 4. Strip /tcp or /udp
    # 5. Ensure it's a number
    exposed_port=$(grep -Ei "^\s*EXPOSE\s+" "$dockerfile_path" | head -n 1 | awk '{print $2}' | cut -d/ -f1 | tr -d '"' | tr -d "'")
    
    # Validation: if not numeric, default to 80
    if [[ ! "$exposed_port" =~ ^[0-9]+$ ]]; then
        log_warn "Could not detect valid port in $dockerfile_path (found '$exposed_port'). Defaulting to 80."
        exposed_port=80
    fi
    
    log_info "Generating App Manifests for $app_name (Port: $exposed_port)..."
    
    # Determine if this is likely a backend (for explicit DB env var mapping)
    is_backend=false
    if [[ "$sub_name" == *"backend"* ]] || [[ "$exposed_port" == "5000" ]] || [[ -n "$DETECTED_DB_HOST_KEY" ]]; then
        is_backend=true
    fi
    
    # Check for Nginx config (Strong signal for Frontend)
    if [[ -f "$dir_path/nginx.conf" ]]; then
        is_backend=false
    fi

    cat > "$MANIFEST_DIR/${app_name}-deployment.yaml" <<EOF
apiVersion: apps/v1
kind: Deployment
metadata:
  name: $app_name
  namespace: $NAMESPACE
  labels:
    app: $app_name
spec:
  replicas: 1
  selector:
    matchLabels:
      app: $app_name
  template:
    metadata:
      labels:
        app: $app_name
    spec:
      containers:
      - name: $app_name
        image: $full_image_name
        imagePullPolicy: IfNotPresent
        ports:
        - containerPort: $exposed_port
EOF

    # Add environment variables
    if [[ "$is_backend" == "true" && "$DB_DEPLOYED" == "true" ]]; then
        # Backend with database: add explicit DB_* mappings
        cat >> "$MANIFEST_DIR/${app_name}-deployment.yaml" <<EOF
        env:
          - name: DB_HOST
            value: "$DETECTED_DB_HOST"
          - name: DB_PORT
            value: "$DB_PORT"
          - name: DB_NAME
            valueFrom:
              configMapKeyRef:
                name: ${NAMESPACE}-config
                key: ${DETECTED_DB_NAME_KEY:-DATABASE_NAME}
                optional: true
          - name: DB_USER
            valueFrom:
              secretKeyRef:
                name: ${NAMESPACE}-secrets
                key: ${DETECTED_DB_USER_KEY:-DATABASE_USER}
                optional: true
          - name: DB_PASSWORD
            valueFrom:
              secretKeyRef:
                name: ${NAMESPACE}-secrets
                key: ${DETECTED_DB_PASS_KEY:-DATABASE_PASSWORD}
                optional: true
        envFrom:
          - secretRef:
              name: ${NAMESPACE}-secrets
              optional: true
          - configMapRef:
              name: ${NAMESPACE}-config
              optional: true
EOF
    else
        # Non-backend or no database: use envFrom only
        cat >> "$MANIFEST_DIR/${app_name}-deployment.yaml" <<EOF
        envFrom:
          - secretRef:
              name: ${NAMESPACE}-secrets
              optional: true
          - configMapRef:
              name: ${NAMESPACE}-config
              optional: true
EOF
    fi


    cat > "$MANIFEST_DIR/${app_name}-service.yaml" <<EOF
apiVersion: v1
kind: Service
metadata:
  name: $app_name
  namespace: $NAMESPACE
spec:
  selector:
    app: $app_name
  ports:
  - port: $exposed_port
    targetPort: $exposed_port
EOF

    # Create 'backend' service alias for non-frontend services
    # Frontend is identified by presence of nginx.conf
    # The alias always uses port 5000 (standard for nginx configs) but forwards to actual backend port
    if [[ "$is_backend" == "true" ]]; then
        log_info "  Creating 'backend' service alias for $app_name (5000 -> $exposed_port)..."
        cat > "$MANIFEST_DIR/backend-service-alias.yaml" <<EOF
apiVersion: v1
kind: Service
metadata:
  name: backend
  namespace: $NAMESPACE
spec:
  selector:
    app: $app_name
  ports:
  - port: 5000
    targetPort: $exposed_port
EOF
    fi
}

# Scan for Dockerfiles
while IFS= read -r df; do
    process_dockerfile "$df"
done < <(find . -name "Dockerfile")

# --- 7. Deployment Phase ---

echo
log_warn "About to apply ${MANIFEST_DIR}/*.yaml to namespace ${NAMESPACE}"
read -p "Continue? [Y/n] " -n 1 -r
echo
if [[ $REPLY =~ ^[Yy]$ ]] || [[ -z $REPLY ]]; then
    log_info "Deploying to namespace $NAMESPACE..."
    kubectl apply -f "$MANIFEST_DIR/"
    log_success "Deployment applied."
    
    # Verify database initialization if database was deployed
    if [[ "$DB_DEPLOYED" == "true" && -n "$DETECTED_DB_HOST" ]]; then
        echo ""
        log_info "Verifying database initialization..."
        sleep 5  # Give Kubernetes time to start creating pods
        
        # Call the verification function
        if ! reapply_db_if_needed "$DETECTED_DB_HOST" "$NAMESPACE" "$MANIFEST_DIR"; then
            log_warn "Database verification completed with warnings. Check logs manually if needed."
            echo "  kubectl logs -l app=$DETECTED_DB_HOST -n $NAMESPACE"
        fi
    fi
    
    echo ""
    log_success "Deployment complete!"
    echo "Check status with: kubectl get all -n $NAMESPACE"
else
    log_info "Skipping deployment."
fi

log_info "Quick access commands:"
echo "  kubectl get pods,svc,pvc -n ${NAMESPACE} -o wide"
echo "  kubectl logs -f deployment/${REPO_NAME} -n ${NAMESPACE}"
if [[ "$DB_DEPLOYED" == "true" ]]; then
    echo "  kubectl logs -l app=${DETECTED_DB_HOST} -n ${NAMESPACE}  # database logs"
fi
echo "  kubectl delete ns ${NAMESPACE}   # cleanup"