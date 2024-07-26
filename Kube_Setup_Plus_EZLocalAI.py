import os
import subprocess
import argparse
import random
import string
import platform
import requests
import time
import yaml
import sys
import ctypes
import re
from dotenv import load_dotenv
from tzlocal import get_localzone


def is_admin():
    try:
        if platform.system().lower() == "windows":
            return ctypes.windll.shell32.IsUserAnAdmin() != 0
        else:
            return os.geteuid() == 0
    except AttributeError:
        return False


def run_as_admin():
    if platform.system().lower() == "windows":
        ctypes.windll.shell32.ShellExecuteW(
            None, "runas", sys.executable, " ".join(sys.argv), None, 1
        )
    else:
        args = ["sudo", sys.executable] + sys.argv + [os.environ]
        os.execlpe("sudo", *args)


def run_shell_command(command):
    try:
        result = subprocess.run(
            command,
            shell=True,
            check=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
        print(result.stdout)
        return result.stdout
    except subprocess.CalledProcessError as e:
        print(f"Error executing command: {e}")
        print(f"Error output: {e.stderr}")
        return None


def is_tool_installed(tool):
    return subprocess.call(f"command -v {tool} > /dev/null 2>&1", shell=True) == 0


def get_public_ip():
    try:
        return requests.get("https://api.ipify.org", timeout=5).text.strip()
    except requests.RequestException:
        print("Warning: Could not determine public IP. Using localhost.")
        return "localhost"


def get_default_env_vars():
    workspace_folder = os.path.normpath(os.path.join(os.getcwd(), "WORKSPACE"))
    machine_tz = str(get_localzone())
    return {
        "AGIXT_API_KEY": "",
        "AGIXT_URI": "http://localhost:7437",
        "AGIXT_AGENT": "AGiXT",
        "AGIXT_BRANCH": "stable",
        "WORKING_DIRECTORY": workspace_folder.replace("\\", "/"),
        "TZ": machine_tz,
        "AGIXT_AUTO_UPDATE": "true",
        "EZLOCALAI_URI": f"http://{get_public_ip()}:8091/v1/",
        "DEFAULT_MODEL": "QuantFactory/dolphin-2.9.2-qwen2-7b-GGUF",
        "VISION_MODEL": "deepseek-ai/deepseek-vl-1.3b-chat",
        "LLM_MAX_TOKENS": "32768",
        "WHISPER_MODEL": "base.en",
        "GPU_LAYERS": "0",
    }


def set_environment(env_updates=None):
    load_dotenv()
    env_vars = get_default_env_vars()

    for key, value in os.environ.items():
        if key in env_vars:
            env_vars[key] = value

    if env_updates:
        env_vars.update(env_updates)

    if not env_vars["AGIXT_API_KEY"]:
        env_vars["AGIXT_API_KEY"] = "".join(
            random.choices(string.ascii_letters + string.digits, k=64)
        )

    with open(".env", "w") as file:
        file.write("\n".join(f'{key}="{value}"' for key, value in env_vars.items()))

    return env_vars


def start_docker_container(env_vars):
    dockerfile = (
        "docker-compose.yml"
        if env_vars["AGIXT_BRANCH"] == "stable"
        else "docker-compose-dev.yml"
    )
    command = f"docker-compose -f {dockerfile} stop && "
    command += (
        f"docker-compose -f {dockerfile} pull && "
        if env_vars["AGIXT_AUTO_UPDATE"].lower() == "true"
        else ""
    )
    command += f"docker-compose -f {dockerfile} up -d"

    run_shell_command(command)


def install_k3s(node_token=None, server_url=None):
    install_command = "curl -sfL https://get.k3s.io | sh -"
    if node_token and server_url:
        install_command = f"curl -sfL https://get.k3s.io | K3S_URL={server_url} K3S_TOKEN={node_token} sh -"

    run_shell_command(install_command)

    while not run_shell_command("k3s kubectl get node"):
        time.sleep(5)


def get_node_token():
    return run_shell_command("sudo cat /var/lib/rancher/k3s/server/node-token").strip()


def get_server_url():
    return f"https://{get_public_ip()}:6443"


def setup_kubernetes_cluster(is_master=True, master_url=None, node_token=None):
    if is_master:
        install_k3s()
        return get_node_token(), get_server_url()
    else:
        install_k3s(node_token, master_url)


def setup_nvidia_kubernetes():
    print("Setting up NVIDIA device plugin for Kubernetes...")
    run_shell_command(
        "k3s kubectl create -f https://raw.githubusercontent.com/NVIDIA/k8s-device-plugin/v0.9.0/nvidia-device-plugin.yml"
    )


def get_service_ports():
    ports = {"agixt": 7437, "streamlit": 8501, "api": 7437}
    if os.getenv("WITH_EZLOCALAI", "false").lower() == "true":
        ports.update({"ezlocalai_api": 8091, "ezlocalai_gui": 8502})
    return ports


def deploy_agixt_to_kubernetes(with_ezlocalai=False):
    services = get_service_ports()

    deployment_yaml = f"""
apiVersion: apps/v1
kind: Deployment
metadata:
  name: agixt
spec:
  replicas: 1
  selector:
    matchLabels:
      app: agixt
  template:
    metadata:
      labels:
        app: agixt
    spec:
      containers:
      - name: agixt
        image: joshxt/agixt:latest
        ports:
        {yaml.dump([{"containerPort": port} for port in services.values() if 'ezlocalai' not in port], default_flow_style=False)}
"""

    if with_ezlocalai:
        deployment_yaml += f"""
---
apiVersion: apps/v1
kind: Deployment
metadata:
  name: ezlocalai
spec:
  replicas: 1
  selector:
    matchLabels:
      app: ezlocalai
  template:
    metadata:
      labels:
        app: ezlocalai
    spec:
      containers:
      - name: ezlocalai
        image: ezlocalai/ezlocalai:latest
        ports:
        {yaml.dump([{"containerPort": port} for name, port in services.items() if 'ezlocalai' in name], default_flow_style=False)}
        resources:
          limits:
            nvidia.com/gpu: 1  # Request 1 GPU
"""

    for service_name, port in services.items():
        app_label = "agixt" if "ezlocalai" not in service_name else "ezlocalai"
        deployment_yaml += f"""
---
apiVersion: v1
kind: Service
metadata:
  name: {service_name}-service
spec:
  type: LoadBalancer
  selector:
    app: {app_label}
  ports:
    - port: {port}
      targetPort: {port}
"""

    with open("agixt-deployment.yaml", "w") as f:
        f.write(deployment_yaml)

    run_shell_command("k3s kubectl apply -f agixt-deployment.yaml")
    print(f"AGiXT{'and EZLocalAI ' if with_ezlocalai else ' '}deployed to Kubernetes.")


def setup_persistent_port_forwarding():
    if not is_admin():
        print("Elevated permissions required. Requesting admin privileges...")
        run_as_admin()
        return

    services = get_service_ports()
    system = platform.system().lower()

    if system == "linux":
        rules = "\n".join(
            [
                f"-A PREROUTING -p tcp --dport {port} -j REDIRECT --to-port {port}"
                for port in services.values()
            ]
        )
        iptables_rules = f"""
*nat
:PREROUTING ACCEPT [0:0]
:INPUT ACCEPT [0:0]
:OUTPUT ACCEPT [0:0]
:POSTROUTING ACCEPT [0:0]
{rules}
COMMIT
"""
        with open("/etc/iptables/rules.v4", "w") as f:
            f.write(iptables_rules)
        run_shell_command("iptables-restore < /etc/iptables/rules.v4")
        run_shell_command("apt-get install -y iptables-persistent")
    elif system == "darwin":
        pf_rules = "\n".join(
            [
                f"rdr pass inet proto tcp from any to any port {port} -> 127.0.0.1 port {port}"
                for port in services.values()
            ]
        )
        with open("/etc/pf.conf", "a") as f:
            f.write(f"\n# AGiXT port forwarding\n{pf_rules}\n")
        run_shell_command("pfctl -ef /etc/pf.conf")
    elif system == "windows":
        for port in services.values():
            run_shell_command(
                f"netsh interface portproxy add v4tov4 listenport={port} listenaddress=0.0.0.0 connectport={port} connectaddress=127.0.0.1"
            )
        run_shell_command(
            'reg add HKLM\\SOFTWARE\\Microsoft\\Windows\\CurrentVersion\\Run /v AGiXTPortForward /t REG_SZ /d "netsh interface portproxy reset"'
        )
    else:
        print(f"Unsupported operating system: {system}")


def setup_port_forwarding():
    services = get_service_ports()
    system = platform.system().lower()

    if system == "linux":
        for port in services.values():
            run_shell_command(
                f"sudo iptables -t nat -A PREROUTING -p tcp --dport {port} -j REDIRECT --to-port {port}"
            )
    elif system == "darwin":
        for port in services.values():
            run_shell_command(
                f'echo "rdr pass inet proto tcp from any to any port {port} -> 127.0.0.1 port {port}" | sudo pfctl -ef -'
            )
    elif system == "windows":
        for port in services.values():
            run_shell_command(
                f"netsh interface portproxy add v4tov4 listenport={port} listenaddress=0.0.0.0 connectport={port} connectaddress=127.0.0.1"
            )
    else:
        print(f"Unsupported operating system: {system}")


def setup_auto_start():
    if not is_admin():
        print("Elevated permissions required. Requesting admin privileges...")
        run_as_admin()
        return

    system = platform.system().lower()
    if system == "linux":
        service_file = """
[Unit]
Description=AGiXT Kubernetes Cluster
After=network.target

[Service]
ExecStart=/usr/local/bin/k3s server
Restart=always

[Install]
WantedBy=multi-user.target
"""
        with open("/etc/systemd/system/agixt-k3s.service", "w") as f:
            f.write(service_file)
        run_shell_command("systemctl enable agixt-k3s.service")
        run_shell_command("systemctl start agixt-k3s.service")
    elif system == "darwin":
        plist_file = """
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
    <key>Label</key>
    <string>com.agixt.k3s</string>
    <key>ProgramArguments</key>
    <array>
        <string>/usr/local/bin/k3s</string>
        <string>server</string>
    </array>
    <key>RunAtLoad</key>
    <true/>
    <key>KeepAlive</key>
    <true/>
</dict>
</plist>
"""
        with open("/Library/LaunchDaemons/com.agixt.k3s.plist", "w") as f:
            f.write(plist_file)
        run_shell_command("launchctl load /Library/LaunchDaemons/com.agixt.k3s.plist")
    elif system == "windows":
        run_shell_command(
            'sc create AGiXTK3s binpath= "C:\Program Files\k3s\k3s.exe server" start= auto'
        )
        run_shell_command("sc start AGiXTK3s")
    else:
        print(f"Unsupported operating system: {system}")


def setup_load_balancer():
    lb_yaml = """
apiVersion: v1
kind: ConfigMap
metadata:
  name: nginx-configuration
  namespace: ingress-nginx
data:
  proxy-body-size: "0"
  proxy-read-timeout: "600"
  proxy-send-timeout: "600"
---
apiVersion: v1
kind: Service
metadata:
  name: ingress-nginx
  namespace: ingress-nginx
spec:
  type: LoadBalancer
  ports:
    - name: http
      port: 80
      targetPort: 80
    - name: https
      port: 443
      targetPort: 443
  selector:
    app.kubernetes.io/name: ingress-nginx
    app.kubernetes.io/part-of: ingress-nginx
"""
    with open("load-balancer.yaml", "w") as f:
        f.write(lb_yaml)
    run_shell_command("k3s kubectl apply -f load-balancer.yaml")
    print("Load balancer setup complete.")


def check_nvidia_gpu():
    system = platform.system().lower()
    if system == "windows":
        try:
            import wmi

            c = wmi.WMI()
            nvidia_gpus = [
                gpu for gpu in c.Win32_VideoController() if "NVIDIA" in gpu.Name
            ]
            return len(nvidia_gpus) > 0
        except ImportError:
            print("WMI module not found. Cannot check for NVIDIA GPU on Windows.")
            return False
    elif system == "linux":
        return run_shell_command("lspci | grep -i nvidia") is not None
    elif system == "darwin":
        print("NVIDIA GPU support is not available on macOS.")
        return False
    else:
        print(f"Unsupported operating system: {system}")
        return False


def get_cuda_vram():
    try:
        output = run_shell_command(
            "nvidia-smi --query-gpu=memory.total,memory.free --format=csv,noheader,nounits"
        )
        if output:
            total, free = map(int, output.strip().split(","))
            return total, free
        return 0, 0
    except Exception as e:
        print(f"Error getting CUDA information: {e}")
        return 0, 0


def start_ezlocalai():
    env = set_environment()
    if not os.path.exists("ezlocalai"):
        run_shell_command("git clone https://github.com/DevXT-LLC/ezlocalai ezlocalai")
    else:
        run_shell_command("cd ezlocalai && git pull && cd ..")

    total_vram, free_vram = get_cuda_vram()
    gpu_layers = int(env["GPU_LAYERS"])
    if free_vram == 0:
        gpu_layers = 0
    elif free_vram > 0 and gpu_layers == -1:
        gpu_layers = (
            33
            if free_vram > 16 * 1024
            else 16 if free_vram > 8 * 1024 else 8 if free_vram > 4 * 1024 else 0
        )

    env_updates = {
        "EZLOCALAI_API_KEY": env["AGIXT_API_KEY"],
        "EZLOCALAI_URI": env["EZLOCALAI_URI"],
        "DEFAULT_MODEL": env["DEFAULT_MODEL"],
        "VISION_MODEL": env["VISION_MODEL"],
        "LLM_MAX_TOKENS": env["LLM_MAX_TOKENS"],
        "WHISPER_MODEL": env["WHISPER_MODEL"],
        "GPU_LAYERS": str(gpu_layers),
    }

    with open(os.path.join("ezlocalai", ".env"), "r") as file:
        lines = file.readlines()

    with open(os.path.join("ezlocalai", ".env"), "w") as file:
        for line in lines:
            key = line.split("=")[0]
            if key in env_updates:
                file.write(f"{key}={env_updates[key]}\n")
            else:
                file.write(line)

    set_environment(env_updates)

    if check_nvidia_gpu() and total_vram > 0:
        run_shell_command(
            "cd ezlocalai && docker-compose -f docker-compose-cuda.yml stop && "
            "docker-compose -f docker-compose-cuda.yml build && "
            "docker-compose -f docker-compose-cuda.yml up -d"
        )
    else:
        run_shell_command(
            "cd ezlocalai && docker-compose stop && docker-compose build && docker-compose up -d"
        )


def setup_environment(
    use_kubernetes=False,
    is_master=False,
    master_url=None,
    node_token=None,
    persistent=False,
    auto_start=False,
    with_ezlocalai=False,
):
    if persistent or auto_start or use_kubernetes:
        if not is_admin():
            print("Elevated permissions required. Requesting admin privileges...")
            run_as_admin()
            return

    env_vars = set_environment()
    services = get_service_ports()

    if not use_kubernetes:
        start_docker_container(env_vars)
        if with_ezlocalai:
            start_ezlocalai()
        for service, port in services.items():
            print(
                f"{service.capitalize()} is now accessible at http://localhost:{port}"
            )
    else:
        if is_master:
            node_token, server_url = setup_kubernetes_cluster(is_master=True)
            print(
                f"Master node setup complete. Use the following details for worker nodes:"
            )
            print(f"Master URL: {server_url}")
            print(f"Node Token: {node_token}")
            setup_load_balancer()
        else:
            if not master_url or not node_token:
                raise ValueError(
                    "Master URL and Node Token are required for worker setup"
                )
            setup_kubernetes_cluster(
                is_master=False, master_url=master_url, node_token=node_token
            )
            print("Worker node setup complete.")

        if with_ezlocalai and check_nvidia_gpu():
            setup_nvidia_kubernetes()

        deploy_agixt_to_kubernetes(with_ezlocalai)

        if persistent:
            setup_persistent_port_forwarding()
        else:
            setup_port_forwarding()

        if auto_start:
            setup_auto_start()

        node_ip = get_public_ip()
        for service, port in services.items():
            print(
                f"{service.capitalize()} is now accessible locally at http://{node_ip}:{port}"
            )

        print("Waiting for LoadBalancer to be assigned...")
        for service, port in services.items():
            while True:
                output = run_shell_command(
                    f'k3s kubectl get service {service}-service --output=jsonpath="{{.status.loadBalancer.ingress[0].ip}}"'
                )
                if output:
                    print(
                        f"{service.capitalize()} is now globally accessible at http://{output}:{port}"
                    )
                    break
                time.sleep(5)

    print("Environment setup complete.")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="AGiXT Environment Setup")
    parser.add_argument(
        "--use-kubernetes",
        action="store_true",
        help="Use Kubernetes instead of standalone Docker",
    )
    parser.add_argument(
        "--master", action="store_true", help="Set up as a Kubernetes master node"
    )
    parser.add_argument(
        "--master-url", help="Master node URL (required for worker setup)"
    )
    parser.add_argument("--node-token", help="Node token (required for worker setup)")
    parser.add_argument(
        "--persistent",
        action="store_true",
        help="Set up persistent configuration across reboots",
    )
    parser.add_argument(
        "--auto-start",
        action="store_true",
        help="Configure the cluster node to auto-start on reboot",
    )
    parser.add_argument(
        "--with-ezlocalai", action="store_true", help="Include EZLocalAI in the setup"
    )
    args = parser.parse_args()

    if (
        args.use_kubernetes
        and not args.master
        and (not args.master_url or not args.node_token)
    ):
        parser.error(
            "--master-url and --node-token are required for Kubernetes worker setup"
        )

    os.environ["WITH_EZLOCALAI"] = str(args.with_ezlocalai).lower()

    setup_environment(
        use_kubernetes=args.use_kubernetes,
        is_master=args.master,
        master_url=args.master_url,
        node_token=args.node_token,
        persistent=args.persistent,
        auto_start=args.auto_start,
        with_ezlocalai=args.with_ezlocalai,
    )
