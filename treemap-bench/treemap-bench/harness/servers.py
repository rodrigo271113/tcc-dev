"""Bring the two treemap servers up/down via docker compose and health-check them."""
from __future__ import annotations

import os
import subprocess
import time
import urllib.error
import urllib.request
from pathlib import Path


class Server:
    def __init__(self, name: str, app_cfg: dict, paths: dict):
        self.name = name
        self.cfg = app_cfg
        self.paths = paths
        self.base_url = app_cfg["base_url"]
        self.container = app_cfg["container"]
        self.compose_file = app_cfg["compose_file"]
        self.compose_cwd = app_cfg["compose_cwd"]
        self.project = app_cfg["project"]

    # -- compose helpers ---------------------------------------------------
    def _env(self) -> dict:
        env = dict(os.environ)
        env["PLOTLY_REPO"] = self.paths["plotly_repo"]
        env["DUKS_REPO"] = self.paths["duks_repo"]
        env.setdefault("UID", str(os.getuid()))
        env.setdefault("GID", str(os.getgid()))
        return env

    def _compose(self, *args: str, check: bool = True) -> subprocess.CompletedProcess:
        cmd = ["docker", "compose", "-p", self.project, "-f", self.compose_file, *args]
        return subprocess.run(
            cmd, cwd=self.compose_cwd, env=self._env(),
            check=check, text=True, capture_output=True,
        )

    def up(self, build: bool = True, timeout: int = 240) -> None:
        print(f"[{self.name}] compose up ...", flush=True)
        args = ["up", "-d"]
        if build:
            args.append("--build")
        r = self._compose(*args, check=False)
        if r.returncode != 0:
            raise RuntimeError(f"[{self.name}] compose up failed:\n{r.stdout}\n{r.stderr}")
        self.wait_healthy(timeout=timeout)

    def down(self) -> None:
        print(f"[{self.name}] compose down ...", flush=True)
        self._compose("down", "-v", check=False)

    def restart(self, timeout: int = 120) -> None:
        subprocess.run(["docker", "restart", self.container], check=True,
                       text=True, capture_output=True)
        self.wait_healthy(timeout=timeout)

    def drop_page_cache_hint(self) -> None:
        """Best-effort: ask the container to drop its own caches is not possible
        without privileges; a restart (see restart()) is the real cold path."""

    # -- health ----------------------------------------------------------
    def wait_healthy(self, timeout: int = 180) -> None:
        probe = self.base_url.rstrip("/") + "/__tmbench_probe__"
        deadline = time.time() + timeout
        last = ""
        while time.time() < deadline:
            try:
                urllib.request.urlopen(probe, timeout=5)
                print(f"[{self.name}] healthy at {self.base_url}", flush=True)
                return
            except urllib.error.HTTPError:
                # Any HTTP response (e.g. 404) means the app is serving.
                print(f"[{self.name}] healthy at {self.base_url}", flush=True)
                return
            except Exception as e:  # ConnectionRefused / timeout while booting
                last = repr(e)
                time.sleep(1.0)
        # Dump recent logs to help debugging.
        logs = subprocess.run(
            ["docker", "logs", "--tail", "40", self.container],
            text=True, capture_output=True,
        )
        raise RuntimeError(
            f"[{self.name}] not healthy after {timeout}s (last: {last})\n"
            f"---- container logs ----\n{logs.stdout}\n{logs.stderr}"
        )

    def logs(self, tail: int = 60) -> str:
        r = subprocess.run(["docker", "logs", "--tail", str(tail), self.container],
                           text=True, capture_output=True)
        return r.stdout + r.stderr


def build_servers(cfg: dict) -> dict[str, Server]:
    return {
        name: Server(name, app_cfg, cfg["paths"])
        for name, app_cfg in cfg["apps"].items()
    }
