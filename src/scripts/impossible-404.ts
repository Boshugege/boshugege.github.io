import * as THREE from "three";
import { ASCII_CHARACTERS, asciiCellSize, createFragments, HOVER_TILT, HOVER_YAW, rotationAt } from "../lib/impossible-404";

const characters = ASCII_CHARACTERS;
let disposeScene: (() => void) | undefined;
let activeRoot: HTMLElement | undefined;

function initialize() {
  const root = document.querySelector<HTMLElement>("[data-impossible-404]");
  if (root && root === activeRoot) return;
  disposeScene?.();
  disposeScene = undefined;
  activeRoot = undefined;
  const container = root?.querySelector<HTMLElement>("[data-404-scene]");
  const canvas = root?.querySelector<HTMLCanvasElement>("[data-404-canvas]");
  if (!root || !container || !canvas) return;
  activeRoot = root;

  let renderer: THREE.WebGLRenderer;
  try {
    renderer = new THREE.WebGLRenderer({ canvas, antialias: false, alpha: false });
  } catch {
    return;
  }

  const controls = root.querySelector<HTMLElement>("[data-404-controls]")!;
  const status = root.querySelector<HTMLElement>("[data-404-status]")!;
  const pause = root.querySelector<HTMLButtonElement>("[data-404-pause]")!;
  const align = root.querySelector<HTMLButtonElement>("[data-404-align]")!;
  const reducedMotion = window.matchMedia("(prefers-reduced-motion: reduce)");
  const scene = new THREE.Scene();
  scene.background = new THREE.Color(0x000000);
  const sculpture = new THREE.Group();
  scene.add(sculpture);
  scene.add(new THREE.AmbientLight(0xffffff, 0.35));
  const key = new THREE.DirectionalLight(0xffffff, 3);
  key.position.set(-3, 5, 8);
  scene.add(key);
  const rim = new THREE.DirectionalLight(0xffffff, 0.8);
  rim.position.set(4, -2, -6);
  scene.add(rim);

  const faceMaterial = new THREE.MeshStandardMaterial({
    color: 0xd0d0d0,
    emissive: 0x707070,
    roughness: 1,
    metalness: 0,
  });
  const sideMaterial = new THREE.MeshStandardMaterial({
    color: 0x686868,
    emissive: 0x303030,
    roughness: 1,
    metalness: 0,
  });
  const materials = [faceMaterial, sideMaterial];
  for (const fragment of createFragments()) {
    const shape = new THREE.Shape();
    fragment.outline.forEach(([x, y], index) => {
      if (index === 0) shape.moveTo(x, y);
      else shape.lineTo(x, y);
    });
    shape.closePath();
    const geometry = new THREE.ExtrudeGeometry(shape, {
      depth: fragment.thickness,
      bevelEnabled: true,
      bevelSize: 0.025,
      bevelThickness: 0.025,
      bevelSegments: 1,
      steps: 1,
      curveSegments: 1,
    });
    const mesh = new THREE.Mesh(geometry, materials);
    mesh.position.set(fragment.x, 0, fragment.z - fragment.thickness / 2);
    sculpture.add(mesh);
  }
  const camera = new THREE.OrthographicCamera(-7, 7, 4, -4, 0.1, 60);
  camera.position.z = 20;
  const target = new THREE.WebGLRenderTarget(1, 1, {
    minFilter: THREE.NearestFilter,
    magFilter: THREE.NearestFilter,
  });

  const atlasCanvas = document.createElement("canvas");
  atlasCanvas.width = characters.length * 24;
  atlasCanvas.height = 32;
  const atlasContext = atlasCanvas.getContext("2d")!;
  atlasContext.fillStyle = "white";
  atlasContext.font = '700 28px Menlo, "SFMono-Regular", Consolas, "Liberation Mono", monospace';
  atlasContext.textAlign = "center";
  atlasContext.textBaseline = "middle";
  [...characters].forEach((character, i) => atlasContext.fillText(character, i * 24 + 12, 16));
  const atlas = new THREE.CanvasTexture(atlasCanvas);
  atlas.minFilter = THREE.LinearFilter;
  atlas.magFilter = THREE.LinearFilter;
  atlas.generateMipmaps = false;

  // Render only atlas glyphs; the cell hash varies density without time-based flicker.
  const asciiMaterial = new THREE.ShaderMaterial({
    uniforms: {
      image: { value: target.texture },
      atlas: { value: atlas },
      grid: { value: new THREE.Vector2(1, 1) },
      paper: { value: new THREE.Color() },
      ink: { value: new THREE.Color() },
      count: { value: characters.length },
    },
    vertexShader: `
      varying vec2 vUv;
      void main() {
        vUv = uv;
        gl_Position = vec4(position.xy, 0.0, 1.0);
      }
    `,
    fragmentShader: `
      uniform sampler2D image;
      uniform sampler2D atlas;
      uniform vec2 grid;
      uniform vec3 paper;
      uniform vec3 ink;
      uniform float count;
      varying vec2 vUv;
      void main() {
        vec2 cell = floor(vUv * grid);
        float light = texture2D(image, (cell + 0.5) / grid).r;
        float surface = step(0.001, light);
        float level = pow(clamp(light * 1.45, 0.0, 1.0), 0.65);
        float grain = fract(sin(dot(cell, vec2(12.9898, 78.233))) * 43758.5453);
        float density = clamp(level + (grain - 0.5) * 0.26, 0.0, 1.0);
        float glyph = floor(2.0 + density * (count - 3.0) + 0.5) * surface;
        vec2 local = fract(vUv * grid);
        float mask = texture2D(atlas, vec2((glyph + local.x) / count, local.y)).a;
        float contrast = surface * (0.24 + 0.76 * level);
        gl_FragColor = vec4(mix(paper, ink, mask * contrast), 1.0);
      }
    `,
    depthTest: false,
    depthWrite: false,
  });
  const screen = new THREE.Scene();
  const quadGeometry = new THREE.PlaneGeometry(2, 2);
  screen.add(new THREE.Mesh(quadGeometry, asciiMaterial));
  const screenCamera = new THREE.Camera();

  let paused = reducedMotion.matches;
  let seconds = 0;
  let previousTime = 0;
  let lastFrame = 0;
  let animation = 0;
  let disposed = false;
  let resetPending = false;
  let aligned = true;
  let renderWidth = 0;
  let renderHeight = 0;
  let pixelRatio = 0;
  const pointer = new THREE.Vector2();
  const offset = new THREE.Vector2();
  const events = new AbortController();

  function render() {
    renderer.setRenderTarget(target);
    renderer.render(scene, camera);
    renderer.setRenderTarget(null);
    renderer.render(screen, screenCamera);
  }

  function setPose() {
    const pose = rotationAt(seconds, offset);
    sculpture.rotation.set(pose.x, pose.y, pose.z);
    aligned = pose.aligned;
    syncStatus();
  }

  function syncStatus() {
    const state = resetPending ? "aligning" : paused ? "paused" : aligned ? "aligned" : "running";
    if (status.dataset.state === state) return;
    status.dataset.state = state;
    status.textContent = state.toUpperCase();
  }

  function frame(now: number) {
    animation = 0;
    if (disposed || document.hidden) return;
    if (now - lastFrame < 1000 / 30) {
      animation = requestAnimationFrame(frame);
      return;
    }
    const delta = previousTime ? Math.min((now - previousTime) / 1000, 0.1) : 0;
    previousTime = now;
    lastFrame = now;
    if (resetPending) {
      const blend = 1 - Math.exp(-delta * 7);
      sculpture.rotation.x *= 1 - blend;
      sculpture.rotation.y = THREE.MathUtils.euclideanModulo(sculpture.rotation.y + Math.PI, Math.PI * 2) - Math.PI;
      sculpture.rotation.y *= 1 - blend;
      sculpture.rotation.z *= 1 - blend;
      if (Math.abs(sculpture.rotation.x) + Math.abs(sculpture.rotation.y) + Math.abs(sculpture.rotation.z) < 0.002) {
        resetPending = false;
        seconds = 0;
        setPose();
      }
    } else if (!paused) {
      seconds += delta;
      offset.lerp(pointer, 1 - Math.exp(-delta * 4));
      setPose();
    }
    render();
    if (!paused || resetPending) animation = requestAnimationFrame(frame);
  }

  function wake() {
    if (animation || disposed || document.hidden) return;
    previousTime = 0;
    animation = requestAnimationFrame(frame);
  }

  function resize() {
    const { width, height } = container!.getBoundingClientRect();
    if (!width || !height) return;
    const ratio = Math.min(window.devicePixelRatio || 1, 2);
    if (width === renderWidth && height === renderHeight && ratio === pixelRatio) return;
    renderWidth = width;
    renderHeight = height;
    pixelRatio = ratio;
    renderer.setPixelRatio(ratio);
    renderer.setSize(width, height, false);
    const cell = asciiCellSize(width);
    const columns = Math.floor(width / cell);
    const rows = Math.floor(height / (cell * 1.6));
    target.setSize(columns, rows);
    asciiMaterial.uniforms.grid.value.set(columns, rows);
    const aspect = width / height;
    const viewHeight = Math.max(6.5, 11.8 / aspect);
    camera.left = -viewHeight * aspect / 2;
    camera.right = viewHeight * aspect / 2;
    camera.top = viewHeight / 2;
    camera.bottom = -viewHeight / 2;
    camera.updateProjectionMatrix();
    if (root!.dataset.ready === "true") render();
  }

  function syncTheme() {
    const style = getComputedStyle(container!);
    // Composite in display space so light and dark terminals keep the same gray separation.
    asciiMaterial.uniforms.paper.value.set(style.getPropertyValue("--terminal-bg").trim()).convertLinearToSRGB();
    asciiMaterial.uniforms.ink.value.set(style.getPropertyValue("--terminal-ink").trim()).convertLinearToSRGB();
    if (root!.dataset.ready === "true") render();
  }

  function syncControls() {
    const label = paused ? "继续旋转" : "暂停旋转";
    pause.setAttribute("aria-label", label);
    pause.setAttribute("aria-pressed", String(paused));
    pause.title = label;
    pause.querySelector("[data-404-pause-label]")!.textContent = label;
    pause.querySelector("[data-404-pause-icon]")!.toggleAttribute("hidden", paused);
    pause.querySelector("[data-404-play-icon]")!.toggleAttribute("hidden", !paused);
    syncStatus();
  }

  function reset() {
    pointer.set(0, 0);
    offset.set(0, 0);
    if (reducedMotion.matches) {
      seconds = 0;
      resetPending = false;
      setPose();
      render();
    } else {
      resetPending = true;
      syncStatus();
      wake();
    }
  }

  pause.addEventListener("click", () => {
    paused = !paused;
    syncControls();
    if (!paused) wake();
  }, { signal: events.signal });
  align.addEventListener("click", reset, { signal: events.signal });
  container.addEventListener("pointermove", (event) => {
    if (paused || reducedMotion.matches || event.pointerType !== "mouse") return;
    const bounds = container.getBoundingClientRect();
    pointer.set(
      ((event.clientX - bounds.left) / bounds.width - 0.5) * 2 * HOVER_YAW,
      ((event.clientY - bounds.top) / bounds.height - 0.5) * 2 * HOVER_TILT,
    );
    wake();
  }, { signal: events.signal });
  container.addEventListener("pointerleave", () => pointer.set(0, 0), { signal: events.signal });
  document.addEventListener("visibilitychange", () => {
    if (document.hidden) {
      cancelAnimationFrame(animation);
      animation = 0;
    } else if (!paused || resetPending) wake();
  }, { signal: events.signal });
  reducedMotion.addEventListener("change", () => {
    paused = reducedMotion.matches;
    reset();
    syncControls();
    if (!paused) wake();
  }, { signal: events.signal });

  const themeObserver = new MutationObserver(syncTheme);
  themeObserver.observe(document.documentElement, { attributes: true, attributeFilter: ["data-theme"] });
  const resizeObserver = new ResizeObserver(resize);
  resizeObserver.observe(container);

  disposeScene = () => {
    if (disposed) return;
    disposed = true;
    cancelAnimationFrame(animation);
    events.abort();
    themeObserver.disconnect();
    resizeObserver.disconnect();
    sculpture.children.forEach((object) => (object as THREE.Mesh).geometry.dispose());
    materials.forEach((material) => material.dispose());
    target.dispose();
    atlas.dispose();
    asciiMaterial.dispose();
    quadGeometry.dispose();
    renderer.dispose();
    activeRoot = undefined;
    root.removeAttribute("data-ready");
    controls.hidden = true;
    status.dataset.state = "static";
    status.textContent = "STATIC";
  };
  canvas.addEventListener("webglcontextlost", () => disposeScene?.(), { signal: events.signal });
  try {
    syncTheme();
    resize();
    syncControls();
    render();
    root.dataset.ready = "true";
    controls.hidden = false;
    if (!paused) wake();
  } catch {
    disposeScene();
  }
}

document.addEventListener("astro:page-load", initialize);
document.addEventListener("astro:before-swap", () => {
  disposeScene?.();
  disposeScene = undefined;
  activeRoot = undefined;
});
initialize();
