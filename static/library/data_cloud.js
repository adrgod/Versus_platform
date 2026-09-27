import * as THREE from "three";
import { CSS3DObject, CSS3DRenderer } from "./vendor/CSS3DRenderer.js";

let activeStage = null;
let cleanupActiveCloud = null;
let suppressClickUntil = 0;

function mountDataCloud() {
  const stage = document.querySelector("[data-data-cloud]");
  if (stage === activeStage) return;
  cleanupActiveCloud?.();
  cleanupActiveCloud = null;
  activeStage = stage;
  if (!stage) return;

  const anchors = [...stage.querySelectorAll("[data-cloud-tag]")];
  if (!anchors.length) return;

  const renderer = new CSS3DRenderer();
  renderer.domElement.className = "data-cloud-renderer";
  stage.append(renderer.domElement);

  const scene = new THREE.Scene();
  const camera = new THREE.PerspectiveCamera(42, 1, 1, 2000);
  camera.position.z = 790;
  const sphere = new THREE.Group();
  scene.add(sphere);

  const radius = 245;
  anchors.forEach((anchor, index) => {
    const tag = new CSS3DObject(anchor);
    if (anchors.length === 2) {
      const direction = index === 0 ? -1 : 1;
      tag.position.set(direction * 165, index === 0 ? -34 : 42, index === 0 ? 175 : -145);
    } else {
      const phi = Math.acos(1 - (2 * (index + 0.5)) / anchors.length);
      const theta = Math.PI * (1 + Math.sqrt(5)) * index;
      tag.position.setFromSphericalCoords(radius, phi, theta);
    }
    sphere.add(tag);
  });

  const resize = () => {
    const width = Math.max(stage.clientWidth, 280);
    const height = Math.max(stage.clientHeight, 320);
    camera.aspect = width / height;
    camera.position.z = width < 620 ? 680 : 790;
    camera.updateProjectionMatrix();
    renderer.setSize(width, height);
  };
  const resizeObserver = new ResizeObserver(resize);
  resizeObserver.observe(stage);
  resize();

  let dragging = false;
  let pointerId = null;
  let startX = 0;
  let startY = 0;
  let previousX = 0;
  let previousY = 0;
  let moved = false;
  let pointerTarget = null;
  let hoverPaused = false;
  let angularVelocity = 0.0011;
  let animationFrame = 0;

  const pointerDown = (event) => {
    if (event.button !== undefined && event.button !== 0) return;
    dragging = true;
    pointerId = event.pointerId;
    startX = previousX = event.clientX;
    startY = previousY = event.clientY;
    moved = false;
    angularVelocity = 0;
    pointerTarget = event.target.closest?.("[data-cloud-tag]") || stage;
    stage.classList.add("is-dragging");
    if (pointerTarget.setPointerCapture) pointerTarget.setPointerCapture(pointerId);
  };

  const pointerMove = (event) => {
    if (!dragging || event.pointerId !== pointerId) return;
    const deltaX = event.clientX - previousX;
    const deltaY = event.clientY - previousY;
    if (Math.abs(event.clientX - startX) + Math.abs(event.clientY - startY) > 5) moved = true;
    sphere.rotation.y += deltaX * 0.006;
    sphere.rotation.x = THREE.MathUtils.clamp(sphere.rotation.x + deltaY * 0.005, -0.95, 0.95);
    angularVelocity = deltaX * 0.00008;
    previousX = event.clientX;
    previousY = event.clientY;
    if (moved) event.preventDefault();
  };

  const pointerUp = (event) => {
    if (!dragging || event.pointerId !== pointerId) return;
    dragging = false;
    stage.classList.remove("is-dragging");
    if (moved) suppressClickUntil = Date.now() + 450;
    if (pointerTarget?.hasPointerCapture?.(event.pointerId)) {
      pointerTarget.releasePointerCapture(event.pointerId);
    }
    pointerId = null;
    pointerTarget = null;
  };

  const pauseRotation = () => { hoverPaused = true; };
  const resumeRotation = () => { hoverPaused = false; };

  stage.addEventListener("pointerdown", pointerDown);
  stage.addEventListener("pointermove", pointerMove);
  stage.addEventListener("pointerup", pointerUp);
  stage.addEventListener("pointercancel", pointerUp);
  stage.addEventListener("pointerenter", pauseRotation);
  stage.addEventListener("pointerleave", resumeRotation);

  const preventDragClick = (event) => {
    if (Date.now() <= suppressClickUntil && stage.contains(event.target)) {
      event.preventDefault();
      event.stopImmediatePropagation();
      suppressClickUntil = 0;
    }
  };
  document.addEventListener("click", preventDragClick, true);

  const animate = () => {
    animationFrame = requestAnimationFrame(animate);
    if (!dragging && !hoverPaused) {
      sphere.rotation.y += angularVelocity || 0.00022;
      angularVelocity *= 0.96;
      if (Math.abs(angularVelocity) < 0.00012) angularVelocity = 0.00022;
    }
    sphere.updateMatrixWorld(true);
    for (const object of sphere.children) {
      object.quaternion.copy(sphere.quaternion).invert();
      const worldPosition = object.getWorldPosition(new THREE.Vector3());
      const depth = THREE.MathUtils.clamp((worldPosition.z + radius) / (radius * 2), 0, 1);
      object.element.style.opacity = String(0.4 + depth * 0.6);
      object.element.style.zIndex = String(Math.round(depth * 100));
    }
    renderer.render(scene, camera);
  };
  animate();

  stage.classList.add("is-active");
  stage.setAttribute("aria-label", "Drag to rotate the data fields, then open one");

  cleanupActiveCloud = () => {
    cancelAnimationFrame(animationFrame);
    resizeObserver.disconnect();
    stage.removeEventListener("pointerdown", pointerDown);
    stage.removeEventListener("pointermove", pointerMove);
    stage.removeEventListener("pointerup", pointerUp);
    stage.removeEventListener("pointercancel", pointerUp);
    stage.removeEventListener("pointerenter", pauseRotation);
    stage.removeEventListener("pointerleave", resumeRotation);
    document.removeEventListener("click", preventDragClick, true);
    renderer.domElement.remove();
    for (const anchor of anchors) stage.querySelector(".data-cloud-labels")?.append(anchor);
    stage.classList.remove("is-active", "is-dragging");
    stage.removeAttribute("aria-label");
  };
}

window.addEventListener("library:page-updated", mountDataCloud);
mountDataCloud();