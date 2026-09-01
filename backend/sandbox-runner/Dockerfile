# Rewrite plan step 5: the hardened execution boundary for model-generated
# code (Schema/Executor, steps 10-11). Deliberately minimal: only the
# stdlib plus the two libraries generated code is allowed to use
# (STATIC_CHECK_ALLOWED_IMPORTS, step 9) -- pandas and openpyxl, pinned to
# the exact versions backend/requirements.txt already ships, so generated
# code behaves identically whether it runs via this container or via
# control/sandbox.py's multiprocessing path for first-party helpers.
#
# python:3.11-slim (Debian-slim) was chosen over a distroless base after
# weighing both: a distroless final stage would need pandas'/numpy's
# compiled C-extension wheels to match its exact glibc + Python build,
# which is fragile to keep in lockstep with backend/requirements.txt
# across Python patch releases, for a security payoff this image does
# not actually need -- the residual risk of a present-but-unused
# /bin/sh and package manager is closed by every OTHER layer
# (--read-only rootfs, --cap-drop ALL, seccomp, --network none, the
# step 9 static AST check gating what code ever reaches this image at
# all), not by the base image's own package list.
FROM python:3.11-slim AS base

# Never inherit the base image's own HOME=/root: Dockerfile ENV has no
# "unset" primitive, so this is the closest equivalent to
# control/sandbox.py's multiprocessing path omitting HOME entirely from
# its child env allowlist.
ENV HOME=""

# No apt packages beyond the base image's own minimal set: no curl, no
# wget, no build tooling. pandas/openpyxl ship manylinux wheels, so no
# compiler is needed to install them on this base.
RUN pip install --no-cache-dir --no-compile \
        pandas==3.0.3 \
        numpy==1.26.4 \
        openpyxl==3.1.5 \
    && python -m compileall -q /usr/local/lib/python3.11 || true \
    && rm -rf /root/.cache

# Non-root, fixed numeric uid/gid -- also passed explicitly at `docker run`
# time (--user 10001:10001) so the runtime flag is authoritative even if
# this image is ever launched without its own USER directive honored.
RUN groupadd --gid 10001 sandboxrunner \
    && useradd --uid 10001 --gid 10001 --no-create-home --shell /usr/sbin/nologin sandboxrunner

COPY runner_shim.py /opt/sandbox-runner/runner_shim.py
RUN chown -R root:root /opt/sandbox-runner && chmod -R a+rX /opt/sandbox-runner

USER sandboxrunner
WORKDIR /workspace

# No shell in the runtime invocation: ContainerRunner always overrides
# this with an explicit `python /opt/sandbox-runner/runner_shim.py`
# argv, but a safe default is set anyway in case the image is ever
# launched interactively for diagnostics.
ENTRYPOINT ["python", "/opt/sandbox-runner/runner_shim.py"]
