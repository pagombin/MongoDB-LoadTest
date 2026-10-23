/* Applies saved theme and density before first paint. */
(function () {
  "use strict";
  try {
    var theme = localStorage.getItem("ballast.theme");
    var density = localStorage.getItem("ballast.density");
    if (theme === "light" || theme === "dark") document.documentElement.setAttribute("data-theme", theme);
    if (density === "compact") document.documentElement.setAttribute("data-density", "compact");
  } catch (e) {
    /* storage unavailable */
  }
})();
