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
  // Bottom tab bar: highlight the current section.
  (function () {
    var path = window.location.pathname;
    document.querySelectorAll(".tabbar a").forEach(function (a) {
      var tab = a.getAttribute("data-tab");
      var on = false;
      if (tab === "feed") on = (path === "/feed" || path === "/");
      else if (tab === "profile") on = (path.indexOf("/u/") === 0);
      else on = (path === "/" + tab || path.indexOf("/" + tab + "/") === 0);
      if (on) a.classList.add("on");
    });
  })();

/* @mention autocomplete: type @ in any composer (post textareas + comment
   inputs) to get a user picker. Dark + gold, touch friendly. */
(function () {
  "use strict";
  var dd = null;            // dropdown element
  var field = null;         // composer currently being typed in
  var results = [];         // last search results
  var sel = 0;              // highlighted index
  var timer = null;         // debounce timer
  var lastFetch = 0;        // stamp out stale responses

  function isComposer(el) {
    if (!el || !el.name || el.name !== "body") return false;
    if (el.tagName === "TEXTAREA") return true;
    return el.tagName === "INPUT" && !!el.closest(".comment-form");
  }

  function ensureDD() {
    if (dd) return dd;
    dd = document.createElement("div");
    dd.className = "mention-dropdown";
    dd.hidden = true;
    dd.setAttribute("role", "listbox");
    document.body.appendChild(dd);
    var pick = function (e) {
      var row = e.target.closest ? e.target.closest(".mention-row") : null;
      if (!row || !field) return;
      e.preventDefault();
      choose(parseInt(row.dataset.idx, 10));
    };
    dd.addEventListener("mousedown", pick);
    dd.addEventListener("click", pick);
    return dd;
  }

  function caretToken(el) {
    var pos = el.selectionStart;
    if (pos == null) return null;
    var m = /(?:^|\s)@([A-Za-z0-9_]{1,20})$/.exec(el.value.slice(0, pos));
    if (!m) return null;
    return { q: m[1], start: pos - m[1].length - 1 };
  }

  function hide() {
    if (dd) dd.hidden = true;
    results = [];
  }

  function position() {
    var r = field.getBoundingClientRect();
    var top = r.bottom + window.scrollY + 6;
    ensureDD();
    dd.style.minWidth = Math.min(Math.max(r.width, 220), 340) + "px";
    dd.style.left = Math.max(8, r.left + window.scrollX) + "px";
    // flip above the field when there is no room below
    var estH = Math.min(results.length, 8) * 46 + 8;
    if (r.bottom + estH + 12 > window.innerHeight && r.top - estH - 12 > 0) {
      top = r.top + window.scrollY - estH - 6;
    }
    dd.style.top = top + "px";
  }

  function esc(s) {
    return String(s).replace(/&/g, "&amp;").replace(/</g, "&lt;")
      .replace(/>/g, "&gt;").replace(/"/g, "&quot;");
  }

  function render() {
    ensureDD();
    if (!results.length) {
      dd.innerHTML = '<div class="mention-empty">No matches</div>';
    } else {
      dd.innerHTML = results.map(function (u, i) {
        var av = u.avatar_url
          ? '<img class="mavatar" src="' + esc(u.avatar_url) + '" alt="" loading="lazy">'
          : '<span class="minitial">' + esc((u.display_name || u.username).charAt(0).toUpperCase()) + "</span>";
        return '<div class="mention-row' + (i === sel ? " sel" : "") + '" role="option" data-idx="' + i + '">' +
          av + '<span class="mnames"><span class="mdisplay">' + esc(u.display_name) + "</span><br>" +
          '<span class="muname">@' + esc(u.username) + "</span></span></div>";
      }).join("");
    }
    position();
    dd.hidden = false;
  }

  function move(d) {
    if (!results.length) return;
    sel = (sel + d + results.length) % results.length;
    render();
  }

  function choose(i) {
    var u = results[i];
    var tok = field && caretToken(field);
    if (!u || !tok) { hide(); return; }
    var insert = "@" + u.username + " ";
    field.value = field.value.slice(0, tok.start) + insert +
      field.value.slice(field.selectionStart);
    var caret = tok.start + insert.length;
    field.setSelectionRange(caret, caret);
    field.focus();
    hide();
  }

  function search(q, stamp) {
    fetch("/api/users/search?q=" + encodeURIComponent(q), { cache: "no-store" })
      .then(function (r) { return r.ok ? r.json() : null; })
      .then(function (data) {
        if (stamp !== lastFetch || !field) return;
        var tok = caretToken(field);
        if (!tok || tok.q !== q) return;  // user kept typing
        results = (data && data.results) || [];
        sel = 0;
        render();
      })
      .catch(function () { /* offline — stay quiet */ });
  }

  document.addEventListener("input", function (e) {
    var el = e.target;
    if (!isComposer(el)) return;
    field = el;
    clearTimeout(timer);
    var tok = caretToken(el);
    if (!tok) { hide(); return; }
    var q = tok.q;
    timer = setTimeout(function () {
      if (field !== el) return;
      var t2 = caretToken(el);
      if (!t2 || t2.q !== q) return;
      lastFetch += 1;
      search(q, lastFetch);
    }, 150);
  });

  document.addEventListener("keydown", function (e) {
    if (!field || !dd || dd.hidden || e.target !== field) return;
    if (e.key === "ArrowDown") { e.preventDefault(); move(1); }
    else if (e.key === "ArrowUp") { e.preventDefault(); move(-1); }
    else if ((e.key === "Enter" || e.key === "Tab") && results.length) {
      e.preventDefault(); choose(sel);
    } else if (e.key === "Escape") { hide(); }
  });

  // blur hides (delayed so a tap on a row still lands); scroll/resize hides now
  document.addEventListener("blur", function (e) {
    if (isComposer(e.target)) setTimeout(hide, 150);
  }, true);
  window.addEventListener("scroll", hide, true);
  window.addEventListener("resize", hide);
})();
