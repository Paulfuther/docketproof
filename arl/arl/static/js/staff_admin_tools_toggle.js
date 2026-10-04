(function () {
  var toggle = document.getElementById("staffAdminToolsToggle");
  var panel = document.getElementById("staffAdminToolsPanel");
  if (!toggle || !panel) return;

  toggle.addEventListener("click", function () {
    var open = panel.hasAttribute("hidden");
    if (open) {
      panel.removeAttribute("hidden");
      toggle.setAttribute("aria-expanded", "true");
      toggle.classList.add("is-open");
    } else {
      panel.setAttribute("hidden", "hidden");
      toggle.setAttribute("aria-expanded", "false");
      toggle.classList.remove("is-open");
    }
  });
})();
