# External Vision Services Reference

## Architecture

BigQgisMCP does NOT embed vision models. They run as separate services
accessible via HTTP. Your PyQGIS scripts call them with `urllib.request`.

Service URLs are configured via environment variables:
- `MOONDREAM_URL` (default: http://localhost:8001)
- `SAMGEO3_URL` (default: http://localhost:8002)
- `DEPTHPRO_URL` (default: http://localhost:8003)

Inside `execute_python`, read them with:
```python
import os
MOONDREAM = os.environ.get("MOONDREAM_URL", "http://localhost:8001")
SAMGEO3 = os.environ.get("SAMGEO3_URL", "http://localhost:8002")
DEPTHPRO = os.environ.get("DEPTHPRO_URL", "http://localhost:8003")
```

## Check service availability

```python
import urllib.request, json, os

def check_service(url):
    try:
        resp = urllib.request.urlopen(f"{url}/health", timeout=5)
        return json.loads(resp.read())
    except:
        return None

services = {}
for name, url_var, default in [
    ("moondream", "MOONDREAM_URL", "http://localhost:8001"),
    ("samgeo3", "SAMGEO3_URL", "http://localhost:8002"),
    ("depthpro", "DEPTHPRO_URL", "http://localhost:8003"),
]:
    url = os.environ.get(url_var, default)
    services[name] = {"url": url, "status": check_service(url)}

result['services'] = services
```

## Moondream (Vision Language Model)

### Describe an image
```python
import urllib.request, json, base64

MOONDREAM = os.environ.get("MOONDREAM_URL", "http://localhost:8001")

with open("/data/image.jpg", "rb") as f:
    img_b64 = base64.b64encode(f.read()).decode()

data = json.dumps({
    "image": img_b64,
    "prompt": "Describe this street scene. Focus on road infrastructure."
}).encode()

req = urllib.request.Request(
    f"{MOONDREAM}/caption",
    data=data,
    headers={"Content-Type": "application/json"}
)
resp = urllib.request.urlopen(req, timeout=30)
caption = json.loads(resp.read())
result['caption'] = caption
```

### Detect objects
```python
data = json.dumps({
    "image": img_b64,
    "query": "pedestrian crossing"
}).encode()

req = urllib.request.Request(
    f"{MOONDREAM}/detect",
    data=data,
    headers={"Content-Type": "application/json"}
)
resp = urllib.request.urlopen(req, timeout=30)
detections = json.loads(resp.read())
result['detections'] = detections
# Returns: [{"bbox": [x1,y1,x2,y2], "confidence": 0.92}, ...]
```

### Query an image (VQA)
```python
data = json.dumps({
    "image": img_b64,
    "question": "Is there a crosswalk in this image? What is its condition?"
}).encode()

req = urllib.request.Request(
    f"{MOONDREAM}/query",
    data=data,
    headers={"Content-Type": "application/json"}
)
resp = urllib.request.urlopen(req, timeout=30)
answer = json.loads(resp.read())
result['answer'] = answer
```

## SAMGeo3 (Segment Anything for Geospatial)

### Segment by text prompt
```python
SAMGEO3 = os.environ.get("SAMGEO3_URL", "http://localhost:8002")

data = json.dumps({
    "image": img_b64,           # or "image_path": "/data/ortho.tif"
    "text_prompt": "buildings",
    "output_format": "geojson",
}).encode()

req = urllib.request.Request(
    f"{SAMGEO3}/segment",
    data=data,
    headers={"Content-Type": "application/json"}
)
resp = urllib.request.urlopen(req, timeout=120)
geojson = json.loads(resp.read())

# Save and load as QGIS layer
with open("/tmp/segments.geojson", "w") as f:
    json.dump(geojson, f)

layer = QgsVectorLayer("/tmp/segments.geojson", "SAM Segments", "ogr")
project.addMapLayer(layer)
result['layer_id'] = layer.id()
```

### Segment by bounding box
```python
data = json.dumps({
    "image_path": "/data/ortho.tif",
    "boxes": [[100, 100, 500, 500]],
    "output_format": "geojson",
    "crs": "EPSG:2154",
}).encode()

req = urllib.request.Request(f"{SAMGEO3}/segment", data=data,
    headers={"Content-Type": "application/json"})
resp = urllib.request.urlopen(req, timeout=120)
```

## DepthPro (Monocular Depth Estimation)

### Estimate depth
```python
DEPTHPRO = os.environ.get("DEPTHPRO_URL", "http://localhost:8003")

data = json.dumps({"image": img_b64}).encode()
req = urllib.request.Request(
    f"{DEPTHPRO}/estimate",
    data=data,
    headers={"Content-Type": "application/json"}
)
resp = urllib.request.urlopen(req, timeout=60)
depth = json.loads(resp.read())
# Returns: {"depth_map_base64": "...", "min_depth": 0.5, "max_depth": 45.2}
```

## Batch processing pattern

```python
import urllib.request, json, base64, os

MOONDREAM = os.environ.get("MOONDREAM_URL", "http://localhost:8001")

# Process multiple images
image_dir = "/data/panoramax_images/"
results = []

for filename in os.listdir(image_dir):
    if not filename.endswith(('.jpg', '.png')):
        continue
    
    with open(os.path.join(image_dir, filename), "rb") as f:
        img_b64 = base64.b64encode(f.read()).decode()
    
    data = json.dumps({"image": img_b64, "query": "pedestrian crossing"}).encode()
    req = urllib.request.Request(f"{MOONDREAM}/detect", data=data,
        headers={"Content-Type": "application/json"})
    
    try:
        resp = urllib.request.urlopen(req, timeout=30)
        detections = json.loads(resp.read())
        results.append({"file": filename, "detections": detections})
        print(f"  {filename}: {len(detections.get('objects', []))} detections")
    except Exception as e:
        print(f"  {filename}: ERROR {e}")

result['processed'] = len(results)
result['detections'] = results
```

## Error handling

Always handle service unavailability gracefully:
```python
import urllib.request, json, os

def call_service(url, endpoint, payload, timeout=30):
    """Call an external service with error handling."""
    try:
        data = json.dumps(payload).encode()
        req = urllib.request.Request(
            f"{url}/{endpoint}", data=data,
            headers={"Content-Type": "application/json"}
        )
        resp = urllib.request.urlopen(req, timeout=timeout)
        return {"success": True, "data": json.loads(resp.read())}
    except urllib.error.URLError as e:
        return {"success": False, "error": f"Service unavailable: {e}"}
    except TimeoutError:
        return {"success": False, "error": f"Timeout after {timeout}s"}
    except Exception as e:
        return {"success": False, "error": str(e)}
```
