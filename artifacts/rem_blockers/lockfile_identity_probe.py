from __future__ import annotations

import json
import os
import select
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from urllib import request as urllib_request

import yaml


def write_config(tmp_dir: str) -> str:
    config = {
        "profiles": {
            "dev": {
                "default": True,
            }
        },
        "auth": {"mode": "open"},
    }
    path = os.path.join(tmp_dir, "tela.yaml")
    with open(path, "w", encoding="utf-8") as handle:
        yaml.dump(config, handle)
    return path


def wait_for_lockfile(lockfile: Path, timeout: float = 10.0) -> str:
    deadline = time.time() + timeout
    while time.time() < deadline:
        if lockfile.exists():
            return lockfile.read_text(encoding="utf-8")
        time.sleep(0.1)
    raise RuntimeError(f"lockfile did not appear within {timeout} seconds: {lockfile}")


def read_json_line(stream) -> str:
    ready, _, _ = select.select([stream], [], [], 10.0)
    if not ready:
        raise RuntimeError("timed out waiting for JSON line response")
    line = stream.readline()
    if not line:
        raise RuntimeError("EOF while reading JSON line response")
    return line.decode("utf-8", errors="replace")


def main() -> int:
    lockfile_path = Path.home() / ".tela" / "gateway.lock"
    with tempfile.TemporaryDirectory() as tmp_dir:
        config_path = write_config(tmp_dir)
        serve_proc = subprocess.Popen(
            [
                sys.executable,
                "-m",
                "tela",
                "serve",
                "--config",
                config_path,
                "--port",
                "0",
                "--default-profile",
                "dev",
            ],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )

        connect_proc = None
        try:
            raw_lockfile = wait_for_lockfile(lockfile_path)
            lock = json.loads(raw_lockfile)

            print(f"lockfile_path={lockfile_path}")
            print("lockfile_raw_begin")
            print(raw_lockfile)
            print("lockfile_raw_end")
            print(f"lockfile_pid={lock['pid']}")
            print(f"lockfile_host={lock['host']}")
            print(f"lockfile_port={lock['port']}")
            print(f"lockfile_token={lock['token']}")
            print(f"probe_base_url=http://{lock['host']}:{lock['port']}")
            print(f"probe_authorization=Bearer {lock['token']}")
            pid_ps = subprocess.run(
                [
                    "ps",
                    "-p",
                    str(lock["pid"]),
                    "-o",
                    "pid=,ppid=,stat=,command=",
                ],
                capture_output=True,
                text=True,
                check=False,
            )
            print(f"pid_ps_returncode={pid_ps.returncode}")
            print("pid_ps_stdout_begin")
            print(pid_ps.stdout, end="")
            print("pid_ps_stdout_end")
            print("pid_ps_stderr_begin")
            print(pid_ps.stderr, end="")
            print("pid_ps_stderr_end")
            listen_lsof = subprocess.run(
                [
                    "lsof",
                    "-a",
                    "-p",
                    str(lock["pid"]),
                    "-iTCP",
                    "-sTCP:LISTEN",
                ],
                capture_output=True,
                text=True,
                check=False,
            )
            print(f"lsof_returncode={listen_lsof.returncode}")
            print("lsof_stdout_begin")
            print(listen_lsof.stdout, end="")
            print("lsof_stdout_end")
            print("lsof_stderr_begin")
            print(listen_lsof.stderr, end="")
            print("lsof_stderr_end")

            status_request = urllib_request.Request(
                f"http://{lock['host']}:{lock['port']}/status",
                method="GET",
                headers={
                    "Authorization": f"Bearer {lock['token']}",
                    "Accept": "application/json",
                },
            )
            with urllib_request.urlopen(status_request, timeout=10.0) as response:
                status_body = response.read().decode("utf-8")
                print(f"status_http_code={response.status}")
                print("status_body_begin")
                print(status_body)
                print("status_body_end")

            connect_proc = subprocess.Popen(
                [
                    sys.executable,
                    "-m",
                    "tela",
                    "connect",
                    "--config",
                    config_path,
                    "--default-profile",
                    "dev",
                ],
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
            )
            assert connect_proc.stdin is not None
            assert connect_proc.stdout is not None
            assert connect_proc.stderr is not None
            time.sleep(2.0)
            print(f"connect_poll_t_plus_2={connect_proc.poll()}")

            initialize_request = {
                "jsonrpc": "2.0",
                "id": 1,
                "method": "initialize",
                "params": {
                    "protocolVersion": "2024-11-05",
                    "capabilities": {},
                    "clientInfo": {"name": "probe", "version": "0.1"},
                },
            }
            init_line = json.dumps(initialize_request) + "\n"
            connect_proc.stdin.write(init_line.encode("utf-8"))
            connect_proc.stdin.flush()
            initialize_response = read_json_line(connect_proc.stdout)
            print("initialize_response_line_begin")
            print(initialize_response, end="")
            print("initialize_response_line_end")

            tools_request = {
                "jsonrpc": "2.0",
                "id": 2,
                "method": "tools/list",
                "params": {},
            }
            tools_line = json.dumps(tools_request) + "\n"
            connect_proc.stdin.write(tools_line.encode("utf-8"))
            connect_proc.stdin.flush()
            tools_response = read_json_line(connect_proc.stdout)
            print("tools_list_response_line_begin")
            print(tools_response, end="")
            print("tools_list_response_line_end")

            stderr_snapshot = ""
            ready_stderr, _, _ = select.select([connect_proc.stderr], [], [], 0.2)
            if ready_stderr:
                stderr_snapshot = os.read(connect_proc.stderr.fileno(), 65536).decode(
                    "utf-8", errors="replace"
                )
            print("connect_stderr_snapshot_begin")
            print(stderr_snapshot)
            print("connect_stderr_snapshot_end")
            print(f"connect_poll_before_term={connect_proc.poll()}")
            return 0
        finally:
            if connect_proc is not None:
                try:
                    if connect_proc.stdin is not None and not connect_proc.stdin.closed:
                        connect_proc.stdin.close()
                except OSError:
                    pass
                connect_proc.terminate()
                try:
                    connect_proc.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    connect_proc.kill()
                    connect_proc.wait(timeout=5)
            serve_proc.terminate()
            try:
                serve_proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                serve_proc.kill()
                serve_proc.wait(timeout=5)


if __name__ == "__main__":
    raise SystemExit(main())
