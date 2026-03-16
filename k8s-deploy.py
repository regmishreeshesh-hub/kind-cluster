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
        self.setup_ingress = False
        self.ingress_config = {}

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

    def _prompt_ingress_config(self):
        """Interactive configuration for Ingress settings"""
        if self.non_interactive:
            return {
                'enable_ingress': True,
                'host': 'localhost',
                'tls_enabled': False,
                'path_prefix': '',
                'custom_annotations': {}
            }
        
        print_step("Ingress Configuration")
        
        # Enable/disable Ingress
        enable_ingress = self._prompt_yes_no("Enable Ingress for external access", default=True)
        
        if not enable_ingress:
            return {
                'enable_ingress': False,
                'host': '',
                'tls_enabled': False,
                'path_prefix': '',
                'custom_annotations': {}
            }
        
        # Host configuration
        default_host = f"{self.repo_name}.localhost"
        host = input(f"{Colors.BOLD}Enter host (default: {default_host}): {Colors.ENDC}").strip()
        if not host:
            host = default_host
        
        # TLS/SSL configuration
        tls_enabled = self._prompt_yes_no("Enable TLS/SSL for Ingress", default=False)
        
        # Path prefix
        path_prefix = input(f"{Colors.BOLD}Enter path prefix (default: /): {Colors.ENDC}").strip()
        if not path_prefix:
            path_prefix = "/"
        if not path_prefix.startswith("/"):
            path_prefix = "/" + path_prefix
        
        # Custom annotations
        custom_annotations = {}
        add_annotations = self._prompt_yes_no("Add custom Ingress annotations", default=False)
        if add_annotations:
            print(f"{Colors.OKCYAN}Enter annotation key-value pairs (empty line to finish):{Colors.ENDC}")
            while True:
                key = input(f"{Colors.BOLD}Annotation key (or press Enter to finish): {Colors.ENDC}").strip()
                if not key:
                    break
                value = input(f"{Colors.BOLD}Annotation value: {Colors.ENDC}").strip()
                custom_annotations[key] = value
        
        return {
            'enable_ingress': enable_ingress,
            'host': host,
            'tls_enabled': tls_enabled,
            'path_prefix': path_prefix,
            'custom_annotations': custom_annotations
        }

    def _prompt_ssl_config(self):
        """Interactive configuration for SSL/TLS settings"""
        if self.non_interactive:
            return {
                'auto_generate': True,
                'cert_path': '',
                'key_path': '',
                'ca_path': '',
                'force_recreate': False
            }
        
        print_step("SSL/TLS Configuration")
        
        # Auto-generate or use existing
        auto_generate = self._prompt_yes_no("Auto-generate SSL certificates", default=True)
        
        if auto_generate:
            force_recreate = self._prompt_yes_no("Force recreate existing certificates", default=False)
            return {
                'auto_generate': True,
                'cert_path': '',
                'key_path': '',
                'ca_path': '',
                'force_recreate': force_recreate
            }
        
        # Use existing certificates
        print(f"{Colors.OKCYAN}Provide paths to existing SSL certificates:{Colors.ENDC}")
        cert_path = input(f"{Colors.BOLD}Certificate file path (.crt): {Colors.ENDC}").strip()
        key_path = input(f"{Colors.BOLD}Private key file path (.key): {Colors.ENDC}").strip()
        ca_path = input(f"{Colors.BOLD}CA bundle file path (optional): {Colors.ENDC}").strip()
        
        return {
            'auto_generate': False,
            'cert_path': cert_path,
            'key_path': key_path,
            'ca_path': ca_path,
            'force_recreate': False
        }

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
        """
        Parse database service from docker-compose.yml files.
        Only checks the main docker-compose.yml file, ignoring test/dev variants.
        """
        # Only check the main docker-compose.yml file (not test.yml, dev.yml, etc.)
        compose_files = []
        for compose_path in sorted(self.detected_files["docker-compose.yml"]):
            filename = os.path.basename(compose_path)
            if filename == "docker-compose.yml" or filename == "docker-compose.yaml":
                compose_files.append(compose_path)
        
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
        # First priority: frontend service
        for svc in self.services:
            if svc.get("component") == "frontend" or svc.get("component") == "web":
                return svc["name"]
        # Second priority: web service (by name pattern)
        for svc in self.services:
            if "web" in svc["name"] and svc["name"] != f"{self.repo_name}-db-service":
                return svc["name"]
        # Third priority: repo-name-service (but not db service)
        for svc in self.services:
            if svc["name"] == f"{self.repo_name}-service" and "db" not in svc["name"]:
                return svc["name"]
        # Fourth priority: backend service
        for svc in self.services:
            if svc.get("component") == "backend":
                return svc["name"]
        # Fallback: first service that's not a database
        for svc in self.services:
            if "db" not in svc["name"]:
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

        # Check for SSL requirements
        ssl_required, ssl_folder_exists, dockerfile_exposes_443, ssl_files = self.check_ssl_requirements()
        if ssl_required:
            print_success("SSL requirements detected in project:")
            if ssl_folder_exists:
                print(f"  ✓ SSL folder exists")
            if dockerfile_exposes_443:
                print(f"  ✓ Port 443/HTTPS exposed in Dockerfile")
            if ssl_files:
                print(f"  ✓ {len(ssl_files)} SSL-related files found")
            
            # Create SSL certificates for the application
            self.create_ssl_certificate(force_create=True)

        self.compose_db = self._parse_compose_db_service()
        
        # Only create database resources if both database service AND init.sql file are found
        if self.compose_db and self.detected_files["init.sql"]:
            # Handle PVC creation
            create_pvc = False
            pvc_size = self.db_pvc_size
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
        if self.compose_db and self.detected_files["init.sql"]:
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
                    "rules": []
                }
            }
            
            # Add host rule if ingress_config is available
            if hasattr(self, 'ingress_config') and self.ingress_config.get('enable_ingress'):
                host = self.ingress_config.get('host', f"{self.repo_name}.localhost")
                rule = {
                    "host": host,
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
                ingress["spec"]["rules"].append(rule)
                
                # Add TLS if enabled
                if self.ingress_config.get('tls_enabled'):
                    ingress["spec"]["tls"] = [{
                        "hosts": [host],
                        "secretName": f"{self.repo_name}-tls"
                    }]
            else:
                # Fallback to default behavior without host
                rule = {
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
                ingress["spec"]["rules"].append(rule)
            self._write_manifest(f"{self.repo_name}-ingress.yaml", ingress)

        # Only create database initialization job if both init.sql AND database service are found
        if self.detected_files["init.sql"] and self.compose_db:
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

        if self.compose_db and self.detected_files["init.sql"]:
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

        # Only retry database initialization if both init.sql AND database service exist
        if self.detected_files["init.sql"] and self.compose_db:
            self._retry_db_init_if_needed()

    def check_ingress_controller(self):
        """Check if ingress controller is installed and running"""
        try:
            # Check for nginx ingress controller
            result = self.run_command(["kubectl", "get", "pods", "-n", "ingress-nginx", "-l", "app.kubernetes.io/component=controller"])
            if "ingress-nginx-controller" in result and "Running" in result:
                return True, "nginx"
        except Exception:
            pass
        
        try:
            # Check for other common ingress controllers
            result = self.run_command(["kubectl", "get", "pods", "-A", "-l", "app.kubernetes.io/name=ingress-nginx"])
            if result and "Running" in result:
                return True, "nginx"
        except Exception:
            pass
        
        return False, None

    def install_ingress_controller(self):
        """Install NGINX ingress controller based on cluster type"""
        print_step("Installing NGINX Ingress Controller")
        
        if self.cluster_type == "kind":
            # Use Kind-specific ingress controller manifest
            ingress_url = "https://raw.githubusercontent.com/kubernetes/ingress-nginx/main/deploy/static/provider/kind/deploy.yaml"
        else:
            # Use generic ingress controller manifest
            ingress_url = "https://raw.githubusercontent.com/kubernetes/ingress-nginx/main/deploy/static/provider/cloud/deploy.yaml"
        
        try:
            self.run_command(["kubectl", "apply", "-f", ingress_url])
            print_success("Ingress controller installation started")
            
            # Wait for ingress controller to be ready
            print("Waiting for ingress controller to be ready...")
            self.run_command(["kubectl", "wait", "--namespace", "ingress-nginx", "--for=condition=ready", "pod", "--selector=app.kubernetes.io/component=controller", "--timeout=120s"])
            print_success("Ingress controller is ready")
            return True
        except Exception as e:
            print_error(f"Failed to install ingress controller: {e}")
            return False

    def check_ssl_requirements(self):
        """Check if SSL is required based on repo_dir: ssl folder, generate-ssl.sh, Dockerfiles, and SSL-related files"""
        ssl_required = False
        ssl_folder_exists = False
        dockerfile_exposes_443 = False

        # Check for SSL generation script in repo_dir (e.g. Keypouch generate-ssl.sh)
        for script_name in ("generate-ssl.sh", "generate_ssl.sh"):
            gen_script = os.path.join(self.repo_dir, script_name)
            if os.path.isfile(gen_script):
                ssl_required = True
                print_success(f"SSL generation script found in repo_dir: {gen_script}")
                break

        # Check for existing SSL folder in repo_dir
        ssl_dir = os.path.join(self.repo_dir, "ssl")
        if os.path.exists(ssl_dir) and os.path.isdir(ssl_dir):
            ssl_folder_exists = True
            print_success(f"SSL folder found at: {ssl_dir}")
        
        # Check Dockerfiles for port 443 or SSL-related content
        for dockerfile_path in self.detected_files["Dockerfile"]:
            try:
                with open(dockerfile_path, "r", encoding="utf-8", errors="ignore") as f:
                    content = f.read().lower()
                    
                # Check for port 443 exposure
                if "expose" in content and ("443" in content or "https" in content):
                    dockerfile_exposes_443 = True
                    print_success(f"SSL/HTTPS detected in Dockerfile: {dockerfile_path}")
                
                # Check for SSL-related keywords
                ssl_keywords = ["ssl", "tls", "https", "certificate", "cert", "key", "openssl"]
                if any(keyword in content for keyword in ssl_keywords):
                    ssl_required = True
                    print_success(f"SSL configuration detected in Dockerfile: {dockerfile_path}")
                    
            except Exception as e:
                print_warning(f"Could not read Dockerfile {dockerfile_path}: {e}")
        
        # Also check for SSL-related files in the repository
        ssl_files = []
        for root, dirs, files in os.walk(self.repo_dir):
            for file in files:
                if file.lower().endswith(('.key', '.crt', '.pem', '.p12')) or 'ssl' in file.lower() or 'cert' in file.lower():
                    ssl_files.append(os.path.join(root, file))
        
        if ssl_files:
            print_success(f"SSL-related files found: {len(ssl_files)} files")
            ssl_required = True
        
        return ssl_required or dockerfile_exposes_443 or ssl_folder_exists, ssl_folder_exists, dockerfile_exposes_443, ssl_files

    def find_existing_ssl_certificates(self):
        """Find existing SSL certificates in the SSL folder"""
        ssl_dir = os.path.join(self.repo_dir, "ssl")
        certificates = {"key": None, "cert": None, "ca": None}
        
        if not os.path.exists(ssl_dir):
            return certificates
        
        # Look for certificate files
        for file in os.listdir(ssl_dir):
            file_path = os.path.join(ssl_dir, file)
            if os.path.isfile(file_path):
                file_lower = file.lower()
                if file_lower.endswith('.key') and 'key' in file_lower:
                    certificates["key"] = file_path
                elif file_lower.endswith('.crt') and ('cert' in file_lower or file_lower.startswith(self.repo_name.lower())):
                    certificates["cert"] = file_path
                elif file_lower.endswith('.pem') and ('ca' in file_lower or 'bundle' in file_lower):
                    certificates["ca"] = file_path
        
        return certificates

    def create_ssl_certificate(self, force_create=False):
        """Create SSL certificate for the application if required"""
        # Check if SSL is required
        ssl_required, ssl_folder_exists, dockerfile_exposes_443, ssl_files = self.check_ssl_requirements()
        
        if not ssl_required and not force_create:
            print_warning("SSL requirements not detected. Skipping SSL certificate creation.")
            print_warning("Use --ingress flag to force SSL setup for ingress.")
            return None, None
        
        print_step("Creating SSL Certificate")
        
        # Check for existing certificates first
        existing_certs = self.find_existing_ssl_certificates()
        if existing_certs["key"] and existing_certs["cert"]:
            print_success("Using existing SSL certificates:")
            print(f"  Key: {existing_certs['key']}")
            print(f"  Cert: {existing_certs['cert']}")
            return existing_certs["key"], existing_certs["cert"]
        
        # Create SSL directory if it doesn't exist
        ssl_dir = os.path.join(self.repo_dir, "ssl")
        os.makedirs(ssl_dir, exist_ok=True)
        
        # Generate certificate paths
        key_path = os.path.join(ssl_dir, f"{self.repo_name}.key")
        crt_path = os.path.join(ssl_dir, f"{self.repo_name}.crt")
        
        # Check if certificates already exist
        if os.path.exists(key_path) and os.path.exists(crt_path):
            print_success("SSL certificates already exist")
            return key_path, crt_path
        
        hostname = f"{self.repo_name}.localhost"
        
        try:
            # Generate self-signed certificate with extended information
            cmd = [
                "openssl", "req", "-x509", "-nodes", "-days", "365",
                "-newkey", "rsa:2048",
                "-keyout", key_path,
                "-out", crt_path,
                "-subj", f"/C=US/ST=State/L=City/O={self.repo_name.title()}/OU=IT Department/CN={hostname}"
            ]
            self.run_command(cmd)
            print_success(f"SSL certificate created for {hostname}")
            print(f"  Certificate: {crt_path}")
            print(f"  Private Key: {key_path}")
            
            # Also create a PEM bundle for convenience
            pem_path = os.path.join(ssl_dir, f"{self.repo_name}.pem")
            with open(pem_path, "w") as pem_file:
                with open(crt_path, "r") as cert_file:
                    pem_file.write(cert_file.read())
                with open(key_path, "r") as key_file:
                    pem_file.write(key_file.read())
            print_success(f"SSL bundle created: {pem_path}")
            
            return key_path, crt_path
        except Exception as e:
            print_error(f"Failed to create SSL certificate: {e}")
            return None, None

    def update_hosts_file(self):
        """Update /etc/hosts file with application hostname"""
        hostname = f"{self.repo_name}.localhost"
        
        try:
            # Check if hostname already exists in /etc/hosts
            with open("/etc/hosts", "r") as f:
                hosts_content = f.read()
                if hostname in hosts_content:
                    print_success(f"Hostname {hostname} already exists in /etc/hosts")
                    return True
            
            # Add hostname to /etc/hosts
            hosts_entry = f"127.0.0.1 {hostname}"
            cmd = ["echo", hosts_entry, "|", "sudo", "tee", "-a", "/etc/hosts"]
            self.run_command(["bash", "-c", f"echo '{hosts_entry}' | sudo tee -a /etc/hosts"])
            print_success(f"Added {hostname} to /etc/hosts")
            return True
        except Exception as e:
            print_warning(f"Failed to update /etc/hosts: {e}")
            print_warning(f"Please manually add: '127.0.0.1 {hostname}'")
            return False

    def patch_ingress_with_hostname(self):
        """Patch existing ingress to use unique hostname and SSL"""
        print_step("Patching Ingress Configuration")
        
        ingress_name = f"{self.repo_name}-ingress"
        
        try:
            # Check if ingress exists
            result = self.run_command(["kubectl", "get", "ingress", ingress_name, "-n", self.namespace])
        except Exception:
            print_warning(f"Ingress {ingress_name} not found")
            return False
        
        hostname = f"{self.repo_name}.localhost"
        
        # Create SSL certificate (force creation for ingress setup)
        key_path, crt_path = self.create_ssl_certificate(force_create=True)
        if key_path and crt_path:
            # Create TLS secret
            secret_name = f"{self.repo_name}-tls"
            try:
                self.run_command(["kubectl", "create", "secret", "tls", secret_name, 
                               f"--cert={crt_path}", f"--key={key_path}", "-n", self.namespace])
                print_success(f"Created TLS secret: {secret_name}")
                
                # Patch ingress with hostname and TLS
                patch_spec = {
                    "spec": {
                        "rules": [{
                            "host": hostname,
                            "http": {
                                "paths": [{
                                    "backend": {
                                        "service": {
                                            "name": f"{self.repo_name}-service",
                                            "port": {"number": 3000}
                                        }
                                    },
                                    "path": "/",
                                    "pathType": "Prefix"
                                }]
                            }
                        }],
                        "tls": [{
                            "hosts": [hostname],
                            "secretName": secret_name
                        }]
                    }
                }
                
                import json
                patch_json = json.dumps(patch_spec)
                self.run_command(["kubectl", "patch", "ingress", ingress_name, "-n", self.namespace, 
                               "--type=merge", "-p", patch_json])
                print_success(f"Patched ingress with hostname: {hostname}")
                return True
                
            except Exception as e:
                print_error(f"Failed to patch ingress with TLS: {e}")
                return False
        else:
            # Patch without TLS
            patch_spec = {
                "spec": {
                    "rules": [{
                        "host": hostname,
                        "http": {
                            "paths": [{
                                "backend": {
                                    "service": {
                                        "name": f"{self.repo_name}-service",
                                        "port": {"number": 3000}
                                    }
                                },
                                "path": "/",
                                "pathType": "Prefix"
                            }]
                        }
                    }]
                }
            }
            
            import json
            patch_json = json.dumps(patch_spec)
            try:
                self.run_command(["kubectl", "patch", "ingress", ingress_name, "-n", self.namespace, 
                               "--type=merge", "-p", patch_json])
                print_success(f"Patched ingress with hostname: {hostname}")
                return True
            except Exception as e:
                print_error(f"Failed to patch ingress: {e}")
                return False

    def setup_port_forwarding(self):
        """Setup port forwarding for ingress access"""
        print_step("Setting up Port Forwarding")
        
        # Kill existing port forwarding processes
        try:
            self.run_command(["pkill", "-f", "kubectl.*port-forward.*ingress-nginx"])
        except Exception:
            pass
        
        # Setup HTTP port forwarding
        try:
            # Start port forwarding in background
            cmd = ["kubectl", "port-forward", "-n", "ingress-nginx", "svc/ingress-nginx-controller", "8080:80"]
            self.run_command(["nohup", "bash", "-c", f"{' '.join(cmd)} > /dev/null 2>&1 &"])
            print_success("Port forwarding started: localhost:8080 -> ingress controller")
            
            # Setup HTTPS port forwarding if SSL is configured
            hostname = f"{self.repo_name}.localhost"
            try:
                https_cmd = ["kubectl", "port-forward", "-n", "ingress-nginx", "svc/ingress-nginx-controller", "8443:443"]
                self.run_command(["nohup", "bash", "-c", f"{' '.join(https_cmd)} > /dev/null 2>&1 &"])
                print_success("HTTPS port forwarding started: localhost:8443 -> ingress controller")
            except Exception:
                pass
            
            return True
        except Exception as e:
            print_error(f"Failed to setup port forwarding: {e}")
            return False

    def print_access_info(self):
        """Print access information for the deployed application"""
        hostname = f"{self.repo_name}.localhost"
        
        print_step("Access Information")
        print(f"\n{Colors.OKGREEN}🚀 Your application is now accessible!{Colors.ENDC}")
        print(f"\n{Colors.BOLD}Application URLs:{Colors.ENDC}")
        print(f"  📱 {Colors.OKBLUE}HTTP:{Colors.ENDC}  http://{hostname}:8080")
        print(f"  🔒 {Colors.OKBLUE}HTTPS:{Colors.ENDC} https://{hostname}:8443 (if SSL configured)")
        print(f"  🌐 {Colors.OKBLUE}Direct:{Colors.ENDC} http://{hostname}:30030")
        
        print(f"\n{Colors.BOLD}Alternative Access:{Colors.ENDC}")
        print(f"  📱 http://localhost:8080")
        print(f"  🌐 http://localhost:30030")
        
        print(f"\n{Colors.BOLD}Service Information:{Colors.ENDC}")
        print(f"  📍 Namespace: {self.namespace}")
        print(f"  🔧 Service: {self.repo_name}-service")
        print(f"  🌉 Ingress: {self.repo_name}-ingress")
        print(f"  🏷️  Hostname: {hostname}")
        
        print(f"\n{Colors.BOLD}Port Forwarding:{Colors.ENDC}")
        print(f"  🔄 HTTP: localhost:8080 → ingress controller")
        print(f"  🔄 HTTPS: localhost:8443 → ingress controller")
        
        print(f"\n{Colors.BOLD}Management Commands:{Colors.ENDC}")
        print(f"  📊 Check status: kubectl get pods -n {self.namespace}")
        print(f"  🌐 Check ingress: kubectl get ingress -n {self.namespace}")
        print(f"  🔄 Restart port-forward: kubectl port-forward -n ingress-nginx svc/ingress-nginx-controller 8080:80 &")
        
        print(f"\n{Colors.OKGREEN}✨ Happy deploying!{Colors.ENDC}\n")

    def setup_ingress_access(self):
        """Main method to setup complete ingress access"""
        print_step("Setting Up Ingress Access")
        
        # Check if ingress controller is installed
        ingress_installed, ingress_type = self.check_ingress_controller()
        
        if not ingress_installed:
            print_warning("No ingress controller found. Installing NGINX ingress controller...")
            if not self.install_ingress_controller():
                print_error("Failed to install ingress controller")
                return False
        else:
            print_success(f"Ingress controller found: {ingress_type}")
        
        # Update hosts file
        self.update_hosts_file()
        
        # Patch ingress with hostname and SSL
        if not self.patch_ingress_with_hostname():
            print_error("Failed to patch ingress")
            return False
        
        # Setup port forwarding
        self.setup_port_forwarding()
        
        # Wait a bit for everything to settle
        import time
        time.sleep(3)
        
        # Print access information
        self.print_access_info()
        
        return True

    def run(self):
        try:
            self.get_github_repo()
            self.clone_repo()
            
            # Interactive SSL configuration
            ssl_config = self._prompt_ssl_config()
            if ssl_config['auto_generate']:
                self.ensure_ssl_certs()
            
            self.scan_repo()
            self.patch_vite_proxy()
            self.ensure_web_env_exists()
            self.patch_frontend_env_api_url()
            self.build_images()
            self.detect_cluster()
            self.load_images()
            
            # Interactive Ingress configuration
            ingress_config = self._prompt_ingress_config()
            self.ingress_config = ingress_config
            
            self.generate_manifests()
            self.deploy_to_cluster()
            
            # Setup Ingress access if enabled
            if ingress_config['enable_ingress']:
                self.setup_ingress_access()
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
    parser.add_argument("--ingress", action="store_true", help="Setup/patch ingress after deployment")
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
    deployer.setup_ingress = args.ingress

    deployer.run()

    print_step("Execution Finished")
    print(f"Workspace: {deployer.base_dir}")
    print("Run `kubectl delete namespace <repo-name>` to clean up if needed.")


if __name__ == "__main__":
    main()
