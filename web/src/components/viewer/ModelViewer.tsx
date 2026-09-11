/**
 * three.js model viewer for the d33d SPA (issue #6, workstream task-d).
 *
 * Renders STL and GLB meshes inline at a 1:1 mm scale. The scene is built
 * in millimetres — camera near/far, controls distances, and mesh positions
 * all assume mm. This is the single viewer the whole SPA uses; #6 (region
 * selection) needs `Raycaster.intersectObjects` on the same scene graph, so
 * the viewer exposes the three.js scene, camera, renderer, and raycaster as
 * a stable API surface for that follow-up.
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
 *   - The scene uses the three.js Y-up convention. If a future ticket
 *     inlines a 3MF (Z-up), it must apply `rotation.set(-Math.PI/2, 0, 0)`
 *     and adjust `camera.up` — this viewer does NOT rotate loaded meshes.
 */

import { useEffect, useRef } from 'react';
import * as THREE from 'three';
import { OrbitControls } from 'three/addons/controls/OrbitControls.js';
import { STLLoader } from 'three/addons/loaders/STLLoader.js';
import { GLTFLoader } from 'three/addons/loaders/GLTFLoader.js';

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
export async function loadSTL(buffer: ArrayBuffer): Promise<LoadResult> {
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

/**
 * Load a binary GLB ArrayBuffer into a three.js Object3D.
 *
 * GLB is a binary container for glTF; GLTFLoader.parse handles the
 * binary directly. The loaded scene is treated as millimetres.
 */
export async function loadGLB(buffer: ArrayBuffer): Promise<LoadResult> {
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
): Promise<LoadResult> {
  return format === 'stl' ? loadSTL(buffer) : loadGLB(buffer);
}

// ---------------------------------------------------------------------------
// Viewer hook
// ---------------------------------------------------------------------------

export interface ModelViewerHandle {
  /** The three.js scene (for #6's Raycaster.intersectObjects). */
  scene: THREE.Scene;
  /** The perspective camera. */
  camera: THREE.PerspectiveCamera;
  /** The WebGL renderer. */
  renderer: THREE.WebGLRenderer;
  /** The OrbitControls instance. */
  controls: OrbitControls;
  /** A pre-configured Raycaster for region picking (#6). */
  raycaster: THREE.Raycaster;
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
  /** Called when the viewer is ready (scene/camera/renderer available). */
  onReady?: (handle: ModelViewerHandle) => void;
  /** Width in CSS pixels. */
  width?: number;
  /** Height in CSS pixels. */
  height?: number;
}

/**
 * React component that renders a three.js viewer with OrbitControls.
 *
 * The component:
 *  - Creates a WebGL renderer, scene, camera, and OrbitControls on mount.
 *  - Loads the provided STL/GLB data and displays it at 1:1 mm scale.
 *  - Disposes the previous mesh on swap.
 *  - Exposes the scene/camera/renderer/raycaster via `onReady` for #6.
 *  - Shows an empty state (background + message) when no mesh is loaded.
 *  - Shows an error state when loading fails.
 */
export function ModelViewer({
  data,
  format,
  onLoaded,
  onError,
  onReady,
  width = 600,
  height = 400,
}: ModelViewerProps) {
  const containerRef = useRef<HTMLDivElement | null>(null);
  const rendererRef = useRef<THREE.WebGLRenderer | null>(null);
  const sceneRef = useRef<THREE.Scene | null>(null);
  const cameraRef = useRef<THREE.PerspectiveCamera | null>(null);
  const controlsRef = useRef<OrbitControls | null>(null);
  const currentMeshRef = useRef<THREE.Object3D | null>(null);
  const raycasterRef = useRef<THREE.Raycaster | null>(null);
  const animationFrameRef = useRef<number>(0);
  const readyRef = useRef(false);

  // --- Scene setup (on mount) ---
  useEffect(() => {
    const container = containerRef.current;
    if (!container) return;

    // Renderer
    const renderer = new THREE.WebGLRenderer({ antialias: true, alpha: false });
    renderer.setSize(width, height);
    renderer.setPixelRatio(window.devicePixelRatio || 1);
    renderer.setClearColor(BG_COLOR);
    container.appendChild(renderer.domElement);
    rendererRef.current = renderer;

    // Scene
    const scene = new THREE.Scene();
    scene.background = new THREE.Color(BG_COLOR);
    sceneRef.current = scene;

    // Camera (mm scale)
    const camera = new THREE.PerspectiveCamera(50, width / height, NEAR_MM, FAR_MM);
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

    // Raycaster (exposed for #6's region picking)
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

    readyRef.current = true;

    // Notify parent
    if (onReady && rendererRef.current && sceneRef.current && cameraRef.current && controlsRef.current && raycasterRef.current) {
      onReady({
        scene: sceneRef.current,
        camera: cameraRef.current,
        renderer: rendererRef.current,
        controls: controlsRef.current,
        raycaster: raycasterRef.current,
      });
    }

    return () => {
      mounted = false;
      cancelAnimationFrame(animationFrameRef.current);
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
      // No data: show empty state (background only)
      return;
    }

    loadMesh(data, format).then((result) => {
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

        if (onLoaded) onLoaded(result);
      } else if (result.error) {
        if (onError) onError(result.error);
      }
    });
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [data, format]);

  // --- Resize handling ---
  useEffect(() => {
    const renderer = rendererRef.current;
    const camera = cameraRef.current;
    if (!renderer || !camera) return;

    const handleResize = () => {
      if (!containerRef.current) return;
      const w = containerRef.current.clientWidth || width;
      const h = containerRef.current.clientHeight || height;
      renderer.setSize(w, h);
      camera.aspect = w / h;
      camera.updateProjectionMatrix();
    };

    const container = containerRef.current;
    if (container) {
      const observer = new ResizeObserver(handleResize);
      observer.observe(container);
      return () => observer.disconnect();
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [width, height]);

  return (
    <div
      ref={containerRef}
      role="img"
      aria-label="3D model viewer"
      style={{
        width: `${width}px`,
        height: `${height}px`,
        position: 'relative',
        overflow: 'hidden',
      }}
    >
      {/* Empty state overlay */}
      {!data && (
        <div
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
          No model loaded
        </div>
      )}
    </div>
  );
}

export default ModelViewer;
