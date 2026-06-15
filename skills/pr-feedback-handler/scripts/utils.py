import subprocess
import sys

def run_cmd(args, cwd=None, timeout=30):
    try:
        result = subprocess.run(args, capture_output=True, text=True, check=True, cwd=cwd, timeout=timeout)
        return result.stdout.strip()
    except subprocess.TimeoutExpired as e:
        error_msg = f"Command {' '.join(str(x) for x in args)} timed out after {timeout} seconds."
        raise RuntimeError(error_msg) from e
    except subprocess.CalledProcessError as e:
        error_msg = f"Command {' '.join(str(x) for x in args)} failed with exit status {e.returncode}.\nStdout: {e.stdout}\nStderr: {e.stderr}"
        raise RuntimeError(error_msg) from e

def run_git(args, cwd=None, timeout=30):
    try:
        result = subprocess.run(["git"] + args, capture_output=True, text=True, check=True, cwd=cwd, timeout=timeout)
        return result.stdout.strip()
    except Exception:
        return ""
