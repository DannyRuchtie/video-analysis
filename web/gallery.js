export const byId = (id) => document.getElementById(id);
const strip = byId("strip");
const tooltip = byId("strip-tooltip");
const gallery = byId("gallery");
const dialog = byId("lightbox");

let current;
let activeShot = 0;

function element(tag, className, text) {
  const node = document.createElement(tag);
  if (className) node.className = className;
  if (text) node.textContent = text;
  return node;
}

function clock(seconds, precision = 0) {
  const factor = 10 ** precision;
  const total = Math.round(seconds * factor);
  const hours = Math.floor(total / (factor * 3600));
  const minutes = Math.floor(total / (factor * 60)) % 60;
  const wholeSeconds = Math.floor(total / factor) % 60;
  let label = (hours ? String(hours).padStart(2, "0") + ":" : "")
    + String(minutes).padStart(2, "0") + ":" + String(wholeSeconds).padStart(2, "0");
  if (precision) label += "." + String(total % factor).padStart(precision, "0");
  return label;
}

function listNumber(shot) {
  return String(shot.number).padStart(2, "0");
}

function swatch(color) {
  const chip = element("span", "swatch");
  chip.style.setProperty("--color", color);
  chip.setAttribute("aria-hidden", "true");
  return chip;
}

export async function json(url, options) {
  const response = await fetch(url, options);
  const payload = await response.json();
  if (!response.ok) throw new Error(payload.error || "The request failed.");
  return payload;
}

function showTooltip(target, shot) {
  tooltip.textContent = "Shot " + listNumber(shot) + " · " + clock(shot.start, 2) + "  " + shot.color;
  tooltip.hidden = false;
  const stripBox = strip.getBoundingClientRect();
  const segmentBox = target.getBoundingClientRect();
  const centered = segmentBox.left - stripBox.left + (segmentBox.width - tooltip.offsetWidth) / 2;
  const left = Math.max(0, Math.min(centered, stripBox.width - tooltip.offsetWidth));
  tooltip.style.left = left + "px";
}

function hideTooltip() {
  tooltip.hidden = true;
}

function segmentFor(shot, index) {
  const segment = element("button", "segment");
  segment.type = "button";
  segment.style.setProperty("--duration", shot.end - shot.start);
  segment.style.setProperty("--color", shot.color);
  segment.setAttribute("aria-label", "Shot " + listNumber(shot) + ", " + clock(shot.start, 2) + ", " + shot.color);
  segment.tabIndex = index === 0 ? 0 : -1;
  segment.addEventListener("mouseenter", () => showTooltip(segment, shot));
  segment.addEventListener("mouseleave", () => { if (document.activeElement !== segment) hideTooltip(); });
  segment.addEventListener("focus", () => showTooltip(segment, shot));
  segment.addEventListener("blur", hideTooltip);
  segment.addEventListener("click", () => openShot(index));
  return segment;
}

function shotCard(shot, index, total) {
  const button = element("button", "shot-card");
  button.type = "button";
  button.setAttribute("aria-label", "Open shot " + shot.number + " of " + total + " at " + clock(shot.start, 2));
  const image = element("img");
  image.alt = "";
  image.loading = index < 6 ? "eager" : "lazy";
  image.decoding = "async";
  image.src = current.base + shot.thumbnail;
  const caption = element("span", "shot-caption");
  caption.append(
    element("span", "", listNumber(shot)),
    element("span", "", clock(shot.start, 2) + " – " + clock(shot.end, 2)),
  );

  button.append(image, caption);
  button.addEventListener("click", () => openShot(index));
  return button;
}

function downloadName(suffix) {
  const title = current.title.replace(/[\\/:*?"<>|]/g, "-").trim().slice(0, 150) || "video";
  return title + "-" + suffix;
}

function shotTotal(count) {
  return count + (count === 1 ? " shot" : " shots");
}

export async function renderAnalysis(url) {
  const resolved = new URL(url, location.href);
  const data = await json(resolved);
  current = { ...data, base: new URL(".", resolved).href };
  activeShot = 0;
  const { title, duration, shots } = current;
  byId("video-title").textContent = title;
  byId("video-meta").textContent = clock(duration, duration < 10 ? 1 : 0) + " · " + shotTotal(shots.length);
  const watch = byId("watch-video");
  watch.hidden = !current.url;
  watch.href = current.url || "";
  const colors = byId("download-colors");
  colors.href = current.base + "color-strip.png";
  colors.download = downloadName("color-strip.png");
  strip.replaceChildren(...shots.map(segmentFor));
  gallery.replaceChildren(...shots.map((shot, index) => shotCard(shot, index, shots.length)));
  const precision = duration < 4 ? (duration < 0.4 ? 2 : 1) : 0;
  byId("ticks").replaceChildren(...Array.from({ length: 5 }, (_, tick) =>
    element("span", "tick", clock(duration * tick / 4, precision))
  ));
  hideTooltip();
  byId("analysis").hidden = false;
  byId("loading").hidden = true;
}

function openShot(index) {
  const { shots, base } = current;
  if (index < 0 || index >= shots.length) return;

  const tabStop = strip.querySelector('[tabindex="0"]');
  if (tabStop) tabStop.tabIndex = -1;
  strip.children[activeShot].classList.remove("current");
  gallery.children[activeShot].classList.remove("current");
  activeShot = index;
  strip.children[index].tabIndex = 0;
  strip.children[index].classList.add("current");
  gallery.children[index].classList.add("current");

  const shot = shots[index];
  byId("lightbox-title").textContent = "Shot " + listNumber(shot) + " / " + shots.length;
  byId("lightbox-frame-time").textContent = "Frame at " + clock(shot.screenshot_time, 2);
  const image = byId("lightbox-image");
  image.src = base + shot.image;
  image.alt = "Screenshot from shot " + shot.number;
  byId("lightbox-color").replaceChildren(swatch(shot.color), document.createTextNode(shot.color));

  const frame = byId("download-frame");
  frame.href = base + shot.image;
  frame.download = downloadName("shot-" + listNumber(shot) + ".jpg");
  byId("previous").disabled = index === 0;
  byId("next").disabled = index === shots.length - 1;
  hideTooltip();
  if (!dialog.open) dialog.showModal();
}

strip.addEventListener("keydown", (event) => {
  const items = Array.from(strip.children);
  const index = items.indexOf(event.target);
  if (index < 0) return;
  const destinations = { ArrowLeft: index - 1, ArrowRight: index + 1, Home: 0, End: items.length - 1 };
  if (!Object.hasOwn(destinations, event.key)) return;
  event.preventDefault();
  const next = items[destinations[event.key]];
  if (next) {
    const tabStop = strip.querySelector('[tabindex="0"]');
    if (tabStop) tabStop.tabIndex = -1;
    next.tabIndex = 0;
    next.focus();
  }
});
byId("close-lightbox").addEventListener("click", () => dialog.close());
byId("previous").addEventListener("click", () => openShot(activeShot - 1));
byId("next").addEventListener("click", () => openShot(activeShot + 1));
dialog.addEventListener("click", (event) => {
  if (event.target === dialog) dialog.close();
});
dialog.addEventListener("keydown", (event) => {
  if (event.key === "ArrowLeft" || event.key === "ArrowRight") {
    event.preventDefault();
    openShot(activeShot + (event.key === "ArrowRight" ? 1 : -1));
  }
});

if (document.body.dataset.manifest) {
  renderAnalysis(document.body.dataset.manifest).catch(() => {
    byId("loading").textContent = "Could not load the video analysis.";
  });
}
