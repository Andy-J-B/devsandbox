# Running Container Isolation Security Tests

This guide outlines how to execute the container isolation and security hardening test suite (`tests/security/test_container_isolation.py`) inside an isolated Linux container environment.

---

### Step 1: Launch an Interactive Container Session

Start a new container session as root (`--user 0:0`) using a clean Python 3.12 slim image, mounting your workspace and the host's Docker socket:

```bash
docker run --rm -it \
  -v /var/run/docker.sock:/var/run/docker.sock \
  -v "$(pwd)":/workspace \
  -w /workspace \
  --user 0:0 \
  python:3.12-slim \
  bash

```

---

### Step 2: Install Dependencies and Docker CLI

Inside your container shell, update package lists to install the Docker CLI client, then install the package in editable development mode:

```bash
apt-get update && apt-get install -y docker.io
pip install -e '.[dev]'

```

---

### Step 3: Build the Validator Image

Build and tag the validator image so that the security test suite's image inspection checks succeed:

```bash
 docker build -t devsandbox/validator:latest -f ./docker/validator-base/Dockerfile .

```

---

### Step 4: Run the Security Tests

Set the required environment flags and run pytest with stdout printing enabled:

```bash
export DEVSANDBOX_E2E="1"
export DEVSANDBOX_RUN_VALIDATION_INTEGRATION="1"
python3 -m pytest tests/security/test_container_isolation.py -s

```
