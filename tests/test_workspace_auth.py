"""Authentification des appels entrants du workspace (audit 2026-09-26, SEC-2).

L'API (8080, dont /api/execute), le serveur MCP (8100) et noVNC (6080)
n'authentifiaient rien ; x11vnc ecoutait sans mot de passe sur toutes les
interfaces. Le vecteur de reference est le meme que dans qgis-sspcloud
hub/tests/test_workspace_auth.py : les deux calculs doivent rester alignes.
"""

from __future__ import annotations

import hashlib
import hmac
import sys
from pathlib import Path

import pytest

_RACINE = Path(__file__).resolve().parents[1]
for chemin in (str(_RACINE), str(_RACINE / "src")):
    if chemin not in sys.path:
        sys.path.insert(0, chemin)

from src import workspace_auth  # noqa: E402

CLE_REFERENCE = "qgis_alice_0123456789abcdef0123456789abcdef"
JETON_REFERENCE = hmac.new(
    CLE_REFERENCE.encode(), b"qgis-workspace-v1", hashlib.sha256).hexdigest()


@pytest.fixture
def env(monkeypatch):
    monkeypatch.setenv("HUB_API_KEY", CLE_REFERENCE)
    monkeypatch.delenv("WORKSPACE_TOKEN", raising=False)
    monkeypatch.setenv("WORKSPACE_AUTH_MODE", "enforce")
    return monkeypatch


# ── Regle de decision ────────────────────────────────────────────────────────

def test_jeton_de_reference(env):
    assert workspace_auth.jeton_attendu() == JETON_REFERENCE


def test_enforce(env):
    d = workspace_auth.decider
    assert d("/api/execute", "10.0.0.5", {})[0] is False
    assert d("/api/execute", "10.0.0.5", {"x-workspace-token": "faux"})[0] is False
    assert d("/api/execute", "10.0.0.5",
             {"x-workspace-token": JETON_REFERENCE})[0] is True
    assert d("/mcp", "10.0.0.5",
             {"authorization": f"Bearer {CLE_REFERENCE}"})[0] is True
    # La cle maitre n'est pas un jeton de workspace, et inversement.
    assert d("/mcp", "10.0.0.5", {"x-workspace-token": CLE_REFERENCE})[0] is False
    assert d("/mcp", "10.0.0.5",
             {"authorization": f"Bearer {JETON_REFERENCE}"})[0] is False


def test_boucle_locale_et_sante_sans_jeton(env):
    for hote in ("127.0.0.1", "::1"):
        assert workspace_auth.decider("/api/command", hote, {})[0] is True
    assert workspace_auth.decider("/health", "10.0.0.5", {})[0] is True


def test_enforce_sans_cle_ferme(env):
    env.delenv("HUB_API_KEY")
    autorise, raison = workspace_auth.decider("/api/execute", "10.0.0.5", {})
    assert autorise is False and "HUB_API_KEY" in raison
    # Un jeton vide n'est jamais egal a un jeton vide.
    assert workspace_auth.decider(
        "/api/execute", "10.0.0.5", {"x-workspace-token": ""})[0] is False


def test_permissive_laisse_passer(env):
    env.setenv("WORKSPACE_AUTH_MODE", "permissive")
    autorise, raison = workspace_auth.decider("/api/execute", "10.0.0.5", {})
    assert autorise is True and raison.startswith("permissif")


def test_mode_inconnu_vaut_enforce(env):
    env.setenv("WORKSPACE_AUTH_MODE", "enforced")
    assert workspace_auth.mode() == "enforce"
    env.delenv("WORKSPACE_AUTH_MODE")
    assert workspace_auth.mode() == "permissive"


def test_jeton_explicite(env):
    env.setenv("WORKSPACE_TOKEN", "fourni")
    assert workspace_auth.decider(
        "/mcp", "10.0.0.5", {"x-workspace-token": "fourni"})[0] is True


def test_cors_par_defaut_local(monkeypatch):
    monkeypatch.delenv("WORKSPACE_CORS_ORIGINS", raising=False)
    liste, motif = workspace_auth.origines_cors()
    assert liste == [] and "localhost" in motif
    monkeypatch.setenv("WORKSPACE_CORS_ORIGINS", "https://a.fr, https://b.fr")
    assert workspace_auth.origines_cors() == (["https://a.fr", "https://b.fr"], None)


# ── Branchement dans les serveurs ────────────────────────────────────────────

def test_api_server_refuse_sans_jeton(env):
    from fastapi.testclient import TestClient
    import api_server

    client = TestClient(api_server.app)  # hote client : « testclient »
    assert client.post("/api/execute", json={"code": "1"}).status_code == 401
    assert client.get("/api/files").status_code == 401
    # /health reste libre (sonde Docker) : il repond, quel que soit l'etat.
    assert client.get("/health").status_code != 401
    # Avec le jeton, la requete atteint la route (503 : pas de pont QGIS ici).
    r = client.get("/api/files",
                   headers={"X-Workspace-Token": JETON_REFERENCE})
    assert r.status_code != 401


def test_api_server_cors_ne_renvoie_plus_etoile(env):
    from fastapi.testclient import TestClient
    import api_server

    client = TestClient(api_server.app)
    r = client.options("/api/files", headers={
        "Origin": "https://attaquant.example",
        "Access-Control-Request-Method": "GET",
    })
    assert r.headers.get("access-control-allow-origin") not in ("*", "https://attaquant.example")


def test_serveur_mcp_refuse_sans_jeton(env):
    from starlette.testclient import TestClient
    import main_mcp

    client = TestClient(main_mcp.app)
    corps = {"jsonrpc": "2.0", "id": 1, "method": "tools/list"}
    r = client.post("/mcp", json=corps)
    assert r.status_code == 401
    assert r.json()["error"]["code"] == -32001
    r = client.post("/mcp", json=corps,
                    headers={"X-Workspace-Token": JETON_REFERENCE})
    assert r.status_code != 401


# ── noVNC, x11vnc, flux MJPEG ────────────────────────────────────────────────

def test_novnc_enforce_ajoute_basic(env):
    from src import lancer_novnc
    args = lancer_novnc.arguments()
    assert "--auth-plugin" in args and "BasicHTTPAuth" in args
    assert f"hub:{JETON_REFERENCE}" in args
    assert args[-2:] == ["6080", "localhost:5900"]


def test_novnc_permissif_inchange(env):
    from src import lancer_novnc
    env.setenv("WORKSPACE_AUTH_MODE", "permissive")
    args = lancer_novnc.arguments()
    assert "--auth-plugin" not in args
    assert args == ["/usr/bin/websockify", "--web=/opt/novnc", "6080", "localhost:5900"]


def test_novnc_enforce_sans_cle_ferme(env):
    from src import lancer_novnc
    env.delenv("HUB_API_KEY")
    args = lancer_novnc.arguments()
    source = args[args.index("--auth-source") + 1]
    assert source.startswith("hub:") and len(source) > len("hub:") + 32


def test_supervisord():
    conf = (_RACINE / "supervisord.conf").read_text(encoding="utf-8")
    ligne_vnc = next(l for l in conf.splitlines() if l.startswith("command=/usr/bin/x11vnc"))
    assert "-localhost" in ligne_vnc
    assert "command=python3 /app/src/lancer_novnc.py" in conf


def test_flux_mjpeg_adresse_configurable():
    source = (_RACINE / "src" / "stream_server.py").read_text(encoding="utf-8")
    assert 'os.environ.get("STREAM_BIND_HOST"' in source
