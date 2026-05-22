# Cadastre Solaire — Pipeline d'analyse d'ensoleillement

## Vue d'ensemble

Le pipeline solaire calcule l'irradiance solaire annuelle sur un territoire donné, au niveau du terrain (MNT), des toitures et des façades de bâtiments. Il produit :
- Un **raster d'irradiance** (kWh/m²/an) drapé sur le MNT dans QGIS
- Un **raster d'heures d'insolation** (h/an) mensuel
- Un **GPKG de façades enrichies** (irradiance par étage et orientation, z-score local)
- Un **observatoire HTML** interactif (3D temps réel + tableaux analytiques)

## Sources de données

| Source | Usage | Téléchargement |
|--------|-------|----------------|
| **RGE ALTI 1m** (IGN) | Modèle Numérique de Terrain haute résolution | `helpers.download_rge_alti(bbox, resolution=1)` |
| **BD TOPO bâtiments** (IGN) | Emprise au sol + hauteur + nb étages | `smart_load("bdtopo_batiments")` |
| **BD TOPO voirie** (IGN) | Tronçons routiers pour analyse rues | `smart_load("bdtopo_routes")` |
| **PVGIS TMY** (JRC) | Données météo typiques (turbidité Linke) | API européenne, optionnel |

## Pipeline

### Étape 1 — Zone d'étude et données
```python
helpers.set_study_zone("Marseille 4ème", buffer_km=0.2)
helpers.download_rge_alti(bbox, resolution=1)  # MNT 1m
smart_load("bdtopo_batiments")
smart_load("bdtopo_routes")
```

### Étape 2 — Construction du DSM hybride
Fusion du MNT avec les hauteurs de bâtiments BD TOPO :
```
DSM(x,y) = max(MNT(x,y), MNT(centroïde_bati) + hauteur_bati)
```
Le buffer (200m par défaut) autour de la zone d'étude capture les ombres projetées par les bâtiments voisins.

### Étape 3 — Calcul r.sun (GRASS GIS)
Pour chaque mois (15 du mois comme jour représentatif) :
- `r.sun` calcule l'irradiance globale et les heures d'insolation
- Paramètres : DSM, pente, exposition, turbidité de Linke saisonnière, albédo 0.2
- Résultat : 12 rasters `glob_rad_MM.tif` + 12 rasters `insol_time_MM.tif`

Agrégation annuelle : somme pondérée des 12 mois → `irradiance_annuelle_kwh.tif`

### Étape 4 — Analyse des façades (ray-marching)
Pour chaque bâtiment BD TOPO :
- Segmenter le footprint en arêtes (façades > 3m)
- Pour chaque façade × chaque étage : positionner un point d'échantillonnage 1m en avant
- Pour chaque point × chaque demi-heure de l'année : lancer un rayon vers le soleil et marcher sur le DSM pour tester l'occlusion
- Résultat : bitset temporel (soleil/ombre) par façade × étage × 408 timesteps

### Étape 5 — Scores et classifications
- **Irradiance annuelle** : kWh/m²/an par façade (de `irradiance_annuelle_kwh.tif`)
- **z-score local** : (irradiance - moyenne_50m) / écart_type_50m → identifie les pépites contextuelles
- **Score soleil** (0-100) : percentile d'irradiance dans la zone
- **Score canicule** (0-100) : inversé — les zones les plus ombragées en été-midi sont les mieux notées
- **Score confort** (0-100) : combine soleil hivernal élevé + ombre estivale après-midi

### Étape 6 — Application raster sur MNT dans QGIS
Le raster d'irradiance est appliqué comme symbologie sur le MNT :
```python
# Charger le MNT comme couche raster
# Appliquer une rampe de couleur (bleu→jaune→rouge) sur l'irradiance
# Le relief 3D est visible via le MNT, les couleurs indiquent l'irradiance
processing.run("native:rasterize", {
    "INPUT": irradiance_raster,
    "BURN_IN": mnt_layer,
    ...
})
```

## Profils de calcul

### `rapide` — exploration (2-5 min)
```python
config = {
    "resolution": 10,       # maille 10m
    "buffer_m": 100,         # buffer 100m
    "months": [1, 4, 7, 10], # 4 mois représentatifs
    "time_step": 1.0,        # pas horaire
    "ray_step": 3.0,         # marche 3m
    "max_shadow_dist": 200,  # 200m de portée
    "facade_min_length": 5,  # façades > 5m
}
```

### `standard` — étude courante (10-30 min)
```python
config = {
    "resolution": 5,
    "buffer_m": 200,
    "months": list(range(1, 13)),  # 12 mois
    "time_step": 0.5,
    "ray_step": 1.5,
    "max_shadow_dist": 300,
    "facade_min_length": 3,
}
```

### `precision` — cadastre officiel (30 min - 4h)
```python
config = {
    "resolution": 1,
    "buffer_m": 300,
    "months": list(range(1, 13)),
    "time_step": 0.5,
    "ray_step": 1.0,
    "max_shadow_dist": 500,
    "facade_min_length": 2,
}
```

## Turbidité de Linke saisonnière (Méditerranée)

| Mois | Jan | Fév | Mar | Avr | Mai | Jun | Jul | Aoû | Sep | Oct | Nov | Déc |
|------|-----|-----|-----|-----|-----|-----|-----|-----|-----|-----|-----|-----|
| TL   | 2.5 | 2.5 | 3.0 | 3.5 | 3.5 | 3.5 | 3.5 | 3.5 | 3.0 | 3.0 | 2.5 | 2.5 |

Ces valeurs sont typiques du climat méditerranéen. Pour d'autres régions, adapter selon les données PVGIS ou Soda-IS.

## Interprétation des résultats

| Irradiance kWh/m²/an | Interprétation |
|---|---|
| < 400 | Zone très ombragée (cœur d'îlot dense, versant nord) |
| 400 - 800 | Zone partiellement ombragée |
| 800 - 1000 | Bon ensoleillement |
| 1000 - 1200 | Excellent ensoleillement (toiture plate, façade sud haute) |
| > 1200 | Exceptionnel (rare en zone urbaine dense) |

| z-score | Signification |
|---|---|
| < 0 | En dessous de la moyenne locale |
| 0 - 1 | Normal pour le voisinage |
| 1 - 1.5 | Au-dessus de la moyenne locale |
| 1.5 - 2 | Remarquable — pépite locale |
| > 2 | Exceptionnel — bien au-dessus de son contexte |

## Livrables

| Livrable | Outil | Format |
|---|---|---|
| Raster irradiance sur MNT | `get_screenshot` | Vue QGIS (PNG) |
| Carte PDF | `export_pdf` | PDF A3/A4 |
| Carte web interactive | `export_web_map` | HTML Leaflet |
| Observatoire 3D temps réel | Script dédié | HTML Three.js + DSFR |
| Export couches | `export_layer` | GPKG / GeoJSON |
| Document Grist | `export_grist` | .grist SQLite |
