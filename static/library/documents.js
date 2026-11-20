// Liner-notes files on the album page: the "Include for AI enrichment" checkbox beside each file,
// plus a name and a kind (lyrics, promo material, biography...) for the file. Saved as soon as they change.
// Delegated from the document because pages are swapped in without a reload.
(() => {
  const say = (root, text, error = false) => {
    const note = root.querySelector("[data-doc-saved]");
    if (!note) return;
    note.textContent = text;
    note.classList.toggle("is-error", error);
  };

  document.addEventListener("change", async (event) => {
    const check = event.target.closest("[data-doc-include]");
    if (check) {
      const wanted = check.checked;
      const result = await window.versusPost(check.dataset.docInclude, { include: wanted });
      if (!result.ok) check.checked = !wanted;
      return;
    }
    const fields = event.target.closest("[data-doc-fields]");
    if (!fields) return;
    const row = fields.closest(".doc-row");
    const title = fields.querySelector("[data-doc-title]");
    const kind = fields.querySelector("[data-doc-kind]");
    say(fields, "Saving…");
    const result = await window.versusPost(fields.dataset.url, { title: title.value, kind: kind.value });
    if (!result.ok) {
      say(fields, "Could not save. Try again.", true);
      return;
    }
    title.value = result.data.title;
    row.querySelector("[data-doc-name]").textContent = result.data.title || title.placeholder;
    row.querySelector("[data-doc-kind-label]").textContent = kind.options[kind.selectedIndex].text;
    say(fields, "Saved.");
  });
})();
