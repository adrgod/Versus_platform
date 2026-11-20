// Album review box: formatting toolbar, 1-10 star score (hover + click), save / delete.
// Everything is delegated from the document because pages are swapped in without a reload.
(() => {
  const box = (node) => node.closest("[data-review]");
  const body = (root) => root.querySelector("[data-review-body]");

  const setScore = (root, value) => {
    const score = root.querySelector("[data-review-score]");
    score.dataset.value = String(value);
    score.querySelectorAll("[data-review-star]").forEach((star) => {
      star.classList.toggle("is-on", Number(star.dataset.reviewStar) <= value);
    });
    root.querySelector("[data-review-score-value]").textContent = value ? `${value}/10` : "—/10";
  };

  const paintHover = (root, upTo) => {
    root.querySelectorAll("[data-review-star]").forEach((star) => {
      star.classList.toggle("is-hover", upTo > 0 && Number(star.dataset.reviewStar) <= upTo);
    });
    root.querySelector("[data-review-score]").classList.toggle("is-hovering", upTo > 0);
  };

  const say = (root, text, kind = "") => {
    const status = root.querySelector("[data-review-status]");
    status.textContent = text;
    status.dataset.kind = kind;
  };

  const markActiveCommands = () => {
    for (const root of document.querySelectorAll("[data-review]")) {
      const editor = body(root);
      const inside = editor && editor.contains(window.getSelection()?.anchorNode);
      root.querySelectorAll("[data-cmd]").forEach((button) => {
        const cmd = button.dataset.cmd;
        let active = false;
        if (inside && ["bold", "italic", "underline", "strikeThrough", "insertUnorderedList", "insertOrderedList"].includes(cmd)) {
          try { active = document.queryCommandState(cmd); } catch { /* not supported */ }
        }
        button.classList.toggle("is-active", active);
      });
    }
  };
  document.addEventListener("selectionchange", markActiveCommands);

  // Keep the text selection in the editor while a toolbar button is pressed.
  document.addEventListener("mousedown", (event) => {
    if (event.target.closest(".review-toolbar button")) event.preventDefault();
  });

  document.addEventListener("click", async (event) => {
    const command = event.target.closest(".review-toolbar [data-cmd]");
    if (command) {
      const root = box(command);
      const editor = body(root);
      editor.focus();
      document.execCommand("defaultParagraphSeparator", false, "p");
      let value = command.dataset.value || null;
      if (command.dataset.cmd === "createLink") {
        value = window.prompt("Link address (https://…)", "https://");
        if (!value || value === "https://") return;
      }
      if (command.dataset.cmd === "formatBlock") {
        // A second press on the same block type turns it back into a paragraph.
        const current = document.queryCommandValue("formatBlock").toLowerCase();
        if (`<${current}>` === value) value = "<p>";
      }
      document.execCommand(command.dataset.cmd, false, value);
      markActiveCommands();
      return;
    }

    const star = event.target.closest("[data-review-star]");
    if (star) {
      setScore(box(star), Number(star.dataset.reviewStar));
      return;
    }

    const save = event.target.closest("[data-review-save]");
    if (save) {
      const root = box(save);
      const score = Number(root.querySelector("[data-review-score]").dataset.value) || 0;
      if (!score) {
        say(root, "Pick a score from 1 to 10 first.", "error");
        return;
      }
      save.disabled = true;
      say(root, "Saving…");
      const result = await window.versusPost(root.dataset.saveUrl, {
        score,
        html: body(root).innerHTML,
        author: root.querySelector("[data-review-author]").value,
      });
      save.disabled = false;
      if (!result.ok) {
        say(root, result.data?.error || "Could not save the review. Try again.", "error");
        return;
      }
      root.querySelector("[data-review-author]").value = result.data.author;
      root.querySelector("[data-review-meta]").textContent = `Written ${result.data.written} · saved`;
      save.textContent = "Update review";
      say(root, "Saved.", "ok");
      return;
    }

    const remove = event.target.closest("[data-review-delete]");
    if (remove) {
      const root = box(remove);
      if (!remove.dataset.confirm) { // two presses: the first only asks
        remove.dataset.confirm = "1";
        remove.textContent = "Really delete?";
        setTimeout(() => { delete remove.dataset.confirm; remove.textContent = "Delete review"; }, 4000);
        return;
      }
      const result = await window.versusPost(root.dataset.deleteUrl, {});
      if (result.ok) window.location.reload();
      else say(root, "Could not delete the review.", "error");
    }
  });

  document.addEventListener("mouseover", (event) => {
    const star = event.target.closest("[data-review-star]");
    if (star) paintHover(box(star), Number(star.dataset.reviewStar));
  });
  document.addEventListener("mouseout", (event) => {
    const star = event.target.closest("[data-review-star]");
    if (star && !star.contains(event.relatedTarget)) {
      const root = box(star);
      if (!event.relatedTarget?.closest?.("[data-review-star]")) paintHover(root, 0);
    }
  });

  // Pasted text keeps no foreign formatting.
  document.addEventListener("paste", (event) => {
    if (!event.target.closest?.("[data-review-body]")) return;
    event.preventDefault();
    document.execCommand("insertText", false, event.clipboardData.getData("text/plain"));
  });
})();
