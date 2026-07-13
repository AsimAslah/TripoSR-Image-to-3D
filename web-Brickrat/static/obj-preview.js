import * as THREE from "https://esm.sh/three@0.166.1";
import { OBJLoader } from "https://esm.sh/three@0.166.1/examples/jsm/loaders/OBJLoader.js";
import { OrbitControls } from "https://esm.sh/three@0.166.1/examples/jsm/controls/OrbitControls.js";

function disposeMaterial(material) {
  if (!material) return;
  Object.values(material).forEach((value) => {
    if (value?.isTexture) value.dispose();
  });
  material.dispose?.();
}

export async function createObjPreview(container, objText, onStage = () => {}) {
  if (!objText?.trim()) throw new Error("The OBJ response was empty.");

  const scene = new THREE.Scene();
  scene.background = new THREE.Color(0xf1f3f0);
  const camera = new THREE.PerspectiveCamera(38, 1, 0.01, 1000);
  let renderer;
  try {
    renderer = new THREE.WebGLRenderer({ antialias: true, alpha: false, powerPreference: "high-performance" });
  } catch (error) {
    error.stage = "rendering";
    throw error;
  }
  renderer.setPixelRatio(Math.min(window.devicePixelRatio || 1, 2));
  renderer.outputColorSpace = THREE.SRGBColorSpace;
  renderer.toneMapping = THREE.ACESFilmicToneMapping;
  renderer.toneMappingExposure = 1.05;
  container.replaceChildren(renderer.domElement);

  scene.add(new THREE.HemisphereLight(0xffffff, 0x8a8178, 2.2));
  const keyLight = new THREE.DirectionalLight(0xffffff, 2.6);
  keyLight.position.set(4, 6, 5);
  scene.add(keyLight);
  const fillLight = new THREE.DirectionalLight(0xddeeff, 1.2);
  fillLight.position.set(-4, 2, -3);
  scene.add(fillLight);

  const controls = new OrbitControls(camera, renderer.domElement);
  controls.enableDamping = true;
  controls.dampingFactor = 0.07;
  controls.autoRotate = true;
  controls.autoRotateSpeed = 1.1;
  controls.screenSpacePanning = true;

  let object;
  try {
    await new Promise((resolve) => requestAnimationFrame(resolve));
    object = new OBJLoader().parse(objText);
  } catch (error) {
    renderer.dispose();
    error.stage = "parsing";
    throw error;
  }
  onStage("materials");

  let meshCount = 0;
  object.traverse((child) => {
    if (!child.isMesh) return;
    meshCount += 1;
    if (Array.isArray(child.material)) child.material.forEach(disposeMaterial);
    else disposeMaterial(child.material);
    child.material = new THREE.MeshStandardMaterial({
      color: child.geometry.hasAttribute("color") ? 0xffffff : 0xb98d68,
      vertexColors: child.geometry.hasAttribute("color"),
      roughness: 0.76,
      metalness: 0.02,
      side: THREE.DoubleSide,
    });
    child.geometry.computeVertexNormals();
    child.geometry.computeBoundingBox();
  });
  if (!meshCount) {
    renderer.dispose();
    const error = new Error("The OBJ contains no renderable mesh geometry.");
    error.stage = "parsing";
    throw error;
  }

  const box = new THREE.Box3().setFromObject(object);
  if (box.isEmpty()) {
    renderer.dispose();
    const error = new Error("The OBJ geometry has an empty bounding box.");
    error.stage = "geometry";
    throw error;
  }
  const center = box.getCenter(new THREE.Vector3());
  const size = box.getSize(new THREE.Vector3());
  object.position.sub(center);
  scene.add(object);

  const maxSize = Math.max(size.x, size.y, size.z, 0.001);
  const distance = (maxSize / (2 * Math.tan(THREE.MathUtils.degToRad(camera.fov / 2)))) * 1.55;
  camera.position.set(distance * 0.9, distance * 0.58, distance);
  camera.near = Math.max(distance / 1000, 0.001);
  camera.far = Math.max(distance * 30, 10);
  camera.updateProjectionMatrix();
  controls.target.set(0, 0, 0);
  controls.minDistance = maxSize * 0.25;
  controls.maxDistance = distance * 5;
  controls.update();

  const floor = new THREE.Mesh(
    new THREE.CircleGeometry(maxSize * 1.4, 64),
    new THREE.MeshStandardMaterial({ color: 0xdde2dc, roughness: 1, metalness: 0 }),
  );
  floor.rotation.x = -Math.PI / 2;
  floor.position.y = -size.y / 2 - maxSize * 0.025;
  scene.add(floor);

  let disposed = false;
  let frameId = 0;
  function resize() {
    if (disposed) return;
    const width = Math.max(container.clientWidth, 1);
    const height = Math.max(container.clientHeight, 1);
    renderer.setSize(width, height, false);
    camera.aspect = width / height;
    camera.updateProjectionMatrix();
  }
  const resizeObserver = new ResizeObserver(resize);
  resizeObserver.observe(container);
  resize();

  function animate() {
    if (disposed || !container.isConnected) return;
    controls.update();
    renderer.render(scene, camera);
    frameId = requestAnimationFrame(animate);
  }
  animate();

  return {
    dispose() {
      if (disposed) return;
      disposed = true;
      cancelAnimationFrame(frameId);
      resizeObserver.disconnect();
      controls.dispose();
      scene.traverse((child) => {
        child.geometry?.dispose?.();
        if (Array.isArray(child.material)) child.material.forEach(disposeMaterial);
        else disposeMaterial(child.material);
      });
      renderer.dispose();
      renderer.forceContextLoss?.();
      container.replaceChildren();
    },
  };
}
