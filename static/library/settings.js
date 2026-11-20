// Display settings: theme, highlight colour, album grid size, corners, motion.
// Stored per browser in localStorage. The first paint is handled by the inline
// bootstrap in base.html; this file wires up the menu and keeps things in sync.
(() => {
  const STORAGE_KEY = "versus-display";
  const DEFAULTS = { theme: "system", accent: "indigo", grid: "comfortable", corners: "rounded", motion: "full" };
  const ACCENT_NAMES = {
    indigo: "Indigo", blue: "Blue", teal: "Teal", emerald: "Emerald", amber: "Amber",
    orange: "Orange", rose: "Rose", pink: "Pink", violet: "Violet", graphite: "Graphite",
  };
  const root = document.documentElement;

  // Small JSON helper shared with library.js: POSTs with the CSRF token. Only used when
  // signed in, i.e. when the server put the user's saved preferences in the page.
  const csrfToken = () =>
    document.querySelector("[name=csrfmiddlewaretoken]")?.value ||
    (document.cookie.match(/(?:^|; )csrftoken=([^;]*)/) || [])[1] || "";
  // versusPost -> {ok, status, data}; versusApi -> the data, or null when the request failed.
  window.versusPost = (url, body) =>
    fetch(url, {
      method: "POST",
      credentials: "same-origin",
      headers: { "Content-Type": "application/json", "X-CSRFToken": decodeURIComponent(csrfToken()) },
      body: JSON.stringify(body),
    })
      .then((response) => response.json().catch(() => null).then((data) => ({ ok: response.ok, status: response.status, data })))
      .catch(() => ({ ok: false, status: 0, data: null }));
  window.versusApi = (url, body) => window.versusPost(url, body).then((result) => (result.ok ? result.data : null));
  window.versusPrefs = (() => {
    try {
      return JSON.parse(document.getElementById("user-prefs")?.textContent || "null");
    } catch {
      return null;
    }
  })();
  let pushTimer;
  const pushDisplay = (display) => {
    if (!window.versusPrefs) return; // not signed in
    clearTimeout(pushTimer);
    pushTimer = setTimeout(() => window.versusApi("/me/preferences/", { display }), 400);
  };
  const darkQuery = window.matchMedia("(prefers-color-scheme: dark)");

  const load = () => {
    try {
      return { ...DEFAULTS, ...JSON.parse(localStorage.getItem(STORAGE_KEY) || "{}") };
    } catch {
      return { ...DEFAULTS };
    }
  };

  const save = (settings) => {
    try {
      localStorage.setItem(STORAGE_KEY, JSON.stringify(settings));
    } catch {
      /* storage unavailable (private mode): settings still apply for this page */
    }
  };

  const apply = (settings) => {
    const theme = settings.theme === "system" ? (darkQuery.matches ? "dark" : "light") : settings.theme;
    root.dataset.theme = theme;
    root.dataset.accent = ACCENT_NAMES[settings.accent] ? settings.accent : DEFAULTS.accent;
    root.dataset.grid = settings.grid;
    root.dataset.corners = settings.corners;
    root.dataset.motion = settings.motion;
    const meta = document.querySelector('meta[name="theme-color"]');
    if (meta) meta.content = getComputedStyle(root).getPropertyValue("--surface").trim() || "#ffffff";
  };

  let settings = load();
  apply(settings);

  darkQuery.addEventListener("change", () => {
    if (settings.theme === "system") apply(settings);
  });

  // The header is swapped on in-app navigation, so listen on the document
  // instead of on the menu element itself.
  const currentMenu = () => document.querySelector("[data-settings]");

  const syncControls = () => {
    const menu = currentMenu();
    if (!menu) return;
    for (const input of menu.querySelectorAll("input[data-setting]")) {
      input.checked = settings[input.dataset.setting] === input.value;
    }
    const accentLabel = menu.querySelector("[data-accent-name]");
    if (accentLabel) accentLabel.textContent = ACCENT_NAMES[settings.accent] || ACCENT_NAMES.indigo;
  };
  syncControls();
  window.addEventListener("library:page-updated", syncControls);

  const update = (next) => {
    settings = next;
    save(settings);
    apply(settings);
    syncControls();
    pushDisplay(settings);
  };

  document.addEventListener("change", (event) => {
    const input = event.target.closest("[data-settings] input[data-setting]");
    if (!input) return;
    const value = input.type === "checkbox" ? (input.checked ? input.value : input.dataset.off) : input.value;
    update({ ...settings, [input.dataset.setting]: value });
  });

  document.addEventListener("click", (event) => {
    if (event.target.closest("[data-settings-reset]")) {
      update({ ...DEFAULTS });
      return;
    }
    // Close on outside click, like a normal dropdown.
    const menu = currentMenu();
    if (menu?.open && !menu.contains(event.target)) menu.open = false;
  });

  document.addEventListener("keydown", (event) => {
    const menu = currentMenu();
    if (event.key === "Escape" && menu?.open) {
      menu.open = false;
      menu.querySelector("summary")?.focus();
    }
  });

  // Keep several open tabs in step.
  window.addEventListener("storage", (event) => {
    if (event.key !== STORAGE_KEY) return;
    settings = load();
    apply(settings);
    syncControls();
  });
})();
