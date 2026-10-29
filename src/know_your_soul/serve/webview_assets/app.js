/* Know Your Soul webview client — vanilla JS, no build step, no CDN, offline.
 *
 * Safety contract: graph data is rendered exclusively through textContent /
 * createElement; no string of store data ever becomes markup. Layout
 * coordinates come from the server (viz.layout); the client only applies a
 * pan/zoom transform.
 */
(function () {
  "use strict";

  var params = new URLSearchParams(location.search);
  var TOKEN = params.get("token") || "";

  var DOMAIN_COLORS = {
    P1: "#2e6f9e", P2: "#9e5a2e", P3: "#3d8a5a", P4: "#8a3d6e",
    P5: "#6b5cae", P6: "#a8842c", P7: "#4f7f7a"
  };
  var FALLBACK_COLOR = "#6e6e78";

  function el(tag, className, text) {
    var node = document.createElement(tag);
    if (className) node.className = className;
    if (text !== undefined && text !== null) node.textContent = String(text);
    return node;
  }

  function setStatus(message, isError) {
    var status = document.getElementById("status");
    status.textContent = message || "";
    status.className = isError ? "error" : "";
  }

  function api(path) {
    var joiner = path.indexOf("?") === -1 ? "?" : "&";
    return fetch(path + joiner + "token=" + encodeURIComponent(TOKEN), {
      headers: { "Accept": "application/json" }
    }).then(function (response) {
      return response.json().then(function (payload) {
        if (!response.ok) {
          var code = payload && payload.error ? payload.error.code : String(response.status);
          var message = payload && payload.error ? payload.error.message : "request failed";
          throw new Error(code + ": " + message);
        }
        return payload;
      });
    });
  }

  // ------------------------------------------------------------------ tabs

  var loadedOnce = {};

  function showTab(name) {
    var tabs = document.querySelectorAll(".tab");
    for (var i = 0; i < tabs.length; i++) {
      tabs[i].classList.toggle("active", tabs[i].getAttribute("data-tab") === name);
    }
    var views = document.querySelectorAll(".view");
    for (var j = 0; j < views.length; j++) {
      views[j].classList.toggle("active", views[j].id === "view-" + name);
    }
    if (!loadedOnce[name]) {
      loadedOnce[name] = true;
      loadTab(name);
    }
    if (name === "graph") requestDraw();
  }

  function loadTab(name) {
    if (name === "graph") return loadGraph();
    if (name === "metrics") return loadPanel("metrics-body", "/api/metrics", renderMetrics);
    if (name === "health") return loadPanel("health-body", "/api/health", renderHealth);
    if (name === "analytics") return loadPanel("analytics-body", "/api/analytics", renderAnalytics);
    if (name === "audit") return loadPanel("audit-body", "/api/audit?limit=100", renderAudit);
    if (name === "proposals") return loadPanel("proposals-body", "/api/proposals", renderProposals);
  }

  function loadPanel(elementId, path, renderer) {
    var container = document.getElementById(elementId);
    container.textContent = "";
    container.appendChild(el("p", "empty", "loading…"));
    api(path).then(function (payload) {
      container.textContent = "";
      renderer(container, payload);
      setStatus("");
    }).catch(function (error) {
      container.textContent = "";
      container.appendChild(el("p", "bad-badge", String(error.message || error)));
      setStatus(String(error.message || error), true);
    });
  }

  // ----------------------------------------------------------------- graph

  var graphData = null;
  var view = { x: 0, y: 0, scale: 1 };
  var canvas = document.getElementById("graph-canvas");
  var ctx = canvas.getContext("2d");
  var hitIndex = [];

  function loadGraph() {
    api("/api/graph").then(function (payload) {
      graphData = payload;
      var stats = document.getElementById("graph-stats");
      stats.textContent = payload.total_nodes + " nodes · " + payload.total_edges + " edges · layout " +
        payload.layout.mode + (payload.truncated ? " · TRUNCATED" : "");
      fitView();
      requestDraw();
      setStatus("");
    }).catch(function (error) {
      setStatus(String(error.message || error), true);
    });
  }

  function fitView() {
    if (!graphData) return;
    var w = canvas.clientWidth || 1;
    var h = canvas.clientHeight || 1;
    var fw = graphData.layout.width || 1600;
    var fh = graphData.layout.height || 1000;
    view.scale = Math.min(w / fw, h / fh) * 0.95;
    view.x = (w - fw * view.scale) / 2;
    view.y = (h - fh * view.scale) / 2;
  }

  function resizeCanvas() {
    var ratio = window.devicePixelRatio || 1;
    canvas.width = Math.max(1, Math.floor(canvas.clientWidth * ratio));
    canvas.height = Math.max(1, Math.floor(canvas.clientHeight * ratio));
    requestDraw();
  }

  function nodeColor(node) {
    return DOMAIN_COLORS[node.domain] || FALLBACK_COLOR;
  }

  function requestDraw() {
    window.requestAnimationFrame(draw);
  }

  function draw() {
    if (!ctx) return;
    var ratio = window.devicePixelRatio || 1;
    ctx.setTransform(1, 0, 0, 1, 0, 0);
    ctx.fillStyle = "#0d1117";
    ctx.fillRect(0, 0, canvas.width, canvas.height);
    if (!graphData) return;
    ctx.setTransform(ratio * view.scale, 0, 0, ratio * view.scale, ratio * view.x, ratio * view.y);
    hitIndex = [];
    var i, edge, node;
    ctx.strokeStyle = "#30363f";
    ctx.lineWidth = 1.2 / view.scale;
    var byId = {};
    for (i = 0; i < graphData.nodes.length; i++) byId[graphData.nodes[i].id] = graphData.nodes[i];
    for (i = 0; i < graphData.edges.length; i++) {
      edge = graphData.edges[i];
      var s = byId[edge.source];
      var t = byId[edge.target];
      if (!s || !t) continue;
      ctx.beginPath();
      ctx.moveTo(s.x, s.y);
      ctx.lineTo(t.x, t.y);
      ctx.stroke();
    }
    var radius = 7 / view.scale;
    ctx.font = (11 / view.scale) + "px system-ui";
    for (i = 0; i < graphData.nodes.length; i++) {
      node = graphData.nodes[i];
      ctx.beginPath();
      ctx.arc(node.x, node.y, radius, 0, Math.PI * 2);
      ctx.fillStyle = nodeColor(node);
      ctx.fill();
      ctx.strokeStyle = "#0d1117";
      ctx.lineWidth = 1 / view.scale;
      ctx.stroke();
      ctx.fillStyle = "#8b93a1";
      var label = node.label || node.id;
      if (label.length > 28) label = label.slice(0, 27) + "…";
      ctx.fillText(label, node.x + radius + 3 / view.scale, node.y + 4 / view.scale);
      hitIndex.push({ id: node.id, x: node.x, y: node.y, r: radius * 2.2 });
    }
  }

  function toWorld(clientX, clientY) {
    var rect = canvas.getBoundingClientRect();
    return {
      x: (clientX - rect.left - view.x) / view.scale,
      y: (clientY - rect.top - view.y) / view.scale
    };
  }

  var drag = null;
  canvas.addEventListener("mousedown", function (event) {
    drag = { x: event.clientX, y: event.clientY };
    canvas.classList.add("dragging");
  });
  window.addEventListener("mousemove", function (event) {
    if (!drag) return;
    view.x += event.clientX - drag.x;
    view.y += event.clientY - drag.y;
    drag = { x: event.clientX, y: event.clientY };
    requestDraw();
  });
  window.addEventListener("mouseup", function () {
    drag = null;
    canvas.classList.remove("dragging");
  });
  canvas.addEventListener("wheel", function (event) {
    event.preventDefault();
    var factor = event.deltaY < 0 ? 1.12 : 1 / 1.12;
    var rect = canvas.getBoundingClientRect();
    var cx = event.clientX - rect.left;
    var cy = event.clientY - rect.top;
    view.x = cx - (cx - view.x) * factor;
    view.y = cy - (cy - view.y) * factor;
    view.scale *= factor;
    requestDraw();
  }, { passive: false });
  canvas.addEventListener("click", function (event) {
    if (!graphData) return;
    var point = toWorld(event.clientX, event.clientY);
    for (var i = hitIndex.length - 1; i >= 0; i--) {
      var hit = hitIndex[i];
      var dx = point.x - hit.x;
      var dy = point.y - hit.y;
      if (dx * dx + dy * dy <= hit.r * hit.r) {
        showDetail(hit.id);
        return;
      }
    }
  });

  document.getElementById("graph-fit").addEventListener("click", function () {
    fitView();
    requestDraw();
  });
  document.getElementById("graph-reload").addEventListener("click", loadGraph);
  window.addEventListener("resize", resizeCanvas);

  // ---------------------------------------------------------- detail panel

  function kvRow(table, key, value) {
    var dt = el("dt", null, key);
    var dd = el("dd", null, value);
    table.appendChild(dt);
    table.appendChild(dd);
  }

  function scoreBar(parent, label, raw) {
    var value = parseFloat(raw);
    var row = el("div", "scorebar");
    row.appendChild(el("span", "label", label));
    var bar = el("div", "bar");
    var fill = el("div", "fill");
    fill.style.width = (isNaN(value) ? 0 : Math.max(0, Math.min(1, value)) * 100) + "%";
    bar.appendChild(fill);
    row.appendChild(bar);
    row.appendChild(el("span", "value", isNaN(value) ? String(raw) : value.toFixed(3)));
    parent.appendChild(row);
  }

  function showDetail(nodeId) {
    var panel = document.getElementById("detail-panel");
    panel.classList.remove("hidden");
    var title = document.getElementById("detail-title");
    var body = document.getElementById("detail-body");
    title.textContent = nodeId;
    body.textContent = "";
    body.appendChild(el("p", "empty", "loading…"));
    api("/api/node/" + encodeURIComponent(nodeId)).then(function (node) {
      body.textContent = "";
      title.textContent = node.summary || node.node_id;

      var kv = el("dl", "kv");
      kvRow(kv, "id", node.node_id);
      kvRow(kv, "domain", node.domain);
      kvRow(kv, "kind", node.kind);
      kvRow(kv, "status", node.lifecycle_status);
      kvRow(kv, "version", node.version);
      if (node.symbol) kvRow(kv, "symbol", node.symbol.kind + " @ " + node.symbol.path + ":" + node.symbol.line);
      body.appendChild(kv);

      body.appendChild(el("h3", "detail-h", "Scores (P / M / E)"));
      scoreBar(body, "P (proficiency)", node.scores.p);
      scoreBar(body, "M (mastery)", node.scores.m);
      scoreBar(body, "E (evidence)", node.scores.e);
      scoreBar(body, "uncertainty", node.scores.uncertainty);
      scoreBar(body, "confidence", node.scores.confidence);
      kvRow(body.appendChild(el("dl", "kv")), "prof. level", node.professional_level);

      body.appendChild(el("h3", "detail-h", "Provenance"));
      if (node.provenance.length === 0) {
        body.appendChild(el("p", "empty", "(none)"));
      } else {
        for (var i = 0; i < node.provenance.length; i++) {
          body.appendChild(el("span", "chip", node.provenance[i]));
        }
      }

      body.appendChild(el("h3", "detail-h", "Relations"));
      if (node.relations.length === 0) body.appendChild(el("p", "empty", "(none)"));
      for (var r = 0; r < node.relations.length; r++) {
        body.appendChild(relationButton(node.relations[r]));
      }

      body.appendChild(el("h3", "detail-h", "Hierarchy (callers / callees)"));
      var hierarchyBox = el("div");
      hierarchyBox.appendChild(el("p", "empty", "loading…"));
      body.appendChild(hierarchyBox);
      api("/api/hierarchy/" + encodeURIComponent(node.node_id) + "?depth=1").then(function (hier) {
        hierarchyBox.textContent = "";
        hierarchyBox.appendChild(hierarchyList("Callers", hier.callers.entries));
        hierarchyBox.appendChild(hierarchyList("Callees", hier.callees.entries));
      }).catch(function () {
        hierarchyBox.textContent = "";
        hierarchyBox.appendChild(el("p", "empty", "(no call-graph entry for this node)"));
      });
    }).catch(function (error) {
      body.textContent = "";
      body.appendChild(el("p", "bad-badge", String(error.message || error)));
    });
  }

  function relationButton(rel) {
    var btn = el("button", "linkish", rel.kind + " → " + rel.target);
    btn.type = "button";
    btn.addEventListener("click", function () { showDetail(rel.target); });
    return btn;
  }

  function hierarchyList(title, entries) {
    var box = el("div");
    box.appendChild(el("h3", "detail-h", title + " (" + entries.length + ")"));
    if (entries.length === 0) {
      box.appendChild(el("p", "empty", "(none)"));
      return box;
    }
    for (var i = 0; i < entries.length; i++) {
      var entry = entries[i];
      var label = "d" + entry.depth + " " + entry.name + " (" + entry.kind + ") via " + entry.via_edge_kind;
      var btn = el("button", "linkish", label);
      btn.type = "button";
      btn.setAttribute("data-node", entry.node_id);
      box.appendChild(btn);
      box.appendChild(el("br"));
    }
    return box;
  }

  document.getElementById("detail-close").addEventListener("click", function () {
    document.getElementById("detail-panel").classList.add("hidden");
  });

  // ----------------------------------------------------------------- search

  document.getElementById("search-form").addEventListener("submit", function (event) {
    event.preventDefault();
    var q = document.getElementById("search-q").value;
    var kind = document.getElementById("search-kind").value.trim();
    var lang = document.getElementById("search-lang").value.trim();
    var path = "/api/search?q=" + encodeURIComponent(q);
    if (kind) path += "&kind=" + encodeURIComponent(kind);
    if (lang) path += "&lang=" + encodeURIComponent(lang);
    var results = document.getElementById("search-results");
    results.textContent = "";
    results.appendChild(el("p", "empty", "searching…"));
    api(path).then(function (payload) {
      results.textContent = "";
      if (payload.count === 0) {
        results.appendChild(el("p", "empty", "no matches"));
        return;
      }
      for (var i = 0; i < payload.results.length; i++) {
        results.appendChild(searchHit(payload.results[i]));
      }
    }).catch(function (error) {
      results.textContent = "";
      results.appendChild(el("p", "bad-badge", String(error.message || error)));
    });
  });

  function searchHit(hit) {
    var box = el("div", "hit");
    box.appendChild(el("span", "score", typeof hit.score === "number" ? hit.score.toFixed(3) : ""));
    box.appendChild(el("div", "name", hit.name));
    box.appendChild(el("div", "meta",
      hit.kind + " · " + hit.path + ":" + hit.line + " · via " + hit.source));
    box.addEventListener("click", function () {
      showTab("graph");
      showDetail(hit.node_id);
    });
    return box;
  }

  // ---------------------------------------------------- json panel renderers

  function table(parent, headers, rows) {
    var tbl = el("table");
    var head = el("tr");
    for (var i = 0; i < headers.length; i++) head.appendChild(el("th", null, headers[i]));
    tbl.appendChild(head);
    for (var r = 0; r < rows.length; r++) {
      var row = el("tr");
      for (var c = 0; c < rows[r].length; c++) row.appendChild(el("td", null, rows[r][c]));
      tbl.appendChild(row);
    }
    parent.appendChild(tbl);
  }

  function renderMetrics(container, payload) {
    container.appendChild(el("h3", null, "Health score: " + payload.health.score + " / " + payload.health.max_score));
    var countRows = [];
    var keys = Object.keys(payload.counts).sort();
    for (var i = 0; i < keys.length; i++) countRows.push([keys[i], payload.counts[keys[i]]]);
    container.appendChild(el("h3", null, "Counts"));
    table(container, ["metric", "value"], countRows);
    var cap = payload.publish_capacity;
    container.appendChild(el("h3", null, "Publish capacity"));
    table(container, ["field", "value"], [
      ["estimated_bytes", cap.estimated_bytes],
      ["guard_bytes", cap.guard_bytes],
      ["headroom_bytes", cap.headroom_bytes],
      ["utilization_permille", cap.utilization_permille],
      ["ok", cap.ok]
    ]);
    container.appendChild(el("h3", null, "Latency (ms)"));
    var ops = Object.keys(payload.latency_ms).sort();
    if (ops.length === 0) {
      container.appendChild(el("p", "empty", "(no completed invocations recorded)"));
    } else {
      var rows = [];
      for (var j = 0; j < ops.length; j++) {
        var s = payload.latency_ms[ops[j]];
        rows.push([ops[j], s.count, s.min, s.mean, s.p50, s.p95, s.max]);
      }
      table(container, ["operation", "count", "min", "mean", "p50", "p95", "max"], rows);
    }
  }

  function renderHealth(container, payload) {
    var badge = el("p", payload.ok ? "ok-badge" : "bad-badge",
      payload.ok ? "OK — no error findings" : "ERROR findings present (exit " + payload.exit_code + ")");
    container.appendChild(badge);
    container.appendChild(el("p", null,
      "errors: " + payload.error_count + " · warnings: " + payload.warning_count +
      " · worst: " + payload.worst_severity));
    if (payload.findings.length === 0) {
      container.appendChild(el("p", "empty", "(no findings)"));
      return;
    }
    var rows = [];
    for (var i = 0; i < payload.findings.length; i++) {
      var f = payload.findings[i];
      rows.push([f.severity, f.code, f.message]);
    }
    table(container, ["severity", "code", "message"], rows);
  }

  function renderAnalytics(container, payload) {
    if (payload.partial) {
      container.appendChild(el("p", "bad-badge",
        "PARTIAL: graph exceeds the analytics cap; computed on the first " + payload.node_count +
        " of " + payload.total_nodes + " nodes"));
    }
    container.appendChild(el("h3", null, "Centrality — top by PageRank"));
    var pr = (payload.centrality.global && payload.centrality.global.pagerank) || [];
    table(container, ["node", "pagerank"],
      pr.slice(0, 15).map(function (entry) { return [entry.node_id, entry.score]; }));
    container.appendChild(el("h3", null, "Communities (" + payload.communities.community_count + ")"));
    var comms = payload.communities.communities || [];
    if (comms.length === 0) container.appendChild(el("p", "empty", "(none)"));
    table(container, ["id", "size", "label", "cohesion", "signature"],
      comms.map(function (c) { return [c.id, c.size, c.label, c.cohesion, c.membership_sig]; }));
    container.appendChild(el("h3", null, "Bridges (surprising connections)"));
    var bridges = payload.bridges.bridges || [];
    if (bridges.length === 0) container.appendChild(el("p", "empty", "(none)"));
    table(container, ["source", "target", "relation", "kind", "surprise"],
      bridges.map(function (b) { return [b.source, b.target, b.relation, b.kind, b.surprise_score]; }));
  }

  function renderAudit(container, payload) {
    container.appendChild(el("p", null, payload.count + " most recent audit events (redacted)"));
    if (payload.count === 0) {
      container.appendChild(el("p", "empty", "(no audit events)"));
      return;
    }
    var rows = payload.events.map(function (event) {
      return [event.event_seq, event.event_type, event.actor, event.occurred_at,
        JSON.stringify(event.payload)];
    });
    table(container, ["seq", "type", "actor", "at", "payload"], rows);
  }

  function renderProposals(container, payload) {
    container.appendChild(el("p", null,
      payload.count + " review candidates (proposal-only; nothing here mutates the store)"));
    if (payload.count === 0) {
      container.appendChild(el("p", "empty", "(no candidates)"));
      return;
    }
    var rows = payload.signals.map(function (signal) {
      return [signal.signal, (signal.reason_codes || []).join(", "),
        (signal.source_node_ids || []).join(", "),
        signal.low_confidence ? "hint" : "primary"];
    });
    table(container, ["signal", "reasons", "nodes", "level"], rows);
  }

  // ------------------------------------------------------------------ refs

  function renderRefs(container, payload) {
    container.appendChild(el("h3", null, "Unresolved references"));
    table(container, ["status", "count"], [
      ["pending", payload.pending],
      ["resolved", payload.resolved],
      ["failed", payload.failed],
      ["total", payload.total]
    ]);
    container.appendChild(el("h3", null, "Top failed name tails"));
    if (payload.top_failed.length === 0) container.appendChild(el("p", "empty", "(none)"));
    table(container, ["name_tail", "count"],
      payload.top_failed.map(function (row) { return [row.name_tail, row.count]; }));
    container.appendChild(el("h3", null, "Failed samples"));
    if (payload.failed_samples.length === 0) container.appendChild(el("p", "empty", "(none)"));
    table(container, ["source", "target", "kind", "reason"],
      payload.failed_samples.map(function (row) {
        return [row.source_node_id, row.target_name, row.kind, row.failure_reason || ""];
      }));
  }

  // Wire the Refs view into the proposals tab? No — refs get their own fetch
  // inside the audit tab footer for one-screen completeness.
  var origAudit = renderAudit;
  renderAudit = function (container, payload) {
    origAudit(container, payload);
    var refsBox = el("div");
    refsBox.appendChild(el("p", "empty", "loading refs…"));
    container.appendChild(refsBox);
    api("/api/refs").then(function (refs) {
      refsBox.textContent = "";
      renderRefs(refsBox, refs);
    }).catch(function () {
      refsBox.textContent = "";
    });
  };

  // ------------------------------------------------------------------- boot

  var tabButtons = document.querySelectorAll(".tab");
  for (var t = 0; t < tabButtons.length; t++) {
    (function (button) {
      button.addEventListener("click", function () {
        showTab(button.getAttribute("data-tab"));
      });
    })(tabButtons[t]);
  }

  if (!TOKEN) {
    setStatus("missing token — open the URL printed by the CLI", true);
  }
  resizeCanvas();
  loadGraph();
})();
