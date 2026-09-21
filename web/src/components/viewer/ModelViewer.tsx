/**
 * three.js model viewer for the d33d SPA (issue #6, workstream task-d).
 *
 * Renders STL and GLB meshes inline at a 1:1 mm scale. The scene is built
 * in millimetres — camera near/far, controls distances, and mesh positions
 * all assume mm. This is the single viewer the whole SPA uses; region
 * selection (issue #98) needs `Raycaster.intersectObjects` on the same
 * scene graph, so the viewer exposes the three.js scene, camera, renderer,
 * and raycaster as a stable API surface for the App-level point pick.
 *
 * 3MF is NOT loaded here. The final 3MF is a download artifact only —
 * there is no native 3MF loader in the three.js addon set, and the spec
 * explicitly defers adding one.
 *
 * Key invariants:
 *   - Every loaded mesh is treated as millimetres (STL is unitless by
 *     convention; OpenSCAD output is mm; the render worker contract is mm).
 *   - Geometry and materials are disposed on swap to avoid GPU memory
 *     accumulation (six renders × non-indexed meshes would leak otherwise).
 *   - Multi-solid STL (ASCII multi-solid → `geometry.groups.length > 1`)
 *     gets each group centred and given its own material.
 *   - The scene uses the three.js Y-up convention. OpenSCAD/STL models are
 *     Z-up (CAD convention: +Z is the model's "top"), so every loaded mesh
 *     root is rotated -PI/2 about X and the camera's up axis is set to +Z
 *     (applyZUpToYUp). The viewport top of the screen is the model's top.
 *   - 3MF is Z-up as well; if a future ticket inlines a 3MF it must apply
 *     the SAME rotation (rotation.set(-Math.PI/2, 0, 0) + camera.up) — the
 *     viewer's Y-up scene assumption is documented so future work does not
 *     silently double-rotate.
 *
 * Single-click region picking (issue #98)
 * ----------------------------------------
 * `resolvePointPick` is the client-side picking half of #98's
 * "how the click resolves" contract. Given ONE click location (in the
 * viewport's on-screen CSS-pixel space — the same space the App-level
 * pick layer records via `getBoundingClientRect()`) and the scene's model
 * root, it:
 *
 *   1. Converts the pixel coordinate to NDC and casts ONE ray via
 *      `Raycaster.intersectObjects(sceneRoot, true)` — three.js returns
 *      hits nearest-first, so the first hit is exactly what is visible at
 *      that pixel (z-buffer occlusion for free).
 *   2. The pick is a HIT only when the ray lands on some loaded geometry
 *      (`hits.length > 0`). Unnamed geometry (a streamed STL) is still a
 *      valid pick — the module-name bonus is absent, never blocking.
 *   3. `module` is the `name` of the NEAREST hit's object when that
 *      object has a non-empty name (the named-module GLB fixture path),
 *      else null. Resolution is solely via `object.name`, never
 *      per-face/per-vertex colour (the server-side OpenSCAD colour-ID pass
 *      is dead: `color()` is discarded by `--render`/STL export).
 *
 * A click on empty background (no geometry under the pixel) returns
 * `hit: false` — the caller surfaces a notice and does NOT select: a
 * marker on empty background grounds nothing and would mislead the vision
 * model.
 *
 * Server-side disocclusion is out of scope (spec: "a rabbit hole"). A
 * module fully hidden behind another in the current view is not
 * selectable; the escape hatch is `object.visible = false` on the
 * occluding mesh (excluding it from `intersectObjects`) so the user can
 * hide-and-reselect, per spec.
 */

import { useEffect, useRef } from 'react';
import * as THREE from 'three';
import { OrbitControls } from 'three/addons/controls/OrbitControls.js';
import { STLLoader } from 'three/addons/loaders/STLLoader.js';
import { GLTFLoader } from 'three/addons/loaders/GLTFLoader.js';
import copy from '../../copy';

// ---------------------------------------------------------------------------
// Types
// ---------------------------------------------------------------------------

/** A mesh loaded into the viewer, ready to display. */
export interface LoadedMesh {
  /** The three.js mesh or group. */
  object: THREE.Object3D;
  /** The source format, for display / debugging. */
  format: 'stl' | 'glb';
}

/** Result of loading a mesh file. */
export interface LoadResult {
  ok: boolean;
  mesh?: LoadedMesh;
  error?: string;
  /** Bounding box dimensions in mm, if the mesh loaded successfully. */
  boundingBox?: { x: number; y: number; z: number };
}

/** Validation state shown when no mesh is loaded or the mesh is invalid. */
export type ViewerState =
  | { status: 'empty' }
  | { status: 'loading' }
  | { status: 'loaded'; format: 'stl' | 'glb'; bbox: { x: number; y: number; z: number } }
  | { status: 'error'; message: string };

// ---------------------------------------------------------------------------
// Scene setup (mm scale)
// ---------------------------------------------------------------------------

/** Camera near/far in mm. A 27 mm earring must not clip; a 320 mm plate
 *  must not overflow the far plane. */
const NEAR_MM = 0.1;
const FAR_MM = 100_000;

/** Initial camera distance: ~2× the typical bounding box diagonal. */
const INITIAL_DISTANCE_MM = 200;

/** Orbit control limits in mm. */
const MIN_DISTANCE_MM = 1;
const MAX_DISTANCE_MM = 50_000;

/** Background colour for the viewer canvas. */
const BG_COLOR = 0x1a1a2e;

// ---------------------------------------------------------------------------
// Disposal
// ---------------------------------------------------------------------------

/**
 * Recursively dispose a three.js object's geometry and materials.
 *
 * This is mandatory: STLLoader returns non-indexed BufferGeometry, and six
 * renders × non-indexed meshes accumulate GPU memory. Without explicit
 * disposal, the viewer leaks VRAM on every model swap.
 *
 * `traverse` includes the root object itself, so a top-level mesh (or the
 * loaded scene root for a GLB) has its geometry/materials disposed too —
 * not only the nested children.
 */
export function disposeObject(obj: THREE.Object3D): void {
  obj.traverse((child) => {
    const mesh = child as THREE.Mesh;
    if (mesh.geometry) {
      mesh.geometry.dispose();
    }
    if (mesh.material) {
      if (Array.isArray(mesh.material)) {
        mesh.material.forEach((m: THREE.Material) => m.dispose());
      } else {
        (mesh.material as THREE.Material).dispose();
      }
    }
  });
  // Remove from parent so the renderer stops processing it.
  if (obj.parent) {
    obj.parent.remove(obj);
  }
}

// ---------------------------------------------------------------------------
// Mesh loading
// ---------------------------------------------------------------------------

/**
 * Load a binary STL ArrayBuffer into a three.js Object3D.
 *
 * STLLoader returns non-indexed BufferGeometry. For multi-solid ASCII STL
 * the geometry has multiple groups (`geometry.groups.length > 1`); each
 * group gets its own material so the groups are visually distinct.
 *
 * The mesh is treated as millimetres — no scaling is applied.
 */
export async function loadSTL(
  buffer: ArrayBuffer,
  camera?: THREE.Camera,
): Promise<LoadResult> {
  try {
    const loader = new STLLoader();
    const geometry = loader.parse(buffer);

    // STLLoader returns non-indexed geometry; centre it at the origin.
    geometry.center();

    // Determine if this is a multi-solid STL (multiple geometry groups).
    const groupCount = geometry.groups.length;

    let object: THREE.Object3D;

    if (groupCount > 1) {
      // Multi-solid: create a Group, one mesh per group.
      // For non-indexed geometry, groups are ranges of the position
      // attribute. Each group becomes a sub-mesh with its own material
      // so the solids are visually distinct.
      const group = new THREE.Group();
      const posAttr = geometry.getAttribute('position');
      const normAttr = geometry.getAttribute('normal');
      for (let i = 0; i < groupCount; i++) {
        const g = geometry.groups[i];
        const subGeoSlice = new THREE.BufferGeometry();
        if (posAttr) {
          const arr = posAttr.array as Float32Array;
          const sub = arr.slice(g.start, g.start + g.count);
          subGeoSlice.setAttribute(
            'position',
            new THREE.BufferAttribute(sub, 3),
          );
        }
        if (normAttr) {
          const arr = normAttr.array as Float32Array;
          const sub = arr.slice(g.start, g.start + g.count);
          subGeoSlice.setAttribute(
            'normal',
            new THREE.BufferAttribute(sub, 3),
          );
        }
        const mat = new THREE.MeshStandardMaterial({
          color: new THREE.Color().setHSL(i / groupCount, 0.6, 0.5),
        });
        const mesh = new THREE.Mesh(subGeoSlice, mat);
        group.add(mesh);
      }
      object = group;
    } else {
      const material = new THREE.MeshStandardMaterial({
        color: 0x8899cc,
        metalness: 0.1,
        roughness: 0.8,
      });
      object = new THREE.Mesh(geometry, material);
    }

    const box = new THREE.Box3().setFromObject(object);
    const size = box.getSize(new THREE.Vector3());

    // Z-up source → Y-up scene (applyZUpToYUp): model's +Z → scene's +Y.
    if (camera) applyZUpToYUp(object, camera);

    return {
      ok: true,
      mesh: { object, format: 'stl' },
      boundingBox: { x: size.x, y: size.y, z: size.z },
    };
  } catch (e) {
    return {
      ok: false,
      error: e instanceof Error ? e.message : String(e),
    };
  }
}

// ---------------------------------------------------------------------------
// Coordinate convention (Z-up source data → Y-up scene)
// ---------------------------------------------------------------------------

/**
 * Rotate a Z-up source model into the viewer's Y-up scene.
 *
 * OpenSCAD and STL are Z-up: +Z is the model's "up" (CAD convention). three.js
 * scenes are Y-up, so a Z-up model must be rotated -PI/2 about X to display
 * correctly (model's +Z → scene's +Y). We also set the camera's up axis to +Z
 * so OrbitControls orbits around the model's true vertical — without that the
 * damped orbit axis would be the model's front-back axis. (Equivalently: the
 * viewport top of the screen is the model's top.)
 *
 * Applied to the loaded object root (not per-mesh) so the multi-solid group
 * and single mesh both end up oriented the same way. The scene camera itself
 * is NOT rotated — the camera is the viewer's coordinate frame.
 */
export function applyZUpToYUp(object: THREE.Object3D, camera: THREE.Camera): void {
  object.rotation.set(-Math.PI / 2, 0, 0);
  camera.up.set(0, 0, 1);
}

/**
 * Load a binary GLB ArrayBuffer into a three.js Object3D.
 *
 * GLB is a binary container for glTF; GLTFLoader.parse handles the
 * binary directly. The loaded scene is treated as millimetres.
 */
export async function loadGLB(
  buffer: ArrayBuffer,
  camera?: THREE.Camera,
): Promise<LoadResult> {
  try {
    const loader = new GLTFLoader();
    const gltf = await new Promise<{ scene: THREE.Scene }>((resolve, reject) => {
      loader.parse(buffer, '', (g) => resolve(g as unknown as { scene: THREE.Scene }), reject);
    });

    // Centre the scene at the origin.
    const box = new THREE.Box3().setFromObject(gltf.scene);
    const center = box.getCenter(new THREE.Vector3());
    gltf.scene.position.sub(center);

    const size = box.getSize(new THREE.Vector3());

    // Z-up source → Y-up scene (applyZUpToYUp): model's +Z → scene's +Y.
    if (camera) applyZUpToYUp(gltf.scene, camera);

    return {
      ok: true,
      mesh: { object: gltf.scene, format: 'glb' },
      boundingBox: { x: size.x, y: size.y, z: size.z },
    };
  } catch (e) {
    return {
      ok: false,
      error: e instanceof Error ? e.message : String(e),
    };
  }
}

/**
 * Load a mesh from an ArrayBuffer, dispatching by format.
 */
export async function loadMesh(
  buffer: ArrayBuffer,
  format: 'stl' | 'glb',
  camera?: THREE.Camera,
): Promise<LoadResult> {
  return format === 'stl' ? loadSTL(buffer, camera) : loadGLB(buffer, camera);
}

// ---------------------------------------------------------------------------
// Single-click region picking (#98)
// ---------------------------------------------------------------------------

/** A 2-D point in on-screen viewport CSS-pixel coordinates (not photo
 *  pixels, not NDC, not drawing-buffer pixels — the same space the pick
 *  layer records via `getBoundingClientRect()`, so the raycast matches
 *  what the user actually saw). */
export interface ScreenPoint {
  x: number;
  y: number;
}

/** Result of resolving a single click against the loaded model meshes.
 *  `hit` is true only when the ray landed on geometry; `module` is the
 *  named module under the click when one resolves (a bonus — its absence
 *  never disables selection, e.g. a streamed unnamed STL). */
export interface PointPickResult {
  /** Whether the click landed on any loaded geometry. */
  hit: boolean;
  /** The named module identifier under the click, or null when the
   *  nearest hit is unnamed (streamed STL) or nothing was hit. */
  module: string | null;
}

/**
 * Resolve a single click (screen-space CSS pixels) to a point pick, per
 * #98's client-side picking contract:
 *
 *   1. Convert the CSS-pixel coordinate to NDC (-1..1, Y flipped) for
 *      `Raycaster.setFromCamera`. `viewportWidth/viewportHeight` MUST be
 *      the viewport's CSS-pixel size (e.g. `renderer.getSize(new
 *      Vector2())`), not the drawing-buffer size — the pointer event
 *      coordinates are CSS pixels regardless of `devicePixelRatio`.
 *   2. Cast ONE ray via `raycaster.intersectObjects([sceneRoot], true)`.
 *      three.js returns hits nearest-first — the first hit is exactly what
 *      is visible at that pixel (z-buffer occlusion with no extra work).
 *   3. `hit` is true iff at least one geometry hit exists. Unnamed
 *      geometry is still a hit — a streamed STL (one unnamed mesh) must
 *      select fine; the module-name bonus is simply absent.
 *   4. `module` is the nearest hit object's `name` when non-empty (the
 *      named-module GLB path), else null. Resolution is via `object.name`
 *      only — never per-face/per-vertex colour (the server-side
 *      OpenSCAD colour-ID pass is dead per spec).
 *
 * To let the user "hide the front module and re-select" (the spec's
 * documented remedy for occlusion, since server-side disocclusion is a
 * rabbit hole), set `object.visible = false` on the mesh first —
 * `intersectObjects` already skips invisible objects.
 */
export function resolvePointPick(
  point: ScreenPoint,
  viewportWidth: number,
  viewportHeight: number,
  camera: THREE.Camera,
  raycaster: THREE.Raycaster,
  sceneRoot: THREE.Object3D,
): PointPickResult {
  // Convert screen-space CSS-pixel coords to NDC (-1..1, Y flipped) for
  // Raycaster.setFromCamera.
  const ndcX = (point.x / viewportWidth) * 2 - 1;
  const ndcY = -(point.y / viewportHeight) * 2 + 1;
  raycaster.setFromCamera(new THREE.Vector2(ndcX, ndcY), camera);

  const hits = raycaster.intersectObjects([sceneRoot], true);
  if (hits.length === 0) return { hit: false, module: null };

  // Nearest-first is three.js's contract for intersectObjects — only the
  // first (nearest) hit is "visible" at this pixel.
  const name = hits[0].object.name;
  return { hit: true, module: name || null };
}

// ---------------------------------------------------------------------------
// Viewer hook
// ---------------------------------------------------------------------------

export interface ModelViewerHandle {
  /** The three.js scene. */
  scene: THREE.Scene;
  /** The perspective camera. */
  camera: THREE.PerspectiveCamera;
  /** The WebGL renderer. */
  renderer: THREE.WebGLRenderer;
  /** The OrbitControls instance. */
  controls: OrbitControls;
  /** A pre-configured Raycaster for region picking (#98). */
  raycaster: THREE.Raycaster;
  /** The root of the loaded model in the scene (the pick raycast target),
   *  or null while no model is loaded. Updated on every load/swap. */
  modelRoot: THREE.Object3D | null;
}

interface ModelViewerProps {
  /** The ArrayBuffer to load (STL or GLB binary). */
  data: ArrayBuffer | null;
  /** The format of the data. */
  format: 'stl' | 'glb';
  /** Called when the mesh is loaded and displayed. */
  onLoaded?: (result: LoadResult) => void;
  /** Called when a load error occurs. */
  onError?: (message: string) => void;
  /** Called when the viewer is ready (scene/camera/renderer available),
   *  and again whenever the loaded model root changes. */
  onReady?: (handle: ModelViewerHandle) => void;
  /** Called ONCE per orbit gesture, at the gesture's start — the moment
   *  OrbitControls begins moving the camera, BEFORE the pose has crossed
   *  any clear threshold (issue #129). The region pin uses this to
   *  desaturate "about to go" while the user can still stop, rather than
   *  announcing its clearance afterwards. Fires from OrbitControls'
   *  `start` event; the `end`/`changed` events are left alone. */
  onOrbitStart?: () => void;
  /** Suppress the "nothing yet" empty-state overlay. The App sets this
   *  from its isFirstRun boolean (issue #208) so the overlay and the
   *  FirstRun card do not both centre themselves in the same viewport.
   *  The viewer stays mounted regardless — only this overlay is gated. */
  hideEmptyState?: boolean;
}

/**
 * React component that renders a three.js viewer with OrbitControls.
 *
 * The component:
 *  - Creates a WebGL renderer, scene, camera, and OrbitControls on mount.
 *  - Loads the provided STL/GLB data and displays it at 1:1 mm scale.
 *  - Disposes the previous mesh on swap.
 *  - Exposes the scene/camera/renderer/raycaster/modelRoot via `onReady`
 *    for #98's single-click region picking.
 *  - Shows an empty state (background + message) when no mesh is loaded.
 *  - Shows an error state when loading fails.
 *  - Sizes itself FLUIDLY from its containing box (issue #119): there is
 *    deliberately NO width/height prop — a component silently falling back
 *    to a fixed default is the pick-drift bug wearing a parameter. The
 *    containing box comes from the stage (position:absolute; inset:0), so
 *    the ResizeObserver's box IS the viewer's box. `renderer.getSize(new
 *    Vector2())` on the handle is THE single source of the CSS-pixel
 *    viewport size that the pick path reads.
 */
export function ModelViewer({ data, format, onLoaded, onError, onReady, onOrbitStart, hideEmptyState }: ModelViewerProps) {
  const containerRef = useRef<HTMLDivElement | null>(null);
  const rendererRef = useRef<THREE.WebGLRenderer | null>(null);
  const sceneRef = useRef<THREE.Scene | null>(null);
  const cameraRef = useRef<THREE.PerspectiveCamera | null>(null);
  const controlsRef = useRef<OrbitControls | null>(null);
  const currentMeshRef = useRef<THREE.Object3D | null>(null);
  const raycasterRef = useRef<THREE.Raycaster | null>(null);
  const animationFrameRef = useRef<number>(0);
  const readyRef = useRef(false);
  // Marks an unmounted component so a late ResizeObserver callback (which
  // fires asynchronously and can still be queued at unmount) never re-publishes.
  const unmountRef = useRef(false);
  // Latest onOrbitStart — held in a ref so the mount effect's OrbitControls
  // `start` listener always invokes the current callback without re-binding.
  const onOrbitStartRef = useRef<ModelViewerProps['onOrbitStart']>(onOrbitStart);
  onOrbitStartRef.current = onOrbitStart;
  // Latest onReady — held in a ref so the mesh-swap effect (whose deps are
  // only [data, format]) can re-notify without re-running the mount effect.
  const onReadyRef = useRef<ModelViewerProps['onReady']>(onReady);
  onReadyRef.current = onReady;

  // --- Scene setup (on mount) ---
  useEffect(() => {
    const container = containerRef.current;
    if (!container) return;

    // Renderer. No fixed size: the fluid box arrives from the ResizeObserver
    // below (issue #119) — if the first observation has not fired yet the
    // renderer stays at a zero size and is UNPUBLISHED to the pick path
    // (onReady waits for a non-zero box), so no pick can ever divide by a
    // zero or stale viewport.
    const renderer = new THREE.WebGLRenderer({ antialias: true, alpha: false });
    renderer.setPixelRatio(window.devicePixelRatio || 1);
    renderer.setClearColor(BG_COLOR);
    container.appendChild(renderer.domElement);
    rendererRef.current = renderer;

    // Scene
    const scene = new THREE.Scene();
    scene.background = new THREE.Color(BG_COLOR);
    sceneRef.current = scene;

    // Camera (mm scale). Aspect 1 until the first non-zero observation —
    // the ResizeObserver sets the real aspect before the handle publishes.
    const camera = new THREE.PerspectiveCamera(50, 1, NEAR_MM, FAR_MM);
    camera.position.set(0, INITIAL_DISTANCE_MM * 0.5, INITIAL_DISTANCE_MM);
    camera.lookAt(0, 0, 0);
    cameraRef.current = camera;

    // OrbitControls (mm scale)
    const controls = new OrbitControls(camera, renderer.domElement);
    controls.enableDamping = true;
    controls.dampingFactor = 0.1;
    controls.minDistance = MIN_DISTANCE_MM;
    controls.maxDistance = MAX_DISTANCE_MM;
    controlsRef.current = controls;

    // The gesture-start seam (issue #129): the region pin must desaturate
    // at the first pose move of a gesture, before POSE_EPS_MM is crossed.
    // OrbitControls fires `start` at exactly that moment. The callback is
    // held in a ref so a re-render never loses the listener; the listener is
    // always registered so OrbitControls' event plumbing is uniform.
    const startHandler = () => onOrbitStartRef.current?.();
    controls.addEventListener('start', startHandler);

    // Raycaster (exposed for #98's single-click picking)
    const raycaster = new THREE.Raycaster();
    raycaster.far = FAR_MM;
    raycasterRef.current = raycaster;

    // Lights
    const ambientLight = new THREE.AmbientLight(0xffffff, 0.4);
    scene.add(ambientLight);
    const dirLight = new THREE.DirectionalLight(0xffffff, 0.8);
    dirLight.position.set(100, 200, 150);
    scene.add(dirLight);
    const dirLight2 = new THREE.DirectionalLight(0xffffff, 0.3);
    dirLight2.position.set(-100, 50, -100);
    scene.add(dirLight2);

    // Animation loop
    let mounted = true;
    const animate = () => {
      if (!mounted) return;
      controls.update();
      renderer.render(scene, camera);
      animationFrameRef.current = requestAnimationFrame(animate);
    };
    animate();

    // The handle publishes ONCE a non-zero viewport box exists (see the
    // ResizeObserver below) — never from this mount effect. Publishing a
    // zero-size renderer would let a pick divide by zero.
    readyRef.current = true;

    return () => {
      mounted = false;
      cancelAnimationFrame(animationFrameRef.current);
      controls.removeEventListener('start', startHandler);
      // Dispose the current mesh
      if (currentMeshRef.current) {
        disposeObject(currentMeshRef.current);
        currentMeshRef.current = null;
      }
      // Dispose remaining scene objects (lights, etc.)
      scene.traverse((child) => {
        const obj = child as unknown as { geometry?: { dispose: () => void }; material?: THREE.Material | THREE.Material[] };
        if (obj.geometry) obj.geometry.dispose();
        if (obj.material) {
          if (Array.isArray(obj.material)) {
            obj.material.forEach((m: THREE.Material) => m.dispose());
          } else {
            (obj.material as THREE.Material).dispose();
          }
        }
      });
      controls.dispose();
      renderer.dispose();
      if (renderer.domElement.parentNode) {
        renderer.domElement.parentNode.removeChild(renderer.domElement);
      }
      readyRef.current = false;
    };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  // --- Mesh loading (on data change) ---
  useEffect(() => {
    const scene = sceneRef.current;
    const camera = cameraRef.current;
    const renderer = rendererRef.current;
    if (!scene || !camera || !renderer) return;

    // Dispose previous mesh
    if (currentMeshRef.current) {
      disposeObject(currentMeshRef.current);
      currentMeshRef.current = null;
    }

    if (!data) {
      // No data: show empty state (background only). Keep the handle's
      // modelRoot in sync so #98's pick layer knows no model is loaded.
      if (
        onReadyRef.current &&
        rendererRef.current &&
        sceneRef.current &&
        cameraRef.current &&
        controlsRef.current &&
        raycasterRef.current
      ) {
        onReadyRef.current({
          scene: sceneRef.current,
          camera: cameraRef.current,
          renderer: rendererRef.current,
          controls: controlsRef.current,
          raycaster: raycasterRef.current,
          modelRoot: null,
        });
      }
      return;
    }

    // Pass the scene camera so loaders can align Z-up source data to the
    // Y-up scene (applyZUpToYUp sets the object rotation AND camera.up).
    loadMesh(data, format, camera).then((result) => {
      if (result.ok && result.mesh) {
        scene.add(result.mesh.object);
        currentMeshRef.current = result.mesh.object;

        // Frame the camera on the mesh
        const box = new THREE.Box3().setFromObject(result.mesh.object);
        const center = box.getCenter(new THREE.Vector3());
        const size = box.getSize(new THREE.Vector3());
        const maxDim = Math.max(size.x, size.y, size.z);
        const distance = maxDim * 2.5;

        camera.position.set(center.x, center.y + distance * 0.5, center.z + distance);
        camera.lookAt(center);
        cameraRef.current = camera;

        // Publish the new model root so the App's pick layer can flip
        // picking to "live" and re-notify with the raycast target.
        if (
          onReadyRef.current &&
          rendererRef.current &&
          sceneRef.current &&
          cameraRef.current &&
          controlsRef.current &&
          raycasterRef.current
        ) {
          onReadyRef.current({
            scene: sceneRef.current,
            camera: cameraRef.current,
            renderer: rendererRef.current,
            controls: controlsRef.current,
            raycaster: raycasterRef.current,
            modelRoot: result.mesh.object,
          });
        }

        if (onLoaded) onLoaded(result);
      } else if (result.error) {
        if (onError) onError(result.error);
      }
    });
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [data, format]);

  // --- Resize handling (issue #119 — the fluid sizing path) ---
  // The ResizeObserver is the SOLE sizing path: on each non-zero box it
  // calls renderer.setSize(w, h) AND camera.aspect = w/h +
  // updateProjectionMatrix(). There is no fallback to a fixed width/height
  // prop (that constant is gone) and a ZERO box — the observation that
  // fires during mount, or when the element is display:none — is skipped
  // entirely: never renderer.setSize(0, 0), never a divide-by-zero aspect.
  // The handle is published from this same callback, so the pick path can
  // only ever observe a renderer whose getSize() matches the on-screen box.
  //
  // devicePixelRatio: the ratio is applied once at mount. It changes when
  // the window moves between displays; this viewer deliberately does NOT
  // re-apply it (tracked as an explicit non-goal of issue #119 — the pick
  // path is DPR-independent because it always reads the CSS-pixel size).
  useEffect(() => {
    const container = containerRef.current;
    if (!container) return;

    let published = false;
    const publish = () => {
      if (published || unmountRef.current) return;
      published = true;
      if (
        onReadyRef.current &&
        rendererRef.current &&
        sceneRef.current &&
        cameraRef.current &&
        controlsRef.current &&
        raycasterRef.current
      ) {
        onReadyRef.current({
          scene: sceneRef.current,
          camera: cameraRef.current,
          renderer: rendererRef.current,
          controls: controlsRef.current,
          raycaster: raycasterRef.current,
          modelRoot: null,
        });
      }
    };

    const handleResize = () => {
      if (unmountRef.current) return; // late async observation after unmount
      const w = container.clientWidth;
      const h = container.clientHeight;
      if (w === 0 || h === 0) return; // zero box: skip, never setSize(0,0)
      const renderer = rendererRef.current;
      const camera = cameraRef.current;
      if (!renderer || !camera) return;
      renderer.setSize(w, h);
      camera.aspect = w / h;
      camera.updateProjectionMatrix();
      publish();
    };

    // A box may already be laid out at mount; cover that without a timer.
    handleResize();

    const observer = new ResizeObserver(handleResize);
    observer.observe(container);
    return () => observer.disconnect();
  }, []);

  // --- Unmount safety: the unmount cleanup must never re-publish ---
  useEffect(() => {
    return () => {
      unmountRef.current = true;
    };
  }, []);

  // The container IS the stage's fluid box (position:absolute; inset:0 —
  // see App.tsx): 100% of the containing block, in both axes. There is no
  // pixel literal here on purpose (issue #119: the pick layer derives its
  // box from this same element, so a fixed size anywhere would be a second
  // size constant).
  return (
    <div
      ref={containerRef}
      role="img"
      aria-label="3D model viewer"
      style={{
        width: '100%',
        height: '100%',
        position: 'absolute',
        inset: 0,
        overflow: 'hidden',
      }}
    >
      {/* Empty state overlay (issue #107): shown only while no model data is
          mounted — the "nothing yet" state. It is distinct from the working
          state (the design-loop progress surface) and from a failure (the
          selection notice), so none of the three reads as another. */}
      {!data && !hideEmptyState && (
        <div
          data-testid="viewer-empty"
          role="status"
          style={{
            position: 'absolute',
            top: '50%',
            left: '50%',
            transform: 'translate(-50%, -50%)',
            color: '#888',
            fontSize: '14px',
            pointerEvents: 'none',
            textAlign: 'center',
          }}
        >
          {copy.shell.viewerEmpty}
        </div>
      )}
    </div>
  );
}

export default ModelViewer;
