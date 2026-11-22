// Support tier information dialog controller (Tier 2 / Tier 1 / Tier 0)
import { tierLabel } from "./drivers/registry.js";

const TIER_INFO = [
  {
    tier: 2,
    label: tierLabel(2),
    subtitle: "Full control",
    activeNote: "Full onboard control is enabled for this mouse.",
    desc: "Full control over DPI stages, polling rate, button mapping, power/sleep, and sensor or lighting tuning where supported — backed by a verified device profile.",
  },
  {
    tier: 1,
    label: tierLabel(1),
    subtitle: "Safe live readouts",
    activeNote: "Live readouts are active; all device writes stay locked for safety.",
    desc: "Safe live readouts like current DPI and firmware while keeping all device writes disabled until this model is hardware-verified.",
  },
  {
    tier: 0,
    label: tierLabel(0),
    subtitle: "Standard mouse mode",
    activeNote: "Pointer-only mode — no device-specific commands are sent to this mouse.",
    desc: "Zero device-specific commands are sent. You can still set a local DPI reference, measure live polling rate, test buttons, and use mouse acceleration & desktop pointer settings.",
  },
];

function getTierInfo(tier) {
  return TIER_INFO.find((t) => t.tier === tier) || TIER_INFO[TIER_INFO.length - 1];
}

function createTierModalController({ $, getSession, doc = document }) {
  const modal = $("tierModal");
  const badgeBtn = $("tierBadge");
  const deviceInfoBtn = $("supportTierInfo");
  const closeBtn = $("tierModalClose");
  const doneBtn = $("tierModalDone");
  let returnFocus = null;

  function isOpen() {
    return !!(modal && !modal.hidden);
  }

  function setExpanded(expanded) {
    const val = expanded ? "true" : "false";
    if (badgeBtn) badgeBtn.setAttribute("aria-expanded", val);
    if (deviceInfoBtn) deviceInfoBtn.setAttribute("aria-expanded", val);
  }

  function render() {
    if (!modal) return;
    const sess = getSession?.() ?? null;
    const activeTier = sess ? (sess.tier ?? 0) : null;
    const activeBox = $("tierModalActive");
    if (activeBox) {
      if (sess) {
        const info = getTierInfo(activeTier);
        const label = sess.tierLabel || info.label;
        const nameEl = $("tierModalActiveName");
        const pillEl = $("tierModalActiveTier");
        const pillTextEl = $("tierModalActiveTierText");
        const noteEl = $("tierModalActiveNote");
        if (nameEl) nameEl.textContent = sess.name || sess.shortName || "Connected mouse";
        if (pillEl) pillEl.className = `tier-pill tier-${activeTier}`;
        if (pillTextEl) pillTextEl.textContent = label;
        if (noteEl) noteEl.textContent = info.activeNote;
        activeBox.hidden = false;
      } else {
        activeBox.hidden = true;
      }
    }

    const cards = modal.querySelectorAll?.(".tier-card[data-tier]") || [];
    for (const card of cards) {
      const cardTier = Number(card.dataset.tier);
      const isCur = activeTier !== null && cardTier === activeTier;
      card.classList.toggle("is-current", isCur);
      if (isCur) card.setAttribute("aria-current", "true");
      else card.removeAttribute("aria-current");
      const tag = card.querySelector?.(".tier-current-tag");
      if (tag) tag.hidden = !isCur;
    }
  }

  function open(triggerEl = null) {
    if (!modal) return;
    returnFocus = triggerEl || doc.activeElement || null;
    render();
    modal.hidden = false;
    setExpanded(true);
    if (closeBtn && typeof closeBtn.focus === "function") {
      closeBtn.focus();
    }
  }

  function close() {
    if (!isOpen()) return;
    modal.hidden = true;
    setExpanded(false);
    const target = returnFocus;
    returnFocus = null;
    if (target && !target.hidden && target.isConnected !== false && typeof target.focus === "function") {
      target.focus();
    }
  }

  function sync() {
    if (isOpen()) render();
  }

  function getFocusable() {
    if (!modal || typeof modal.querySelectorAll !== "function") return [];
    const list = modal.querySelectorAll('button:not([disabled]):not([hidden]), [href], [tabindex]:not([tabindex="-1"])');
    return Array.from(list).filter((el) => !el.hidden);
  }

  badgeBtn?.addEventListener("click", () => open(badgeBtn));
  deviceInfoBtn?.addEventListener("click", () => open(deviceInfoBtn));
  closeBtn?.addEventListener("click", () => close());
  doneBtn?.addEventListener("click", () => close());
  modal?.addEventListener("click", (e) => {
    if (e.target === modal) close();
  });
  doc?.addEventListener("keydown", (e) => {
    if (!isOpen()) return;
    if (e.key === "Escape") {
      e.preventDefault();
      e.stopPropagation();
      close();
      return;
    }
    if (e.key === "Tab") {
      const focusable = getFocusable();
      if (!focusable.length) return;
      const first = focusable[0];
      const last = focusable[focusable.length - 1];
      if (e.shiftKey && doc.activeElement === first) {
        e.preventDefault();
        last.focus();
      } else if (!e.shiftKey && doc.activeElement === last) {
        e.preventDefault();
        first.focus();
      }
    }
  });

  return {
    open,
    close,
    render,
    sync,
    isOpen,
  };
}

export { TIER_INFO, getTierInfo, createTierModalController };
