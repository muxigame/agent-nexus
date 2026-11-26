/* Appearance boot: a classic (blocking) script loaded in <head> before the stylesheets, so the
 * page's look is decided before first paint. Two independent axes, both remembered per browser:
 *   anx-style  "auto" | "pure" | "mica"  -> <html data-style="pure|mica">  (auto: mica on Windows)
 *   anx-theme  "system" | "light" | "dark" -> <html data-theme="light|dark"> (system: no attribute)
 * ui.js exports setStyle / setTheme that write the same keys and attributes. */
(function () {
  var root = document.documentElement;
  function read(key) { try { return localStorage.getItem(key); } catch (e) { return null; } }
  function isWindows() {
    try {
      var data = navigator.userAgentData;
      if (data && data.platform) return /windows/i.test(data.platform);
    } catch (e) { /* ignore */ }
    return /windows/i.test(navigator.userAgent || "") || /^win/i.test(navigator.platform || "");
  }
  var style = read("anx-style");
  if (style !== "pure" && style !== "mica") style = isWindows() ? "mica" : "pure";
  if (!root.hasAttribute("data-style")) root.setAttribute("data-style", style);
  var theme = read("anx-theme");
  if ((theme === "light" || theme === "dark") && !root.hasAttribute("data-theme")) root.setAttribute("data-theme", theme);
})();
