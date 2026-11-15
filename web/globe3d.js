/* 3D globe view: a spinning Moon with the Sun and Earth in space at their
 * true relative sizes and distances for the selected time.
 *
 * Scale: 1 unit = 1 Moon radius = 1,737.4 km. Earth is then radius 3.67 at
 * 221.3 units (about 384,400 km), and the Sun radius 399 at 86,000 units, so
 * both keep the angular sizes a camera near the Moon would really see - the
 * Sun a small blinding disc, the Earth about 3.6 times wider.
 *
 * Three.js and OrbitControls are fetched once by scripts/fetch_globe.py into
 * web/vendor/ - the app never loads them from a CDN.
 */

import * as THREE from "three";
import { OrbitControls } from "/vendor/OrbitControls.js";

const MOON_R = 1.0;
const EARTH_R = 3.67;          // 6,371 km
const EARTH_D = 221.3;         // mean 384,400 km
const SUN_R = 399.0;           // 696,000 km, true angular size from the Moon
const SUN_D = 86_000.0;        // ~149.6 million km, but only its direction matters
const STAR_R = 300_000.0;      // star shell, beyond everything

/* default sub-points (2026-11-14), so the scene is never uninitialised */
const SUN_LL_DEFAULT = [-1.56, 126.62];
const EARTH_LL_DEFAULT = [4.40, -1.23];

/* unit vector from a (lat, lon) sub-point, matching the server's maths */
function dirFromLL(latDeg, lonDeg) {
  const lat = latDeg * Math.PI / 180;
  const lon = lonDeg * Math.PI / 180;
  return new THREE.Vector3(
    Math.cos(lat) * Math.cos(lon),
    Math.cos(lat) * Math.sin(lon),
    Math.sin(lat)
  );
}

/* a small radial-gradient sprite, for the Sun's glare and nothing else */
function glareSprite(color) {
  const c = document.createElement("canvas");
  c.width = c.height = 128;
  const g = c.getContext("2d");
  const grad = g.createRadialGradient(64, 64, 0, 64, 64, 64);
  grad.addColorStop(0.0, "rgba(255,255,255,0.9)");
  grad.addColorStop(0.25, color + "cc");
  grad.addColorStop(1.0, "rgba(255,255,255,0)");
  g.fillStyle = grad;
  g.fillRect(0, 0, 128, 128);
  const tex = new THREE.CanvasTexture(c);
  tex.colorSpace = THREE.SRGBColorSpace;
  const mat = new THREE.SpriteMaterial({
    map: tex, transparent: true, depthWrite: false, blending: THREE.AdditiveBlending,
  });
  const s = new THREE.Sprite(mat);
  s.scale.setScalar(SUN_R * 6); // glare is a few times wider than the disc
  return s;
}

/* a text label as a billboard sprite; anchored above a body */
function labelSprite(text, color) {
  const pad = 24;
  const c = document.createElement("canvas");
  const g = c.getContext("2d");
  g.font = "700 48px ui-sans-serif, system-ui, sans-serif";
  const w = Math.ceil(g.measureText(text).width) + pad * 2;
  c.width = w;
  c.height = 96;
  const g2 = c.getContext("2d");
  g2.font = "700 48px ui-sans-serif, system-ui, sans-serif";
  g2.textAlign = "center";
  g2.textBaseline = "middle";
  g2.fillStyle = "rgba(8,9,11,0.75)";
  g2.fillText(text, w / 2 + 2, 50);       // shadow for readability
  g2.fillStyle = color;
  g2.fillText(text, w / 2, 48);
  const tex = new THREE.CanvasTexture(c);
  tex.colorSpace = THREE.SRGBColorSpace;
  const mat = new THREE.SpriteMaterial({
    map: tex, transparent: true, depthWrite: false,
  });
  const s = new THREE.Sprite(mat);
  s.userData.aspect = w / 96;
  return s;
}

export class Globe3D {
  constructor(canvas) {
    this.canvas = canvas;
    this.frame = null; // requestAnimationFrame id while visible

    // logarithmic depth: the scene spans 1 to 300,000 units, far past what a
    // plain depth buffer can order without z-fighting
    this.renderer = new THREE.WebGLRenderer({
      canvas, antialias: true, logarithmicDepthBuffer: true,
    });
    this.renderer.toneMapping = THREE.ACESFilmicToneMapping;
    this.renderer.toneMappingExposure = 1.1;

    this.scene = new THREE.Scene();
    this.scene.background = new THREE.Color(0x000000);

    this.camera = new THREE.PerspectiveCamera(50, 1, 0.05, 400_000);
    this.camera.up.set(0, 0, 1);

    this.controls = new OrbitControls(this.camera, canvas);
    this.controls.enableDamping = true;
    this.controls.minDistance = 1.3;
    this.controls.maxDistance = 6000.0; // far enough to fit Moon + Earth
    // slow drift until the user grabs the globe
    this.controls.autoRotate = true;
    this.controls.autoRotateSpeed = 0.6;
    this.controls.addEventListener("start", () => { this.controls.autoRotate = false; });

    // a faint star field, on a shell beyond everything else
    const starCount = 1500;
    const starPos = new Float32Array(starCount * 3);
    for (let i = 0; i < starCount; i++) {
      const u = Math.random() * 2 - 1;
      const phi = Math.random() * Math.PI * 2;
      const s = Math.sqrt(1 - u * u);
      starPos[i * 3] = STAR_R * s * Math.cos(phi);
      starPos[i * 3 + 1] = STAR_R * s * Math.sin(phi);
      starPos[i * 3 + 2] = STAR_R * u;
    }
    const starGeo = new THREE.BufferGeometry();
    starGeo.setAttribute("position", new THREE.BufferAttribute(starPos, 3));
    this.scene.add(
      new THREE.Points(starGeo, new THREE.PointsMaterial({ color: 0xdfe3ea, size: 1.2, sizeAttenuation: false }))
    );

    // the Moon, textured from the local server; plain grey while it loads so
    // a missing texture can never pass for a lit surface
    const moonMat = new THREE.MeshStandardMaterial({ color: 0x8a8a8a, roughness: 1.0 });
    new THREE.TextureLoader().load(
      "/api/globe-texture",
      (tex) => {
        tex.colorSpace = THREE.SRGBColorSpace; // without this the texture looks washed
        tex.anisotropy = 4;
        moonMat.map = tex;
        moonMat.color.set(0xffffff);
        moonMat.needsUpdate = true;
      },
      undefined,
      () => console.warn("3d: moon texture unavailable - run scripts/fetch_globe.py")
    );
    this.moon = new THREE.Mesh(new THREE.SphereGeometry(MOON_R, 96, 96), moonMat);
    this.scene.add(this.moon);

    // south-pole marker: a small white pin with an amber ring on the ground,
    // emissive so it stays visible on the night side
    this.pole = new THREE.Group();
    const pinMat = new THREE.MeshBasicMaterial({ color: 0xffffff });
    const dot = new THREE.Mesh(new THREE.SphereGeometry(0.012, 16, 16), pinMat);
    dot.position.set(0, -1.004, 0);
    const ringMat = new THREE.MeshBasicMaterial({
      color: 0xf5b544, transparent: true, opacity: 0.9, side: THREE.DoubleSide,
    });
    const ring = new THREE.Mesh(new THREE.RingGeometry(0.035, 0.045, 48), ringMat);
    ring.rotation.x = -Math.PI / 2; // lie flat on the surface
    ring.position.set(0, -1.002, 0);
    this.pole.add(dot, ring);
    this.moon.add(this.pole); // child of the Moon so it rotates with it

    // one sunlight, from the Sun's true direction; the night side stays dark
    // like real space, with a whisper of ambient standing in for earthshine
    this.sunLight = new THREE.DirectionalLight(0xffffff, 3.0);
    this.sunLight.target = this.moon;
    this.scene.add(this.sunLight);
    this.scene.add(this.sunLight.target);
    this.scene.add(
      // brighter ambient so the night side reads like a photo of the Moon
      // with earthshine, not pitch black
      new THREE.AmbientLight(0xffffff, 0.28)
    );

    // the Sun itself: a small blinding disc at its real distance, plus glare
    this.sun = new THREE.Mesh(
      new THREE.SphereGeometry(SUN_R, 48, 48),
      new THREE.MeshBasicMaterial({ color: 0xfff4d6 })
    );
    this.sunGlare = glareSprite("#ffe9a8");
    this.scene.add(this.sun, this.sunGlare);
    this.sunLabel = labelSprite("Sun", "#ffe9a8");
    this.scene.add(this.sunLabel);

    // the Earth: lit by the same sunlight, so its phase is real
    this.earth = new THREE.Mesh(
      new THREE.SphereGeometry(EARTH_R, 48, 48),
      new THREE.MeshStandardMaterial({ color: 0x6b93d6, roughness: 0.55 })
    );
    this.scene.add(this.earth);
    this.earthLabel = labelSprite("Earth", "#8fd0ff");
    this.scene.add(this.earthLabel);

    // scratch vector reused each frame to offset labels toward screen-up
    this._labelUp = new THREE.Vector3();

    this.recenter();
    this.update(SUN_LL_DEFAULT, EARTH_LL_DEFAULT);
  }

  show() {
    this.resize();
    if (this.frame === null) this.tick();
  }

  hide() {
    if (this.frame !== null) {
      cancelAnimationFrame(this.frame);
      this.frame = null;
    }
  }

  recenter() {
    // look straight at the lunar south pole: the texture's north is +Y on a
    // default THREE sphere, so the south pole faces -Y
    this.camera.position.set(0, -3.2, 0.001);
    this.camera.up.set(0, 0, 1);
    this.controls.target.set(0, 0, 0);
    this.controls.autoRotate = false; // recentering is an explicit act - stop the drift
    this.controls.update();
  }

  update(sunLL, earthLL) {
    if (sunLL && sunLL.length === 2 && Number.isFinite(sunLL[0]) && Number.isFinite(sunLL[1])) {
      const dir = dirFromLL(sunLL[0], sunLL[1]);
      this.sun.position.copy(dir).multiplyScalar(SUN_D);
      this.sunGlare.position.copy(this.sun.position);
      this.sunLight.position.copy(dir).multiplyScalar(1000);
    }
    if (earthLL && earthLL.length === 2 && Number.isFinite(earthLL[0]) && Number.isFinite(earthLL[1])) {
      this.earth.position.copy(dirFromLL(earthLL[0], earthLL[1])).multiplyScalar(EARTH_D);
    }
  }

  async refreshFromServer(iso) {
    // tiny throwaway render: only the headers matter
    const q = new URLSearchParams({ cx: 0, cy: 0, span: 8_000_000, w: 64, h: 64 });
    if (iso) q.set("t", iso);
    const resp = await fetch("/api/globe?" + q);
    if (!resp.ok) throw new Error("globe " + resp.status);
    const ll = (name) => (resp.headers.get(name) || "").split(",").map(Number);
    this.update(ll("X-Sun-Subpoint"), ll("X-Earth-Subpoint"));
  }

  resize() {
    const w = this.canvas.clientWidth;
    const h = this.canvas.clientHeight;
    // hidden or not laid out yet: retry on the next animation frame rather
    // than handing the renderer a bogus size
    if (!w || !h) return false;
    const d = Math.min(2.5, window.devicePixelRatio || 1);
    this.renderer.setPixelRatio(d);
    this.renderer.setSize(w, h, false);
    this.camera.aspect = w / h;
    this.camera.updateProjectionMatrix();
    return true;
  }

  tick() {
    this.frame = requestAnimationFrame(() => this.tick());
    if (!this.resize()) return;
    this.controls.update();

    // labels ride just above their bodies, toward screen-up so "on top"
    // holds from any orbit angle; constant world size, like real objects
    this._labelUp.set(0, 1, 0).applyQuaternion(this.camera.quaternion);
    const dSun = this.camera.position.distanceTo(this.sun.position);
    this.sunLabel.position.copy(this.sun.position).addScaledVector(this._labelUp, SUN_R * 1.8);
    this.sunLabel.scale.set(SUN_R * 2.2 * this.sunLabel.userData.aspect, SUN_R * 2.2, 1);
    this.sunLabel.visible = dSun > 0 && this.sun.position.lengthSq() > 0;
    const dEarth = this.camera.position.distanceTo(this.earth.position);
    this.earthLabel.position.copy(this.earth.position).addScaledVector(this._labelUp, EARTH_R * 1.8);
    this.earthLabel.scale.set(EARTH_R * 3.0 * this.earthLabel.userData.aspect, EARTH_R * 3.0, 1);
    this.earthLabel.visible = dEarth > 0;

    this.renderer.render(this.scene, this.camera);
  }
}
