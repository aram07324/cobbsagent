# Test sandbox modes

Milestone two defaults to the existing isolated Python virtual environment:

```powershell
python cobbsagent_m2.py eval
```

To run visible pytest suites and agent-requested test reruns in Docker instead:

```powershell
docker pull python:3.12-slim
python cobbsagent_m2.py --sandbox docker eval
```

Set `COBBSAGENT_DOCKER_IMAGE` or pass `--docker-image` to use another
pre-pulled Python image. The Docker executor never pulls images implicitly.
It installs the repo's declared requirements and pytest in a resource-limited
container with no inherited host environment, then runs tests in a separate
container with networking disabled, a read-only repository mount, a read-only
package mount, and no-new-privileges. The dependency-install container has
bounded CPU, memory, process count, and execution time; it needs bridge
network access to resolve packages. Test code has no network access.

The pipeline still creates its regular host venv for pip-audit and installed
version inventory, so Docker mode is an additional boundary around test
execution, not a complete replacement for host-side dependency installation.
Use a disposable machine or VM for repositories you do not trust. Docker mode
was validated with a fake runner; the project repository's tests were not run
inside Docker during development.
