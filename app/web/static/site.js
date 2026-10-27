// Small progressive enhancements. Every page works without this file.
document.addEventListener("click", (event) => {
  const toggle = event.target.closest("[data-toggle]");
  if (toggle) {
    const target = document.getElementById(toggle.dataset.toggle);
    if (target) {
      const open = target.classList.toggle("open");
      toggle.setAttribute("aria-expanded", String(open));
    }
  }
  const confirmEl = event.target.closest("[data-confirm]");
  if (confirmEl && !window.confirm(confirmEl.dataset.confirm)) {
    event.preventDefault();
  }
});

// Auto-submit filter selects on the jobs page.
document.querySelectorAll("select[data-autosubmit]").forEach((el) => {
  el.addEventListener("change", () => el.form && el.form.submit());
});

// Admin source form: show only the settings for the chosen reader.
const adapterSelect = document.querySelector("select[name=adapter]");
if (adapterSelect) {
  const sync = () => {
    document.querySelectorAll("[data-adapter-fields]").forEach((group) => {
      const on = group.dataset.adapterFields === adapterSelect.value;
      group.hidden = !on;
      group.querySelectorAll("input, textarea, select").forEach((input) => { input.disabled = !on; });
    });
  };
  adapterSelect.addEventListener("change", sync);
  sync();
}
