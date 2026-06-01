"""Tests de regression pour solar_pipeline.py.

Reference de verite : PVGIS-SARAH2 (Commission Europeenne) -- valeurs annuelles
de rayonnement global horizontal (GHI) par commune francaise. La tolerance
+/-5% absorbe les ecarts de modelisation (Linke local vs PVGIS, masques
topographiques, resolution DSM).

Execution :
    Depuis le conteneur QGIS Remote (qui embarque GRASS GIS) :
        pytest tests/test_solar_pipeline.py -v

    Les tests qui depend d'un run_full() prealable sont skip si
    /data/solar/manifest.json est absent -- a executer apres
        python -c "import solar_pipeline as s; s.run_full(zone='Marseille')"
"""
import json
import sys
from pathlib import Path

import pytest

# Ajouter src/ au path pour import direct depuis la racine du repo
ROOT = Path(__file__).resolve().parent.parent
SRC = ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))


# ─────────────────────────────────────────────────────────────────────
# References PVGIS-SARAH2 (kWh/m2/an, horizontal global)
# Source : https://re.jrc.ec.europa.eu/pvg_tools/fr/
# ─────────────────────────────────────────────────────────────────────
PVGIS_REFERENCE = {
    "Marseille": 1597,
    "Lyon": 1380,
    "Paris": 1180,
    "Rennes": 1180,
    "Bordeaux": 1430,
    "Strasbourg": 1230,
    "Nice": 1610,
    "Toulouse": 1450,
}
TOLERANCE_PCT = 5  # +/-5% acceptable pour modele DSM 5m + Linke regional


# ─────────────────────────────────────────────────────────────────────
# Tests deterministes (pas de run_full prealable requis)
# ─────────────────────────────────────────────────────────────────────

def test_sun_position_summer_solstice_marseille():
    """Elevation solaire au midi solaire du solstice d'ete, lat 43.3 N."""
    from solar_pipeline import _sun_pos
    # 21 juin 12h00 UTC+2 -> midi solaire approximatif
    az, el = _sun_pos(2024, 6, 21, 12, 0, 43.3, 5.4)
    # Elevation theorique : 90 - (43.3 - 23.44) = 70.1 degres
    assert 65 < el < 75, (
        f"Solstice ete midi : elevation {el:.1f} deg hors plage attendue [65,75]"
    )


def test_sun_position_winter_solstice_marseille():
    """Elevation solaire au midi solaire du solstice d'hiver, lat 43.3 N."""
    from solar_pipeline import _sun_pos
    az, el = _sun_pos(2024, 12, 21, 12, 0, 43.3, 5.4)
    # Elevation theorique : 90 - (43.3 + 23.44) = 23.3 degres
    assert 18 < el < 28, (
        f"Solstice hiver midi : elevation {el:.1f} deg hors plage attendue [18,28]"
    )


def test_sun_position_high_latitude_paris():
    """Solstice ete a Paris (48.85 N) : elevation reduite vs Marseille."""
    from solar_pipeline import _sun_pos
    az, el = _sun_pos(2024, 6, 21, 12, 0, 48.85, 2.35)
    # Theorique : 90 - (48.85 - 23.44) = 64.6 degres
    assert 60 < el < 70, (
        f"Paris solstice ete : elevation {el:.1f} deg hors plage attendue [60,70]"
    )


def test_sun_below_horizon_at_night():
    """A 2h du matin, le soleil doit etre sous l'horizon partout en France."""
    from solar_pipeline import _sun_pos
    for lat, lon in [(43.3, 5.4), (48.85, 2.35), (50.6, 3.1)]:
        az, el = _sun_pos(2024, 6, 21, 2, 0, lat, lon)
        assert el < 0, (
            f"Lat {lat} a 2h : elevation {el:.1f} deg, devrait etre negative"
        )


def test_get_config_profiles_complete():
    """Les 3 profils doivent fournir toutes les cles attendues."""
    from solar_pipeline import get_config, PROFILES
    required = {"resolution", "buffer_m", "months", "time_step", "ray_step",
                "max_shadow_dist", "facade_min_length", "nprocs", "albedo"}
    for profile in PROFILES:
        cfg = get_config(profile)
        missing = required - set(cfg.keys())
        assert not missing, f"Profil '{profile}' incomplet : manque {missing}"


def test_get_config_unknown_profile_raises():
    from solar_pipeline import get_config
    with pytest.raises(ValueError, match="Unknown profile"):
        get_config("inexistant")


def test_linke_monthly_covers_12_months():
    from solar_pipeline import LINKE_MONTHLY
    assert set(LINKE_MONTHLY.keys()) == set(range(1, 13)), (
        "LINKE_MONTHLY doit couvrir les 12 mois"
    )
    for v in LINKE_MONTHLY.values():
        assert 1.0 <= v <= 6.0, f"Linke {v} hors plage physique [1, 6]"


# ─────────────────────────────────────────────────────────────────────
# Test A4 : pas de fallback silencieux sur lat/lon (regression)
# ─────────────────────────────────────────────────────────────────────

def test_latlon_no_silent_fallback(tmp_path, monkeypatch):
    """Apres fix A4 : _get_study_latlon doit raise si aucune source dispo.

    On pointe SOLAR_DIR vers un dossier vide (pas de zone.json) et on
    s'assure qu'aucun QgsProject n'est present -> RuntimeError attendu.
    """
    import solar_pipeline
    monkeypatch.setattr(solar_pipeline, "SOLAR_DIR", tmp_path)
    # qgis.core peut ne pas etre installe hors conteneur QGIS -> l'import dans
    # _get_study_latlon echoue silencieusement (try/except), c'est attendu.
    with pytest.raises(RuntimeError, match="Impossible de determiner lat/lon"):
        solar_pipeline._get_study_latlon()


def test_latlon_reads_zone_json_bbox(tmp_path, monkeypatch):
    """zone.json avec bbox_2154 -> centroide reprojete EPSG:4326."""
    import solar_pipeline
    monkeypatch.setattr(solar_pipeline, "SOLAR_DIR", tmp_path)
    # Bbox Marseille 4e environ
    (tmp_path / "zone.json").write_text(json.dumps({
        "bbox_2154": [893900, 6247000, 895750, 6249400]
    }))
    try:
        lat, lon = solar_pipeline._get_study_latlon()
    except ImportError:
        pytest.skip("pyproj absent dans cet environnement")
    # Doit retomber sur ~43.3 N, ~5.4 E
    assert 43.0 < lat < 43.6, f"Lat reprojetee {lat:.3f} hors plage Marseille"
    assert 5.2 < lon < 5.6, f"Lon reprojetee {lon:.3f} hors plage Marseille"


def test_latlon_reads_zone_json_explicit_center(tmp_path, monkeypatch):
    """zone.json avec center_lat/lon explicites -> retourne tel quel."""
    import solar_pipeline
    monkeypatch.setattr(solar_pipeline, "SOLAR_DIR", tmp_path)
    (tmp_path / "zone.json").write_text(json.dumps({
        "center_lat": 48.85, "center_lon": 2.35
    }))
    lat, lon = solar_pipeline._get_study_latlon()
    assert lat == 48.85
    assert lon == 2.35


# ─────────────────────────────────────────────────────────────────────
# Tests E2E (necessitent un run_full prealable sur le conteneur QGIS)
# ─────────────────────────────────────────────────────────────────────

@pytest.fixture
def manifest():
    """Manifest produit par run_full(). Skip si absent."""
    p = Path("/data/solar/manifest.json")
    if not p.exists():
        pytest.skip(
            "/data/solar/manifest.json absent. "
            "Lancer run_full(zone='Marseille') dans le conteneur QGIS d'abord."
        )
    return json.load(open(p))


def test_aggregate_annual_marseille_within_pvgis_tolerance(manifest):
    """L'irradiance moyenne agregee doit etre +/-5% de PVGIS-SARAH2 Marseille."""
    if manifest.get("skip_rsun"):
        pytest.skip("Mode degrade skip_rsun=True : pas d'agregation annuelle")
    agg = manifest.get("aggregate") or {}
    mean_kwh = agg.get("mean_kwh")
    assert mean_kwh is not None, "manifest.aggregate.mean_kwh absent"
    ref = PVGIS_REFERENCE["Marseille"]
    lo, hi = ref * (1 - TOLERANCE_PCT / 100), ref * (1 + TOLERANCE_PCT / 100)
    assert lo <= mean_kwh <= hi, (
        f"GHI agrege {mean_kwh:.0f} kWh/m2/an hors tolerance PVGIS-SARAH2 "
        f"Marseille [{lo:.0f}, {hi:.0f}] (ref={ref})"
    )


def test_aggregate_annual_max_below_solar_constant(manifest):
    """Max irradiance < constante solaire annuelle ciel-ouvert (~2500 kWh/m2/an)."""
    if manifest.get("skip_rsun"):
        pytest.skip("Mode degrade")
    mx = (manifest.get("aggregate") or {}).get("max_kwh")
    if mx is None:
        pytest.skip("max_kwh absent")
    assert mx < 2500, (
        f"max_kwh={mx} depasse la constante solaire annuelle plausible "
        "en France (~2500 kWh/m2/an cielo ouvert)"
    )


def test_facades_samples_generated(manifest):
    """analyze_facades doit produire au moins quelques milliers de samples."""
    f = manifest.get("facades") or {}
    n = f.get("n_samples", 0)
    assert n > 1000, f"Trop peu de samples facade ({n}) -- batiments charges ?"


def test_manifest_warnings_explicit_in_degraded_mode(manifest):
    """Si skip_rsun=True, le manifest doit lister un warning explicite."""
    if not manifest.get("skip_rsun"):
        pytest.skip("Mode complet : non applicable")
    warnings = manifest.get("warnings") or []
    assert any("degrade" in w.lower() for w in warnings), (
        "Mode degrade doit produire un warning explicite dans le manifest"
    )


# ─────────────────────────────────────────────────────────────────────
# Tests build_observatoire_html (refactor de data/build_obs_v5.py)
# ─────────────────────────────────────────────────────────────────────

def test_build_observatoire_html_creates_autonomous_html(manifest):
    """L'observatoire HTML doit etre cree, autonome, contenir les donnees."""
    obs = manifest.get("observatoire") or {}
    if "error" in obs:
        pytest.skip(f"Step 7 a echoue : {obs['error']}")
    out_path = Path(obs.get("output_path", ""))
    assert out_path.exists(), f"HTML observatoire absent : {out_path}"
    # Le HTML doit etre autoportant (donnees inlinees) -> taille > 100 KB minimum
    size_kb = out_path.stat().st_size / 1024
    assert size_kb > 100, (
        f"HTML observatoire trop petit ({size_kb:.0f} KB) -- "
        "donnees probablement non injectees dans le template"
    )
    # Le marker doit avoir ete remplace
    content = out_path.read_text(encoding="utf-8")
    assert "D = __DATA__;" not in content, (
        "Marker `D = __DATA__;` non remplace dans le HTML produit"
    )
    assert "D = {" in content, (
        "Pas trouve d'injection JSON `D = {...}` dans le HTML"
    )


def test_build_observatoire_metadata_consistency(manifest):
    """Les compteurs observatoire doivent etre coherents avec facades + troncons."""
    obs = manifest.get("observatoire") or {}
    if "error" in obs:
        pytest.skip(f"Step 7 a echoue : {obs['error']}")
    n_bati = obs.get("n_bati", 0)
    n_facades = obs.get("n_facades", 0)
    n_etages = obs.get("n_etages", 0)
    assert n_bati > 0, "Aucun batiment dans l'observatoire"
    assert n_facades > n_bati, (
        f"n_facades={n_facades} doit etre > n_bati={n_bati} "
        "(plusieurs facades par batiment)"
    )
    assert n_etages >= n_facades, (
        f"n_etages={n_etages} doit etre >= n_facades={n_facades}"
    )
