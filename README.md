# 🖱️ QgisRemoteMCP

**L'expertise géomatique du CEREMA, accessible depuis n'importe quel assistant IA.**

QgisRemoteMCP fait tourner un QGIS complet dans un conteneur Docker et l'expose comme un serveur MCP (Model Context Protocol). L'assistant IA dispose en permanence d'un canevas géographique — il charge les données, analyse, cartographie et exporte, sans que l'utilisateur ait besoin d'installer quoi que ce soit.

L'administrateur déploie le conteneur une fois (sur un serveur, un NAS ou en local). Les utilisateurs connectent leur client IA et accèdent immédiatement à QGIS dans leurs conversations.

![Architecture](docs/architecture.png)

---

## Pourquoi ce projet ?

**Les collectivités ont des questions géographiques concrètes** — exposition au risque inondation, densité bâtie, pression foncière — mais rarement un géomaticien ou un outil SIG pour y répondre.

**Les assistants IA se généralisent**, y compris dans les services publics. Le protocole MCP leur permet de se connecter à des services externes, exactement comme un navigateur se connecte à un site web.

**QgisRemoteMCP fait le pont** : il transforme l'expertise géomatique en un service que n'importe quel assistant IA peut appeler. Les méthodes d'analyse sont structurées en *recettes* rédigées par les experts métier, mobilisables par un utilisateur sans compétence SIG.

### Trois principes

**Aucune installation côté utilisateur.** QGIS tourne dans Docker. L'utilisateur n'a besoin que de son client IA habituel (Claude Desktop, Cursor, Continue…). Pas de licence, pas de plugin, pas de configuration.

**Les savoir-faire métier sont structurés en recettes.** Chaque recette encapsule une chaîne complète : chargement des données, traitements, symbologie, export. Un expert rédige la recette une fois — tous les utilisateurs connectés en bénéficient immédiatement.

**L'IA dispose d'un canevas géographique permanent.** L'assistant ne se contente pas de générer du texte : il pilote un vrai QGIS, manipule de vraies couches, produit de vraies cartes. Le canevas reste actif entre les requêtes — on peut enchaîner analyse, ajustement de style et export dans la même conversation.

---

## Démarrage rapide

```bash
git clone https://gitlab.cerema.fr/nicolas.laval/QgisRemoteMCP.git
cd QgisRemoteMCP
docker compose up -d --build
```

### Points d'accès

| Port | Service | URL |
|------|---------|-----|
| 6080 | noVNC (QGIS dans le navigateur) | http://localhost:6080 |
| 8100 | Serveur MCP (Streamable HTTP) | http://localhost:8100/mcp |
| 8080 | API REST | http://localhost:8080/docs |
| 8081 | Flux MJPEG | http://localhost:8081/stream |

### Connexion à Claude Desktop

Ajouter dans `claude_desktop_config.json` :

```json
{
  "mcpServers": {
    "qgis": {
      "url": "http://localhost:8100/mcp"
    }
  }
}
```

---

## Chargement intelligent des données

L'innovation centrale : un pipeline structuré qui remplace les connexions WFS en direct (lentes, tronquées, sans index) par des fichiers GeoPackage locaux performants.

### Le problème des flux WFS en direct

| Problème | WFS en direct | Chargement intelligent |
|----------|---------------|------------------------|
| Pagination | L'IGN tronque silencieusement à 5 000 entités | ogr2ogr gère automatiquement toutes les pages |
| Index spatial | Aucun (en mémoire) | R-tree dans le GeoPackage |
| Vitesse de traitement | 60 à 250× plus lent (réseau + absence d'index) | Fichier local rapide |
| Réseau pendant l'analyse | Requêtes HTTP par entité | Zéro réseau |
| Système de coordonnées | Mélange 4326/3857/2154 | Standardisé en EPSG:2154 |

### Pipeline type

```
1. set_study_zone(target="Montpellier")    # Géocodage, bbox, zoom
2. smart_load(id="osm_xyz")                # Fond de carte (flux)
3. smart_load(id="bdtopo_batiments")       # Bâtiments (téléchargement GPKG)
4. smart_load(id="bdtopo_routes")          # Routes (téléchargement GPKG)
5. get_screenshot                           # Vérification visuelle
6. run_processing / execute_python          # Analyse
7. export_pdf / export_web_map / ...        # Livraison (PDF, HTML, QField, Grist)
```

### Performances (Montpellier, bbox ~10 km)

| Opération | Entités | Temps |
|-----------|---------|-------|
| Téléchargement bâtiments | 10 000 | ~30 s |
| Téléchargement routes | 5 000 | 6,8 s |
| Rechargement depuis le cache | 10 000 | instantané |
| Buffer 50 m | 10 000 | 1,8 s |
| Dissolve par usage | 10 000 | 2,2 s |
| Grille de densité 500 m | 440 cellules | 0,3 s |

Les téléchargements sont mis en cache dans `/data/cache/` avec un hash de la bbox. Même zone = rechargement instantané pendant 24 h.

---

## Sources de données disponibles

Toutes les sources sont gratuites (données ouvertes IGN depuis juillet 2021). Aucune clé API nécessaire.

### Données vectorielles (WFS → GeoPackage)

| ID | Nom | Attributs clés |
|----|-----|----------------|
| `bdtopo_batiments` | Bâtiments | nature, usage, hauteur, étages, matériaux |
| `bdtopo_routes` | Routes | nature, importance, largeur, voies, vitesse |
| `bdtopo_hydrographie` | Cours d'eau | nom, classe, largeur |
| `bdtopo_vegetation` | Zones de végétation | nature |
| `bdtopo_voie_ferree` | Voies ferrées | nature, nb_voies |
| `bdtopo_hydro_surfaces` | Plans d'eau | nature, nom |
| `bdtopo_communes` | Communes (BD TOPO) | nom, code INSEE, population |
| `admin_express_communes` | Communes (Admin Express) | nom, code, population |
| `admin_express_departements` | Départements | nom, code |
| `admin_express_regions` | Régions | nom, code |
| `rpg` | Parcelles agricoles (RPG) | type de culture, surface |
| `bdtopo_poi` | Points d'intérêt | nature |
| `bdtopo_lieu_dit` | Lieux-dits | nom |
| `bdtopo_surface_activite` | Zones d'activité | nature |
| `bdtopo_equipement_transport` | Équipements de transport | nature |

### Fonds de carte (flux)

| ID | Nom |
|----|-----|
| `osm_xyz` | OpenStreetMap |
| `ign_planign` | Plan IGN v2 |
| `ign_scan25` | Cartes topographiques IGN 1:25 000 |
| `cartodb_positron` | Fond clair |
| `cartodb_dark` | Fond sombre |
| `esri_world_topo` | Esri World Topographic |
| `stamen_terrain` | Stamen Terrain (relief) |

### Imagerie (flux)

| ID | Nom |
|----|-----|
| `ign_ortho_wmts` | Orthophotos IGN (WMTS, rapide) |
| `ign_ortho_wms` | Orthophotos IGN (WMS) |
| `ign_ortho_irc` | Photos infrarouges IGN |
| `esri_world_imagery` | Satellite Esri |

### Autres (WMS / API)

| ID | Nom |
|----|-----|
| `ign_cadastre` | Parcelles cadastrales |
| `ign_dem` | MNT haute résolution |
| `corine_land_cover` | Occupation du sol 2018 |
| `ban_geocode` | API de géocodage (BAN) |
| `geo_api_communes` | API info communes |
| `dvf_api` | API transactions immobilières (DVF) |
| `panoramax` | Imagerie terrain Panoramax |
| `ign_altimetrie` | API altimétrie |

---

## Recettes

Les recettes sont des modèles de workflow qui automatisent une analyse complète — du chargement des données jusqu'à l'export cartographique stylé. Elles constituent le mécanisme central de mise à disposition des savoir-faire métier : un expert rédige une recette, et elle devient immédiatement disponible pour tous les utilisateurs connectés au service.

| ID | Nom | Description |
|----|-----|-------------|
| `densite_bati` | Densité bâtie | Grille hexagonale de densité avec symbologie graduée |
| `urbanisme_general` | Vue d'ensemble urbaine | Bâtiments, routes, végétation, hydrographie avec styles catégorisés |
| `risque_inondation` | Risque inondation | Zones inondables, exposition des bâtiments, analyse de buffer + carte interactive |
| `occupation_sol` | Occupation du sol | Corine Land Cover avec symbologie catégorisée |
| `pression_fonciere_cotiere` | Pression foncière littorale | Transactions DVF 2020-2024, bandes côtières + carte temporelle |

### Utilisation

```python
# Automatisé (toutes les étapes en une commande)
run_recipe(id="risque_inondation", zone="Nimes")

# Manuel (étape par étape)
get_recipe(id="densite_bati", zone="Montpellier")
# → Renvoie les instructions pas à pas à exécuter individuellement
```

> **Note :** Les recettes fournies sont des exemples à titre indicatif, destinés à tester et illustrer le fonctionnement du système. Elles n'ont pas été validées métier et ne constituent pas des analyses de référence.

### Créer une nouvelle recette

Chaque recette est un fichier JSON dans `recipes/`. Elle décrit la séquence d'étapes (zone d'étude, chargements, traitements, styles, exports) avec des paramètres substituables. Un expert métier peut créer une nouvelle recette sans modifier le code du serveur — elle est automatiquement détectée et proposée aux utilisateurs.

Il est aussi possible de demander directement à l'assistant IA de construire une recette en conversation : décrire l'analyse souhaitée, itérer sur les étapes, tester sur une zone, puis sauvegarder le résultat en JSON pour le rendre réutilisable. L'assistant dispose de tous les outils nécessaires pour assembler et valider une recette de bout en bout.

---

## Formats d'export

### PDF cartographique

PDF prêt à imprimer via les mises en page QGIS, avec des modèles pré-configurés (A3 paysage, A4 portrait). Inclut titre, légende, échelle, flèche nord et sources de données.

```python
apply_layout_template(template="a3_landscape", title="Risque inondation — Nîmes")
export_pdf(layout="a3_landscape")
```

### Carte interactive Leaflet

Fichiers HTML avec données GeoJSON embarquées. Trois modèles spécialisés :

| Modèle | Cas d'usage | Fonctionnalités |
|--------|-------------|-----------------|
| Standard | Carte générale | Sélecteur de couches, popup, légende, choix du fond de carte |
| Inondation | Analyse de risque | Curseur de hauteur d'eau, statistiques d'exposition des bâtiments, animation |
| Temporel | Séries chronologiques | Curseur par année, statistiques par bande, flèches de tendance, lecture animée |

### Package QField (relevé terrain)

Archive ZIP prête pour la collecte de données sur le terrain avec [QField](https://qfield.org/) :

- Projet `.qgz` avec chemins relatifs vers les GeoPackage
- Toutes les couches vectorielles matérialisées en GeoPackage individuels
- Couche **Observations** éditable avec widgets de formulaire (listes déroulantes, sélecteur de date, appareil photo, texte libre)

```python
export_qfield(project_name="releve_terrain")
# → /data/releve_terrain_qfield.zip
```

### Document Grist

Convertit les couches du projet QGIS ou n'importe quelle carte HTML en document collaboratif [Grist](https://grist.numerique.gouv.fr/) (`.grist`) — le tableur collaboratif souverain intégré à La Suite numérique (DINUM/ANCT) :

- **Depuis le projet** — Exporte les couches vectorielles visibles en tables Grist avec un widget carte personnalisé
- **Depuis un HTML** — Convertisseur universel : prend n'importe quel fichier HTML contenant du GeoJSON et crée un document Grist avec les données en tables et la carte interactive en widget personnalisé

Types de colonnes détectés : `Choice` (listes déroulantes colorées), `Date` (timestamps), `Ref` (références entre tables). Les tables de type formulaire obtiennent automatiquement une page Formulaire Grist.

```python
# Depuis le projet QGIS
export_grist(title="Analyse urbaine")

# Depuis un HTML avec GeoJSON
export_grist(html_path="/data/carte_inondation_nimes.html")
```

---

## Outils MCP

### Chargement intelligent

| Outil | Description |
|-------|-------------|
| `set_study_zone` | Définir la zone d'étude (commune, adresse, bbox). Géocode, stocke la bbox, zoome le canevas. |
| `get_study_zone` | Obtenir la zone d'étude courante (nom, bbox en 4326 + 2154). |
| `smart_load` | Charger une donnée par ID du catalogue. WFS → GPKG local avec index spatial. Les rasters sont en flux. |

### Cœur

| Outil | Description |
|-------|-------------|
| `execute_python` | Exécuter du code PyQGIS avec accès complet à iface, project, processing, module `helpers`. |
| `get_screenshot` | Capturer le canevas QGIS en PNG. Inclus automatiquement après les outils qui modifient le canevas. |
| `get_project_info` | État du projet courant (couches, SCR, mises en page, emprise). |
| `run_processing` | Exécuter l'un des 1 000+ algorithmes Processing. |
| `search_algorithms` | Rechercher un algorithme Processing par mot-clé. |
| `zoom_to` | Naviguer vers une emprise, une couche ou un point. |

### Données et couches

| Outil | Description |
|-------|-------------|
| `add_layer` | Ajouter une couche vectorielle/raster/WFS/WMS par URI. |
| `remove_layer` | Supprimer une couche. |
| `get_features` | Interroger les entités avec filtres attributaires ou spatiaux. |
| `list_datasources` | Parcourir le catalogue de données pré-configuré. |
| `add_from_catalog` | Ajouter une source par ID du catalogue. |

### Symbologie

| Outil | Description |
|-------|-------------|
| `set_layer_style` | Appliquer une symbologie simple, catégorisée ou graduée. |
| `set_layer_visibility` | Afficher/masquer des couches. |
| `apply_layout_template` | Appliquer un modèle de mise en page (A3 paysage, A4 portrait). |
| `list_layout_templates` | Lister les modèles de mise en page disponibles. |

### Recettes

| Outil | Description |
|-------|-------------|
| `list_recipes` | Parcourir les recettes de workflow disponibles. |
| `get_recipe` | Obtenir le détail d'une recette avec substitution des paramètres. |
| `run_recipe` | Exécuter une recette complète automatiquement. |

### Export

| Outil | Description |
|-------|-------------|
| `export_pdf` | Exporter la mise en page en PDF. |
| `export_web_map` | Exporter les couches visibles en carte Leaflet interactive. |
| `export_flood_map` | Carte d'analyse inondation interactive (curseur hauteur d'eau, exposition bâtiments). |
| `export_temporal_map` | Carte d'analyse temporelle interactive (curseur par année, lecture animée). |
| `export_qfield` | Package QField (`.qgz` + GPKG + couche Observations éditable). |
| `export_grist` | Document Grist depuis le projet ou depuis un HTML avec GeoJSON. |
| `export_layer` | Exporter une couche vectorielle en GPKG, GeoJSON, Shapefile, CSV. |

### Fichiers

| Outil | Description |
|-------|-------------|
| `upload_file` | Téléverser un fichier (shapefile, GeoJSON, GPKG, CSV, TIFF, projet). |
| `download_file` | Télécharger un fichier depuis /data/. |
| `list_files` | Lister les fichiers dans /data/. |
| `delete_file` | Supprimer un fichier de /data/. |
| `download_project` | Sauvegarder le projet en .qgz. |

### Interaction GUI

| Outil | Description |
|-------|-------------|
| `qgis_desktop_ui` | Ouvrir la vue interactive QGIS (MCP App) dans la conversation. |
| `mouse_click` / `mouse_scroll` / `mouse_drag` / `key_press` | Interaction directe avec l'interface via xdotool. |

### Projets

| Outil | Description |
|-------|-------------|
| `new_project` | Créer un projet vide. |
| `open_project` | Ouvrir un projet .qgz. |
| `save_project` | Sauvegarder le projet courant. |

---

## Ressources MCP (Skills)

Documents de référence qui guident l'expertise de l'assistant IA :

| URI de la ressource | Contenu |
|---------------------|---------|
| `skill://smart-loading` | Pipeline de chargement intelligent — set_study_zone + smart_load, gestion des SCR, cache |
| `skill://pyqgis` | Patterns de scripting PyQGIS et utilisation de l'API |
| `skill://processing` | Guide des algorithmes Processing (natifs, GDAL, GRASS) |
| `skill://cartography` | Symbologie, étiquettes, mises en page, export PDF |
| `skill://helpers` | Helpers Python prêts à l'emploi (géocodage, add_wfs, zoom_to, create_point_layer…) |
| `skill://data-sources` | Référentiel des jeux de données nationaux |
| `skill://recipes` | Référentiel des recettes de workflow |
| `skill://external-services` | Appel de services HTTP externes depuis des scripts PyQGIS |
| `skill://qgis-status` | État en temps réel de l'instance QGIS |

## Prompts MCP

| Prompt | Description |
|--------|-------------|
| `analyse_territoire` | Modèle pour l'analyse territoriale (zone + question). |
| `workflow_donnees` | **Workflow guidé** — chargement thématique (urbanisme, environnement, transport, agriculture, risques) avec instructions pas à pas. |

---

## MCP App

Le protocole MCP permet aussi d'intégrer des interfaces interactives directement dans la conversation (fonctionnalité "MCP App", disponible sur Claude Desktop). L'utilisateur ne quitte jamais son assistant :

- **Visualiseur VNC en direct** — Le canevas QGIS s'affiche dans la conversation, pas besoin d'ouvrir un navigateur séparément
- **Interface avec le stockage du service** — Importer des fichiers depuis son poste (shapefile, GeoJSON, GPKG, projet…) ou récupérer les résultats produits par l'IA (PDF, cartes HTML, packages QField…), directement depuis la conversation
- **Clavier et souris** — Interaction complète avec l'interface QGIS sans changer de fenêtre
- **Repli MJPEG** — Flux léger pour un retour visuel rapide quand le VNC n'est pas nécessaire

L'essentiel du travail se fait par les outils MCP — l'IA pilote QGIS de manière autonome. La MCP App permet de superviser visuellement ce que l'IA fait, d'intervenir manuellement si besoin, et de faire transiter les fichiers entre le poste de l'utilisateur et le service distant.

---

## Exemples d'utilisation

### Analyse urbaine (mode manuel)

```
Utilisateur : "Analyse l'urbanisation autour de Montpellier"

IA : [set_study_zone("Montpellier")]
     → Géocode, stocke la bbox, zoome le canevas

     [smart_load("osm_xyz")]              → Fond de carte OpenStreetMap
     [smart_load("bdtopo_batiments")]     → 10 000 bâtiments en GPKG
     [smart_load("bdtopo_routes")]        → 5 000 tronçons routiers

     [execute_python → grille de densité] → Grille hexagonale 500 m, symbologie graduée
     [apply_layout_template("a3_landscape", title="Densité bâtie — Montpellier")]
     [export_pdf]                         → /data/densite_montpellier.pdf
```

### Risque inondation (recette automatisée)

```
Utilisateur : "Analyse le risque inondation à Nîmes"

IA : [run_recipe("risque_inondation", zone="Nimes")]
     → Exécute automatiquement toutes les étapes :
       1. set_study_zone("Nimes")
       2. Chargement fond de carte + bâtiments + zones inondables
       3. Analyse de buffer (50 m, 100 m, 200 m des zones inondables)
       4. Classification de l'exposition des bâtiments
       5. Symbologie graduée
     → Renvoie une capture d'écran + statistiques

     [export_flood_map(include_fields=["nature","usage","height"])]
     → Carte HTML interactive avec curseur de hauteur d'eau

     [export_grist(html_path="/data/flood_map_nimes.html")]
     → Document Grist avec tables éditables + widget carte embarqué
```

### Relevé terrain (export QField)

```
Utilisateur : "Prépare un relevé terrain pour la commune de Sète"

IA : [set_study_zone("Sète")]
     [smart_load("bdtopo_batiments")]
     [smart_load("bdtopo_routes")]
     [set_layer_style("Batiments", type="categorized", field="usage")]

     [export_qfield(project_name="releve_sete")]
     → ZIP avec .qgz + GPKG + couche Observations (appareil photo, listes, date)
     → Prêt à charger sur l'application mobile QField
```

---

## Extension avec des services externes

Les scripts PyQGIS (`execute_python`) peuvent appeler n'importe quel service HTTP accessible depuis le conteneur — local ou distant. Cela couvre les API d'inférence ML, les backends métier, les services d'élévation ou toute API interne.

Passer les URL de service via les variables d'environnement dans `.env` :

```
MY_SERVICE_URL=http://host.docker.internal:8001
```

Puis les utiliser dans les scripts :

```python
import os, urllib.request, json
url = os.environ.get("MY_SERVICE_URL")
# appeler votre service...
```

`host.docker.internal` résout vers la machine hôte, permettant l'accès aux services qui tournent en dehors du conteneur.

---

## Architecture

```
┌─────────────────────────────────────────────────────────────┐
│                 🖱️ QgisRemoteMCP Container                  │
│                                                              │
│  supervisord                                                 │
│  ├── Xvfb :99                (affichage virtuel 1920×1080)  │
│  ├── fluxbox                 (gestionnaire de fenêtres)     │
│  ├── QGIS Desktop ◄─────────────────────┐                  │
│  │   └── qgis_bridge.py    (démarrage)  │ Socket UNIX      │
│  ├── x11vnc → noVNC         (:6080)     │                  │
│  ├── api_server.py           (:8080) ───┘                   │
│  ├── main_mcp.py             (:8100) ───┘                   │
│  └── stream_server.py        (:8081)                        │
│                                                              │
│  /data/          (fichiers utilisateur, projets)            │
│  /data/cache/    (cache GPKG smart_load, 24 h)              │
│  /app/skills/    (documents de compétence MCP)              │
│  /app/datasources.json  (30+ sources pré-configurées)       │
└──────────┬──────────────────────────────────────────────────┘
           │ HTTP (optionnel)
           ▼
    Services externes     (toute API HTTP accessible depuis le conteneur)
```

### Flux de communication

```
Claude Desktop / Client MCP
  │ JSON-RPC sur Streamable HTTP (:8100)
  ▼
main_mcp.py (Serveur MCP)
  │ Socket UNIX /tmp/qgis_bridge.sock
  ▼
qgis_bridge.py (tourne dans QGIS, thread principal)
  │ API PyQGIS (iface, QgsProject, processing)
  ▼
QGIS Desktop (affichage Xvfb :99)
  │ X11
  ▼
x11vnc → websockify → noVNC (:6080)
  │ WebSocket
  ▼
Navigateur de l'utilisateur
```

---

## Développement

```bash
# Les fichiers sources sont montés en volumes — modifier localement
# Redémarrer pour appliquer les changements :
docker compose restart qgisremotemcp

# Voir les logs
docker compose logs -f qgisremotemcp

# Tester l'API
curl http://localhost:8080/health
curl -X POST http://localhost:8080/api/execute \
  -H "Content-Type: application/json" \
  -d '{"code": "result[\"v\"] = Qgis.version()"}'

# Tester le chargement intelligent
curl -X POST http://localhost:8080/api/execute \
  -H "Content-Type: application/json" \
  -d '{"code": "result.update(helpers.set_study_zone(\"Montpellier\"))", "timeout": 30}'
```

---

## Structure du projet

```
QgisRemoteMCP/
├── main_mcp.py             # Serveur MCP (42 outils, 10 ressources, 2 prompts)
├── datasources.json        # Catalogue de 30+ sources de données pré-configurées
├── qgis_app.html           # MCP App (QGIS interactif dans la conversation)
├── src/
│   ├── qgis_bridge.py      # Tourne dans QGIS (pont socket UNIX, 45 actions)
│   ├── qgis_helpers.py     # Helpers Python (géocodage, chargement intelligent, etc.)
│   ├── api_server.py       # API REST FastAPI
│   └── stream_server.py    # Flux MJPEG
├── skills/                 # Ressources MCP (documents de compétence IA)
│   ├── smart_loading.md
│   ├── pyqgis.md
│   ├── processing.md
│   ├── cartography.md
│   ├── helpers.md
│   ├── data_sources.md
│   └── external_services.md
├── recipes/                # Recettes de workflow (JSON)
│   ├── densite_bati.json
│   ├── urbanisme_general.json
│   ├── risque_inondation.json
│   ├── occupation_sol.json
│   └── pression_fonciere_cotiere.json
├── templates/              # Modèles de mise en page (.qpt)
│   ├── a3_landscape.qpt
│   ├── a4_portrait.qpt
│   └── web/                # Modèles HTML Leaflet
│       ├── leaflet_template.html
│       ├── leaflet_flood_template.html
│       └── leaflet_temporal_template.html
├── projects/               # Projets QGIS (persistants)
├── docs/                   # Schémas d'architecture
├── Dockerfile
├── docker-compose.yml
├── supervisord.conf
├── entrypoint.sh
├── requirements.txt
└── CLAUDE.md
```

---

## Licence

MIT

## Crédits

- **QGIS** — https://qgis.org
- **noVNC** — https://novnc.com
- **MCP** — https://modelcontextprotocol.io
- **IGN Géoplateforme** — https://data.geopf.fr (géodonnées nationales ouvertes)
- **GDAL/OGR** — https://gdal.org (moteur de conversion et de téléchargement)
- **Grist** — https://grist.numerique.gouv.fr (tableur collaboratif, La Suite numérique DINUM/ANCT)