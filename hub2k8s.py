#!/usr/bin/env python3
"""
Interactive GitHub Repository to Fully Deployed Kubernetes Application Script

Automates:
- Cloning GitHub repo with PAT authentication
- Branch selection
- Analysis of Dockerfiles (multiple supported), .env, docker-compose.yaml, init.sql
- Generation of Kubernetes manifests in manifests-k8s/
- Docker image build + load into local cluster
- Namespace creation + deployment
- Database detection, PVC (only if init.sql), init verification with retry
- Progress prompts and final access instructions

Requirements (run once):
pip install pyyaml requests

Usage:
python github-to-k8s.py
"""

import os
import subprocess
import shutil
import random
import re
import time
import base64
import requests
import yaml
from typing import List, Tuple, Optional, Dict


def check_prerequisites():
    """Verify required CLI tools are installed."""
    tools = ['git', 'docker', 'kubectl']
    missing = []
    for tool in tools:
        if not shutil.which(tool):
            missing.append(tool)
    
    if missing:
        print(f"❌ Missing required tools: {', '.join(missing)}. Please install them.")
        exit(1)
    print("✅ Prerequisites checked.")


def parse_github_url(url: str) -> Tuple[str, str]:
    """Extract owner and repo from GitHub URL."""
    url = url.strip().rstrip('/')
    if 'github.com' not in url:
        raise ValueError("Invalid GitHub URL")
    parts = [p for p in url.split('/') if p]
    try:
        idx = parts.index('github.com')
        owner = parts[idx + 1]
        repo = parts[idx + 2].replace('.git', '')
        return owner, repo
    except (ValueError, IndexError):
        raise ValueError("Could not parse GitHub URL. Use format: https://github.com/owner/repo")


def get_branches(owner: str, repo: str, token: str = None) -> List[str]:
    """Fetch branches using GitHub API."""
    headers = {}
    if token:
        headers["Authorization"] = f"token {token}"
    resp = requests.get(f"https://api.github.com/repos/{owner}/{repo}/branches", headers=headers)
    if resp.status_code == 401:
        print("❌ Invalid GitHub token or unauthorized access.")
        exit(1)
    resp.raise_for_status()
    return [b['name'] for b in resp.json()]


def sanitize_name(name: str) -> str:
    """Make name Kubernetes-safe (lowercase, alphanum, -)."""
    return re.sub(r'[^a-z0-9-]', '-', name.lower()).strip('-')


def find_file(base_dir: str, filename: str) -> Optional[str]:
    """Find first occurrence of file recursively."""
    for root, _, files in os.walk(base_dir):
        if filename in files:
            return os.path.join(root, filename)
    return None


def find_dockerfiles(base_dir: str) -> List[str]:
    """Find all Dockerfiles recursively."""
    dfs = []
    for root, _, files in os.walk(base_dir):
        for f in files:
            if f.upper() == 'DOCKERFILE':
                dfs.append(os.path.join(root, f))
    return dfs


def parse_env_file(env_path: str) -> Dict[str, str]:
    """Parse .env file into dict."""
    env = {}
    with open(env_path, 'r', encoding='utf-8') as f:
        for line in f:
            line = line.strip()
            if line and not line.startswith('#') and '=' in line:
                key, value = line.split('=', 1)
                env[key.strip()] = value.strip().strip('"\'')
    return env


def split_env_to_config_secret(env: Dict[str, str], db_service_name: Optional[str] = None) -> Tuple[Dict, Dict]:
    """Split env into ConfigMap (non-sensitive) and Secret. Override DB host for K8s."""
    config = {}
    secret = {}
    for k, v in env.items():
        lower_k = k.lower()
        # Override database host to point to K8s service
        if db_service_name and k.upper() in ('DATABASE_HOST', 'DB_HOST', 'DBURL'):
            v = db_service_name
        if any(word in lower_k for word in ['pass', 'secret', 'key', 'token', 'auth', 'pwd', 'private']):
            secret[k] = v
        else:
            config[k] = v
    return config, secret


def create_configmap_yaml(file_path: str, repo_name: str, ns: str, data: Dict):
    cm = {
        'apiVersion': 'v1',
        'kind': 'ConfigMap',
        'metadata': {'name': f'{repo_name}-configmap', 'namespace': ns},
        'data': data
    }
    with open(file_path, 'w') as f:
        yaml.dump(cm, f, default_flow_style=False, sort_keys=False)


def create_secret_yaml(file_path: str, repo_name: str, ns: str, data: Dict):
    secret = {
        'apiVersion': 'v1',
        'kind': 'Secret',
        'metadata': {'name': f'{repo_name}-secrets', 'namespace': ns},
        'type': 'Opaque',
        'data': {k: base64.b64encode(v.encode('utf-8')).decode('utf-8') for k, v in data.items()}
    }
    with open(file_path, 'w') as f:
        yaml.dump(secret, f, default_flow_style=False, sort_keys=False)


def create_pvc_yaml(file_path: str, repo_name: str, ns: str, size: str):
    pvc = {
        'apiVersion': 'v1',
        'kind': 'PersistentVolumeClaim',
        'metadata': {'name': f'{repo_name}-pvc', 'namespace': ns},
        'spec': {
            'accessModes': ['ReadWriteOnce'],
            'resources': {'requests': {'storage': size}}
        }
    }
    with open(file_path, 'w') as f:
        yaml.dump(pvc, f, default_flow_style=False, sort_keys=False)


def create_init_configmap(file_path: str, repo_name: str, ns: str, init_sql_path: str):
    with open(init_sql_path, 'r', encoding='utf-8') as f:
        sql_content = f.read()
    cm = {
        'apiVersion': 'v1',
        'kind': 'ConfigMap',
        'metadata': {'name': f'{repo_name}-db-init-configmap', 'namespace': ns},
        'data': {'init.sql': sql_content}
    }
    with open(file_path, 'w') as f:
        yaml.dump(cm, f, default_flow_style=False, sort_keys=False)


def create_db_deployment_yaml(file_path: str, repo_name: str, ns: str, db_type: str, db_image: str,
                              db_port: int, init_sql_path: Optional[str], env: Dict):
    db_user = env.get('DATABASE_USER', 'postgres' if db_type == 'postgres' else 'root')
    db_pass = env.get('DATABASE_PASSWORD', 'password')
    db_name = env.get('DATABASE_NAME', 'mydb')

    if db_type == 'postgres':
        db_envs = [
            {'name': 'POSTGRES_USER', 'value': db_user},
            {'name': 'POSTGRES_PASSWORD', 'value': db_pass},
            {'name': 'POSTGRES_DB', 'value': db_name}
        ]
        data_path = '/var/lib/postgresql/data'
    elif db_type in ('mysql', 'mariadb'):
        db_envs = [
            {'name': 'MYSQL_ROOT_PASSWORD', 'value': db_pass},
            {'name': 'MYSQL_DATABASE', 'value': db_name},
            {'name': 'MYSQL_USER', 'value': db_user},
            {'name': 'MYSQL_PASSWORD', 'value': db_pass}
        ]
        data_path = '/var/lib/mysql'
    elif db_type == 'mongo':
        db_envs = [
            {'name': 'MONGO_INITDB_ROOT_USERNAME', 'value': db_user},
            {'name': 'MONGO_INITDB_ROOT_PASSWORD', 'value': db_pass}
        ]
        data_path = '/data/db'
    else:
        db_envs = [{'name': 'POSTGRES_PASSWORD', 'value': db_pass}]
        data_path = '/data'

    volumes = [{'name': 'db-data', 'persistentVolumeClaim': {'claimName': f'{repo_name}-pvc'}}]
    volume_mounts = [{'name': 'db-data', 'mountPath': data_path}]

    if init_sql_path and db_type in ('postgres', 'mysql', 'mariadb'):
        volumes.append({
            'name': 'init-db',
            'configMap': {'name': f'{repo_name}-db-init-configmap'}
        })
        volume_mounts.append({
            'name': 'init-db',
            'mountPath': '/docker-entrypoint-initdb.d/init.sql',
            'subPath': 'init.sql'
        })

    dep = {
        'apiVersion': 'apps/v1',
        'kind': 'Deployment',
        'metadata': {'name': f'{repo_name}-database', 'namespace': ns},
        'spec': {
            'replicas': 1,
            'selector': {'matchLabels': {'app': f'{repo_name}-database'}},
            'template': {
                'metadata': {'labels': {'app': f'{repo_name}-database'}},
                'spec': {
                    'containers': [{
                        'name': 'database',
                        'image': db_image,
                        'ports': [{'containerPort': db_port}],
                        'env': db_envs,
                        'volumeMounts': volume_mounts,
                        'resources': {
                            'requests': {'cpu': '200m', 'memory': '256Mi'},
                            'limits': {'cpu': '500m', 'memory': '512Mi'}
                        }
                    }],
                    'volumes': volumes
                }
            }
        }
    }
    with open(file_path, 'w') as f:
        yaml.dump(dep, f, default_flow_style=False, sort_keys=False)


def create_deployment_yaml(file_path: str, repo_name: str, ns: str, component: str, tag: str,
                           port: int, has_config: bool, has_secret: bool):
    image = f"{repo_name}-{component}:{tag}"
    labels = {'app': f'{repo_name}-{component}'}
    container = {
        'name': component,
        'image': image,
        'ports': [{'containerPort': port}],
        'resources': {
            'requests': {'cpu': '100m', 'memory': '128Mi'},
            'limits': {'cpu': '500m', 'memory': '512Mi'}
        },
        'livenessProbe': {
            'httpGet': {'path': '/', 'port': port},
            'initialDelaySeconds': 30,
            'periodSeconds': 10
        },
        'readinessProbe': {
            'httpGet': {'path': '/', 'port': port},
            'initialDelaySeconds': 5,
            'periodSeconds': 5
        }
    }
    if has_config or has_secret:
        container['envFrom'] = []
        if has_config:
            container['envFrom'].append({'configMapRef': {'name': f'{repo_name}-configmap'}})
        if has_secret:
            container['envFrom'].append({'secretRef': {'name': f'{repo_name}-secrets'}})

    dep = {
        'apiVersion': 'apps/v1',
        'kind': 'Deployment',
        'metadata': {'name': f'{repo_name}-{component}', 'namespace': ns},
        'spec': {
            'replicas': 1,
            'selector': {'matchLabels': labels},
            'template': {
                'metadata': {'labels': labels},
                'spec': {'containers': [container]}
            }
        }
    }
    with open(file_path, 'w') as f:
        yaml.dump(dep, f, default_flow_style=False, sort_keys=False)


def create_service_yaml(file_path: str, repo_name: str, ns: str, service_name: str, selector_app: str, port: int):
    svc = {
        'apiVersion': 'v1',
        'kind': 'Service',
        'metadata': {'name': service_name, 'namespace': ns},
        'spec': {
            'selector': {'app': selector_app},
            'ports': [{'port': port, 'targetPort': port, 'protocol': 'TCP'}],
            'type': 'ClusterIP'
        }
    }
    with open(file_path, 'w') as f:
        yaml.dump(svc, f, default_flow_style=False, sort_keys=False)


def parse_expose(dockerfile_path: str) -> int:
    """Extract first EXPOSE port from Dockerfile."""
    try:
        with open(dockerfile_path, 'r', encoding='utf-8') as f:
            for line in f:
                if re.search(r'^\s*EXPOSE', line, re.IGNORECASE):
                    ports = re.findall(r'\d+', line)
                    if ports:
                        return int(ports[0])
    except:
        pass
    return 80  # default


def load_image_to_cluster(image: str, cluster_type: str, cluster_name: Optional[str]):
    """Load Docker image into chosen cluster."""
    try:
        if cluster_type == 'minikube':
            subprocess.check_call(['minikube', 'image', 'load', image], stdout=subprocess.DEVNULL)
        elif cluster_type == 'kind':
            cmd = ['kind', 'load', 'docker-image', image]
            if cluster_name and cluster_name != 'kind':
                cmd += ['--name', cluster_name]
            subprocess.check_call(cmd, stdout=subprocess.DEVNULL)
        elif cluster_type == 'k3d':
            cmd = ['k3d', 'image', 'import', image]
            if cluster_name:
                cmd += ['--cluster', cluster_name]
            subprocess.check_call(cmd, stdout=subprocess.DEVNULL)
        print(f"   ✅ Loaded {image}")
    except subprocess.CalledProcessError as e:
        print(f"   ⚠️  Failed to load {image}: {e}")


def detect_cluster() -> Optional[Tuple[str, Optional[str]]]:
    """Detect running local cluster."""
    # minikube
    try:
        if subprocess.call(['minikube', 'status'], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL) == 0:
            return ('minikube', None)
    except:
        pass
    # kind
    try:
        out = subprocess.check_output(['kind', 'get', 'clusters'], stderr=subprocess.DEVNULL).decode().strip()
        if out:
            return ('kind', out.splitlines()[0].strip())
    except:
        pass
    # k3d
    try:
        out = subprocess.check_output(['k3d', 'cluster', 'list', '--no-headers'], stderr=subprocess.DEVNULL).decode().strip()
        if out:
            name = out.splitlines()[0].split()[0].strip()
            return ('k3d', name)
    except:
        pass
    return None


def verify_db_init(ns: str, db_type: str, repo_name: str) -> bool:
    """Verify DB init with retry logic (restart pod on failure)."""
    db_label = f"app={repo_name}-database"
    patterns = {
        'postgres': b'database system is ready',
        'mysql': b'ready for connections',
        'mariadb': b'ready for connections',
        'mongo': b'Waiting for connections'
    }
    pattern = patterns.get(db_type, b'ready')

    for attempt in range(1, 6):
        print(f"   DB init check (attempt {attempt}/5)...")
        try:
            pod_cmd = ['kubectl', 'get', 'pod', '-n', ns, '-l', db_label,
                       '-o', 'jsonpath={.items[0].metadata.name}']
            pod_name_bytes = subprocess.check_output(pod_cmd, stderr=subprocess.DEVNULL)
            pod_name = pod_name_bytes.decode().strip()
            if not pod_name:
                time.sleep(10)
                continue

            logs = subprocess.check_output(['kubectl', 'logs', pod_name, '-n', ns, '--tail=200'],
                                           stderr=subprocess.STDOUT)
            if pattern in logs:
                print("   ✅ Database initialization successful!")
                return True
            if b'ERROR' in logs.upper():
                print("   ⚠️  Init error detected, restarting pod...")
                subprocess.call(['kubectl', 'delete', 'pod', pod_name, '-n', ns, '--force', '--grace-period=0'],
                                stderr=subprocess.DEVNULL)
        except Exception:
            pass
        time.sleep(15)
    print("   ❌ DB init failed after retries. Check `kubectl logs` manually.")
    return False


def main():
    print("🚀 GitHub Repo → Kubernetes Deployer\n")
    check_prerequisites()

    # 1. GitHub input
    repo_url = input("Enter GitHub repository URL: ").strip()
    owner, raw_reponame = parse_github_url(repo_url)

    is_public = input("Is this a public repository? (y/N): ").strip().lower() == 'y'
    token = None
    if not is_public:
        token = input("Enter GitHub Personal Access Token: ").strip()
        if not token:
            print("❌ Token is required for private repositories.")
            exit(1)

    # 2. Branch selection
    print("Fetching branches...")
    branches = get_branches(owner, raw_reponame, token)
    print("Available branches:")
    for i, b in enumerate(branches, 1):
        print(f"  {i}. {b}")
    branch_idx = int(input("\nSelect branch number: ")) - 1
    branch = branches[branch_idx]

    # 3. Clone
    clone_dir = f"./{raw_reponame}"
    should_clone = True
    if os.path.exists(clone_dir):
        choice = input(f"Directory {clone_dir} exists. Delete and re-clone? (y/n): ").strip().lower()
        if choice == 'y':
            shutil.rmtree(clone_dir)
        else:
            print("Skipping clone (using existing directory).")
            should_clone = False

    if should_clone:
        print(f"Cloning {raw_reponame}@{branch}...")
        if token:
            clone_url = f"https://{token}@github.com/{owner}/{raw_reponame}.git"
        else:
            clone_url = f"https://github.com/{owner}/{raw_reponame}.git"
        subprocess.check_call(['git', 'clone', '--branch', branch, clone_url, clone_dir])
        print("✅ Cloned.")

    repo_name = sanitize_name(raw_reponame)  # safe K8s name
    ns = repo_name

    # 4. Analyze repo
    dockerfiles = find_dockerfiles(clone_dir)
    if not dockerfiles:
        print("❌ No Dockerfiles found.")
        return
    print(f"Found {len(dockerfiles)} Dockerfiles.")

    env = {}
    env_file = find_file(clone_dir, '.env')
    if env_file:
        env = parse_env_file(env_file)
        print("✅ Parsed .env")

    # Supplementary docker-compose
    compose_file = find_file(clone_dir, 'docker-compose.yaml') or find_file(clone_dir, 'docker-compose.yml')
    if compose_file:
        try:
            with open(compose_file) as f:
                compose = yaml.safe_load(f) or {}
            for svc in compose.get('services', {}).values():
                if 'environment' in svc:
                    if isinstance(svc['environment'], dict):
                        env.update(svc['environment'])
                    elif isinstance(svc['environment'], list):
                        for item in svc['environment']:
                            if '=' in item:
                                k, v = item.split('=', 1)
                                env[k.strip()] = v.strip()
            print("✅ Merged docker-compose environment")
        except Exception:
            pass

    init_sql_path = find_file(clone_dir, 'init.sql')
    has_db = bool(init_sql_path)

    # DB detection
    db_type = 'postgres'
    db_host = env.get('DATABASE_HOST', '').lower()
    for t in ['postgres', 'mysql', 'mariadb', 'mongo', 'mongodb']:
        if t in db_host:
            db_type = 'mongo' if t == 'mongodb' else t
            break
    db_image = {
        'postgres': 'postgres:latest',
        'mysql': 'mysql:latest',
        'mariadb': 'mariadb:latest',
        'mongo': 'mongo:latest'
    }.get(db_type, 'postgres:latest')
    db_port = {'postgres': 5432, 'mysql': 3306, 'mariadb': 3306, 'mongo': 27017}.get(db_type, 5432)

    print(f"Detected database: {db_type} ({db_image})")

    # 5. User prompts
    pvc_size = None
    if has_db:
        pvc_size = input("PVC size for database (default 10Gi): ").strip() or "10Gi"

    tag_input = input("Docker image tag (leave blank for random 5-digit): ").strip()
    tag = tag_input or f"{random.randint(10000, 99999)}"
    print(f"Using tag: {tag}")

    # 6. Cluster
    cluster_info = detect_cluster()
    if cluster_info:
        cluster_type, cluster_name = cluster_info
        print(f"✅ Using existing {cluster_type} cluster")
    else:
        print("No local cluster detected.")
        ch = input("Create cluster? (m)inikube / (k)ind / (3)k3d / (e)xit: ").lower()
        if ch == 'e':
            return
        elif ch == 'm':
            cluster_type, cluster_name = 'minikube', None
            print("Starting minikube...")
            subprocess.check_call(['minikube', 'start'])
        elif ch == 'k':
            cluster_type = 'kind'
            cluster_name = f"{repo_name}-kind"
            print(f"Creating kind cluster {cluster_name}...")
            subprocess.check_call(['kind', 'create', 'cluster', '--name', cluster_name])
        elif ch == '3':
            cluster_type = 'k3d'
            cluster_name = f"{repo_name}-k3d"
            print(f"Creating k3d cluster {cluster_name}...")
            subprocess.check_call(['k3d', 'cluster', 'create', cluster_name])
        else:
            print("Invalid choice.")
            return

    # 7. Build images
    components = []  # (component, port)
    built_images = []
    for df in dockerfiles:
        context = os.path.dirname(df)
        rel = os.path.relpath(context, clone_dir)
        component = 'app' if rel == '.' else sanitize_name(rel.replace(os.sep, '-'))
        expose_port = parse_expose(df)
        image = f"{repo_name}-{component}:{tag}"

        print(f"Building {component} → {image}")
        try:
            subprocess.check_call(['docker', 'build', '-t', image, '-f', df, context])
            built_images.append(image)
            components.append((component, expose_port))
            print(f"   ✅ Built {component}")
        except subprocess.CalledProcessError as e:
            print(f"   ❌ Build failed for {component}: {e}")

    if not components:
        print("❌ No images built successfully.")
        return

    # 8. Load images
    print("Loading images into cluster...")
    for img in built_images:
        load_image_to_cluster(img, cluster_type, cluster_name)

    # 9. Manifests directory
    manifests_dir = os.path.join(clone_dir, 'manifests-k8s')
    os.makedirs(manifests_dir, exist_ok=True)

    # 10. Prepare env split
    db_service_name = f"{repo_name}-database" if has_db else None
    config_data, secret_data = split_env_to_config_secret(env, db_service_name)
    has_config = bool(config_data)
    has_secret = bool(secret_data)

    # 11. Generate manifests
    resources = []

    if has_config:
        cm_file = os.path.join(manifests_dir, f"{repo_name}-configmap.yaml")
        create_configmap_yaml(cm_file, repo_name, ns, config_data)
        resources.append(cm_file)

    if has_secret:
        sec_file = os.path.join(manifests_dir, f"{repo_name}-secrets.yaml")
        create_secret_yaml(sec_file, repo_name, ns, secret_data)
        resources.append(sec_file)

    if has_db:
        # init configmap (required for init.sql)
        init_cm_file = os.path.join(manifests_dir, f"{repo_name}-db-init-configmap.yaml")
        create_init_configmap(init_cm_file, repo_name, ns, init_sql_path)
        resources.append(init_cm_file)

        pvc_file = os.path.join(manifests_dir, f"{repo_name}-pvc.yaml")
        create_pvc_yaml(pvc_file, repo_name, ns, pvc_size)
        resources.append(pvc_file)

        db_dep_file = os.path.join(manifests_dir, f"{repo_name}-database-deployment.yaml")
        create_db_deployment_yaml(db_dep_file, repo_name, ns, db_type, db_image, db_port, init_sql_path, env)
        resources.append(db_dep_file)

        db_svc_file = os.path.join(manifests_dir, f"{repo_name}-database-service.yaml")
        create_service_yaml(db_svc_file, repo_name, ns,
                            f"{repo_name}-database-service", f"{repo_name}-database", db_port)
        resources.append(db_svc_file)

    for component, port in components:
        dep_file = os.path.join(manifests_dir, f"{repo_name}-{component}-deployment.yaml")
        create_deployment_yaml(dep_file, repo_name, ns, component, tag, port, has_config, has_secret)
        resources.append(dep_file)

        svc_file = os.path.join(manifests_dir, f"{repo_name}-{component}-service.yaml")
        create_service_yaml(svc_file, repo_name, ns,
                            f"{repo_name}-{component}-service", f"{repo_name}-{component}", port)
        resources.append(svc_file)

    # 12. Namespace + Apply
    print(f"Creating namespace {ns}...")
    subprocess.call(['kubectl', 'create', 'namespace', ns], stderr=subprocess.DEVNULL)

    print("Applying manifests in order...")
    for res in resources:
        print(f"   → {os.path.basename(res)}")
        subprocess.check_call(['kubectl', 'apply', '-f', res, '-n', ns])

    # 13. DB verification
    if has_db:
        verify_db_init(ns, db_type, repo_name)

    # 14. Final output
    print("\n🎉 DEPLOYMENT COMPLETE!")
    print(f"Namespace: {ns}")
    print(f"Manifests: {manifests_dir}")
    print("\nStatus:")
    subprocess.call(['kubectl', 'get', 'all', '-n', ns])

    print("\n🔗 Access instructions:")
    for component, port in components:
        svc = f"{repo_name}-{component}-service"
        print(f"  {component}:")
        print(f"    kubectl port-forward svc/{svc} 8080:{port} -n {ns}")
        print(f"    → http://localhost:8080")
    if has_db:
        print(f"  Database (internal): {repo_name}-database-service:{db_port}")

    print("\n✅ All done! Use `kubectl` commands above to access services.")


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        print("\n\nOperation cancelled by user.")
    except Exception as e:
        print(f"\n❌ Unexpected error: {e}")
        raise