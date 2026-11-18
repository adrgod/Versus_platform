// Tag explorer: filter long value lists and reveal the values past the first ten.
document.querySelectorAll("[data-facet-group]").forEach((group) => {
  const rows = [...group.querySelectorAll("[data-facet-name]")];
  const filter = group.querySelector("[data-facet-filter]");
  const toggle = group.querySelector("[data-facet-more]");
  let expanded = false;

  const render = () => {
    const text = (filter?.value || "").trim().toLowerCase();
    rows.forEach((row) => {
      const extra = row.classList.contains("facet-more");
      row.hidden = text ? !row.dataset.facetName.includes(text) : extra && !expanded;
    });
    if (toggle) toggle.hidden = Boolean(text);
  };

  filter?.addEventListener("input", render);
  toggle?.addEventListener("click", () => {
    expanded = !expanded;
    toggle.textContent = expanded ? "Show fewer" : toggle.dataset.moreLabel;
    render();
  });
});
