"""
container_manager.py — Per-user Docker container lifecycle for QgisRemoteMCP multi-user mode.

Each authenticated user gets an isolated Docker container running the full QGIS stack.
Containers are started on first MCP call and stopped after an idle timeout.

Ports (external, dynamic):
  - API      : base_api_port    + index  (default 9000+)
  - Stream   : base_stream_port + index  (default 9100+)
  - noVNC    : base_novnc_port  + index  (default 9200+)
"""

from __future__ import annotations

import asyncio
import os
import time
import uuid
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Set

import httpx

# Docker SDK — imported lazily so the module doesn't crash if not installed
try:
    import docker as _docker
    import docker.errors as _docker_errors
    _HAS_DOCKER = True
except ImportError:
    _HAS_DOCKER = False

# Internal ports inside each QGIS container
_CONTAINER_API_PORT    = 8080
_CONTAINER_STREAM_PORT = 8081
_CONTAINER_NOVNC_PORT  = 6080

# Docker network name shared by gateway + workers
_NETWORK_NAME = "qgis-net"

# Label used to identify containers managed by this gateway
_LABEL_KEY   = "qgisremotemcp.managed"
_LABEL_VALUE = "true"


# ──────────────────────────────────────────────────────────────────────────────
# Data model
# ──────────────────────────────────────────────────────────────────────────────

@dataclass
class UserSession:
    user_id:      str
    session_id:   str
    container_id: str
    api_port:     int          # host-mapped port (for external access / noVNC URLs)
    stream_port:  int
    novnc_port:   int
    container_ip: str = ""     # internal Docker network IP (for gateway→worker calls)
    created_at:   float = field(default_factory=time.time)
    last_activity: float = field(default_factory=time.time)
    status:       str = "starting"   # starting | ready | unhealthy | stopping

    def to_dict(self) -> dict:
        return {
            "user_id":       self.user_id,
            "session_id":    self.session_id,
            "container_id":  self.container_id[:12],
            "api_port":      self.api_port,
            "stream_port":   self.stream_port,
            "novnc_port":    self.novnc_port,
            "container_ip":  self.container_ip,
            "created_at":    self.created_at,
            "last_activity": self.last_activity,
            "status":        self.status,
        }

    @property
    def internal_api_url(self) -> str:
        """URL to reach the worker's API from within the Docker network."""
        if self.container_ip:
            return f"http://{self.container_ip}:{_CONTAINER_API_PORT}"
        # Fallback to host-mapped port (works when gateway runs on host)
        return f"http://localhost:{self.api_port}"


# ──────────────────────────────────────────────────────────────────────────────
# Manager
# ──────────────────────────────────────────────────────────────────────────────

class ContainerManager:
    """
    Manages one Docker container per user.

    Usage:
        manager = ContainerManager(...)
        await manager.initialize()

        session = await manager.start_session("user_abc")
        result  = await manager.execute_on_container("user_abc", "/api/command", "POST", {...})
        await manager.stop_session("user_abc")
        await manager.shutdown()
    """

    def __init__(
        self,
        image_name: str = "qgisremotemcp:latest",
        network_name: str = _NETWORK_NAME,
        base_api_port: int = 9000,
        base_stream_port: int = 9100,
        base_novnc_port: int = 9200,
        max_containers: int = 50,
        idle_timeout_minutes: int = 30,
        data_dir: str = "data",
    ):
        self.image_name          = image_name
        self.network_name        = network_name
        self.base_api_port       = base_api_port
        self.base_stream_port    = base_stream_port
        self.base_novnc_port     = base_novnc_port
        self.max_containers      = max_containers
        self.idle_timeout        = idle_timeout_minutes * 60
        self.data_dir            = data_dir

        self.sessions: Dict[str, UserSession] = {}   # user_id → session
        self._used_indices: Set[int] = set()
        self._docker_client      = None
        self._cleanup_task: Optional[asyncio.Task] = None
        self._gpu_available: bool  = False

    # ── Lifecycle ─────────────────────────────────────────────────────────────

    async def initialize(self) -> None:
        """Connect to Docker, ensure network exists, clean up stale containers."""
        if not _HAS_DOCKER:
            raise RuntimeError(
                "docker SDK not installed. "
                "Add 'docker>=7.0.0' to requirements.txt."
            )
        # Try Unix socket first, then TCP (Docker Desktop on some setups)
        try:
            self._docker_client = _docker.from_env()
            self._docker_client.ping()
        except Exception:
            try:
                self._docker_client = _docker.DockerClient(base_url="tcp://localhost:2375")
                self._docker_client.ping()
            except Exception as e:
                raise RuntimeError(
                    f"Cannot connect to Docker. "
                    f"Ensure Docker is running and the socket is accessible. ({e})"
                )

        self._ensure_network()
        self._cleanup_stale_containers()
        self._detect_gpu()

        self._cleanup_task = asyncio.create_task(self._cleanup_loop())
        gpu_str = "GPU available (NVIDIA)" if self._gpu_available else "CPU only"
        print(f"[ContainerManager] Ready — image={self.image_name} "
              f"ports API:{self.base_api_port}+ "
              f"stream:{self.base_stream_port}+ "
              f"noVNC:{self.base_novnc_port}+ "
              f"idle={self.idle_timeout//60}min "
              f"compute={gpu_str}")

    async def shutdown(self) -> None:
        """Cancel cleanup loop. (Containers are left running for graceful reconnect.)"""
        if self._cleanup_task:
            self._cleanup_task.cancel()
            try:
                await self._cleanup_task
            except asyncio.CancelledError:
                pass

    # ── Session management ────────────────────────────────────────────────────

    async def start_session(self, user_id: str) -> UserSession:
        """
        Start a new QGIS container for user_id.
        Waits up to 120 s for the container to become healthy.
        """
        # Idempotent: return existing ready session
        existing = self.get_session(user_id)
        if existing and existing.status == "ready":
            existing.last_activity = time.time()
            return existing

        if len(self.sessions) >= self.max_containers:
            raise RuntimeError(
                f"Maximum number of containers ({self.max_containers}) reached."
            )

        idx    = self._allocate_index()
        api_p  = self.base_api_port    + idx
        str_p  = self.base_stream_port + idx
        vnc_p  = self.base_novnc_port  + idx

        # Ensure per-user data directory exists
        user_data_dir = os.path.abspath(os.path.join(self.data_dir, "users", user_id))
        os.makedirs(user_data_dir, exist_ok=True)

        # Environment forwarded to worker containers
        env = {
            "QGIS_RESOLUTION":   os.environ.get("QGIS_RESOLUTION", "1920x1080x24"),
            "MCP_PORT":          "8100",
            "VNC_HOST":          "localhost",
            "MOONDREAM_URL":     os.environ.get("MOONDREAM_URL", "http://host.docker.internal:8001"),
            "SAMGEO3_URL":       os.environ.get("SAMGEO3_URL",   "http://host.docker.internal:8002"),
            "DEPTHPRO_URL":      os.environ.get("DEPTHPRO_URL",  "http://host.docker.internal:8003"),
            "PYTHONUNBUFFERED":  "1",
            "MULTI_USER_MODE":   "false",   # workers must not spawn further containers
            # Host-side port so the bridge generates correct download URLs
            "API_HOST_PORT":     str(api_p),
        }

        print(f"[ContainerManager] Starting container for user={user_id} "
              f"api={api_p} stream={str_p} novnc={vnc_p}")

        container = await asyncio.to_thread(
            self._run_container,
            user_id=user_id,
            api_port=api_p,
            stream_port=str_p,
            novnc_port=vnc_p,
            user_data_dir=user_data_dir,
            env=env,
        )

        # Get container's internal IP on the Docker network
        container.reload()
        container_ip = ""
        networks = container.attrs.get("NetworkSettings", {}).get("Networks", {})
        if self.network_name in networks:
            container_ip = networks[self.network_name].get("IPAddress", "")

        session = UserSession(
            user_id=user_id,
            session_id=uuid.uuid4().hex,
            container_id=container.id,
            api_port=api_p,
            stream_port=str_p,
            novnc_port=vnc_p,
            container_ip=container_ip,
        )
        self.sessions[user_id] = session
        print(f"[ContainerManager] Container IP: {container_ip} (network={self.network_name})")

        # Wait for health (non-blocking from caller perspective)
        await self._wait_for_healthy(session)
        return session

    def get_session(self, user_id: str) -> Optional[UserSession]:
        """Return session if it exists and the container is still running."""
        session = self.sessions.get(user_id)
        if not session:
            return None

        # Verify container is still alive
        try:
            container = self._docker_client.containers.get(session.container_id)
            if container.status not in ("running", "starting"):
                self._release_session(user_id)
                return None
        except Exception:
            self._release_session(user_id)
            return None

        return session

    async def stop_session(self, user_id: str) -> None:
        """Stop and remove the user's container."""
        session = self.sessions.get(user_id)
        if not session:
            return

        session.status = "stopping"
        print(f"[ContainerManager] Stopping container for user={user_id}")

        await asyncio.to_thread(self._stop_container, session.container_id)
        self._release_session(user_id)

    def touch_session(self, user_id: str) -> None:
        """Update last_activity timestamp to prevent idle cleanup."""
        session = self.sessions.get(user_id)
        if session:
            session.last_activity = time.time()

    # ── HTTP proxy to container ───────────────────────────────────────────────

    async def execute_on_container(
        self,
        user_id: str,
        endpoint: str,
        method: str = "POST",
        data: Optional[dict] = None,
        timeout: float = 60.0,
    ) -> dict:
        """HTTP call to a user's container API."""
        session = self.sessions.get(user_id)
        if not session:
            return {"error": f"No active session for user {user_id}"}

        url = f"{session.internal_api_url}{endpoint}"
        self.touch_session(user_id)

        try:
            async with httpx.AsyncClient(timeout=timeout) as client:
                if method.upper() == "GET":
                    resp = await client.get(url)
                else:
                    resp = await client.post(url, json=data or {})
                return resp.json()
        except Exception as e:
            return {"error": f"Container call failed: {e}"}

    # ── Internal: Docker ──────────────────────────────────────────────────────

    def _run_container(
        self,
        user_id: str,
        api_port: int,
        stream_port: int,
        novnc_port: int,
        user_data_dir: str,
        env: dict,
    ):
        """Synchronous Docker container.run() call (run in thread).
        Passes GPU to the container if available (try/fallback)."""
        kwargs = dict(
            detach=True,
            network=self.network_name,
            ports={
                f"{_CONTAINER_API_PORT}/tcp":    api_port,
                f"{_CONTAINER_STREAM_PORT}/tcp": stream_port,
                f"{_CONTAINER_NOVNC_PORT}/tcp":  novnc_port,
            },
            volumes={
                user_data_dir: {"bind": "/data", "mode": "rw"},
            },
            environment=env,
            mem_limit="4g",
            nano_cpus=2_000_000_000,   # 2 CPUs
            shm_size="512m",           # X11 shared memory for QGIS (prevents signal 11 crash)
            labels={
                _LABEL_KEY:              _LABEL_VALUE,
                "qgisremotemcp.user_id": user_id,
            },
            extra_hosts={"host.docker.internal": "host-gateway"},
            remove=False,   # we remove manually on stop
        )

        # Try with GPU if available — fallback to CPU if it fails
        if self._gpu_available:
            try:
                gpu_env = {**env, "NVIDIA_VISIBLE_DEVICES": "all",
                           "NVIDIA_DRIVER_CAPABILITIES": "compute,utility"}
                return self._docker_client.containers.run(
                    self.image_name,
                    **{**kwargs, "environment": gpu_env,
                       "device_requests": [_docker.types.DeviceRequest(
                           count=-1, capabilities=[["gpu"]]
                       )]},
                )
            except Exception as e:
                print(f"[ContainerManager] GPU launch failed for user={user_id} "
                      f"({e.__class__.__name__}), falling back to CPU")

        return self._docker_client.containers.run(self.image_name, **kwargs)

    def _stop_container(self, container_id: str) -> None:
        """Synchronous stop + remove (run in thread)."""
        try:
            container = self._docker_client.containers.get(container_id)
            container.stop(timeout=10)
            container.remove(force=True)
        except _docker_errors.NotFound:
            pass
        except Exception as e:
            print(f"[ContainerManager] Warning during stop: {e}")

    async def _wait_for_healthy(self, session: UserSession, timeout: float = 120.0) -> None:
        """Poll /health until 200 or timeout."""
        url = f"{session.internal_api_url}/health"
        deadline = time.time() + timeout
        attempt = 0

        while time.time() < deadline:
            try:
                async with httpx.AsyncClient(timeout=3.0) as client:
                    resp = await client.get(url)
                    if resp.status_code == 200:
                        session.status = "ready"
                        print(f"[ContainerManager] Container ready for user={session.user_id} "
                              f"(after {attempt} attempts)")
                        return
            except Exception:
                pass

            attempt += 1
            await asyncio.sleep(2.0)

        session.status = "unhealthy"
        print(f"[ContainerManager] Container unhealthy for user={session.user_id} "
              f"after {timeout}s")

    # ── Internal: network + cleanup ───────────────────────────────────────────

    def _detect_gpu(self) -> None:
        """Probe for NVIDIA GPU by running a throwaway container with --gpus all.
        Sets self._gpu_available = True if the GPU is usable, False otherwise.
        This never breaks startup — failures are silently caught."""
        try:
            result = self._docker_client.containers.run(
                "ubuntu:22.04",
                command="nvidia-smi --query-gpu=name --format=csv,noheader",
                device_requests=[_docker.types.DeviceRequest(
                    count=-1, capabilities=[["gpu"]]
                )],
                remove=True,
                detach=False,
                stdout=True,
                stderr=True,
            )
            gpu_name = result.decode().strip()
            if gpu_name:
                self._gpu_available = True
                print(f"[ContainerManager] GPU detected: {gpu_name}")
            else:
                print("[ContainerManager] GPU probe returned empty — CPU mode")
        except Exception as e:
            print(f"[ContainerManager] No GPU available ({e.__class__.__name__}) — CPU mode")

    def _ensure_network(self) -> None:
        try:
            self._docker_client.networks.get(self.network_name)
        except _docker_errors.NotFound:
            self._docker_client.networks.create(
                self.network_name,
                driver="bridge",
                check_duplicate=True,
            )
            print(f"[ContainerManager] Created Docker network: {self.network_name}")

    def _cleanup_stale_containers(self) -> None:
        """Remove leftover containers from a previous gateway run."""
        try:
            containers = self._docker_client.containers.list(
                all=True,
                filters={"label": f"{_LABEL_KEY}={_LABEL_VALUE}"},
            )
            for c in containers:
                try:
                    c.stop(timeout=5)
                    c.remove(force=True)
                    print(f"[ContainerManager] Removed stale container: {c.short_id}")
                except Exception:
                    pass
        except Exception as e:
            print(f"[ContainerManager] Warning during stale cleanup: {e}")

    async def _cleanup_loop(self) -> None:
        """Background task: stop idle containers every 60 s."""
        while True:
            try:
                await asyncio.sleep(60)
                now = time.time()
                idle_users = [
                    uid for uid, s in list(self.sessions.items())
                    if now - s.last_activity > self.idle_timeout
                ]
                for uid in idle_users:
                    print(f"[ContainerManager] Idle timeout — stopping container for user={uid}")
                    await self.stop_session(uid)
            except asyncio.CancelledError:
                break
            except Exception as e:
                print(f"[ContainerManager] Cleanup loop error: {e}")

    def _allocate_index(self) -> int:
        for i in range(self.max_containers):
            if i not in self._used_indices:
                self._used_indices.add(i)
                return i
        raise RuntimeError("No free container slot available")

    def _release_session(self, user_id: str) -> None:
        session = self.sessions.pop(user_id, None)
        if session:
            # Find and release the index
            idx = session.api_port - self.base_api_port
            self._used_indices.discard(idx)

    # ── Info ──────────────────────────────────────────────────────────────────

    def list_sessions(self) -> List[dict]:
        return [s.to_dict() for s in self.sessions.values()]
