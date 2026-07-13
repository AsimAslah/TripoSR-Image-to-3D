import json
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


class FrontendContractTests(unittest.TestCase):
    def test_manifest_has_installable_icons(self):
        manifest = json.loads((ROOT / "static" / "manifest.json").read_text(encoding="utf-8"))
        self.assertEqual(manifest["display"], "standalone")
        self.assertEqual(manifest["start_url"], "/")
        sizes = {icon["sizes"] for icon in manifest["icons"]}
        self.assertIn("192x192", sizes)
        self.assertIn("512x512", sizes)
        for icon in manifest["icons"]:
            self.assertTrue((ROOT / "static" / icon["src"].removeprefix("/static/")).is_file())

    def test_result_uses_direct_validated_usdz_quick_look_link(self):
        template = (ROOT / "templates" / "conversion_result.html").read_text(encoding="utf-8")
        self.assertEqual(template.count('rel="ar"'), 1)
        self.assertIn('href="{{ usdz_url }}"', template)
        self.assertIn("{% if usdz_url %}", template)
        self.assertIn('class="button primary android-ar-action"', template)

    def test_page_has_viewport_and_does_not_eager_load_viewer_modules(self):
        page = (ROOT / "templates" / "index.html").read_text(encoding="utf-8")
        self.assertIn('name="viewport" content="width=device-width, initial-scale=1"', page)
        self.assertNotIn("model-viewer.min.js", page)
        self.assertNotIn("obj-preview.js", page)

    def test_previews_are_on_demand_and_recoverable(self):
        template = (ROOT / "templates" / "conversion_result.html").read_text(encoding="utf-8")
        script = (ROOT / "static" / "app.js").read_text(encoding="utf-8")
        self.assertNotIn("<model-viewer", template)
        self.assertNotIn('class="obj-preview"', template)
        self.assertIn("Load GLB Preview", template)
        self.assertIn("Load OBJ Preview", template)
        self.assertIn("Retry preview", template)
        self.assertIn("Download model", template)
        self.assertIn('button.addEventListener("click", () => loadPreview', script)
        self.assertIn("unloadPreview(root, { hide: false })", script)
        self.assertIn("URL.revokeObjectURL", script)

    def test_mobile_css_prevents_horizontal_overflow_and_has_touch_targets(self):
        styles = (ROOT / "static" / "styles.css").read_text(encoding="utf-8")
        self.assertIn("overflow-x: hidden", styles)
        self.assertIn("@media (max-width: 767px)", styles)
        self.assertIn("min-height: 48px", styles)

    def test_service_worker_does_not_cache_generated_models(self):
        worker = (ROOT / "static" / "service-worker.js").read_text(encoding="utf-8")
        self.assertIn('url.pathname.startsWith("/generated/")', worker)
        self.assertIn('url.pathname.startsWith("/models/")', worker)
        self.assertIn('cache: "no-store"', worker)


if __name__ == "__main__":
    unittest.main()
