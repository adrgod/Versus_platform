// Edit Tags pages: highlight changed values, count them, let album header fields
// fill their column, spreadsheet-style keys, and a warning before leaving unsaved work.
// Listens on the document because pages are swapped in place by library.js.
(() => {
  const editor = () => document.querySelector("[data-tag-editor]");
  const fields = (form) => [...form.querySelectorAll("input[type=text][data-initial]")];
  const changed = (input) => input.value.trim() !== (input.dataset.initial || "").trim();
  let submitting = false;

  const refresh = () => {
    const form = editor();
    if (!form) return 0;
    let count = 0;
    for (const input of fields(form)) {
      const isChanged = changed(input);
      if (isChanged) count += 1;
      input.closest("td, .editor-field")?.classList.toggle("is-changed", isChanged);
    }
    for (const label of form.querySelectorAll("[data-change-count]")) {
      label.textContent = count === 0 ? "No changes yet" : `${count} change${count === 1 ? "" : "s"} not saved`;
      label.classList.toggle("has-changes", count > 0);
    }
    for (const button of form.querySelectorAll("[data-save]")) {
      if (!button.title.includes("Import Data")) button.disabled = count === 0;
    }
    return count;
  };

  document.addEventListener("input", (event) => {
    const form = editor();
    const input = event.target;
    if (!form || !form.contains(input)) return;
    const column = input.dataset.headerFor;
    if (column) {
      // Typing in an album field sets that tag on every track; going back to the
      // original value gives each track its own value again.
      const reset = !changed(input);
      for (const cell of form.querySelectorAll(`.cell-input[data-col="${column}"]`)) {
        cell.value = reset ? cell.dataset.initial : input.value;
      }
    }
    refresh();
  });

  document.addEventListener("keydown", (event) => {
    const input = event.target;
    if (!input.matches?.("[data-tag-editor] input[type=text]")) return;
    if (event.key === "Escape") {
      input.value = input.dataset.initial || "";
      input.dispatchEvent(new Event("input", { bubbles: true }));
      return;
    }
    if (event.key === "Enter") {
      // Enter moves down a row (Shift+Enter up) instead of submitting.
      event.preventDefault();
      if (!input.classList.contains("cell-input")) return;
      const rows = [...editor().querySelectorAll(`.cell-input[data-col="${input.dataset.col}"]`)];
      const next = rows[rows.indexOf(input) + (event.shiftKey ? -1 : 1)];
      if (next) { next.focus(); next.select(); }
    }
  });

  // "Use …" next to an empty album field fills in what the folder name says.
  document.addEventListener("click", (event) => {
    const button = event.target.closest?.("[data-tag-editor] [data-fill]");
    if (!button) return;
    const input = button.closest(".editor-field")?.querySelector("input[type=text]");
    if (!input) return;
    input.value = button.dataset.fill;
    input.dispatchEvent(new Event("input", { bubbles: true }));
    input.focus();
  });

  // "Apply found data": fill every empty tag that has a found value; Save writes them to the files.
  document.addEventListener("click", (event) => {
    if (!event.target.closest?.("[data-tag-editor] [data-fill-all]")) return;
    const form = editor();
    for (const button of form.querySelectorAll(".editor-field [data-fill]")) button.click();
    for (const cell of form.querySelectorAll(".cell-input[data-suggest]")) {
      if (cell.value.trim()) continue;
      cell.value = cell.dataset.suggest;
      cell.dispatchEvent(new Event("input", { bubbles: true }));
    }
  });

  document.addEventListener("focusin", (event) => {
    if (event.target.matches?.("[data-tag-editor] .cell-input")) event.target.select();
  });

  document.addEventListener("submit", (event) => {
    if (event.target.matches?.("[data-tag-editor]")) submitting = true;
  }, true);

  // Leaving with unsaved changes: in-app links (captured before library.js) and full page loads.
  document.addEventListener("click", (event) => {
    const link = event.target.closest?.("a[href]");
    if (!link || submitting || !editor() || refresh() === 0) return;
    if (!window.confirm("You have changes that are not saved. Leave without saving?")) {
      event.preventDefault();
      event.stopImmediatePropagation();
    }
  }, true);
  window.addEventListener("beforeunload", (event) => {
    if (!submitting && editor() && refresh() > 0) event.preventDefault();
  });

  window.addEventListener("library:page-updated", () => { submitting = false; refresh(); });
  refresh();
})();
