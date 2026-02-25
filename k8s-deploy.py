#!/usr/bin/env python3
import argparse
import base64
import datetime
import getpass
import os
import re
import subprocess
import sys
from pathlib import Path

import yaml



class Colors:
    HEADER = "\033[95m"
    OKBLUE = "\033[94m"
    OKCYAN = "\033[96m"
    OKGREEN = "\033[92m"
    WARNING = "\033[93m"
    FAIL = "\033[91m"
    ENDC = "\033[0m"
    BOLD = "\033[1m"

def print_success(msg):
    print(f"{Colors.OKGREEN}{msg}{Colors.ENDC}")

def print_warning(msg):
    print(f"{Colors.WARNING}{msg}{Colors.ENDC}")

def print_error(msg):
    print(f"{Colors.FAIL}{msg}{Colors.ENDC}")


def print_step(msg):
    print(f"\n{Colors.OKCYAN}{Colors.BOLD}==> {msg}{Colors.ENDC}")


class K8sDeployer:
    def ensure_web_env_exists(self):
        """
        If web/.env does not exist but web/.env.example does, copy it.
        """
        env_path = os.path.join(self.repo_dir, "web", ".env")
        env_example_path = os.path.join(self.repo_dir, "web", ".env.example")
        if not os.path.exists(env_path) and os.path.exists(env_example_path):
            try:
                with open(env_example_path, "r", encoding="utf-8") as src, open(env_path, "w", encoding="utf-8") as dst:
                    dst.write(src.read())
                print_success("Copied web/.env.example to web/.env")
            except Exception as e:
                print_error(f"Failed to copy web/.env.example to web/.env: {e}")

    def patch_frontend_env_api_url(self):
        """
        Patch the web/.env file to set VITE_API_URL to empty string for Ingress-based routing.
        """
        env_path = os.path.join(self.repo_dir, "web", ".env")
        if not os.path.exists(env_path):
            print_warning(f"web/.env not found at {env_path}, skipping VITE_API_URL patch.")
            return
        try:
            with open(env_path, "r", encoding="utf-8") as f:
                lines = f.readlines()
            found = False
            for i, line in enumerate(lines):
                if line.startswith("VITE_API_URL="):
                    lines[i] = "VITE_API_URL=\n"
                    found = True
            if not found:
                lines.append("VITE_API_URL=\n")
            with open(env_path, "w", encoding="utf-8") as f:
                f.writelines(lines)
            print_success("Patched web/.env to set VITE_API_URL for Ingress routing.")
        except Exception as e:
            print_error(f"Failed to patch web/.env: {e}")

    def patch_configmap_vite_api_url(self):
        """
        Ensure VITE_API_URL is set to empty string in the ConfigMap for proper Ingress routing.
        This method is called after _collect_env_vars to override any hardcoded values.
        """
        if "VITE_API_URL" in self.config_vars:
            print_warning(f"Overriding VITE_API_URL from '{self.config_vars['VITE_API_URL']}' to empty string for Ingress routing")
        self.config_vars["VITE_API_URL"] = ""
        self.env_vars["VITE_API_URL"] = ""
        print_success("Set VITE_API_URL to empty string in ConfigMap for proper Ingress routing.")

    def patch_vite_proxy(self):
        """
        Patch vite.config.ts in the web directory to use the correct backend service name for Kubernetes.
        """
        vite_path = os.path.join(self.repo_dir, "web", "vite.config.ts")
        if not os.path.exists(vite_path):
            print_warning(f"vite.config.ts not found at {vite_path}, skipping Vite proxy patch.")
            return
        try:
            with open(vite_path, "r", encoding="utf-8") as f:
                content = f.read()
            
            # Determine the backend service name
            backend_service_name = None
            for component, service_name in self.service_name_by_component.items():
                if component in ["backend", "api"]:
                    backend_service_name = service_name
                    break
            
            if not backend_service_name:
                # Fallback to repo-based naming
                backend_service_name = f"{self.repo_name}-backend-service"
            
            # Patch common backend proxy targets
            old_targets = [
                "target: 'http://backend:5001'",
                "target: 'http://localhost:5001'",
                "target: 'http://api:5001'"
            ]
            
            new_target = f"target: 'http://{backend_service_name}:5001'"
            content_modified = False
            
            for old_target in old_targets:
                if old_target in content:
                    content = content.replace(old_target, new_target)
                    content_modified = True
                    print_success(f"Patched vite.config.ts to use {backend_service_name} for proxy target.")
                    break
            
            if not content_modified:
                print_warning("No common backend proxy targets found in vite.config.ts, skipping patch.")
                return
            
            with open(vite_path, "w", encoding="utf-8") as f:
                f.write(content)
                
        except Exception as e:
            print_error(f"Failed to patch vite.config.ts: {e}")

    def ensure_ssl_certs(self):
        """
        Ensure SSL certificates exist for KeyPouch. If not, generate them using generate-ssl.sh.
        """
        ssl_dir = os.path.join(self.repo_dir, "ssl")
        key_path = os.path.join(ssl_dir, "keypouch.key")
        crt_path = os.path.join(ssl_dir, "keypouch.crt")
        dhparam_path = os.path.join(ssl_dir, "dhparam.pem")
        gen_script = os.path.join(self.repo_dir, "generate-ssl.sh")

        # If all certs exist, skip
        if all(os.path.exists(p) for p in [key_path, crt_path, dhparam_path]):
            print_success(f"SSL certificates already exist in {ssl_dir}")
            return

        if not os.path.exists(gen_script):
            print_warning(f"SSL generation script not found: {gen_script}\nSkipping SSL generation.")
            return

        print_step(f"Generating SSL certificates using {gen_script}")
        try:
            self.run_command(["bash", gen_script], cwd=self.repo_dir)
            print_success(f"SSL certificates generated in {ssl_dir}")
        except Exception as e:
            print_error(f"Failed to generate SSL certificates: {e}")
            raise

    def __init__(self):
        self.repo_url = ""
        self.repo_name = ""
        self.namespace = ""
        self.branch = ""
        self.token = ""
        self.cluster_name = ""
        self.cluster_type = ""
        self.non_interactive = False
        self.auto_apply = False
        self.db_pvc_enabled = True
        self.db_pvc_size = "1Gi"

        self.timestamp = datetime.datetime.now().strftime("%Y%m%d-%H%M%S")
        self.base_dir = ""
        self.repo_dir = ""
        self.manifests_dir = ""

        self.detected_files = {
            ".env": [],
            "Dockerfile": [],
            "docker-compose.yml": [],
            "nginx.conf": [],
            "init.sql": [],
        }

        self.env_vars = {}
        self.config_vars = {}
        self.secret_vars = {}
        self.images = []
        self.services = []
        self.primary_service_name = ""
        self.db_init_job_name = ""
        self.service_name_by_component = {}
        self.nginx_configmap_name = ""
        self.compose_db = None

    def _sanitize_name(self, value):
        value = value.lower().replace("_", "-")
        value = re.sub(r"[^a-z0-9-]", "-", value)
        value = re.sub(r"-+", "-", value).strip("-")
        return value[:63] if value else "app"

    def _mask_sensitive(self, text, sensitive_values):
        masked = text
        for secret in sensitive_values or []:
            if secret:
                masked = masked.replace(secret, "***")
        return masked

    def run_command(self, cmd, cwd=None, input_text=None, env=None, sensitive_values=None):
        try:
            result = subprocess.run(
                cmd,
                cwd=cwd,
                input=input_text,
                text=True,
                capture_output=True,
                check=True,
                shell=isinstance(cmd, str),
                env=env,
            )
            return result.stdout.strip()
        except subprocess.CalledProcessError as exc:
            command_text = " ".join(cmd) if isinstance(cmd, list) else cmd
            print_error(f"Command failed: {self._mask_sensitive(command_text, sensitive_values)}")
            if exc.stderr:
                print_error(self._mask_sensitive(exc.stderr.strip(), sensitive_values))
            raise

    def _build_git_auth_env(self):
        if not self.token:
            return None, []
        auth_value = f"AUTHORIZATION: token {self.token}"
        env = os.environ.copy()
        env.update(
            {
                "GIT_CONFIG_COUNT": "1",
                "GIT_CONFIG_KEY_0": "http.extraHeader",
                "GIT_CONFIG_VALUE_0": auth_value,
                "GIT_TERMINAL_PROMPT": "0",
            }
        )
        return env, [self.token]

    def _is_sensitive_env_key(self, key):
        patterns = [
            "USER",
            "SECRET",
            "TOKEN",
            "PASSWORD",
            "PASS",
            "API_KEY",
            "PRIVATE_KEY",
            "ACCESS_KEY",
            "CREDENTIAL",
            "AUTH",
            "DB_URL",
            "DATABASE_URL",
        ]
        upper_key = key.upper()
        return any(p in upper_key for p in patterns)

    def _prompt_yes_no(self, message, default=True):
        if self.non_interactive:
            return default
        default_text = "y" if default else "n"
        answer = input(f"{Colors.BOLD}{message} (y/n, default: {default_text}): {Colors.ENDC}").strip().lower()
        if not answer:
            return default
        return answer == "y"

    def _extract_repo_name(self):
        base = self.repo_url.rstrip("/").split("/")[-1]
        if base.endswith(".git"):
            base = base[:-4]
        self.repo_name = self._sanitize_name(base)
        self.namespace = self.repo_name
        self.base_dir = f"/tmp/{self.repo_name}-deploy-{self.timestamp}"
        self.repo_dir = os.path.join(self.base_dir, self.repo_name)
        self.manifests_dir = os.path.join(self.repo_dir, "k8s-manifests")

    def _component_for_dockerfile(self, dockerfile_path):
        dockerfile_dir = os.path.dirname(dockerfile_path)
        if dockerfile_dir == self.repo_dir:
            return "root"
        rel_dir = os.path.relpath(dockerfile_dir, self.repo_dir)
        rel_dir = rel_dir.replace(os.sep, "-")
        return self._sanitize_name(rel_dir)

    def _service_name_for_component(self, component):
        if component == "root":
            return f"{self.repo_name}-service"
        return f"{self.repo_name}-{component}-service"

    def _deployment_name_for_component(self, component):
        if component == "root":
            return f"{self.repo_name}-deployment"
        return f"{self.repo_name}-{component}-deployment"

    def get_github_repo(self):
        print_step("GitHub Repository Handling")

        if not self.repo_url:
            self.repo_url = input(f"{Colors.BOLD}Enter GitHub repository URL: {Colors.ENDC}").strip()
        if not self.repo_url:
            raise ValueError("Repository URL is required.")

        self._extract_repo_name()

        print("Checking repository accessibility...")
        is_private = False
        try:
            self.run_command(["git", "ls-remote", "--heads", self.repo_url])
            print_success("Repository appears public/reachable without token.")
        except subprocess.CalledProcessError:
            is_private = True
            print_warning("Repository likely private or requires authentication.")

        if is_private and not self.token:
            if self.non_interactive:
                raise ValueError("Private repository requires --token in non-interactive mode.")
            self.token = getpass.getpass(f"{Colors.BOLD}Enter GitHub token (hidden): {Colors.ENDC}").strip()
            if not self.token:
                raise ValueError("Token was empty.")

        git_env, sensitive_values = self._build_git_auth_env()

        try:
            output = self.run_command(
                ["git", "ls-remote", "--heads", self.repo_url],
                env=git_env,
                sensitive_values=sensitive_values,
            )
        except subprocess.CalledProcessError:
            raise ValueError("Unable to read repository heads. Check URL/token permissions.")

        branches = []
        for line in output.splitlines():
            if "refs/heads/" in line:
                branches.append(line.split("refs/heads/")[-1].strip())

        if not branches:
            branches = ["main"]

        print("Available branches:")
        for idx, name in enumerate(branches, start=1):
            print(f"{idx}. {name}")

        if self.branch and self.branch in branches:
            print_success(f"Using branch from CLI: {self.branch}")
        elif self.branch and self.branch not in branches:
            print_warning(f"Requested branch '{self.branch}' not found. Falling back to {branches[0]}.")
            self.branch = branches[0]
        elif self.non_interactive:
            self.branch = branches[0]
            print_success(f"Using default branch: {self.branch}")
        else:
            raw = input(f"{Colors.BOLD}Select branch number (default: 1): {Colors.ENDC}").strip()
            if raw.isdigit() and 1 <= int(raw) <= len(branches):
                self.branch = branches[int(raw) - 1]
            else:
                self.branch = branches[0]

    def clone_repo(self):
        print_step(f"Cloning branch '{self.branch}' into {self.repo_dir}")
        os.makedirs(self.base_dir, exist_ok=True)

        git_env, sensitive_values = self._build_git_auth_env()

        self.run_command(
            ["git", "clone", "--branch", self.branch, self.repo_url, self.repo_dir],
            env=git_env,
            sensitive_values=sensitive_values,
        )
        print_success("Repository cloned.")

    def scan_repo(self):
        print_step("Repository Scanning")

        for root, dirs, files in os.walk(self.repo_dir):
            if ".git" in dirs:
                dirs.remove(".git")

            for file_name in files:
                abs_path = os.path.join(root, file_name)

                if file_name == ".env" or file_name.startswith(".env"):
                    self.detected_files[".env"].append(abs_path)
                elif file_name == "Dockerfile" or file_name.startswith("Dockerfile"):
                    self.detected_files["Dockerfile"].append(abs_path)
                elif file_name in ["docker-compose.yml", "docker-compose.yaml"]:
                    self.detected_files["docker-compose.yml"].append(abs_path)
                elif file_name == "nginx.conf" or file_name.endswith(".conf"):
                    self.detected_files["nginx.conf"].append(abs_path)
                elif file_name == "init.sql":
                    self.detected_files["init.sql"].append(abs_path)

        for key, paths in self.detected_files.items():
            print(f"{key}: {len(paths)} file(s)")

    def _extract_ports_from_dockerfile(self, dockerfile_path):
        ports = []
        expose_pattern = re.compile(r"^\s*EXPOSE\s+(.+)$", re.IGNORECASE)
        with open(dockerfile_path, "r", encoding="utf-8", errors="ignore") as handle:
            for line in handle:
                match = expose_pattern.match(line)
                if not match:
                    continue
                for item in match.group(1).split():
                    port_str = item.split("/")[0].strip()
                    if port_str.isdigit():
                        ports.append(int(port_str))
        unique_ports = sorted(set(ports))
        return unique_ports or [80]

    def build_images(self):
        print_step("Docker Image Handling")
        dockerfiles = sorted(self.detected_files["Dockerfile"])

        # Parse docker-compose port mappings if present
        compose_ports = {}
        compose_files = sorted(self.detected_files["docker-compose.yml"])
        for compose_path in compose_files:
            try:
                with open(compose_path, "r", encoding="utf-8", errors="ignore") as handle:
                    compose = yaml.safe_load(handle)
            except Exception:
                continue
            services = compose.get("services", {}) if isinstance(compose, dict) else {}
            if not isinstance(services, dict):
                continue
            for svc_name, svc_def in services.items():
                if not isinstance(svc_def, dict):
                    continue
                ports = svc_def.get("ports", [])
                if isinstance(ports, list) and ports:
                    # Only use the container port (right side of mapping)
                    first = ports[0]
                    if isinstance(first, str) and ":" in first:
                        container_port = first.split(":")[-1].split("/")[0]
                        if container_port.isdigit():
                            compose_ports[self._sanitize_name(svc_name)] = int(container_port)
                    elif isinstance(first, int):
                        compose_ports[self._sanitize_name(svc_name)] = first
                    elif isinstance(first, dict) and str(first.get("target", "")).isdigit():
                        compose_ports[self._sanitize_name(svc_name)] = int(first["target"])

        self.service_name_by_component = {}
        component_ports = {}
        for idx, dockerfile_path in enumerate(dockerfiles, start=1):
            dockerfile_dir = os.path.dirname(dockerfile_path)
            component = self._component_for_dockerfile(dockerfile_path)
            service_name = self._service_name_for_component(component)
            deployment_name = self._deployment_name_for_component(component)

            # Prefer docker-compose port mapping if available
            port = compose_ports.get(component)
            if port is None:
                port = self._extract_ports_from_dockerfile(dockerfile_path)[0]
            component_ports[component] = port
            self.service_name_by_component[component] = service_name

        if self.detected_files["nginx.conf"]:
            default_service = self.service_name_by_component.get("backend")
            if not default_service and self.service_name_by_component:
                first_component = sorted(self.service_name_by_component.keys())[0]
                default_service = self.service_name_by_component[first_component]
            default_port = component_ports.get("backend", 80)
            for nginx_path in self.detected_files["nginx.conf"]:
                self._rewrite_nginx_conf(nginx_path, default_service, default_port, component_ports)

        for idx, dockerfile_path in enumerate(dockerfiles, start=1):
            dockerfile_dir = os.path.dirname(dockerfile_path)
            component = self._component_for_dockerfile(dockerfile_path)
            service_name = self._service_name_for_component(component)
            deployment_name = self._deployment_name_for_component(component)

            if len(dockerfiles) == 1:
                image_name = self.repo_name
                image_tag = f"{self.repo_name}:{self.timestamp}"
            else:
                image_name = f"{self.repo_name}-{component}"
                image_tag = f"{self.repo_name}-{component}:{self.timestamp}"

            # Use the resolved port (compose or Dockerfile)
            ports = [component_ports[component]]
            print(f"Building image {image_tag} from {dockerfile_path}")
            self.run_command(["docker", "build", "-t", image_tag, "-f", dockerfile_path, dockerfile_dir])

            self.images.append(
                {
                    "name": image_name,
                    "tag": image_tag,
                    "ports": ports,
                    "dockerfile": dockerfile_path,
                    "component": component,
                    "service_name": service_name,
                    "deployment_name": deployment_name,
                }
            )
            print_success(f"Built {image_tag}; ports={ports}")

    def _rewrite_nginx_conf(self, nginx_path, default_service_name, default_service_port, component_ports):
        with open(nginx_path, "r", encoding="utf-8", errors="ignore") as handle:
            content = handle.read()

        alias_map = {
            "backend": self.service_name_by_component.get("backend", default_service_name),
            "api": self.service_name_by_component.get("backend", default_service_name),
            "frontend": self.service_name_by_component.get("frontend", self.service_name_by_component.get("web", default_service_name)),
            "web": self.service_name_by_component.get("web", self.service_name_by_component.get("frontend", default_service_name)),
            "app": default_service_name,
            "localhost": default_service_name,
            "127.0.0.1": default_service_name,
            "0.0.0.0": default_service_name,
        }
        alias_map = {k: v for k, v in alias_map.items() if v}

        def repl(match):
            prefix, host, port = match.group(1), match.group(2), match.group(3)
            host_lower = host.lower()
            
            if host_lower == "web" and "web" in self.service_name_by_component:
                mapped = self.service_name_by_component["web"]
                resolved_port = port
                if not resolved_port:
                    if "web" in component_ports:
                        resolved_port = str(component_ports["web"])
                    else:
                        resolved_port = "80"
            else:
                if host_lower in self.service_name_by_component:
                    mapped = self.service_name_by_component[host_lower]
                else:
                    mapped = alias_map.get(host_lower, alias_map.get("backend", default_service_name))
                
                resolved_port = port or str(default_service_port)
            
            if mapped == self.service_name_by_component.get("web") and port == "3000":
                if "web" in component_ports:
                    resolved_port = str(component_ports["web"])
            
            return f"{prefix}{mapped}:{resolved_port}"

        rewritten = re.sub(
            r"(server\s+|http://)([A-Za-z0-9_.-]+)(?::(\d+))?",
            repl,
            content,
            flags=re.IGNORECASE,
        )

        if rewritten != content:
            with open(nginx_path, "w", encoding="utf-8") as handle:
                handle.write(rewritten)
            print_success(f"Updated nginx upstream targets in {nginx_path}")
        else:
            print_warning(f"nginx.conf found but no upstream targets rewritten: {nginx_path}")
        return rewritten

    def detect_cluster(self):
        print_step("Cluster Detection")
        clusters = []

        try:
            output = self.run_command(["kind", "get", "clusters"])
            for line in output.splitlines():
                line = line.strip()
                if line:
                    clusters.append(("kind", line))
        except Exception:
            pass

        try:
            output = self.run_command(["minikube", "profile", "list"])
            if "minikube" in output:
                clusters.append(("minikube", "minikube"))
        except Exception:
            pass

        try:
            output = self.run_command(["k3d", "cluster", "list"])
            for line in output.splitlines():
                line = line.strip()
                if line and not line.startswith("NAME"):
                    clusters.append(("k3d", line.split()[0]))
        except Exception:
            pass

        if not clusters:
            raise RuntimeError("No local Kubernetes clusters (kind/minikube/k3d) detected.")

        if self.cluster_name:
            matched = [c for c in clusters if c[1] == self.cluster_name]
            if matched:
                self.cluster_type, self.cluster_name = matched[0]
                print_success(f"Using specified cluster: {self.cluster_type}:{self.cluster_name}")
            else:
                raise ValueError(f"Cluster '{self.cluster_name}' not found in detected clusters.")
        elif len(clusters) == 1:
            self.cluster_type, self.cluster_name = clusters[0]
            print_success(f"Using single detected cluster: {self.cluster_type}:{self.cluster_name}")
        elif self.non_interactive:
            self.cluster_type, self.cluster_name = clusters[0]
            print_success(f"Using first detected cluster in non-interactive mode: {self.cluster_type}:{self.cluster_name}")
        else:
            print("Detected clusters:")
            for idx, (ctype, cname) in enumerate(clusters, start=1):
                print(f"{idx}. {ctype}:{cname}")
            raw = input(f"{Colors.BOLD}Select cluster number (default: 1): {Colors.ENDC}").strip()
            if raw.isdigit() and 1 <= int(raw) <= len(clusters):
                self.cluster_type, self.cluster_name = clusters[int(raw) - 1]
            else:
                self.cluster_type, self.cluster_name = clusters[0]
            print_success(f"Using cluster: {self.cluster_type}:{self.cluster_name}")

    def load_images(self):
        print_step("Loading Images Into Cluster")
        for img in self.images:
            tag = img["tag"]
            if self.cluster_type == "kind":
                self.run_command(["kind", "load", "docker-image", tag, "--name", self.cluster_name])
            elif self.cluster_type == "minikube":
                self.run_command(["minikube", "image", "load", tag, "--profile", self.cluster_name])
            elif self.cluster_type == "k3d":
                self.run_command(["k3d", "image", "import", tag, "-c", self.cluster_name])
            print_success(f"Loaded image {tag}")

    def _parse_compose_db_service(self):
        compose_files = sorted(self.detected_files["docker-compose.yml"])
        for compose_path in compose_files:
            try:
                with open(compose_path, "r", encoding="utf-8", errors="ignore") as handle:
                    compose = yaml.safe_load(handle)
            except Exception:
                continue

            services = compose.get("services", {}) if isinstance(compose, dict) else {}
            if not isinstance(services, dict):
                continue

            for svc_name, svc_def in services.items():
                if not isinstance(svc_def, dict):
                    continue
                image = str(svc_def.get("image", "")).strip()
                lower_image = image.lower()
                lower_name = str(svc_name).lower()

                db_type = ""
                default_port = 0
                if "postgres" in lower_image or lower_name in ["postgres", "postgresql"]:
                    db_type = "postgres"
                    default_port = 5432
                elif any(x in lower_image for x in ["mysql", "mariadb"]) or lower_name in ["mysql", "mariadb"]:
                    db_type = "mysql"
                    default_port = 3306
                if not db_type:
                    continue

                port = default_port
                ports = svc_def.get("ports", [])
                if isinstance(ports, list) and ports:
                    first = ports[0]
                    if isinstance(first, str):
                        tail = first.split(":")[-1].split("/")[0]
                        if tail.isdigit():
                            port = int(tail)
                    elif isinstance(first, int):
                        port = first
                    elif isinstance(first, dict) and str(first.get("target", "")).isdigit():
                        port = int(first["target"])

                env_data = {}
                
                env_files = svc_def.get("env_file", [])
                if isinstance(env_files, str):
                    env_files = [env_files]
                for env_file in env_files:
                    env_file_path = os.path.join(os.path.dirname(compose_path), env_file)
                    if os.path.exists(env_file_path):
                        with open(env_file_path, "r", encoding="utf-8", errors="ignore") as handle:
                            for line in handle:
                                raw = line.strip()
                                if not raw or raw.startswith("#") or "=" not in raw:
                                    continue
                                key, value = raw.split("=", 1)
                                env_data[key.strip()] = value.strip()
                
                env_section = svc_def.get("environment", {})
                if isinstance(env_section, dict):
                    for k, v in env_section.items():
                        env_data[str(k)] = str(v)
                elif isinstance(env_section, list):
                    for item in env_section:
                        if isinstance(item, str) and "=" in item:
                            k, v = item.split("=", 1)
                            env_data[k.strip()] = v.strip()

                component = self._sanitize_name(str(svc_name))
                service_name = f"{self.repo_name}-{component}-service"
                deployment_name = f"{self.repo_name}-{component}-deployment"

                return {
                    "component": component,
                    "compose_name": str(svc_name),
                    "image": image,
                    "db_type": db_type,
                    "port": port,
                    "environment": env_data,
                    "service_name": service_name,
                    "deployment_name": deployment_name,
                }
        return None

    def _resolve_db_host(self, raw_host):
        available_service_names = {svc["name"] for svc in self.services}
        if self.compose_db:
            available_service_names.add(self.compose_db["service_name"])

        if not raw_host:
            if self.compose_db:
                return self.compose_db["service_name"]
            return self.primary_service_name

        host = str(raw_host).strip()
        host_lower = host.lower()

        if host in available_service_names:
            return host
        if "." in host:
            return host

        mapped = self.service_name_by_component.get(self._sanitize_name(host_lower))
        if mapped:
            return mapped

        guessed = f"{self.repo_name}-{self._sanitize_name(host_lower)}-service"
        if guessed in available_service_names:
            return guessed

        if self.compose_db and host_lower in {"postgres", "postgresql", "mysql", "mariadb", "db", "database", "localhost"}:
            return self.compose_db["service_name"]

        return host

    def _determine_primary_service(self):
        if not self.services:
            return ""
        for svc in self.services:
            if svc.get("component") == "frontend":
                return svc["name"]
        for svc in self.services:
            if svc["name"] == f"{self.repo_name}-service":
                return svc["name"]
        for svc in self.services:
            if svc.get("component") == "backend":
                return svc["name"]
        return self.services[0]["name"]

    def _write_manifest(self, file_name, document):
        path = os.path.join(self.manifests_dir, file_name)
        with open(path, "w", encoding="utf-8") as handle:
            yaml.safe_dump(document, handle, sort_keys=False)

    def _add_nginx_mount_to_deployment_manifest(self, deployment_file):
        path = os.path.join(self.manifests_dir, deployment_file)
        if not os.path.exists(path) or not self.nginx_configmap_name:
            return

        with open(path, "r", encoding="utf-8") as handle:
            deployment = yaml.safe_load(handle)

        pod_spec = deployment["spec"]["template"]["spec"]
        containers = pod_spec.get("containers", [])
        if not containers:
            return

        nginx_volume = {
            "name": "nginx-config",
            "configMap": {"name": self.nginx_configmap_name},
        }
        existing_volumes = pod_spec.get("volumes", [])
        if not any(v.get("name") == "nginx-config" for v in existing_volumes if isinstance(v, dict)):
            existing_volumes.append(nginx_volume)
        pod_spec["volumes"] = existing_volumes

        nginx_mount = {
            "name": "nginx-config",
            "mountPath": "/etc/nginx/nginx.conf",
            "subPath": "nginx.conf",
        }
        existing_mounts = containers[0].get("volumeMounts", [])
        has_mount = any(
            m.get("name") == "nginx-config" or m.get("mountPath") == "/etc/nginx/nginx.conf"
            for m in existing_mounts
            if isinstance(m, dict)
        )
        if not has_mount:
            existing_mounts.append(nginx_mount)
        containers[0]["volumeMounts"] = existing_mounts

        with open(path, "w", encoding="utf-8") as handle:
            yaml.safe_dump(deployment, handle, sort_keys=False)

    def generate_manifests(self):
        print_step("Kubernetes Manifest Generation")
        os.makedirs(self.manifests_dir, exist_ok=True)
        self.nginx_configmap_name = ""
        self.compose_db = None

        self.compose_db = self._parse_compose_db_service()

        self._collect_env_vars()
        
        # Patch ConfigMap to ensure VITE_API_URL is empty for proper Ingress routing
        self.patch_configmap_vite_api_url()

        namespace = {"apiVersion": "v1", "kind": "Namespace", "metadata": {"name": self.namespace}}
        self._write_manifest("namespace.yaml", namespace)

        if self.config_vars:
            cm = {
                "apiVersion": "v1",
                "kind": "ConfigMap",
                "metadata": {"name": f"{self.repo_name}-config", "namespace": self.namespace},
                "data": self.config_vars,
            }
            self._write_manifest(f"{self.repo_name}-configmap.yaml", cm)
        if self.secret_vars:
            secret = {
                "apiVersion": "v1",
                "kind": "Secret",
                "metadata": {"name": f"{self.repo_name}-secret", "namespace": self.namespace},
                "type": "Opaque",
                "stringData": self.secret_vars,
            }
            self._write_manifest(f"{self.repo_name}-secret.yaml", secret)
        if not self.detected_files[".env"] and not self.config_vars and not self.secret_vars:
            print_warning("No .env or discovered env vars found. Skipping ConfigMap/Secret creation.")

        create_pvc = False
        pvc_size = self.db_pvc_size
        if self.detected_files["init.sql"]:
            create_pvc = self.db_pvc_enabled
            if not self.non_interactive:
                create_pvc = self._prompt_yes_no("Create DB PVC", default=True)
                if create_pvc:
                    typed = input(f"{Colors.BOLD}PVC size (default: 1Gi): {Colors.ENDC}").strip()
                    pvc_size = typed or "1Gi"
            if create_pvc:
                pvc = {
                    "apiVersion": "v1",
                    "kind": "PersistentVolumeClaim",
                    "metadata": {"name": f"{self.repo_name}-pvc", "namespace": self.namespace},
                    "spec": {
                        "accessModes": ["ReadWriteOnce"],
                        "resources": {"requests": {"storage": pvc_size}},
                    },
                }
                self._write_manifest(f"{self.repo_name}-pvc.yaml", pvc)

        self.compose_db = self._parse_compose_db_service()
        if self.compose_db:
            self.service_name_by_component[self.compose_db["component"]] = self.compose_db["service_name"]
            db_container = {
                "name": self.compose_db["component"],
                "image": self.compose_db["image"],
                "imagePullPolicy": "IfNotPresent",
                "ports": [{"containerPort": self.compose_db["port"]}],
            }
            if self.compose_db["environment"]:
                db_container["env"] = [
                    {"name": k, "value": v} for k, v in self.compose_db["environment"].items()
                ]
            if create_pvc:
                mount_path = "/var/lib/postgresql/data" if self.compose_db["db_type"] == "postgres" else "/var/lib/mysql"
                db_container["volumeMounts"] = [{"name": "db-storage", "mountPath": mount_path}]

            db_pod_spec = {"containers": [db_container]}
            if create_pvc:
                db_pod_spec["volumes"] = [
                    {
                        "name": "db-storage",
                        "persistentVolumeClaim": {"claimName": f"{self.repo_name}-pvc"},
                    }
                ]

            db_deployment = {
                "apiVersion": "apps/v1",
                "kind": "Deployment",
                "metadata": {"name": self.compose_db["deployment_name"], "namespace": self.namespace},
                "spec": {
                    "replicas": 1,
                    "selector": {"matchLabels": {"app": self.compose_db["service_name"]}},
                    "template": {
                        "metadata": {"labels": {"app": self.compose_db["service_name"]}},
                        "spec": db_pod_spec,
                    },
                },
            }
            self._write_manifest(f"{self.compose_db['deployment_name']}.yaml", db_deployment)

            db_service = {
                "apiVersion": "v1",
                "kind": "Service",
                "metadata": {"name": self.compose_db["service_name"], "namespace": self.namespace},
                "spec": {
                    "selector": {"app": self.compose_db["service_name"]},
                    "ports": [
                        {
                            "name": f"port-{self.compose_db['port']}",
                            "port": self.compose_db["port"],
                            "targetPort": self.compose_db["port"],
                            "protocol": "TCP",
                        }
                    ],
                    "type": "ClusterIP",
                },
            }
            self._write_manifest(f"{self.compose_db['service_name']}.yaml", db_service)
            print_success(
                f"Generated DB resources from compose service '{self.compose_db['compose_name']}' -> {self.compose_db['service_name']}"
            )

        self.services = []
        if self.compose_db:
            self.services.append(
                {
                    "name": self.compose_db["service_name"],
                    "ports": [self.compose_db["port"]],
                    "component": self.compose_db["component"],
                }
            )
        for image in self.images:
            component = image.get("component", "root")
            deployment_name = image.get("deployment_name", self._deployment_name_for_component(component))
            service_name = image.get("service_name", self._service_name_for_component(component))

            container = {
                "name": image["name"],
                "image": image["tag"],
                "imagePullPolicy": "IfNotPresent",
                "ports": [{"containerPort": p} for p in image["ports"]],
            }

            env_from = []
            if self.config_vars:
                env_from.append({"configMapRef": {"name": f"{self.repo_name}-config"}})
            if self.secret_vars:
                env_from.append({"secretRef": {"name": f"{self.repo_name}-secret"}})
            if env_from:
                container["envFrom"] = env_from

            deployment = {
                "apiVersion": "apps/v1",
                "kind": "Deployment",
                "metadata": {"name": deployment_name, "namespace": self.namespace},
                "spec": {
                    "replicas": 1,
                    "selector": {"matchLabels": {"app": service_name}},
                    "template": {
                        "metadata": {"labels": {"app": service_name}},
                        "spec": {"containers": [container]},
                    },
                },
            }

            deployment_file = f"{deployment_name}.yaml"
            self._write_manifest(deployment_file, deployment)

            service = {
                "apiVersion": "v1",
                "kind": "Service",
                "metadata": {"name": service_name, "namespace": self.namespace},
                "spec": {
                    "selector": {"app": service_name},
                    "ports": [
                        {"name": f"port-{p}", "port": p, "targetPort": p, "protocol": "TCP"}
                        for p in image["ports"]
                    ],
                    "type": "ClusterIP",
                },
            }

            service_file = f"{service_name}.yaml"
            self._write_manifest(service_file, service)
            self.services.append({"name": service_name, "ports": image["ports"], "component": component})

        self.primary_service_name = self._determine_primary_service()

        if self.detected_files["nginx.conf"] and self.primary_service_name:
            primary_port = 80
            web_service_name = None
            web_port = 3000
            backend_service_name = None
            backend_port = 5001
            for svc in self.services:
                if svc.get("component") == "frontend" or svc.get("component") == "web":
                    web_service_name = svc["name"]
                    if svc["ports"]:
                        web_port = svc["ports"][0]
                if svc.get("component") == "backend":
                    backend_service_name = svc["name"]
                    if svc["ports"]:
                        backend_port = svc["ports"][0]
            # Fallbacks
            if not web_service_name:
                web_service_name = self.primary_service_name
            if not backend_service_name:
                backend_service_name = self.primary_service_name

            nginx_source = self.detected_files["nginx.conf"][0]
            with open(nginx_source, "r", encoding="utf-8", errors="ignore") as handle:
                rewritten = handle.read()
                self.nginx_configmap_name = f"{self.repo_name}-nginx-config"
                nginx_cm = {
                    "apiVersion": "v1",
                    "kind": "ConfigMap",
                    "metadata": {"name": self.nginx_configmap_name, "namespace": self.namespace},
                    "data": {"nginx.conf": rewritten},
                }
                self._write_manifest(f"{self.repo_name}-nginx-config.yaml", nginx_cm)
                for image in self.images:
                    component = image.get("component", "root")
                    if component in ["frontend", "root"]:
                        deployment_name = image.get(
                            "deployment_name", self._deployment_name_for_component(component)
                        )
                        self._add_nginx_mount_to_deployment_manifest(f"{deployment_name}.yaml")

            ingress = {
                "apiVersion": "networking.k8s.io/v1",
                "kind": "Ingress",
                "metadata": {
                    "name": f"{self.repo_name}-ingress",
                    "namespace": self.namespace,
                    "annotations": {"kubernetes.io/ingress.class": "nginx"},
                },
                "spec": {
                    "rules": [
                        {
                            "http": {
                                "paths": [
                                    {
                                        "path": "/api",
                                        "pathType": "Prefix",
                                        "backend": {
                                            "service": {
                                                "name": backend_service_name,
                                                "port": {"number": backend_port},
                                            }
                                        },
                                    },
                                    {
                                        "path": "/",
                                        "pathType": "Prefix",
                                        "backend": {
                                            "service": {
                                                "name": web_service_name,
                                                "port": {"number": web_port},
                                            }
                                        },
                                    }
                                ]
                            }
                        }
                    ]
                },
            }
            self._write_manifest(f"{self.repo_name}-ingress.yaml", ingress)

        if self.detected_files["init.sql"]:
            sql_data = {}
            for idx, sql_path in enumerate(self.detected_files["init.sql"], start=1):
                with open(sql_path, "r", encoding="utf-8", errors="ignore") as handle:
                    sql_data[f"init-{idx}.sql"] = handle.read()

            sql_cm_name = f"{self.repo_name}-db-init-config"
            sql_cm = {
                "apiVersion": "v1",
                "kind": "ConfigMap",
                "metadata": {"name": sql_cm_name, "namespace": self.namespace},
                "data": sql_data,
            }
            self._write_manifest(f"{self.repo_name}-db-init-configmap.yaml", sql_cm)

            raw_db_host = self.env_vars.get("DB_HOST") or self.env_vars.get("DATABASE_HOST") or ""
            db_host = self._resolve_db_host(raw_db_host)
            db_name = self.env_vars.get("DB_NAME") or self.env_vars.get("POSTGRES_DB") or "postgres"
            db_user = self.env_vars.get("DB_USER") or self.env_vars.get("POSTGRES_USER") or "postgres"
            db_pass = (
                self.env_vars.get("DB_PASSWORD")
                or self.env_vars.get("POSTGRES_PASSWORD")
                or self.env_vars.get("MYSQL_PASSWORD")
                or ""
            )
            db_port = (
                self.env_vars.get("DB_PORT")
                or self.env_vars.get("POSTGRES_PORT")
                or self.env_vars.get("MYSQL_PORT")
                or "5432"
            )

            if self.compose_db and not self.env_vars.get("DB_PORT"):
                db_port = str(self.compose_db["port"])

            uses_mysql = False
            if self.compose_db and self.compose_db["db_type"] == "mysql":
                uses_mysql = True
            elif "MYSQL" in " ".join(self.env_vars.keys()).upper():
                uses_mysql = True

            if uses_mysql:
                job_image = "mysql:8"
                run_sql = (
                    "until mysqladmin ping -h \"$DB_HOST\" -P \"$DB_PORT\" -u \"$DB_USER\" -p\"$DB_PASSWORD\" --silent; do echo 'waiting for mysql'; sleep 2; done; "
                    "for f in /sql/*.sql; do "
                    "mysql -h \"$DB_HOST\" -P \"$DB_PORT\" -u \"$DB_USER\" -p\"$DB_PASSWORD\" \"$DB_NAME\" < \"$f\"; "
                    "done"
                )
            else:
                job_image = "postgres:16"
                run_sql = (
                    "until pg_isready -h \"$DB_HOST\" -p \"$DB_PORT\" -U \"$DB_USER\"; do echo 'waiting for postgres'; sleep 2; done; "
                    "for f in /sql/*.sql; do "
                    "PGPASSWORD=\"$DB_PASSWORD\" psql -h \"$DB_HOST\" -p \"$DB_PORT\" -U \"$DB_USER\" -d \"$DB_NAME\" -f \"$f\"; "
                    "done"
                )

            self.db_init_job_name = f"{self.repo_name}-db-init"
            db_job = {
                "apiVersion": "batch/v1",
                "kind": "Job",
                "metadata": {"name": self.db_init_job_name, "namespace": self.namespace},
                "spec": {
                    "template": {
                        "spec": {
                            "restartPolicy": "Never",
                            "containers": [
                                {
                                    "name": "db-init",
                                    "image": job_image,
                                    "command": ["sh", "-c", run_sql],
                                    "env": [
                                        {"name": "DB_HOST", "value": db_host},
                                        {"name": "DB_PORT", "value": str(db_port)},
                                        {"name": "DB_NAME", "value": db_name},
                                        {"name": "DB_USER", "value": db_user},
                                        {"name": "DB_PASSWORD", "value": db_pass},
                                    ],
                                    "volumeMounts": [{"name": "init-sql", "mountPath": "/sql"}],
                                }
                            ],
                            "volumes": [{"name": "init-sql", "configMap": {"name": sql_cm_name}}],
                        }
                    },
                    "backoffLimit": 2,
                },
            }
            self._write_manifest(f"{self.repo_name}-db-init-job.yaml", db_job)
        else:
            print_warning("No init.sql found. Skipping DB initialization resources.")

        print_success(f"Manifests written to {self.manifests_dir}")

    def _collect_env_vars(self):
        self.env_vars = {}
        self.config_vars = {}
        self.secret_vars = {}
        for env_path in self.detected_files[".env"]:
            try:
                with open(env_path, "r", encoding="utf-8", errors="ignore") as handle:
                    for line in handle:
                        line = line.strip()
                        if not line or line.startswith("#"):
                            continue
                        if "=" not in line:
                            continue
                        key, value = line.split("=", 1)
                        key = key.strip()
                        value = value.strip()
                        self.env_vars[key] = value
                        if self._is_sensitive_env_key(key):
                            self.secret_vars[key] = value
                        else:
                            self.config_vars[key] = value
            except Exception:
                pass

        if self.compose_db:
            for k, v in self.compose_db["environment"].items():
                if k not in self.env_vars:
                    self.env_vars[k] = str(v)
                    if self._is_sensitive_env_key(k):
                        self.secret_vars[k] = str(v)
                    else:
                        self.config_vars[k] = str(v)

        if "DATABASE_HOST" in self.env_vars and self.env_vars["DATABASE_HOST"] in ["postgres", "localhost"]:
            new_host = self._resolve_db_host(self.env_vars["DATABASE_HOST"])
            if new_host != self.env_vars["DATABASE_HOST"]:
                print_warning(f"Updating DATABASE_HOST from '{self.env_vars['DATABASE_HOST']}' to '{new_host}'")
                self.config_vars["DATABASE_HOST"] = new_host
                self.env_vars["DATABASE_HOST"] = new_host
            # Also set DB_HOST for applications that use that variable name
            self.config_vars["DB_HOST"] = new_host
            self.env_vars["DB_HOST"] = new_host

        if "DB_HOST" in self.env_vars and self.env_vars["DB_HOST"] in ["localhost"]:
            new_host = self._resolve_db_host(self.env_vars["DB_HOST"])
            if new_host != self.env_vars["DB_HOST"]:
                print_warning(f"Updating DB_HOST from '{self.env_vars['DB_HOST']}' to '{new_host}'")
                self.config_vars["DB_HOST"] = new_host
                self.env_vars["DB_HOST"] = new_host
            # Also set DATABASE_HOST for applications that use that variable name
            self.config_vars["DATABASE_HOST"] = new_host
            self.env_vars["DATABASE_HOST"] = new_host

        # Ensure both DB_USER/DATABASE_USER and DB_PASSWORD/DATABASE_PASSWORD are set
        if "DATABASE_USER" in self.env_vars:
            self.config_vars["DB_USER"] = self.env_vars["DATABASE_USER"]
            self.env_vars["DB_USER"] = self.env_vars["DATABASE_USER"]
        if "DB_USER" in self.env_vars:
            self.config_vars["DATABASE_USER"] = self.env_vars["DB_USER"]
            self.env_vars["DATABASE_USER"] = self.env_vars["DB_USER"]
        
        if "DATABASE_PASSWORD" in self.env_vars:
            self.secret_vars["DB_PASSWORD"] = self.env_vars["DATABASE_PASSWORD"]
            self.env_vars["DB_PASSWORD"] = self.env_vars["DATABASE_PASSWORD"]
        if "DB_PASSWORD" in self.env_vars:
            self.secret_vars["DATABASE_PASSWORD"] = self.env_vars["DB_PASSWORD"]
            self.env_vars["DATABASE_PASSWORD"] = self.env_vars["DB_PASSWORD"]

        # Ensure both DB_NAME/DATABASE_NAME are set and consistent
        if "DATABASE_NAME" in self.env_vars:
            self.config_vars["DB_NAME"] = self.env_vars["DATABASE_NAME"]
            self.env_vars["DB_NAME"] = self.env_vars["DATABASE_NAME"]
        if "DB_NAME" in self.env_vars:
            self.config_vars["DATABASE_NAME"] = self.env_vars["DB_NAME"]
            self.env_vars["DATABASE_NAME"] = self.env_vars["DB_NAME"]
        
        # If no database name is set, use a sensible default based on compose_db or repo name
        if "DB_NAME" not in self.env_vars and "DATABASE_NAME" not in self.env_vars:
            if self.compose_db and "POSTGRES_DB" in self.compose_db["environment"]:
                default_db_name = self.compose_db["environment"]["POSTGRES_DB"]
            else:
                default_db_name = f"{self.repo_name}_db"
            
            print_warning(f"No database name found, setting DB_NAME and DATABASE_NAME to '{default_db_name}'")
            self.config_vars["DB_NAME"] = default_db_name
            self.env_vars["DB_NAME"] = default_db_name
            self.config_vars["DATABASE_NAME"] = default_db_name
            self.env_vars["DATABASE_NAME"] = default_db_name

    def _kubectl_context_ok(self):
        context = self.run_command(["kubectl", "config", "current-context"])
        if not context:
            raise RuntimeError("kubectl current-context is empty.")
        print_success(f"kubectl context: {context}")

    def _apply_manifest_if_exists(self, file_name):
        path = os.path.join(self.manifests_dir, file_name)
        if os.path.exists(path):
            print(f"Applying {file_name}")
            self.run_command(["kubectl", "apply", "-f", path])

    def _retry_db_init_if_needed(self):
        if not self.db_init_job_name:
            return

        print_step("DB Initialization Status")
        job_name = self.db_init_job_name
        ns = self.namespace

        succeeded = ""
        try:
            succeeded = self.run_command(
                ["kubectl", "get", "job", job_name, "-n", ns, "-o", "jsonpath={.status.succeeded}"]
            )
        except Exception:
            print_warning("DB init Job not found. Applying...")
            self._apply_manifest_if_exists(f"{self.repo_name}-db-init-job.yaml")
            try:
                self.run_command(
                    ["kubectl", "wait", "--for=condition=complete", f"job/{job_name}", "-n", ns, "--timeout=180s"]
                )
                print_success("DB init Job completed after re-apply.")
            except Exception:
                print_warning("DB init Job still incomplete. Check logs:")
                print(f"kubectl logs -n {ns} -l job-name={job_name} --tail=200")
            return

        if succeeded == "1":
            print_success("DB init Job already completed successfully.")
            return

        print_warning(f"DB init Job not completed (succeeded={succeeded}). Deleting and re-applying...")
        try:
            self.run_command(["kubectl", "delete", "job", job_name, "-n", ns])
            self._apply_manifest_if_exists(f"{self.repo_name}-db-init-job.yaml")
            self.run_command(
                ["kubectl", "wait", "--for=condition=complete", f"job/{job_name}", "-n", ns, "--timeout=180s"]
            )
            print_success("DB init Job completed after re-apply.")
        except Exception:
            print_warning("DB init Job still incomplete. Check logs:")
            print(f"kubectl logs -n {ns} -l job-name={job_name} --tail=200")

    def deploy_to_cluster(self):
        print_step("Deployment")
        self._kubectl_context_ok()

        if self.non_interactive:
            apply_now = self.auto_apply
        else:
            apply_now = self._prompt_yes_no("Apply manifests to cluster now", default=True)

        if not apply_now:
            print_warning("Deployment skipped by user.")
            return

        self._apply_manifest_if_exists("namespace.yaml")

        ordered_prefixes = [
            f"{self.repo_name}-configmap.yaml",
            f"{self.repo_name}-secret.yaml",
            f"{self.repo_name}-nginx-config.yaml",
            f"{self.repo_name}-pvc.yaml",
            f"{self.repo_name}-db-init-configmap.yaml",
            f"{self.repo_name}-deployment.yaml",
            f"{self.repo_name}-service.yaml",
            f"{self.repo_name}-ingress.yaml",
        ]
        for file_name in ordered_prefixes:
            self._apply_manifest_if_exists(file_name)

        all_manifest_files = sorted(
            [p.name for p in Path(self.manifests_dir).glob("*.yaml") if p.name != "namespace.yaml"]
        )
        for file_name in all_manifest_files:
            if file_name in ordered_prefixes:
                continue
            self._apply_manifest_if_exists(file_name)

        self._apply_manifest_if_exists(f"{self.repo_name}-db-init-job.yaml")

        print_step("Waiting For Readiness")
        deployments = self.run_command(
            [
                "kubectl",
                "get",
                "deployments",
                "-n",
                self.namespace,
                "-o",
                "jsonpath={range .items[*]}{.metadata.name}{'\\n'}{end}",
            ]
        )
        for dep in [d.strip() for d in deployments.splitlines() if d.strip()]:
            self.run_command(
                ["kubectl", "rollout", "status", f"deployment/{dep}", "-n", self.namespace, "--timeout=180s"]
            )
            print_success(f"Deployment ready: {dep}")

        print_step("Service Endpoints")
        svc_out = self.run_command(["kubectl", "get", "svc", "-n", self.namespace, "-o", "wide"])
        print(svc_out)

        if self.detected_files["init.sql"]:
            self._retry_db_init_if_needed()

    def run(self):
        try:
            self.get_github_repo()
            self.clone_repo()
            self.ensure_ssl_certs()
            self.scan_repo()
            self.patch_vite_proxy()
            self.ensure_web_env_exists()
            self.patch_frontend_env_api_url()
            self.build_images()
            self.detect_cluster()
            self.load_images()
            self.generate_manifests()
            self.deploy_to_cluster()
        except Exception as exc:
            print_error(f"An error occurred: {str(exc)}")
            import traceback
            print_error("Stack trace:")
            print_error(traceback.format_exc())
            sys.exit(1)


def main():
    parser = argparse.ArgumentParser(description="Deploy GitHub repository to local Kubernetes cluster")
    parser.add_argument("--url", help="GitHub repository URL")
    parser.add_argument("--branch", help="Branch to deploy (default: main)")
    parser.add_argument("--token", help="GitHub token (for private repos)")
    parser.add_argument("--cluster", help="Cluster name (kind/minikube/k3d)")
    parser.add_argument("--non-interactive", action="store_true", help="Disable prompts and use defaults")
    parser.add_argument("--apply", action="store_true", help="Auto-apply manifests in non-interactive mode")
    parser.add_argument("--db-pvc-size", default="1Gi", help="PVC size (default: 1Gi)")
    parser.add_argument("--no-db-pvc", action="store_true", help="Disable DB PVC creation")
    args = parser.parse_args()

    deployer = K8sDeployer()
    deployer.repo_url = args.url or ""
    deployer.branch = args.branch or ""
    deployer.token = args.token or ""
    deployer.cluster_name = args.cluster or ""
    deployer.non_interactive = args.non_interactive
    deployer.auto_apply = args.apply
    deployer.db_pvc_size = args.db_pvc_size
    deployer.db_pvc_enabled = not args.no_db_pvc

    deployer.run()

    print_step("Execution Finished")
    print(f"Workspace: {deployer.base_dir}")
    print("Run `kubectl delete namespace <repo-name>` to clean up if needed.")


if __name__ == "__main__":
    main()
