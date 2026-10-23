(() => {
  "use strict";

  const ready = (fn) => {
    if (document.readyState === "loading") {
      document.addEventListener("DOMContentLoaded", fn);
    } else {
      fn();
    }
  };

  ready(() => {
    const body = document.body;
    const menuButton = document.querySelector("[data-menu-toggle]");
    const scrim = document.querySelector("[data-menu-close]");

    const closeMenu = () => {
      body.classList.remove("nav-open");
      if (menuButton) menuButton.setAttribute("aria-expanded", "false");
      if (scrim) scrim.hidden = true;
    };

    if (menuButton) {
      menuButton.addEventListener("click", () => {
        const open = body.classList.toggle("nav-open");
        menuButton.setAttribute("aria-expanded", String(open));
        if (scrim) scrim.hidden = !open;
      });
    }
    if (scrim) scrim.addEventListener("click", closeMenu);
    document.addEventListener("keydown", (event) => {
      if (event.key === "Escape") closeMenu();
    });

    document.querySelectorAll("[data-dismiss-flash]").forEach((button) => {
      button.addEventListener("click", () => {
        const flash = button.closest(".flash");
        if (flash) flash.remove();
      });
    });

    document.querySelectorAll("form[data-auto-submit]").forEach((form) => {
      const checkbox = form.querySelector("[data-task-checkbox]");
      const target = form.querySelector("[data-target-input]");
      if (!checkbox || !target) return;
      checkbox.addEventListener("change", () => {
        target.value = checkbox.checked ? "completed" : "reopen";
        checkbox.disabled = true;
        form.classList.add("is-saving");
        form.submit();
      });
    });

    document.querySelectorAll("form[data-confirm]").forEach((form) => {
      form.addEventListener("submit", (event) => {
        const message = form.getAttribute("data-confirm") || "Are you sure?";
        if (!window.confirm(message)) {
          event.preventDefault();
        }
      });
    });

    document.querySelectorAll("details.task-menu").forEach((details) => {
      details.addEventListener("toggle", () => {
        if (!details.open) return;
        document.querySelectorAll("details.task-menu").forEach((other) => {
          if (other !== details) other.open = false;
        });
      });
    });

    document.querySelectorAll("[data-history-back]").forEach((button) => {
      button.addEventListener("click", () => {
        if (window.history.length > 1) window.history.back();
      });
    });

    const dateInputs = document.querySelectorAll('input[type="date"][name="new_date"]');
    const today = new Date();
    const iso = `${today.getFullYear()}-${String(today.getMonth() + 1).padStart(2, "0")}-${String(today.getDate()).padStart(2, "0")}`;
    dateInputs.forEach((input) => {
      if (!input.min) input.min = iso;
    });
  });
})();
