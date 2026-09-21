// Refreshes the dashboard body every few seconds. The server renders the figures; this only swaps them in.
(function () {
  var live = document.getElementById("live");
  if (!live) return;
  var busy = false;
  function refresh() {
    if (busy || document.hidden) return;
    busy = true;
    fetch(live.dataset.url, { credentials: "same-origin", headers: { Accept: "text/html" } })
      .then(function (r) { return r.ok ? r.text() : null; })
      .then(function (html) { if (html) live.innerHTML = html; })
      .catch(function () { /* an offline blip: keep showing the last figures */ })
      .then(function () { busy = false; });
  }
  setInterval(refresh, 3000);
})();
