// Adaptive Climate - UI shell (vanilla JS, no build step).
// STATUS: skeleton. Wires tab switching and a health poll; the five
// views (Now / Analysis / Almanac / Config / Log) are Phases 12-15.

const view = document.getElementById("view");
const strip = document.getElementById("status-strip");

document.querySelectorAll("#tabs button").forEach((b) => {
  b.addEventListener("click", () => {
    document.querySelectorAll("#tabs button").forEach((x) => x.classList.remove("active"));
    b.classList.add("active");
    view.innerHTML = `<p class="placeholder">"${b.dataset.view}" view — not yet built.</p>`;
  });
});

async function poll() {
  try {
    const r = await fetch("/healthz");
    const j = await r.json();
    strip.textContent = j.idle ? "idle (no rooms)"
      : j.ha_connected ? "connected" : "degraded";
  } catch {
    strip.textContent = "unreachable";
  }
}
poll();
setInterval(poll, 15000);
