# -*- coding: utf-8 -*-
"""Quatre defauts mesures sur l'instance de production le 2026-09-17.

Eprouves en pilotant le vrai poste QGIS, pas en lisant le code :

  * `result = {...}` -- la facon naturelle d'ecrire, et celle que suggere la
    doc de l'outil -- renvoyait `success: true` avec un resultat VIDE. Le pont
    relisait sa propre variable locale au lieu de l'espace du script. L'appelant
    se croyait servi et n'avait rien : echec muet, le pire des cas.
  * Le projet perdait son identite a chaque script : `_auto_save` fait
    `project.write(...)`, qui reaffecte le nom de fichier. Mesure en direct :
    `fileName()` valait `/data/.autosave.qgz` au lieu du chemin de l'etude.
  * Le catalogue annoncait 54 sources pour 49 reelles : il comptait ses propres
    pseudo-entrees `_comment`, et les renvoyait a l'appelant.
  * La carte ne suivait pas les donnees : on ne cadrait que sur la toute
    premiere couche, or un fond de carte mondial est charge en premier. Mesure :
    52 000 batiments charges, `zoomed: false`, canevas reste sur le monde.

Ces tests ne demandent pas QGIS : ils verifient la semantique Python en cause
et la presence des correctifs dans le source.
"""
from pathlib import Path

_RACINE = Path(__file__).resolve().parents[1]
_PONT = (_RACINE / "src" / "qgis_bridge.py").read_text(encoding="utf-8")
_AIDES = (_RACINE / "src" / "qgis_helpers.py").read_text(encoding="utf-8")
_MCP = (_RACINE / "main_mcp.py").read_text(encoding="utf-8")


# ── 1. Le retour du script ───────────────────────────────────────────────

def _executer(code):
    """Reproduit le contrat du pont : un `result` fourni, relu APRES exec."""
    result = {}
    espace = {"result": result}
    exec(code, espace)
    produit = espace.get("result", result)
    if not isinstance(produit, dict):
        produit = {"valeur": produit}
    return produit


def test_la_mutation_de_result_est_rendue():
    assert _executer("result['a'] = 1") == {"a": 1}


def test_l_affectation_de_result_est_rendue():
    """C'est le cas qui renvoyait {} en silence."""
    assert _executer("result = {'a': 1}") == {"a": 1}


def test_un_result_non_dict_n_est_plus_perdu():
    assert _executer("result = [1, 2]") == {"valeur": [1, 2]}


def test_l_ancienne_lecture_perdait_bien_l_affectation():
    """Verrou : la lecture fautive doit rester demontrable."""
    result = {}
    espace = {"result": result}
    exec("result = {'a': 1}", espace)
    assert result == {}          # l'objet d'origine reste vide
    assert espace["result"] == {"a": 1}


def test_le_pont_relit_l_espace_du_script():
    assert 'produit = exec_globals.get("result", result)' in _PONT
    assert "for k, v in produit.items():" in _PONT


# ── 2. L'identite du projet survit a l'auto-save ─────────────────────────

def test_l_auto_save_restaure_le_nom_du_projet():
    bloc = _PONT.split("def _auto_save")[1].split("def ")[0]
    assert "nom_avant = project.fileName()" in bloc
    assert "project.setFileName(nom_avant)" in bloc


# ── 3. Le catalogue ne compte que des sources chargeables ────────────────

def test_le_catalogue_ecarte_ses_pseudo_entrees():
    bloc = _PONT.split("def _action_list_datasources")[1].split("def ")[0]
    assert 'if s.get("id")' in bloc


def test_le_plafond_d_entites_ne_se_presente_plus_comme_un_defaut():
    """Le schema annoncait 10000 alors que le pont ne plafonne pas."""
    bloc = _MCP.split('"name": "smart_load"')[1].split('"name":')[0]
    assert '"default": 10000' not in bloc
    assert "AUCUN plafond" in bloc


# ── 4. La carte suit les donnees ─────────────────────────────────────────

def test_le_cadrage_ne_depend_plus_de_la_seule_premiere_couche():
    bloc = _AIDES.split("def _finalize_layer")[1].split("\ndef ")[0]
    assert "hors_champ" in bloc and "noyee" in bloc
    # On ne bouge pas une vue deja correcte.
    assert "if hors_champ or noyee:" in bloc


# ── 5. La zone d'etude s'explique ────────────────────────────────────────

def test_la_zone_dit_ce_qu_elle_a_fait():
    bloc = _AIDES.split("def set_study_zone")[1].split("\ndef ")[0]
    assert '"resume"' in bloc
    assert '"methode"' in bloc
    assert "largeur_km" in bloc
    # Les quatre facons d'obtenir une zone sont tracees.
    for attendu in ("emprise fournie directement",
                    "emprise administrative de la commune",
                    "centre de la commune",
                    "adresse geocodee"):
        assert attendu in bloc, attendu


def test_un_chargement_hors_zone_est_signale():
    bloc = _PONT.split("def _action_smart_load")[1].split("\n    def ")[0]
    assert "avertissement" in bloc
    assert "HORS de la zone" in bloc


# ── 6. Le suivi d'un traitement long ne tombe pas ────────────────────────

_API = (_RACINE / "src" / "api_server.py").read_text(encoding="utf-8")


def test_la_sonde_ne_bloque_plus_la_boucle_d_evenements():
    """Elle faisait de la socket bloquante dans un endpoint async : le serveur
    ne repondait plus quand Qt gelait, et poll_job expirait."""
    bloc = _API.split("async def get_job")[1].split("\ndef ")[0]
    assert "asyncio.to_thread(_sonder_le_pont" in bloc
    assert "asyncio.wait_for" in bloc
    # La socket bloquante a quitte l'endpoint.
    assert "probe_sock.connect" not in bloc


def test_la_sonde_est_bornee_dans_le_temps():
    bloc = _API.split("async def get_job")[1].split("\ndef ")[0]
    assert "timeout=4" in bloc


# ── 7. Une couche dense reste lisible ────────────────────────────────────

def test_le_contour_est_retire_sur_les_polygones_denses():
    bloc = _AIDES.split("def _alleger_le_contour_si_couche_dense")[1].split("\ndef ")[0]
    assert "Qt.NoPen" in bloc
    # Un style pose par quelqu'un n'est jamais ecrase.
    assert "QgsSingleSymbolRenderer" in bloc
    assert "PolygonGeometry" in bloc


def test_l_allegement_est_appele_au_chargement():
    bloc = _AIDES.split("def _finalize_layer")[1].split("\ndef ")[0]
    assert "_alleger_le_contour_si_couche_dense(layer)" in bloc


# ── 8. Les bases de donnees declarees dans QGIS sont visibles ────────────

def test_le_pont_sait_lister_les_connexions_enregistrees():
    """Le fournisseur PostGIS etait present et le serveur joignable, mais rien
    ne reliait les deux : l'agent ne pouvait pas savoir qu'une base existe."""
    bloc = _PONT.split("def _action_list_database_connections")[1].split("\n    def ")[0]
    assert "providerMetadata" in bloc
    assert "meta.connections(False)" in bloc
    # Aucun mot de passe ne doit sortir d'ici.
    assert "password" not in bloc


def test_charger_une_table_ne_demande_aucun_identifiant():
    bloc = _PONT.split("def _action_add_database_layer")[1].split("\n    def ")[0]
    assert "conn.tableUri(schema, table)" in bloc
    assert "createConnection" in bloc
    # En cas d'echec, l'URI renvoyee est tronquee avant tout secret.
    assert 'uri.split("password=")[0]' in bloc


def test_les_outils_bd_sont_exposes():
    for outil in ("list_database_connections", "add_database_layer"):
        assert f'"name": "{outil}"' in _MCP, outil
        assert f'"{outil}": _tool_' in _MCP, outil
