"""Le .grist produit par `export_grist` est-il lisible par Grist et par Atlas ?

Contexte (2026-09-26). La table SceneManifest etait declaree a Grist par des
lignes de 8 et 12 valeurs, dans des tables qui en ont 7 et 17 : SQLite refusait,
l'exception etait avalee, `scene_manifest_embedded` valait False et la table
restait en SQL sans exister pour Grist. Le manifest fourni par qgis-sspcloud
designait en outre ses couches par des fichiers du volume (`geojson_path`),
qu'Atlas ne peut pas lire depuis un document.

Ces tests EXERCENT les helpers sur une vraie base SQLite, sans QGIS : les
helpers sont extraits du source et executes.
"""

from __future__ import annotations

import ast
import json
import sqlite3
import textwrap
from pathlib import Path

import pytest

_RACINE = Path(__file__).resolve().parents[1]
_SRC = (_RACINE / "src" / "qgis_bridge.py").read_text(encoding="utf-8")
_MCP = (_RACINE / "main_mcp.py").read_text(encoding="utf-8")

_HELPERS = (
    "_grist_create_meta_tables",
    "_grist_horodatage",
    "_grist_style_declaratif",
    "_grist_colonnes_geometrie",
    "_grist_manifest_minimal",
    "_grist_lier_manifest_aux_tables",
    "_grist_embarquer_scene_manifest",
)


def _classe():
    """Une classe qui porte les helpers du pont, tels qu'ecrits dans le source."""
    arbre = ast.parse(_SRC)
    bouts = {}
    for n in ast.walk(arbre):
        if isinstance(n, ast.FunctionDef) and n.name in _HELPERS:
            bouts.setdefault(n.name, n)
    assert set(bouts) == set(_HELPERS), set(_HELPERS) - set(bouts)
    corps = []
    for nom in _HELPERS:
        noeud = bouts[nom]
        segment = ast.get_source_segment(_SRC, noeud)
        decorateurs = "".join(
            f"@{ast.get_source_segment(_SRC, d)}\n" for d in noeud.decorator_list)
        corps.append(textwrap.indent(decorateurs + textwrap.dedent(segment), "    "))
    code = "class Pont:\n" + "\n".join(corps)
    espace: dict = {}
    exec(code, espace)
    return espace["Pont"]


Pont = _classe()


def _document(manifest):
    conn = sqlite3.connect(":memory:")
    cur = conn.cursor()
    Pont._grist_create_meta_tables(cur)
    meta = Pont._grist_embarquer_scene_manifest(cur, manifest, "etude_aix")
    conn.commit()
    return conn, meta


MANIFEST_ETUDE = {
    "version": "0.2.2",
    "title": "Aix",
    "layers": [
        {"id": "batiments", "name": "Bâtiments", "geometry_type": "polygon",
         "geojson_path": "/data/studies/s1/projects/p1/layers/batiments.geojson",
         "style": {"declarative": {"kind": "single", "color": "#ff0000"}}},
        {"id": "ortho", "name": "Ortho IGN", "geometry_type": "raster",
         "source": {"type": "wmts", "url": "https://data.geopf.fr/wmts"}},
    ],
}

COUCHES = [
    {"table": "Batiments", "nom": "Bâtiments", "geometrie": "polygon",
     "visible": True, "n": 12, "champs": [], "style": None},
    {"table": "Arrets_bus", "nom": "Arrêts bus", "geometrie": "point",
     "visible": False, "n": 3,
     "champs": [{"name": "nom", "label": "nom", "gType": "Text"}],
     "style": {"kind": "single", "color": "#00ff00", "opacity": 1.0}},
]


# ── La table SceneManifest existe pour Grist ─────────────────────────────


def test_l_embarquement_ne_leve_plus():
    """Avant : « table _grist_Tables has 7 columns but 8 values »."""
    _, meta = _document(MANIFEST_ETUDE)
    assert meta["n_layers"] == 2
    assert meta["version"] == "0.2.2"


def test_la_table_est_declaree_avec_sa_section_brute():
    conn, _ = _document(MANIFEST_ETUDE)
    cur = conn.cursor()
    tid, vue, section = cur.execute(
        "SELECT id, primaryViewId, rawViewSectionRef FROM _grist_Tables "
        "WHERE tableId='SceneManifest'").fetchone()
    assert section, "sans rawViewSectionRef, Grist n'affiche pas la table"
    assert cur.execute(
        "SELECT tableRef FROM _grist_Views_section WHERE id=?",
        (section,)).fetchone() == (tid,)


def test_chaque_colonne_declaree_existe_en_sql_et_inversement():
    conn, _ = _document(MANIFEST_ETUDE)
    cur = conn.cursor()
    declarees = {r[0] for r in cur.execute(
        "SELECT colId FROM _grist_Tables_column c JOIN _grist_Tables t "
        "ON c.parentId=t.id WHERE t.tableId='SceneManifest'")}
    en_sql = {r[1] for r in cur.execute("PRAGMA table_info(SceneManifest)")} - {"id"}
    assert declarees == en_sql
    assert "manualSort" in declarees


def test_la_ligne_porte_le_manifest_et_un_horodatage():
    conn, meta = _document(MANIFEST_ETUDE)
    texte, empreinte, cree = conn.execute(
        "SELECT manifest_json, scene_hash, created_at FROM SceneManifest").fetchone()
    assert json.loads(texte)["title"] == "Aix"
    assert empreinte == meta["scene_hash"]
    assert isinstance(cree, (int, float)) and cree > 1_700_000_000


# ── Les couches du manifest pointent vers les tables ─────────────────────


def test_une_couche_d_etude_est_rattachee_a_sa_table():
    lie = Pont._grist_lier_manifest_aux_tables(MANIFEST_ETUDE, COUCHES)
    bati = lie["layers"][0]
    assert "geojson_path" not in bati, "Atlas classerait la couche « d'atelier »"
    assert bati["source"] == {
        "type": "grist", "table": "Batiments",
        "geometry_fields": {"geojson": "_geojson", "lat": "centroid_lat",
                            "lon": "centroid_lon"}}
    assert bati["style"]["declarative"]["color"] == "#ff0000"


def test_une_couche_sans_table_reste_intacte():
    lie = Pont._grist_lier_manifest_aux_tables(MANIFEST_ETUDE, COUCHES)
    assert lie["layers"][1] == MANIFEST_ETUDE["layers"][1]


def test_le_manifest_fourni_n_est_pas_modifie_en_place():
    avant = json.dumps(MANIFEST_ETUDE, sort_keys=True)
    Pont._grist_lier_manifest_aux_tables(MANIFEST_ETUDE, COUCHES)
    assert json.dumps(MANIFEST_ETUDE, sort_keys=True) == avant


def test_le_manifest_genere_couvre_chaque_table():
    m = Pont._grist_lier_manifest_aux_tables(
        Pont._grist_manifest_minimal("Aix", COUCHES), COUCHES)
    assert m["version"] == "0.2.2"
    assert [c["source"]["table"] for c in m["layers"]] == ["Batiments", "Arrets_bus"]
    arrets = m["layers"][1]
    assert arrets["source"]["geometry_fields"] == {"lat": "latitude", "lon": "longitude"}
    assert arrets["visibility"] == {"defaultVisible": False}
    assert arrets["style"]["declarative"]["color"] == "#00ff00"
    assert arrets["fields"][0]["name"] == "nom"


# ── Le style QGIS arrive sous le nom de colonne ──────────────────────────


def test_un_rendu_categorise_lit_la_colonne_grist():
    rendu = {"type": "categorized", "field": "Nature du bâti",
             "categories": [{"value": "Eglise", "color": "#aa0000", "label": "Église"}]}
    style = Pont._grist_style_declaratif(rendu, {"Nature du bâti": "Nature_du_bati"})
    assert style == {"kind": "categorized", "field": "Nature_du_bati",
                     "stops": [{"value": "Eglise", "color": "#aa0000", "label": "Église"}]}


def test_un_rendu_gradue_porte_ses_bornes():
    rendu = {"type": "graduated", "field": "hauteur",
             "classes": [{"min": 0, "max": 10, "color": "#111111", "label": "bas"}]}
    style = Pont._grist_style_declaratif(rendu, {"hauteur": "hauteur"})
    assert style["stops"][0]["lower"] == 0 and style["stops"][0]["upper"] == 10


def test_un_rendu_sur_expression_retombe_sur_une_couleur():
    rendu = {"type": "categorized", "field": "upper(nature)",
             "categories": [{"value": "A", "color": "#123456", "label": "A"}]}
    assert Pont._grist_style_declaratif(rendu, {}) == {
        "kind": "single", "color": "#123456", "opacity": 1.0}


# ── Dates : des secondes epoch, pas du texte ─────────────────────────────


@pytest.mark.parametrize("valeur, gtype, attendu", [
    ("2024-03-05", "Date", 1709596800),
    ("2024-03-05T10:00:00", "DateTime:Europe/Paris", 1709629200),
    ("2024-03-05 10:00:00+00:00", "DateTime:Europe/Paris", 1709632800),
    (None, "Date", None),
    ("", "Date", None),
    ("pas une date", "Date", "pas une date"),
])
def test_les_dates_sont_des_horodatages(valeur, gtype, attendu):
    assert Pont._grist_horodatage(valeur, gtype) == attendu


# ── Ce que Grist lit ─────────────────────────────────────────────────────


def test_le_widget_de_saisie_est_configure_sous_customview():
    """Sous `customDef`, Grist ignorait la configuration."""
    assert '"customDef"' not in _SRC


def test_le_lien_est_construit_sur_le_chemin_complet():
    debut = _SRC.find("def _action_export_grist(")
    fin = _SRC.find("def _grist_parse_html_geojson")
    assert "self._lien_hub(fname)" not in _SRC[debut:fin]


def test_la_description_reste_courte_et_dit_comment_ouvrir():
    debut = _MCP.find('"name": "export_grist"')
    bloc = _MCP[debut:_MCP.find("# ── Recipes", debut)]
    assert len(bloc) < 2600, len(bloc)
    assert "ouvrir_dans_grist" in bloc
    assert "detect_relationships" not in bloc, "parametre sans effet"


def test_la_table_de_statistiques_porte_les_cles_que_la_carte_lit():
    """La page Carte lit `geom_type` et `layer_obj` sur toutes les tables."""
    debut = _SRC.find("'source_layer': '(computed)'")
    bloc = _SRC[debut:debut + 400]
    assert "'geom_type'" in bloc and "'layer_obj'" in bloc
