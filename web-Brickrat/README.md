# Furniture 3D AR Studio

FastAPI/PWA frontend for converting a furniture photo with TripoSR, exporting
portable OBJ/GLB assets, optionally converting a validated GLB to USDZ, and
saving the product and public asset URLs to Supabase.

## What the export contains

TripoSR in this repository returns geometry plus per-vertex colours. It does not
produce an original photograph UV texture. The export pipeline now distinguishes
three truthful material cases:

1. Preserve an existing source UV/base-colour texture when one is available.
2. Bake TripoSR vertex colours into an embedded texture atlas. This keeps colour
   visible in GLB and gives Quick Look a supported UV texture instead of relying
   on unsupported vertex-colour rendering.
3. If neither exists, derive a dominant colour from the uploaded object and mark
   the result as a fallback in `material_report.json` and in the UI.

The GLB is rejected unless it contains a mesh, UVs, a PBR material, a base-colour
texture, and an embedded image. OBJ is exported with its MTL and texture files.
USDZ is offered only when conversion succeeds and the package contains both a
USD scene and texture image.

## Install and run

The requirements include Pixar OpenUSD's `usd-core` wheel so USDZ generation
works on Windows, Linux, and macOS without a separately installed converter.

```powershell
cd C:\Git\TripoSR\web-Brickrat
py -3.10 -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install --upgrade pip
python -m pip install -r requirements.txt
Copy-Item .env.example .env
# Fill in SUPABASE_URL, SUPABASE_KEY, and SUPABASE_STORAGE_BUCKET in .env.
python -m uvicorn main:app --host 0.0.0.0 --port 8000 --reload
```

Open `http://127.0.0.1:8000`. Mobile AR requires the deployed app and model
URLs to use HTTPS. Run `supabase.sql` in Supabase and create a public Storage
bucket named `products` before saving.

## USDZ generation

The backend authors a binary USD scene from the textured Trimesh mesh and uses
OpenUSD's package writer to create the uncompressed, 64-byte-aligned USDZ that
Apple Quick Look requires. The exporter includes the UV texture, PBR material,
normals, metric scale, Y-up orientation, and floor placement. The resulting
package is rejected unless its USD scene, texture, ZIP storage, and alignment
all validate.

An existing compatible converter can still be selected as an optional override:

```powershell
$env:USDZ_CONVERTER = "C:\Tools\usd_from_gltf.exe"
& $env:USDZ_CONVERTER --help
python -m uvicorn main:app --host 0.0.0.0 --port 8000
```

If the optional converter fails validation, the app automatically falls back to
its OpenUSD exporter. A failed USDZ export does not invalidate the GLB or OBJ.

## Mobile previews, AR, and PWA behaviour

The conversion result initially renders only the product image, validation
status, file sizes, and actions. GLB, OBJ, `<model-viewer>`, and Three.js are
loaded only after the corresponding preview button is pressed. Downloads show
measured transferred bytes, mobile files over 10 MB show a warning, and files
over 25 MB require confirmation. Closing a preview aborts its request, revokes
blob URLs, stops render loops, and disposes WebGL resources. Opening the other
preview unloads the active one first.

- Android opens the validated public HTTPS GLB in Scene Viewer and falls back to
  the unchanged product page when Scene Viewer is unavailable.
- iPhone/iPad uses a direct `rel="ar"` Apple Quick Look link to a validated USDZ.
  A GLB is never presented as an iPhone AR file.
- Desktop shows on-demand WebGL previews without misleading mobile AR controls.

Preview assets use explicit inline routes and model MIME types; download routes
use attachments. Both support byte ranges and `no-store` cache headers. Public
asset URLs are built from the current or forwarded host/protocol, so an HTTPS
ngrok page does not embed localhost or mixed-content model URLs.

`manifest.json` includes 192 px, 512 px, and maskable icons. The PWA uses a
standalone display, root start URL/scope, a registered service worker, hashed
asset query versions, network-first frontend updates, old-cache deletion, and
network-only generated/model routes. The service worker does not intercept Apple
Quick Look and cannot substitute a cached GLB, OBJ, or USDZ.

## Diagnostics and tests

Inspect generated content without loading the ML model:

```powershell
python model_diagnostics.py generated\<conversion-id>\model.glb `
  --usdz generated\<conversion-id>\model.usdz
```

Or temporarily enable the developer endpoint (never enable it publicly):

```powershell
$env:MODEL_DEBUG = "true"
# GET /debug/models/<conversion-id>
```

Run tests and syntax checks:

```powershell
python -m unittest discover -s tests -v
python -m compileall -q .
```

The application logs UV presence, vertex-colour presence, material strategy,
texture filenames/sizes, embedded GLB images, USDZ package textures, export file
sizes, converter selection, Supabase upload results, final public URLs, and
frontend device/AR-mode detection. Secrets and environment values are not logged.

## Manual device checklist

1. Convert white, dark, and multi-coloured furniture samples. Confirm the GLB
   preview matches the available TripoSR colour and the UI states whether a
   source texture, vertex-colour atlas, or fallback was used.
2. Download the GLB on another device and confirm it works without OBJ/MTL files.
3. On Android Chrome over HTTPS, tap the single **View in AR** button and test
   WebXR/Scene Viewer, then return to the unchanged product page.
4. On iPhone Safari over HTTPS, confirm the AR button appears only with a valid
   USDZ, opens Quick Look, and returns to the same page when Quick Look closes.
5. Add the site to the iPhone Home Screen and repeat from standalone PWA mode.
6. Confirm each conversion reports a validated USDZ and that the downloaded file
   opens in Apple Quick Look with its texture intact.
7. Simulate a Supabase failure and confirm no database row is created and newly
   uploaded assets are cleaned up.
8. Deploy changed frontend files, reopen an existing installed PWA, and confirm
   the service worker activates the new asset version instead of stale JS/CSS.
