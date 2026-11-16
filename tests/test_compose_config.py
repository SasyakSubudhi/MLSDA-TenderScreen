"""Static checks on the Compose files (no Docker needed): secrets scoping, loopback ports,
raw Docker socket confinement, and that the ${VAR:-default} tunables match .env.example."""
import re
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[1]


def _load(name):
    return yaml.safe_load((ROOT / name).read_text(encoding="utf-8"))


def _env_example():
    out = {}
    for line in (ROOT / ".env.example").read_text(encoding="utf-8").splitlines():
        if line and not line.startswith("#") and "=" in line:
            k, v = line.split("=", 1)
            out[k.strip()] = v.strip()
    return out


def test_tunable_defaults_match_env_example():
    tun = _load("docker-compose.yml")["x-tunables"]
    ex = _env_example()
    for key, val in tun.items():
        m = re.fullmatch(r"\$\{(\w+):-(.*)\}", str(val))
        assert m and m.group(1) == key, key
        assert m.group(2) == ex[key], f"{key}: compose default {m.group(2)!r} != .env.example {ex[key]!r}"


def test_no_service_reads_the_project_env_file():
    for name, svc in _load("docker-compose.yml")["services"].items():
        for ref in svc.get("env_file", []) or []:
            path = ref["path"] if isinstance(ref, dict) else ref
            assert path == ".env.example", (name, path)


def test_published_ports_are_loopback_only():
    for name, svc in _load("docker-compose.yml")["services"].items():
        for port in svc.get("ports", []) or []:
            assert str(port).startswith("127.0.0.1:"), (name, port)


def test_only_the_proxy_mounts_the_docker_socket_and_it_is_read_only_scoped():
    svcs = _load("docker-compose.yml")["services"]
    mounts = {n for n, s in svcs.items() if any("docker.sock" in str(v) for v in s.get("volumes", []) or [])}
    assert mounts == {"docker-proxy"}
    assert all(str(v).endswith(":ro") for v in svcs["docker-proxy"]["volumes"])
    env = svcs["docker-proxy"]["environment"]
    assert env["CONTAINERS"] == "1" and env["POST"] == "0" and env["EXEC"] == "0"
    assert not svcs["docker-proxy"].get("ports")


def test_hosted_override_gives_the_key_to_the_gateway_only():
    hosted = _load("docker-compose.hosted.yml")
    assert list(hosted["services"]) == ["llm-gateway"]
    gw = hosted["services"]["llm-gateway"]
    assert gw["secrets"] == ["llm_api_key"]
    assert gw["environment"]["LLM_API_KEY_FILE"] == "/run/secrets/llm_api_key"
    assert "LLM_API_KEY" not in gw["environment"]
    assert "llm_api_key" in hosted["secrets"]


def test_shared_override_disables_demo_routes_and_uses_a_grafana_file_secret():
    shared = _load("docker-compose.shared.yml")
    assert shared["services"]["api"]["environment"]["DEMO_MODE"] == "false"
    g = shared["services"]["grafana"]
    assert g["environment"]["GF_SECURITY_ADMIN_PASSWORD__FILE"].startswith("/run/secrets/")
    assert g["environment"]["GF_AUTH_ANONYMOUS_ENABLED"] == "false"
    assert set(shared["services"]) == {"api", "grafana"}
