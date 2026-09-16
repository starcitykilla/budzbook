/* The Higher Club — tiny vanilla JS helpers. No framework. */
(function () {
  "use strict";

  // Show chosen filename next to the photo button in composers.
  document.querySelectorAll('input[type="file"][name="image"]').forEach(function (input) {
    input.addEventListener("change", function () {
      var label = document.getElementById("filelabel");
      if (label && input.files.length) label.textContent = "📎 " + input.files[0].name;
    });
  });

  // DM polling: if we're on a conversation page, fetch new messages every 5s.
  var conv = document.getElementById("conv");
  if (conv) {
    var peer = conv.dataset.peer;
    var lastId = parseInt(conv.dataset.last || "0", 10);

    function esc(s) {
      return s.replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;");
    }

    async function poll() {
      try {
        var r = await fetch("/messages/" + encodeURIComponent(peer) +
                            "/poll?after_id=" + lastId);
        if (!r.ok) return;
        var data = await r.json();
        (data.messages || []).forEach(function (m) {
          lastId = Math.max(lastId, m.id);
          var div = document.createElement("div");
          div.className = "dm " + (m.mine ? "me" : "them");
          div.dataset.id = m.id;
          div.innerHTML = '<div class="dm-body">' + esc(m.body) + "</div>";
          conv.appendChild(div);
        });
        if (data.messages && data.messages.length) {
          conv.scrollTop = conv.scrollHeight;
        }
      } catch (e) { /* offline or logged out — next poll will retry */ }
    }
    conv.scrollTop = conv.scrollHeight;
    setInterval(poll, 5000);
  }
})();
