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

import { describe, it, expect, vi, beforeAll, afterEach } from 'vitest';
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
  const mockObject3D = class {
    children: unknown[] = [];
    parent: unknown = null;
    position = {
      x: 0,
      y: 0,
      z: 0,
      set(x: number, y: number, z: number) {
        this.x = x;
        this.y = y;
        this.z = z;
        return this;
      },
    };
    rotation = {
      x: 0,
      y: 0,
      z: 0,
      set(x: number, y: number, z: number) {
        this.x = x;
        this.y = y;
        this.z = z;
        return this;
      },
    };
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
    toNonIndexed = vi.fn(function (this: InstanceType<typeof mockGeometry>) {
      return this;
    });
    // Real BufferGeometry.dispose is a plain method; the test asserts on the
    // spy attached below (vi.spyOn), so the base implementation is a no-op.
    dispose() {}
    getAttribute = vi.fn((_name: string) => null);
  };
  const mockMaterial = class {
    // Real Material.dispose is a plain method; the test asserts on the spy
    // attached below (vi.spyOn), so the base implementation is a no-op.
    dispose() {}
  };
  const mockMesh = class extends mockObject3D {
    geometry: InstanceType<typeof mockGeometry>;
    material: InstanceType<typeof mockMaterial> | InstanceType<typeof mockMaterial>[];
    constructor(geo: unknown, mat: unknown) {
      super();
      this.geometry = geo as InstanceType<typeof mockGeometry>;
      this.material = mat as InstanceType<typeof mockMaterial> | InstanceType<typeof mockMaterial>[];
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
    up = { x: 0, y: 1, z: 0 };
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
    domElement = (typeof document !== 'undefined'
      ? document.createElement('canvas')
      : { parentNode: null }) as unknown as { parentNode: unknown };
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
    setHSL = vi.fn(function (this: InstanceType<typeof mockColor>) {
      return this;
    });
  };
  const mockBox3 = class {
    setFromObject = vi.fn(function (this: InstanceType<typeof mockBox3>) {
      return this;
    });
    getCenter = vi.fn((_target: unknown) => ({ x: 0, y: 0, z: 0 }));
    getSize = vi.fn((_target: unknown) => ({ x: 10, y: 10, z: 10 }));
  };
  const mockVector3 = class {
    x = 0;
    y = 0;
    z = 0;
    sub = vi.fn(function (
      this: InstanceType<typeof mockVector3>,
      other: { x: number; y: number; z: number },
    ) {
      this.x -= other.x;
      this.y -= other.y;
      this.z -= other.z;
      return this;
    });
  };
  const mockVector2 = class {
    x: number;
    y: number;
    constructor(x = 0, y = 0) {
      this.x = x;
      this.y = y;
    }
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
    Vector2: mockVector2,
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
    addEventListener: vi.fn(),
    removeEventListener: vi.fn(),
  })),
}));

// Mirror the real three.js loader behaviour: binary STL / GLB parse bytewise
// against the data, so a too-short / non-GLB buffer actually fails instead of
// silently resolving (the old mocks always succeeded, which masked the
// component's catch path). A valid fixture is the committed binary file itself
// (mini-box.stl / mini-model.glb), which passes the header check.
vi.mock('three/addons/loaders/STLLoader.js', () => ({
  STLLoader: vi.fn().mockImplementation(() => ({
    parse: vi.fn((buffer: ArrayBuffer) => {
      // Header/format validation mirrors the real STLLoader.isBinary exactly:
      // binary STL must match 80-byte header + face-count + face array, ASCII
      // must start with 'solid' (≤5-byte BOM prefix). Anything else (e.g. a
      // 16-byte buffer) throws 'Unrecognized STL file format', exactly like
      // production behaviour.
      const reader = new DataView(buffer);
      const faceSize = (32 / 8) * 3 + (32 / 8) * 3 * 3 + 16 / 8;
      const nFaces = reader.byteLength >= 84 ? reader.getUint32(80, true) : 0;
      // Real STLLoader.isBinary: expect = 80 (header) + 4 (face count) + n*faceSize.
      const isBinary = 80 + 32 / 8 + nFaces * faceSize === reader.byteLength;
      const solid = [115, 111, 108, 105, 100];
      let isAscii = false;
      if (!isBinary) {
        for (let off = 0; off < 5 && !isAscii; off++) {
          isAscii = solid.every((q, i) => reader.getUint8(off + i) === q);
        }
      }
      if (!isBinary && !isAscii) {
        throw new Error('THREE.STLLoader: Unrecognized STL file format.');
      }
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
      (
        buffer: ArrayBuffer,
        _path: string,
        resolve: (g: unknown) => void,
        reject: (e: unknown) => void,
      ) => {
        // Mirror the real GLTFLoader validation: a GLB starts with the
        // 0x46546c67 'glTF' magic (12-byte header minimum); anything else
        // is routed through JSON.parse and fails via onError. A 16-byte
        // buffer (the invalid-data test) is neither → rejects, exactly like
        // production behaviour.
        const GLB_MAGIC = 0x46546c67; // 'glTF'
        if (buffer.byteLength < 12) {
          reject(new Error('THREE.GLTFLoader: Unsupported glTF-Binary header.'));
          return;
        }
        const magic = new DataView(buffer).getUint32(0, true);
        if (magic !== GLB_MAGIC) {
          reject(
            new SyntaxError(
              'THREE.GLTFLoader: Invalid JSON: could not parse document.',
            ),
          );
          return;
        }
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
  applyZUpToYUp,
  resolvePointPick,
  ModelViewer,
} from '../ModelViewer';
import copy from '../../../copy';

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
      if (!result.ok || !result.mesh) {
        throw new Error(`STL load failed: ${result.error}`);
      }

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
      if (!result.ok || !result.mesh) {
        throw new Error(`GLB load failed: ${result.error}`);
      }

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
      const { BufferGeometry, MeshStandardMaterial, Mesh, Group } = require('three');
      const geo = new BufferGeometry();
      const mat = new MeshStandardMaterial({});
      const mesh = new Mesh(geo, mat);
      const group = new Group();
      group.add(mesh);

      // dispose() is a plain method in the mock (not a spy); attach spies so
      // the assertion below can verify the component actually called it.
      const geoSpy = vi.spyOn(geo, 'dispose');
      const matSpy = vi.spyOn(mat, 'dispose');

      disposeObject(group);

      expect(geoSpy).toHaveBeenCalled();
      expect(matSpy).toHaveBeenCalled();
    });

    it('removes the object from its parent', () => {
      const { BufferGeometry, MeshStandardMaterial, Mesh, Group } = require('three');
      const geo = new BufferGeometry();
      const mat = new MeshStandardMaterial({});
      const mesh = new Mesh(geo, mat);
      const parent = new Group();
      parent.add(mesh);

      expect(parent.children).toContain(mesh);

      disposeObject(mesh);

      expect(parent.children).not.toContain(mesh);
    });

    it('disposes materials in an array', () => {
      const { BufferGeometry, MeshStandardMaterial, Mesh } = require('three');
      const geo = new BufferGeometry();
      const mat1 = new MeshStandardMaterial({});
      const mat2 = new MeshStandardMaterial({});
      const mesh = new Mesh(geo, [mat1, mat2]);

      const mat1Spy = vi.spyOn(mat1, 'dispose');
      const mat2Spy = vi.spyOn(mat2, 'dispose');

      disposeObject(mesh);

      expect(mat1Spy).toHaveBeenCalled();
      expect(mat2Spy).toHaveBeenCalled();
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

  // --- Y-up convention ---
  //
  // The issue spec ("3MF is Z-up; three.js is Y-up... it needs
  // rotation.set(-Math.PI/2, 0, 0)") prescribes the rotation for an INLINE
  // 3MF — but 3MF is download-only in this ticket and there is no 3MF loader
  // here. For STL/GLB the component is the one deciding what is displayed:
  // OpenSCAD/STL models are Z-up (CAD convention: +Z is the model's "top")
  // and the scene is Y-up, so the CORRECT behavior is to rotate the root
  // -PI/2 about X (and set camera.up to +Z so OrbitControls' up axis matches
  // the model's up). The old test asserted the rotation's absence — that was
  // the test being wrong, so it is fixed to assert the rotation IS applied
  // (and that it is applied on the object root, not per-mesh).

  describe('Y-up convention (Z-up STL/GLB → Y-up scene)', () => {
    it('applyZUpToYUp rotates the object -PI/2 about X and sets camera.up to +Z', () => {
      const { Mesh, PerspectiveCamera } = require('three');
      const obj = new Mesh(new (require('three').BufferGeometry)(), new (require('three').MeshStandardMaterial)({}));
      const camera = new PerspectiveCamera(50, 1, 1, 100);

      applyZUpToYUp(obj, camera);

      // Object rotated -PI/2 about X (Z-up → Y-up).
      expect(obj.rotation.x).toBeCloseTo(-Math.PI / 2);
      expect(obj.rotation.y).toBeCloseTo(0);
      expect(obj.rotation.z).toBeCloseTo(0);
      // Camera up axis set to +Z (model's up).
      expect(camera.up.x).toBeCloseTo(0);
      expect(camera.up.y).toBeCloseTo(0);
      expect(camera.up.z).toBeCloseTo(1);
    });

    it('applies the Z-up → Y-up rotation when loading an STL (camera passed)', async () => {
      const THREE = require('three');
      const camera = new THREE.PerspectiveCamera(50, 1, 1, 100);
      const buf = readFixture('mini-box.stl');
      const result = await loadSTL(buf, camera);

      if (!result.ok || !result.mesh) {
        throw new Error(`STL load failed: ${result.error}`);
      }
      expect(result.ok).toBe(true);
      const rotation = (result.mesh!.object as unknown as { rotation: { x: number } })
        .rotation;
      expect(rotation.x).toBeCloseTo(-Math.PI / 2);
      // Camera's up axis set to +Z (model's up → viewport top).
      expect((camera.up as { z: number }).z).toBeCloseTo(1);
    });

    it('applies the Z-up → Y-up rotation when loading a GLB (camera passed)', async () => {
      const THREE = require('three');
      const camera = new THREE.PerspectiveCamera(50, 1, 1, 100);
      const buf = readFixture('mini-model.glb');
      const result = await loadGLB(buf, camera);

      if (!result.ok || !result.mesh) {
        throw new Error(`GLB load failed: ${result.error}`);
      }
      expect(result.ok).toBe(true);
      const rotation = (result.mesh!.object as unknown as { rotation: { x: number } })
        .rotation;
      expect(rotation.x).toBeCloseTo(-Math.PI / 2);
    });

    it('documents the Z-up → Y-up rationale in the component source', () => {
      const source = readFileSync(SOURCE_PATH, 'utf-8');
      expect(source).toContain('Z-up');
      expect(source).toContain('Y-up');
    });
  });

  // --- Empty state ---

  describe('empty state (no model loaded)', () => {
    it('renders the "nothing yet" overlay (issue #107) when data is null', () => {
      const source = readFileSync(SOURCE_PATH, 'utf-8');
      // The overlay's copy lives in copy.ts (copy.shell.viewerEmpty) and is
      // pinned against that single source here.
      expect(source).toContain('copy.shell.viewerEmpty');
      expect(source).toContain('data-testid="viewer-empty"');
    });
  });


    // DOM presence/absence of the overlay (issue #208): the source-string
    // assertion above could not detect the FirstRun/empty-state overlap —
    // these render the real component under the three.js mocks and query
    // the testid in the DOM (jsdom has no layout engine, so this is a
    // presence/absence check, not a bounding-box overlap check).
    //
    // This suite lives in a .ts file (no JSX), so the render calls use
    // createElement directly. The imports use require() (not top-level
    // import) to avoid disturbing the vi.mock hoisting that this file
    // depends on (the mock factories reference the top-level vi import,
    // and adding a new top-level import shifts the __vi_import_N__ numbers
    // that the hoisted factories capture).
    describe('overlay DOM presence (issue #208 hideEmptyState)', () => {
      const { createElement } = require('react');
      const { render, screen, cleanup } = require('@testing-library/react');

      afterEach(() => {
        cleanup();
      });

      it('renders the overlay when data is null and hideEmptyState is unset', () => {
        render(createElement(ModelViewer, { data: null, format: 'stl' }));
        expect(screen.queryByTestId('viewer-empty')).not.toBeNull();
        expect(screen.getByTestId('viewer-empty').textContent).toBe(
          copy.shell.viewerEmpty,
        );
      });

      it('renders the overlay when data is null and hideEmptyState is false', () => {
        render(createElement(ModelViewer, { data: null, format: 'stl', hideEmptyState: false }));
        expect(screen.queryByTestId('viewer-empty')).not.toBeNull();
      });

      it('suppresses the overlay when data is null and hideEmptyState is true', () => {
        render(createElement(ModelViewer, { data: null, format: 'stl', hideEmptyState: true }));
        expect(screen.queryByTestId('viewer-empty')).toBeNull();
        // The viewer element itself stays mounted (issue #208: suppression
        // is a render-branch on the overlay, never an unmount of the viewer).
        expect(screen.getByRole('img', { name: '3D model viewer' })).not.toBeNull();
      });

      it('stays suppressed across re-renders and reappears when the flag flips back', () => {
        const view = render(
          createElement(ModelViewer, { data: null, format: 'stl', hideEmptyState: true }),
        );
        expect(screen.queryByTestId('viewer-empty')).toBeNull();
        // No one-way latch: suppression is a live function of the prop, so
        // it re-exposes the overlay the moment the flag clears (the
        // createProject-rejection re-exposure path, issue #208).
        view.rerender(
          createElement(ModelViewer, { data: null, format: 'stl', hideEmptyState: false }),
        );
        expect(screen.queryByTestId('viewer-empty')).not.toBeNull();
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

// ---------------------------------------------------------------------------
// Single-click region picking (#98)
// ---------------------------------------------------------------------------

/** A minimal fake THREE.Raycaster/Camera pair for resolvePointPick
 *  unit tests — these test the pick CONTRACT (hit only on geometry,
 *  nearest-hit module bonus, no-.colour structural), not the three.js
 *  internals (which are exercised by the mocked module above for the
 *  loader/disposal tests). */
function makeFakeRaycaster(
  hits: { object: { name: string } }[],
) {
  return {
    setFromCamera: vi.fn(),
    intersectObjects: vi.fn(() => hits),
  };
}

describe('resolvePointPick', () => {
  it('returns hit:false and null module when the click misses all geometry', () => {
    const raycaster = makeFakeRaycaster([]);
    const camera = {} as unknown as import('three').Camera;
    const result = resolvePointPick(
      { x: 10, y: 10 },
      200,
      200,
      camera,
      raycaster as unknown as import('three').Raycaster,
      {} as unknown as import('three').Object3D,
    );
    expect(result.hit).toBe(false);
    expect(result.module).toBeNull();
  });

  it('hits unnamed geometry (a streamed STL) with a null module bonus — selection is never blocked by an absent name', () => {
    const raycaster = makeFakeRaycaster([{ object: { name: '' } }]);
    const camera = {} as unknown as import('three').Camera;
    const result = resolvePointPick(
      { x: 100, y: 100 },
      200,
      200,
      camera,
      raycaster as unknown as import('three').Raycaster,
      {} as unknown as import('three').Object3D,
    );
    expect(result.hit).toBe(true);
    expect(result.module).toBeNull();
  });

  it('returns the NEAREST hit\'s name as the module bonus (occlusion for free)', () => {
    // Two overlapping modules; nearest-first order means only the first
    // element counts — the occluded one is invisible at that pixel.
    const raycaster = makeFakeRaycaster([
      { object: { name: 'curl_4' } }, // nearest
      { object: { name: 'ear_wire' } }, // occluded behind curl_4
    ]);
    const camera = {} as unknown as import('three').Camera;
    const result = resolvePointPick(
      { x: 100, y: 100 },
      200,
      200,
      camera,
      raycaster as unknown as import('three').Raycaster,
      {} as unknown as import('three').Object3D,
    );
    expect(result.hit).toBe(true);
    expect(result.module).toBe('curl_4');
  });

  it('casts through the live camera/viewport: setFromCamera is called with the NDC-converted point', () => {
    const raycaster = makeFakeRaycaster([{ object: { name: '' } }]);
    const camera = {} as unknown as import('three').Camera;
    resolvePointPick(
      { x: 50, y: 50 },
      200,
      100,
      camera,
      raycaster as unknown as import('three').Raycaster,
      {} as unknown as import('three').Object3D,
    );
    expect(raycaster.setFromCamera).toHaveBeenCalledTimes(1);
    const ndc = raycaster.setFromCamera.mock.calls[0]![0] as unknown as { x: number; y: number };
    // x=50 of 200 → NDC -0.5 (left quarter); y=50 of 100 (centre) → NDC 0
    // (Y flipped: centre stays at 0).
    expect(ndc.x).toBeCloseTo(-0.5);
    expect(ndc.y).toBeCloseTo(0);
  });

  it('a click at the SAME FRACTION of the viewport resolves to the same model point at TWO DIFFERENT viewport sizes (issue #119)', () => {
    // The decisive pick test: the pick layer records a CSS-pixel point from
    // its element's getBoundingClientRect(), and resolvePointPick divides by
    // renderer.getSize() — the SAME box (the fluid stage). At two viewport
    // sizes, the same viewport fraction must map to the same NDC ray. If
    // the layer and the renderer ever read DIFFERENT elements (or moments),
    // the fractions would diverge and this test goes red.
    //
    // PROVEN ABLE TO FAIL (issue #119 red-check): the RED state is
    // constructed by pointing the pick layer's box at a DIFFERENT size than
    // the renderer size — i.e. computing the click point with a 600x400
    // box while resolvePointPick divides by a 1600x1000 viewport. That is
    // the old shared-constants bug wearing new clothes, and it makes the
    // two NDC values below disagree. Restored to the shared-box shape it
    // is green again (pinned in this suite).
    const camera = {} as unknown as import('three').Camera;

    const sizeA = { width: 600, height: 400 };
    const sizeB = { width: 1600, height: 1000 };
    const FRACTION = { x: 0.25, y: 0.5 }; // same fraction, two viewports

    // The pick layer derives its box from the SAME element the renderer
    // sizes itself from — so the click point at fraction f of viewport N is
    // f * N (CSS px, one and the same box).
    const pointA = { x: FRACTION.x * sizeA.width, y: FRACTION.y * sizeA.height };
    const pointB = { x: FRACTION.x * sizeB.width, y: FRACTION.y * sizeB.height };

    const raycasterA = makeFakeRaycaster([{ object: { name: 'module_a' } }]);
    const raycasterB = makeFakeRaycaster([{ object: { name: 'module_b' } }]);

    resolvePointPick(pointA, sizeA.width, sizeA.height, camera, raycasterA as unknown as import('three').Raycaster, {} as unknown as import('three').Object3D);
    resolvePointPick(pointB, sizeB.width, sizeB.height, camera, raycasterB as unknown as import('three').Raycaster, {} as unknown as import('three').Object3D);

    const ndcA = raycasterA.setFromCamera.mock.calls[0]![0] as unknown as { x: number; y: number };
    const ndcB = raycasterB.setFromCamera.mock.calls[0]![0] as unknown as { x: number; y: number };

    // The same viewport fraction must produce the SAME NDC ray in both
    // viewports — the ray (hence the model point it hits) is size-invariant.
    expect(ndcA.x).toBeCloseTo(ndcB.x, 10);
    expect(ndcA.y).toBeCloseTo(ndcB.y, 10);
    // And it must be the NDC the fraction maps to, not a drift artefact.
    // x: 0.25 → -0.5 ; y: 0.5 → 0 (centre, Y-flip invariant).
    expect(ndcA.x).toBeCloseTo(-0.5, 10);
    expect(ndcA.y).toBeCloseTo(0, 10);
  });

  it('never reads per-face/per-vertex colour — resolution is via object.name only', () => {
    // Structural assertion: the function signature never receives a
    // colour buffer/material, and the source never references `.color`
    // in the resolution path — this is the negative test for the dead
    // server-side OpenSCAD colour-ID pass (spec: color() is discarded by
    // --render/STL export and difference() cut faces take the
    // subtracted object's colour, corrupting IDs exactly where picks
    // land).
    const source = readFileSync(SOURCE_PATH, 'utf-8');
    const fnStart = source.indexOf('export function resolvePointPick');
    const fnEnd = source.indexOf('\n// ', fnStart + 1);
    const fnSource = source.slice(fnStart, fnEnd === -1 ? undefined : fnEnd);
    expect(fnSource).not.toMatch(/\.color\b/);
    expect(fnSource).toContain('.name');
  });
});
