# File Exchange — bridge_put / bridge_get

> **Status: design — pending implementation.** This skill documents the
> target API for the unified file exchange bridge. The current repo still
> exposes `upload_file` / `download_file` (see bottom of this page for the
> mapping). New code should target `bridge_put` / `bridge_get`; the
> implementation will converge to the contract described here.

## Cheatsheet

```
Local file (any OS) → bridge_put('C:\\path\\file.gpkg', auto_load=true) → layer_id (1 call)
Public URL          → bridge_put('https://...',           auto_load=true) → layer_id
Inline (<1 MB)      → bridge_put({content_base64, name},  auto_load=true) → layer_id
Already in /data/   → add_layer(uri='/data/file.gpkg')                    → layer_id
Pull file out       → bridge_get('/data/export.pdf', dest='C:\\Out\\')    → local path
```

`bridge_put` resolves source automatically (local path vs URL vs inline).
With `auto_load=true` it streams the file into `/data/` **and** loads it
in QGIS in a single round-trip — no separate `add_layer` call needed.

## Common patterns

### 1. Load a local GeoJSON into a fresh project

```python
# MCP tool calls (pseudo)
new_project()
bridge_put(
    src=r"C:\Users\me\Desktop\parcels.geojson",
    auto_load=True,
    layer_name="Parcelles",
)
# → {"path": "/data/parcels.geojson", "layer_id": "Parcelles_abc123",
#    "feature_count": 412, "crs": "EPSG:4326"}
```

### 2. Retrieve an export PDF locally

```python
export_pdf(layout="Export A3 Landscape", filename="rapport.pdf")
bridge_get(src="/data/rapport.pdf", dest=r"C:\Users\me\Documents\\")
# → {"local_path": "C:\\Users\\me\\Documents\\rapport.pdf", "bytes": 3145728}
```

### 3. Iterate on a layer (mutate, replace source)

```python
# 1. Pull current source
bridge_get("/data/buildings.gpkg", dest=r"C:\tmp\\")
# 2. Edit locally (QGIS, ogr2ogr, geopandas...)
# 3. Push back, replacing the in-project source in one shot
bridge_put(
    src=r"C:\tmp\buildings.gpkg",
    auto_load=True,
    replace_layer_id="buildings_xyz",   # swap source on the existing layer
)
```

### 4. Files larger than 100 MB

```python
# bridge_put returns an upload endpoint when file is too big to inline
resp = bridge_put(src="ortho.tif", mode="endpoint")
# → {"upload_url": "http://localhost:8080/api/upload/abcd",
#    "method": "POST", "field": "file", "expires_in": 600}
# Stream upload from the client side (no base64 round-trip).
# Then: add_layer(uri="/data/ortho.tif")
```

`bridge_put` auto-switches to endpoint mode when `os.path.getsize(src) > 50 MB`.

### 5. Load directly from a public URL

```python
bridge_put(
    src="https://wxs.ign.fr/.../parcelles.geojson",
    auto_load=True,
    layer_name="Cadastre",
)
# Server fetches the URL itself — no client bandwidth used.
```

### 6. Inline content under 1 MB

```python
bridge_put(
    content_base64=b64,
    name="points.geojson",
    auto_load=True,
)
# Equivalent to passing src as a base64 blob; reserved for tiny payloads.
```

## Anti-patterns

| Don't                                       | Do                                              |
|---------------------------------------------|-------------------------------------------------|
| `curl -F file=@... http://.../api/upload`   | `bridge_put(src=local_path)`                    |
| `upload_file(...)` then `add_layer(...)`    | `bridge_put(src=..., auto_load=True)`           |
| `download_file('/data/big.gpkg')` to filter | `get_features(layer_id, filter="surface > 1000")` |
| Hardcode `/data/foo.gpkg` in code           | Use the `path` returned by `bridge_put`         |
| Base64-encode a 200 MB raster               | Let `bridge_put` switch to `mode="endpoint"`    |
| Re-upload the same file every prompt        | Reuse the returned `path` (cached in /data/)    |

## Decision tree

```
I have a file to load?
├── Local path (Windows/Mac/Linux)  → bridge_put('C:\\...' or '/home/...')
├── Public URL                      → bridge_put('https://...')
├── Inline base64 < 1 MB            → bridge_put({content_base64, name})
└── Already in /data/               → add_layer(uri='/data/...')

I need a file out?
├── < 5 MB                          → bridge_get returns inline base64
└── ≥ 5 MB                          → bridge_get returns download_url
```

## Auto-verification (post-upload)

Always sanity-check the layer right after `bridge_put(auto_load=True)`:

```python
# Run via execute_python with the returned layer_id
layer = project.mapLayer(layer_id)
result["valid"]    = layer.isValid()                # must be True
result["count"]    = layer.featureCount()           # must be > 0
result["crs"]      = layer.crs().authid()           # must not be empty
ext = layer.extent()
result["extent"]   = [ext.xMinimum(), ext.yMinimum(),
                      ext.xMaximum(), ext.yMaximum()]
# Compare extent against expected bounds (study zone bbox)
zone = helpers.bbox_from_canvas()
result["in_zone"]  = (ext.xMinimum() >= zone[0] - 1 and
                      ext.xMaximum() <= zone[2] + 1)
```

Common red flags:

- `isValid() == False` → driver mismatch (e.g. `.shp` without `.dbf`/`.shx`).
- `featureCount() == 0` → CRS mismatch with project, or empty source.
- `crs().authid() == ""` → assign explicitly:
  `layer.setCrs(QgsCoordinateReferenceSystem('EPSG:4326'))`.
- extent outside study zone → wrong file, or coordinates in degrees vs metres.

## API contract (target)

```json
{
  "name": "bridge_put",
  "args": {
    "src":             "local path | URL | base64 (mutually exclusive with content_base64)",
    "content_base64":  "base64 string (paired with name, <1 MB)",
    "name":            "target filename in /data/ (optional, derived from src)",
    "auto_load":       "bool (default false) — open in QGIS after upload",
    "layer_name":      "display name when auto_load=true",
    "replace_layer_id":"swap source on an existing layer instead of adding a new one",
    "mode":            "auto | inline | endpoint (default auto)"
  },
  "returns": {
    "path":          "/data/<name>",
    "bytes":         123456,
    "sha256":        "...",
    "layer_id":      "set when auto_load=true",
    "feature_count": "set for vector layers",
    "crs":           "EPSG:xxxx",
    "upload_url":    "set when mode=endpoint"
  }
}
```

```json
{
  "name": "bridge_get",
  "args": {
    "src":   "/data/<file>",
    "dest":  "local directory or full path (optional)",
    "inline":"bool (default auto, true for <5 MB)"
  },
  "returns": {
    "local_path":    "set when dest provided",
    "download_url":  "always set",
    "content_base64":"set for inline mode",
    "bytes":         123456,
    "sha256":        "..."
  }
}
```

## Troubleshooting

| Error code         | Cause                                              | Fix                                                                 |
|--------------------|----------------------------------------------------|---------------------------------------------------------------------|
| `CRS_UNDEFINED`    | Source has no `.prj` or empty CRS metadata         | Call `layer.setCrs(QgsCoordinateReferenceSystem('EPSG:xxxx'))` after load, or pass `crs="EPSG:2154"` to `bridge_put` |
| `OGR_PARSE_FAILED` | Corrupt GeoJSON / wrong driver / encoding          | Validate with `ogrinfo`; ensure UTF-8 and balanced braces           |
| `QUOTA_EXCEEDED`   | `/data/` full (default 5 GB)                       | Run `list_files` + `delete_file` to free space, or raise quota env  |
| `TIMEOUT`          | URL fetch > 60 s, or upload stalled                | Use `mode="endpoint"` and stream from client; retry; cache locally  |
| `PATH_TRAVERSAL`   | `name` contains `..` or absolute path              | Use a basename only — the server pins to `/data/`                   |
| `SIZE_EXCEEDED`    | Inline payload > 1 MB or endpoint > 50 MB          | Split, or set `mode="endpoint"`; for >50 MB use chunked upload      |
| `LAYER_INVALID`    | `auto_load=true` but driver couldn't open the file | Check filename extension matches content; verify with `bridge_get`  |
| `HASH_MISMATCH`    | Network corruption during upload                   | Retry; hash is SHA-256 over the bytes written to `/data/`           |

## Migration from upload_file / download_file

Existing tools remain available; map them to the new API as follows:

| Legacy                                          | Target                                       |
|-------------------------------------------------|----------------------------------------------|
| `upload_file(name, url=...)`                    | `bridge_put(src=url, name=name)`             |
| `upload_file(name, content_base64=...)`         | `bridge_put(content_base64=..., name=name)`  |
| `upload_file(name)` then user POSTs multipart   | `bridge_put(src=..., mode="endpoint")`       |
| `download_file(path)`                           | `bridge_get(src=path)`                       |

Do not chain `upload_file` + `add_layer` in new code — pass `auto_load=true`
to `bridge_put` and consume the returned `layer_id` directly.
