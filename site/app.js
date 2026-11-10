// gitchen landing page: three small touches, none of them needed to read the page.
const reduced = matchMedia("(prefers-reduced-motion: reduce)").matches;

// 1. Scroll reveal.
const reveals = document.querySelectorAll(".reveal");
if ("IntersectionObserver" in window && !reduced) {
  const seen = new IntersectionObserver((entries) => {
    for (const entry of entries) {
      if (entry.isIntersecting) { entry.target.classList.add("in"); seen.unobserve(entry.target); }
    }
  }, { rootMargin: "0px 0px -8% 0px", threshold: 0.08 });
  reveals.forEach((el) => seen.observe(el));
} else {
  reveals.forEach((el) => el.classList.add("in"));
}

// 2. The hero kitchen: an order really does move pm > builder > reviewer > pm.
const SCRIPT = [
  { role: "pm", say: "NEXT: assign builder" },
  { role: "builder", say: "NEXT: assign reviewer" },
  { role: "reviewer", say: "NEXT: return" },
  { role: "pm", say: "NEXT: merge" },
];
const kitchen = document.querySelector(".kitchen");
const who = document.getElementById("scene-who");
const cooks = [...document.querySelectorAll(".cook")];

function activate(role, say) {
  for (const cook of cooks) {
    const on = cook.dataset.cook === role;
    cook.classList.toggle("active", on);
    cook.querySelector(".say")?.remove();
    if (on) {
      const bubble = document.createElement("span");
      bubble.className = "say";
      bubble.innerHTML = `<code></code><i class="tail" aria-hidden="true"></i>`;
      bubble.firstChild.textContent = say;
      cook.prepend(bubble);
    }
  }
  if (who) who.textContent = `on the stove · ${role}`;
  fitBubble();
}

// Keep the speech bubble inside the kitchen on narrow screens, and its tail
// on the bubble, pointing at the chef. Both are set as plain pixel offsets: no
// CSS variable in the bob keyframes or a pseudo-element, which Safari can
// leave stale and so draw the tail off the bubble.
const TAIL_ROOM = 14 + 8; // the bubble's corner radius plus half the tail
function fitBubble() {
  const bubble = document.querySelector(".cook.active .say");
  if (!bubble || !kitchen) return;
  let tail = bubble.querySelector(".tail");
  if (!tail) {
    tail = document.createElement("i");
    tail.className = "tail";
    tail.setAttribute("aria-hidden", "true");
    bubble.appendChild(tail);
  }
  bubble.style.marginLeft = "0px";
  const bubbleBox = bubble.getBoundingClientRect();
  const kitchenBox = kitchen.getBoundingClientRect();
  const pad = 8;
  const shift = Math.max(0, kitchenBox.left + pad - bubbleBox.left)
    - Math.max(0, bubbleBox.right - (kitchenBox.right - pad));
  bubble.style.marginLeft = `${Math.round(shift)}px`;
  // The chef is at the bubble's centre minus the shift; keep the tail on the
  // straight part of the bottom edge even when the bubble had to move far.
  const room = Math.max(0, bubbleBox.width / 2 - TAIL_ROOM);
  const toChef = Math.max(-room, Math.min(room, -shift));
  tail.style.left = `calc(50% + ${Math.round(toChef)}px)`;
}
addEventListener("resize", fitBubble);
addEventListener("load", fitBubble);
document.fonts?.ready.then(fitBubble); // the bubble's width changes once the font is in

let step = 0;
let timer = null;
function play() {
  if (reduced || timer) return;
  timer = setInterval(() => {
    step = (step + 1) % SCRIPT.length;
    activate(SCRIPT[step].role, SCRIPT[step].say);
  }, 3200);
}
if (kitchen) {
  // only cook while it is on screen
  if ("IntersectionObserver" in window) {
    new IntersectionObserver(([entry]) => {
      if (entry.isIntersecting) play();
      else { clearInterval(timer); timer = null; }
    }).observe(kitchen);
  } else play();
}

// 3. Copy buttons on the terminal.
document.addEventListener("click", (event) => {
  const copy = event.target.closest("[data-copy]");
  if (copy) {
    navigator.clipboard?.writeText(copy.dataset.copy).then(() => {
      copy.textContent = "copied ♡";
      setTimeout(() => { copy.textContent = "copy"; }, 1400);
    }).catch(() => {});
  }
});
