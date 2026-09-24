# -*- coding: utf-8 -*-
"""Une source WFS du catalogue est toujours bornee a la zone.

Mesure sur l'instance de production le 2026-09-24, scenario « charge les
batis sur Aix-en-Provence » : l'agent appelle
`add_from_catalog(bdtopo_batiments, bbox=<Aix>)`. La couche creee porte
l'URI

    url='https://data.geopf.fr/wfs/ows' typename='BDTOPO_V3:batiment'
    srsname='EPSG:4326' bbox='43.446031,5.269537,43.62598,5.506288'
    pagingEnabled='true'

et annonce 51 450 444 entites, emprise [-63.28, -21.77, 55.9, 51.97] : la
France et l'outre-mer. Le provider WFS de QGIS ignore la cle `bbox` sans
erreur. La garde « couche WFS ingerable » ne jouait pas non plus : elle vit
dans qgis_helpers._finalize_layer, que ce chemin n'empruntait pas.

Ces tests ne demandent pas QGIS.
"""
from pathlib import Path

_RACINE = Path(__file__).resolve().parents[1]
_PONT = (_RACINE / "src" / "qgis_bridge.py").read_text(encoding="utf-8")
_MCP = (_RACINE / "main_mcp.py").read_text(encoding="utf-8")

_AJOUT = _PONT.split("def _action_add_from_catalog")[1].split("\n    def ")[0]
_CHARGEMENT = _PONT.split("def _action_smart_load")[1].split("\n    def ")[0]

_AIX = [5.269537, 43.446031, 5.506288, 43.62598]
_FRANCE_ET_OUTRE_MER = [-63.28, -21.77, 55.9, 51.97]


# ── 1. Plus de couche WFS servie en direct ───────────────────────────────

def test_le_wfs_du_catalogue_passe_par_le_telechargement():
    assert "self._action_smart_load(" in _AJOUT


def test_plus_aucune_couche_wfs_en_direct():
    assert '"WFS")' not in _AJOUT
    assert "bbox='" not in _AJOUT, "la cle bbox de l'URI WFS est ignoree par QGIS"


def test_sans_emprise_la_zone_d_etude_prime_sur_le_canevas():
    zone = _AJOUT.index("self._zone_etude_bbox()")
    canevas = _AJOUT.index("self._bbox_du_canevas()")
    assert zone < canevas


def test_le_telechargement_ne_renvoie_pas_vers_l_ajout():
    """smart_load delegue les rasters a add_from_catalog : pas l'inverse
    pour le WFS, sinon les deux s'appelleraient sans fin."""
    wfs = _CHARGEMENT.split('if src_type == "wfs":')[1].split("return result")[0]
    assert "_action_add_from_catalog" not in wfs


# ── 2. Le delai suit le telechargement ───────────────────────────────────

def test_l_ajout_a_le_delai_d_un_telechargement():
    bloc = _MCP.split("def _tool_add_from_catalog")[1].split("\ndef ")[0]
    assert "timeout=SOCKET_TIMEOUT_LONG" in bloc


def test_l_ajout_est_une_action_longue_pour_les_recettes():
    bloc = _MCP.split("_LONG_TIMEOUT_ACTIONS = frozenset({")[1].split("})")[0]
    assert '"add_from_catalog"' in bloc


def test_la_description_ne_reclame_plus_de_bbox():
    bloc = _MCP.split('"name": "add_from_catalog"')[1].split('"name":')[0]
    assert "required for WFS" not in bloc
    assert "smart_load" in bloc


# ── 3. Le chargement dit ce qu'il a charge ───────────────────────────────

def test_le_chargement_porte_une_verification():
    assert 'result["verification"] = self._verification_chargement(' in _CHARGEMENT


def test_la_verification_rappelle_bbox_et_contour():
    bloc = _PONT.split("def _verification_chargement")[1].split("\n    def ")[0]
    assert "pas le contour administratif" in bloc
    assert "native:clip" in bloc


def _rapport(zone, couche):
    """Reproduit le calcul de _verification_chargement."""
    aire_zone = (zone[2] - zone[0]) * (zone[3] - zone[1])
    aire_couche = (couche[2] - couche[0]) * (couche[3] - couche[1])
    return aire_couche / aire_zone


def _seuil():
    ligne = next(l for l in _PONT.splitlines()
                 if l.startswith("_RATIO_EMPRISE_ABERRANTE = "))
    return float(ligne.split("=")[1])


def test_l_incident_d_aix_aurait_ete_signale():
    assert _rapport(_AIX, _FRANCE_ET_OUTRE_MER) > _seuil()


def test_un_chargement_borne_n_est_pas_signale():
    """Les batiments qui touchent le rectangle le debordent un peu."""
    deborde = [_AIX[0] - 0.01, _AIX[1] - 0.01, _AIX[2] + 0.01, _AIX[3] + 0.01]
    assert _rapport(_AIX, deborde) < _seuil()
