"""
runner.py
---------
Runs a GENERATED project's Python (FastAPI) backend so you can fully test the
app from inside the builder.

For each project we:
  1. create an isolated virtual-env at  projects/<id>/.venv  (once),
  2. pip-install any requirements.txt we find,
  3. launch the backend with uvicorn on a free port,
  4. capture logs and expose start / stop / status.

Cross-platform (Windows + macOS + Linux). No third-party deps — stdlib only.

SECURITY NOTE: this executes model-generated Python on your machine. Only run
projects you have reviewed and trust. See README.md.
"""

from __future__ import annotations

import os
import re
import socket
import subprocess
import sys
import threading
import time
from collections import deque
from dataclasses import dataclass, field
from pathlib import Path

import storage

IS_WIN = os.name == "nt"

# entry-point filenames we look for, in priority order
ENTRY_CANDIDATES = [
    "backend/server.py",
    "server.py",
    "backend/main.py",
    "main.py",
    "backend/app.py",
    "app.py",
]

_APP_VAR_RE = re.compile(r"^\s*(\w+)\s*=\s*FastAPI\s*\(", re.MULTILINE)


@dataclass
class RunState:
    project_id: str
    status: str = "stopped"          # stopped|installing|starting|running|error|exited
    port: int | None = None
    pid: int | None = None
    message: str = ""
    started_at: float | None = None
    proc: subprocess.Popen | None = None
    logs: deque = field(default_factory=lambda: deque(maxlen=400))
    lock: threading.Lock = field(default_factory=threading.Lock)

    def log(self, line: str) -> None:
        self.logs.append(line.rstrip("\n"))

    def public(self) -> dict:
        running = self.status == "running" and self.proc is not None and self.proc.poll() is None
        return {
            "project_id": self.project_id,
            "status": self.status,
            "port": self.port,
            "url": f"http://localhost:{self.port}" if (running and self.port) else None,
            "message": self.message,
            "pid": self.pid,
            "started_at": self.started_at,
            "logs": list(self.logs)[-120:],
        }


class RunnerManager:
    def __init__(self) -> None:
        self._states: dict[str, RunState] = {}
        self._guard = threading.Lock()

    # -- helpers ---------------------------------------------------------
    def _state(self, project_id: str) -> RunState:
        with self._guard:
            st = self._states.get(project_id)
            if st is None:
                st = RunState(project_id=project_id)
                self._states[project_id] = st
            return st

    @staticmethod
    def _free_port() -> int:
        s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]
        s.close()
        return port

    @staticmethod
    def _venv_python(pdir: Path) -> Path:
        if IS_WIN:
            return pdir / ".venv" / "Scripts" / "python.exe"
        return pdir / ".venv" / "bin" / "python"

    @staticmethod
    def _find_entry(pdir: Path) -> Path | None:
        for rel in ENTRY_CANDIDATES:
            p = pdir / rel
            if p.exists():
                return p
        # last resort: any *.py containing `= FastAPI(`
        for p in pdir.rglob("*.py"):
            if ".venv" in p.parts:
                continue
            try:
                if "FastAPI(" in p.read_text(encoding="utf-8", errors="ignore"):
                    return p
            except OSError:
                continue
        return None

    @staticmethod
    def _app_var(entry: Path) -> str:
        try:
            m = _APP_VAR_RE.search(entry.read_text(encoding="utf-8", errors="ignore"))
            if m:
                return m.group(1)
        except OSError:
            pass
        return "app"

    @staticmethod
    def _requirements_files(pdir: Path) -> list[Path]:
        found = []
        for rel in ("requirements.txt", "backend/requirements.txt"):
            p = pdir / rel
            if p.exists():
                found.append(p)
        return found

    # -- public API ------------------------------------------------------
    def status(self, project_id: str) -> dict:
        st = self._state(project_id)
        # detect a process that died on its own
        if st.proc is not None and st.proc.poll() is not None and st.status == "running":
            st.status = "exited"
            st.message = f"Backend process exited (code {st.proc.returncode})."
        return st.public()

    def start(self, project_id: str) -> dict:
        st = self._state(project_id)
        with st.lock:
            if st.status in ("installing", "starting"):
                return st.public()
            if st.status == "running" and st.proc and st.proc.poll() is None:
                return st.public()

            pdir = storage.project_dir(project_id)
            if not pdir.exists():
                st.status = "error"
                st.message = "Project files not found on disk."
                return st.public()

            entry = self._find_entry(pdir)
            if entry is None:
                st.status = "error"
                st.message = (
                    "No Python backend found in this project — it's a static site. "
                    "Use the Preview tab directly."
                )
                return st.public()

            st.status = "installing"
            st.message = "Preparing environment…"
            st.logs.clear()

        t = threading.Thread(
            target=self._bootstrap_and_run, args=(project_id, pdir, entry), daemon=True
        )
        t.start()
        return st.public()

    def _bootstrap_and_run(self, project_id: str, pdir: Path, entry: Path) -> None:
        st = self._state(project_id)
        try:
            venv_py = self._venv_python(pdir)

            # 1. create venv if missing
            if not venv_py.exists():
                st.log("Creating virtual environment (.venv)…")
                self._run_blocking(
                    [sys.executable, "-m", "venv", str(pdir / ".venv")], st, pdir
                )

            # 2. upgrade pip quietly + install requirements
            reqs = self._requirements_files(pdir)
            # uvicorn/fastapi are required for the entry point regardless
            st.log("Installing dependencies (this can take a minute the first time)…")
            base = [str(venv_py), "-m", "pip", "install", "--disable-pip-version-check", "-q"]
            self._run_blocking(base + ["fastapi", "uvicorn[standard]"], st, pdir)
            for rf in reqs:
                st.log(f"pip install -r {rf.relative_to(pdir)}")
                self._run_blocking(base + ["-r", str(rf)], st, pdir)

            # 3. launch uvicorn
            st.status = "starting"
            st.message = "Starting backend…"
            port = self._free_port()
            app_var = self._app_var(entry)
            run_cwd = entry.parent
            module = entry.stem  # e.g. "server"

            env = os.environ.copy()
            env.update(storage.get_project_env(project_id))  # user-provided API keys
            env["PORT"] = str(port)
            env["PYTHONUNBUFFERED"] = "1"
            # let imports resolve from both the entry dir and the project root
            env["PYTHONPATH"] = (
                f"{run_cwd}{os.pathsep}{pdir}{os.pathsep}" + env.get("PYTHONPATH", "")
            )

            cmd = [
                str(venv_py), "-m", "uvicorn",
                f"{module}:{app_var}",
                "--host", "127.0.0.1",
                "--port", str(port),
            ]
            st.log(f"$ {' '.join(cmd)}  (cwd={run_cwd})")

            creationflags = subprocess.CREATE_NEW_PROCESS_GROUP if IS_WIN else 0
            popen_kwargs = dict(
                cwd=str(run_cwd),
                env=env,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
                bufsize=1,
            )
            if IS_WIN:
                popen_kwargs["creationflags"] = creationflags
            else:
                popen_kwargs["start_new_session"] = True

            proc = subprocess.Popen(cmd, **popen_kwargs)
            st.proc = proc
            st.pid = proc.pid
            st.port = port
            st.started_at = time.time()

            threading.Thread(target=self._pump_logs, args=(project_id, proc), daemon=True).start()

            # wait for the port to accept connections (up to ~20s)
            if self._wait_for_port(port, proc, timeout=20.0):
                st.status = "running"
                st.message = f"Running on http://localhost:{port}"
                st.log(st.message)
            else:
                if proc.poll() is not None:
                    st.status = "error"
                    st.message = "Backend exited during startup — check logs."
                else:
                    # process alive but slow; treat as running
                    st.status = "running"
                    st.message = f"Running on http://localhost:{port} (slow start)"
        except Exception as e:  # noqa: BLE001
            st.status = "error"
            st.message = f"{type(e).__name__}: {e}"
            st.log(st.message)

    def _pump_logs(self, project_id: str, proc: subprocess.Popen) -> None:
        st = self._state(project_id)
        try:
            assert proc.stdout is not None
            for line in proc.stdout:
                st.log(line)
        except Exception:
            pass
        finally:
            code = proc.poll()
            if st.status == "running":
                st.status = "exited"
                st.message = f"Backend process exited (code {code})."

    @staticmethod
    def _wait_for_port(port: int, proc: subprocess.Popen, timeout: float) -> bool:
        deadline = time.time() + timeout
        while time.time() < deadline:
            if proc.poll() is not None:
                return False
            try:
                with socket.create_connection(("127.0.0.1", port), timeout=0.5):
                    return True
            except OSError:
                time.sleep(0.3)
        return False

    def _run_blocking(self, cmd: list[str], st: RunState, cwd: Path) -> None:
        proc = subprocess.run(
            cmd, cwd=str(cwd), stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True
        )
        for line in (proc.stdout or "").splitlines():
            st.log(line)
        if proc.returncode != 0:
            raise RuntimeError(f"Command failed ({proc.returncode}): {' '.join(cmd)}")

    def stop(self, project_id: str) -> dict:
        st = self._state(project_id)
        with st.lock:
            proc = st.proc
            if proc is not None and proc.poll() is None:
                st.log("Stopping backend…")
                try:
                    if IS_WIN:
                        subprocess.run(
                            ["taskkill", "/F", "/T", "/PID", str(proc.pid)],
                            stdout=subprocess.DEVNULL,
                            stderr=subprocess.DEVNULL,
                        )
                    else:
                        import signal
                        os.killpg(os.getpgid(proc.pid), signal.SIGTERM)
                        try:
                            proc.wait(timeout=5)
                        except subprocess.TimeoutExpired:
                            os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
                except Exception as e:  # noqa: BLE001
                    st.log(f"stop error: {e}")
            st.status = "stopped"
            st.message = "Backend stopped."
            st.proc = None
            st.pid = None
            st.port = None
        return st.public()

    def stop_all(self) -> None:
        for pid in list(self._states.keys()):
            try:
                self.stop(pid)
            except Exception:
                pass


runner = RunnerManager()
