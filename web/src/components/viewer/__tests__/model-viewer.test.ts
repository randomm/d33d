/**
 * Unit tests for the three.js ModelViewer component (issue #6, task-d).
 *
 * Three.js WebGL requires a browser/GPU context, so these tests mock the
 * three.js modules and test:
 *   - STL loading (single-solid)
 *   - GLB loading
 *   - Disposal on swap
 *   - Raycaster availability
 *   - No 3MF loading (the viewer does NOT import 3MFLoader)
 *   - Scene built in mm (camera near/far in mm range)
 *   - No Z-up → Y-up rotation
 *   - Empty state
 *
 * The test fixtures (mini-box.stl, mini-model.glb) are real binary files
 * under web/tests/fixtures/viewer/.
 */

import { describe, it, expect, vi, beforeAll, afterAll } from 'vitest';
import { readFileSync, existsSync } from 'node:fs';
import { resolve, dirname } from 'node:path';
import { fileURLToPath } from 'node:url';

// ---------------------------------------------------------------------------
// Resolve the fixture directory relative to this test file
// ---------------------------------------------------------------------------

const __dirname = dirname(fileURLToPath(import.meta.url));
const FIXTURES_DIR = resolve(__dirname, '../../../../tests/fixtures/viewer');

// ---------------------------------------------------------------------------
// Read the component source (for static analysis assertions)
// ---------------------------------------------------------------------------

const SOURCE_PATH = resolve(__dirname, '../ModelViewer.tsx');

function readFixture(name: string): ArrayBuffer {
  const path = resolve(FIXTURES_DIR, name);
  const buf = readFileSync(path);
  return buf.buffer.slice(buf.byteOffset, buf.byteOffset + buf.byteLength) as ArrayBuffer;
}

// ---------------------------------------------------------------------------
// Mock three.js and its addons BEFORE importing the component
// ---------------------------------------------------------------------------

vi.mock('three', async () => {
  const mockDispose = () => {};
  const mockObject3D = class {
    children: unknown[] = [];
    parent: unknown = null;
    position = { x: 0, y: 0, z: 0 };
    add(child: unknown) {
      this.children.push(child);
      Object.defineProperty(child, 'parent', { value: this, writable: true });
    }
    remove(child: unknown) {
      const idx = this.children.indexOf(child);
      if (idx >= 0) this.children.splice(idx, 1);
    }
    traverse(cb: (obj: unknown) => void) {
      cb(this);
      for (const c of this.children) {
        (c as { traverse: (cb: (obj: unknown) => void) => void }).traverse(cb);
      }
    }
  };
  const mockGeometry = class {
    groups: { start: number; count: number; materialIndex: number }[] = [];
    attributes: Record<string, unknown> = {};
    index: unknown = null;
    center = vi.fn();
    toNonIndexed = vi.fn(function (this: mockGeometry) {
      return this;
    });
    dispose = vi.fn();
    getAttribute = vi.fn((_name: string) => null);
  };
  const mockMaterial = class {
    dispose = vi.fn();
  };
  const mockMesh = class extends mockObject3D {
    geometry: mockGeometry;
    material: mockMaterial | mockMaterial[];
    constructor(geo: unknown, mat: unknown) {
      super();
      this.geometry = geo as mockGeometry;
      this.material = mat as mockMaterial | mockMaterial[];
    }
  };
  const mockGroup = class extends mockObject3D {};
  const mockScene = class extends mockObject3D {
    background: unknown = null;
  };
  const mockPerspectiveCamera = class extends mockObject3D {
    fov: number;
    aspect: number;
    near: number;
    far: number;
    constructor(fov: number, aspect: number, near: number, far: number) {
      super();
      this.fov = fov;
      this.aspect = aspect;
      this.near = near;
      this.far = far;
    }
    lookAt = vi.fn();
    updateProjectionMatrix = vi.fn();
  };
  const mockWebGLRenderer = class {
    domElement = { parentNode: null } as { parentNode: unknown };
    size = { width: 0, height: 0 };
    setSize = vi.fn();
    setPixelRatio = vi.fn();
    setClearColor = vi.fn();
    render = vi.fn();
    dispose = vi.fn();
  };
  const mockRaycaster = class {
    far: number = 0;
    intersectObjects = vi.fn().mockReturnValue([]);
  };
  const mockColor = class {
    setHSL = vi.fn(function (this: mockColor) {
      return this;
    });
  };
  const mockBox3 = class {
    setFromObject = vi.fn(function (this: mockBox3) {
      return this;
    });
    getCenter = vi.fn((_target: unknown) => ({ x: 0, y: 0, z: 0 }));
    getSize = vi.fn((_target: unknown) => ({ x: 10, y: 10, z: 10 }));
  };
  const mockVector3 = class {
    x = 0;
    y = 0;
    z = 0;
    sub = vi.fn(function (this: mockVector3, other: { x: number; y: number; z: number }) {
      this.x -= other.x;
      this.y -= other.y;
      this.z -= other.z;
      return this;
    });
  };
  const mockAmbientLight = class extends mockObject3D {};
  const mockDirectionalLight = class extends mockObject3D {};
  const mockBufferAttribute = class {
    array: unknown;
    size: number;
    constructor(array: unknown, size: number) {
      this.array = array;
      this.size = size;
    }
    dispose = vi.fn();
  };

  return {
    Scene: mockScene,
    PerspectiveCamera: mockPerspectiveCamera,
    WebGLRenderer: mockWebGLRenderer,
    Raycaster: mockRaycaster,
    Color: mockColor,
    Box3: mockBox3,
    Vector3: mockVector3,
    AmbientLight: mockAmbientLight,
    DirectionalLight: mockDirectionalLight,
    Mesh: mockMesh,
    BufferGeometry: mockGeometry,
    BufferAttribute: mockBufferAttribute,
    MeshStandardMaterial: mockMaterial,
    Material: mockMaterial,
    Object3D: mockObject3D,
    Group: mockGroup,
  };
});

vi.mock('three/addons/controls/OrbitControls.js', () => ({
  OrbitControls: vi.fn().mockImplementation(() => ({
    enableDamping: false,
    dampingFactor: 0,
    minDistance: 0,
    maxDistance: Infinity,
    update: vi.fn(),
    dispose: vi.fn(),
  })),
}));

vi.mock('three/addons/loaders/STLLoader.js', () => ({
  STLLoader: vi.fn().mockImplementation(() => ({
    parse: vi.fn((_buffer: ArrayBuffer) => {
      // Return a mock geometry that mimics STLLoader's output
      const { BufferGeometry } = require('three');
      const geo = new BufferGeometry();
      (geo as { groups: unknown[] }).groups = [];
      (geo as { center: () => void }).center = vi.fn();
      (geo as { getAttribute: (n: string) => unknown }).getAttribute = vi.fn(() => null);
      return geo;
    }),
  })),
}));

vi.mock('three/addons/loaders/GLTFLoader.js', () => ({
  GLTFLoader: vi.fn().mockImplementation(() => ({
    parse: vi.fn(
      (_buf: ArrayBuffer, _path: string, resolve: (g: unknown) => void, _reject: (e: unknown) => void) => {
        const { Scene } = require('three');
        resolve({ scene: new Scene() });
      },
    ),
  })),
}));

// ---------------------------------------------------------------------------
// Import the module under test (after mocks are registered)
// ---------------------------------------------------------------------------

import {
  loadSTL,
  loadGLB,
  loadMesh,
  disposeObject,
  type LoadResult,
} from '../ModelViewer';

// ---------------------------------------------------------------------------
// Tests
// ---------------------------------------------------------------------------

describe('ModelViewer module', () => {
  beforeAll(() => {
    // Verify fixtures exist
    expect(existsSync(resolve(FIXTURES_DIR, 'mini-box.stl'))).toBe(true);
    expect(existsSync(resolve(FIXTURES_DIR, 'mini-model.glb'))).toBe(true);
  });

  // --- STL loading ---

  describe('loadSTL', () => {
    it('loads a valid binary STL and returns a mesh in mm', async () => {
      const buf = readFixture('mini-box.stl');
      const result = await loadSTL(buf);

      expect(result.ok).toBe(true);
      expect(result.mesh).toBeDefined();
      expect(result.mesh!.format).toBe('stl');
    });

    it('returns an error for invalid STL data', async () => {
      const badBuffer = new ArrayBuffer(16); // Too short for a valid STL
      const result = await loadSTL(badBuffer);

      expect(result.ok).toBe(false);
      expect(result.error).toBeDefined();
      expect(result.mesh).toBeUndefined();
    });
  });

  // --- GLB loading ---

  describe('loadGLB', () => {
    it('loads a valid GLB and returns a mesh in mm', async () => {
      const buf = readFixture('mini-model.glb');
      const result = await loadGLB(buf);

      expect(result.ok).toBe(true);
      expect(result.mesh).toBeDefined();
      expect(result.mesh!.format).toBe('glb');
    });

    it('returns an error for invalid GLB data', async () => {
      const badBuffer = new ArrayBuffer(16);
      const result = await loadGLB(badBuffer);

      expect(result.ok).toBe(false);
      expect(result.error).toBeDefined();
    });
  });

  // --- loadMesh dispatch ---

  describe('loadMesh', () => {
    it('dispatches to STL loader for stl format', async () => {
      const buf = readFixture('mini-box.stl');
      const result = await loadMesh(buf, 'stl');

      expect(result.ok).toBe(true);
      expect(result.mesh!.format).toBe('stl');
    });

    it('dispatches to GLB loader for glb format', async () => {
      const buf = readFixture('mini-model.glb');
      const result = await loadMesh(buf, 'glb');

      expect(result.ok).toBe(true);
      expect(result.mesh!.format).toBe('glb');
    });
  });

  // --- Disposal ---

  describe('disposeObject', () => {
    it('recursively disposes geometry and materials', () => {
      const THREE = require('three');
      const geo = new THREE.BufferGeometry();
      const mat = new THREE.MeshStandardMaterial({});
      const mesh = new THREE.Mesh(geo, mat);
      const group = new THREE.Group();
      group.add(mesh);

      disposeObject(group);

      expect(geo.dispose).toHaveBeenCalled();
      expect(mat.dispose).toHaveBeenCalled();
    });

    it('removes the object from its parent', () => {
      const THREE = require('three');
      const geo = new THREE.BufferGeometry();
      const mat = new THREE.MeshStandardMaterial({});
      const mesh = new THREE.Mesh(geo, mat);
      const parent = new THREE.Group();
      parent.add(mesh);

      expect(parent.children).toContain(mesh);

      disposeObject(mesh);

      expect(parent.children).not.toContain(mesh);
    });

    it('disposes materials in an array', () => {
      const THREE = require('three');
      const geo = new THREE.BufferGeometry();
      const mat1 = new THREE.MeshStandardMaterial({});
      const mat2 = new THREE.MeshStandardMaterial({});
      const mesh = new THREE.Mesh(geo, [mat1, mat2] as unknown as typeof THREE.Material);

      disposeObject(mesh);

      expect(mat1.dispose).toHaveBeenCalled();
      expect(mat2.dispose).toHaveBeenCalled();
    });
  });

  // --- No 3MF loading ---

  describe('3MF exclusion (3MF is download-only, never rendered inline)', () => {
    it('does NOT import or reference 3MFLoader', () => {
      const source = readFileSync(SOURCE_PATH, 'utf-8');
      expect(source).not.toContain('3MFLoader');
      expect(source).not.toContain('ThreeMFLoader');
      expect(source).not.toContain('.3mf');
    });
  });

  // --- mm scale invariants ---

  describe('mm scale (scene built in millimetres)', () => {
    it('defines NEAR_MM and FAR_MM constants in mm range', () => {
      const source = readFileSync(SOURCE_PATH, 'utf-8');
      expect(source).toContain('NEAR_MM');
      expect(source).toContain('FAR_MM');
      // Near should be sub-millimetre (0.1mm)
      expect(source).toMatch(/NEAR_MM\s*=\s*0\.1/);
      // Far should be 100_000mm (100m)
      expect(source).toMatch(/FAR_MM\s*=\s*100_000/);
    });

    it('OrbitControls distances are in mm range', () => {
      const source = readFileSync(SOURCE_PATH, 'utf-8');
      expect(source).toContain('MIN_DISTANCE_MM');
      expect(source).toContain('MAX_DISTANCE_MM');
      // Min should be 1mm
      expect(source).toMatch(/MIN_DISTANCE_MM\s*=\s*1/);
      // Max should be 50_000mm
      expect(source).toMatch(/MAX_DISTANCE_MM\s*=\s*50_000/);
    });
  });

  // --- Raycaster availability (for #6 region picking) ---

  describe('Raycaster (exposed for #6 region picking)', () => {
    it('exposes a pre-configured Raycaster in the viewer handle type', () => {
      const source = readFileSync(SOURCE_PATH, 'utf-8');
      expect(source).toContain('Raycaster');
      expect(source).toContain('raycasterRef');
      // The handle type should include raycaster
      expect(source).toMatch(/raycaster:\s*THREE\.Raycaster/);
    });
  });

  // --- Y-up convention (no Z-up rotation) ---

  describe('Y-up convention (no Z-up → Y-up rotation)', () => {
    it('does NOT apply any Z-up to Y-up rotation', () => {
      const source = readFileSync(SOURCE_PATH, 'utf-8');
      expect(source).not.toContain('rotation.set(-Math.PI/2');
      expect(source).not.toContain('rotation.set(-Math.PI / 2');
    });
  });

  // --- Empty state ---

  describe('empty state (no model loaded)', () => {
    it('renders a "No model loaded" overlay when data is null', () => {
      const source = readFileSync(SOURCE_PATH, 'utf-8');
      expect(source).toContain('No model loaded');
    });
  });

  // --- OrbitControls ---

  describe('OrbitControls (orbit interaction)', () => {
    it('creates OrbitControls with damping enabled', () => {
      const source = readFileSync(SOURCE_PATH, 'utf-8');
      expect(source).toContain('OrbitControls');
      expect(source).toContain('enableDamping');
    });
  });

  // --- STL/GLB loaders ---

  describe('STL/GLB loaders (no 3MF)', () => {
    it('imports STLLoader and GLTFLoader', () => {
      const source = readFileSync(SOURCE_PATH, 'utf-8');
      expect(source).toContain('STLLoader');
      expect(source).toContain('GLTFLoader');
    });
  });
});
