(function () {
  const KEY = "simplenews:theme";
  const button = document.getElementById("theme-toggle");
  if (!button) return;

  function current() {
    return document.documentElement.getAttribute("data-theme") === "dark" ? "dark" : "light";
  }

  function render() {
    button.textContent = current() === "dark" ? "light" : "dark";
  }

  button.addEventListener("click", () => {
    const next = current() === "dark" ? "light" : "dark";
    document.documentElement.setAttribute("data-theme", next);
    try {
      localStorage.setItem(KEY, next);
    } catch {
      /* localStorage unavailable (private mode, etc) - toggle still works this visit */
    }
    render();
  });

  render();
})();
