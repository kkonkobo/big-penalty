// Put the visitor's own platform first and make it the primary button.
(function () {
  var ua = navigator.userAgent, platform = navigator.platform || "";
  var key = /Win/i.test(platform) || /Windows/i.test(ua) ? "dl-windows"
          : /Mac/i.test(platform) && !/iPhone|iPad/i.test(ua) ? "dl-mac" : null;
  if (!key) return;
  var button = document.getElementById(key);
  button.classList.add("primary");
  button.parentNode.insertBefore(button, button.parentNode.firstChild);
})();

// Show the current version and date. The download links work without this.
fetch("https://api.github.com/repos/kkonkobo/big-penalty/releases/latest")
  .then(function (r) { return r.ok ? r.json() : Promise.reject(); })
  .then(function (release) {
    var date = new Date(release.published_at).toLocaleDateString("fr-FR", { day: "numeric", month: "long", year: "numeric" });
    var sizes = {};
    release.assets.forEach(function (a) { sizes[a.name] = Math.round(a.size / 1048576) + " Mo"; });
    var meta = document.getElementById("meta");
    meta.textContent = "Version " + release.tag_name.replace(/^v/, "") + " · " + date + " · ";
    var link = document.createElement("a");
    link.href = release.html_url;
    link.textContent = "notes de version";
    meta.appendChild(link);
    [["dl-windows", "Big-Penalty-windows-x64-setup.exe"], ["dl-mac", "Big-Penalty-macos-arm64.zip"], ["dl-mac-intel", "Big-Penalty-macos-intel.zip"]]
      .forEach(function (pair) {
        var small = document.querySelector("#" + pair[0] + " small");
        if (sizes[pair[1]]) small.textContent += " · " + sizes[pair[1]];
      });
  })
  .catch(function () {});
