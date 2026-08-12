"""A dockable attribute table for the Leaflet viewers.

Three separate generators build viewers in this repository, so the pane lives
here as one CSS block and one JS block that each of them injects. Duplicating
it per viewer is how the rest of this codebase drifted.

Usage from a generator:

    from leakrelocation.viewer_pane import PANE_CSS, PANE_JS, PANE_HTML

    html = "...<style>" + PANE_CSS + "</style>..." + PANE_HTML + \
           "<script>" + PANE_JS + "</script>"

Then, once the Leaflet layers hold data, register each one:

    AttributePane.register('leaks', 'Relocated leaks', leakLayer);
    AttributePane.build();

`register` takes a key, a display label and an L.geoJSON layer. `build()` reads
the features currently in those layers and renders the tabs.
"""

# Body becomes a flex column so the pane docks below the map rather than
# covering it; the map is given the remaining height.
PANE_CSS = """
html,body{height:100%;width:100%;margin:0;padding:0;font-family:Arial,sans-serif}
body{display:flex;flex-direction:column}
#map{flex:1 1 auto;width:100%;min-height:120px}
#attrPane{flex:0 0 auto;display:flex;flex-direction:column;height:260px;
  border-top:2px solid #888;background:#fff;font-size:12px;overflow:hidden}
#attrPane.collapsed{height:32px}
#attrPaneBar{flex:0 0 auto;display:flex;align-items:center;gap:8px;
  padding:4px 8px;background:#f0f0f0;border-bottom:1px solid #ccc;min-height:24px}
#attrPaneTabs{display:flex;gap:4px;flex-wrap:wrap}
.attr-tab{padding:3px 10px;border:1px solid #bbb;border-radius:3px 3px 0 0;
  background:#e4e4e4;cursor:pointer;white-space:nowrap}
.attr-tab.active{background:#fff;border-bottom-color:#fff;font-weight:bold}
.attr-tab .attr-count{color:#666;font-weight:normal;margin-left:4px}
#attrPaneSpacer{flex:1 1 auto}
#attrFilter{padding:2px 6px;border:1px solid #bbb;border-radius:3px;width:180px}
#attrPaneToggle{padding:2px 10px;border:1px solid #bbb;border-radius:3px;
  background:#fff;cursor:pointer}
#attrPaneNote{color:#666;padding:0 8px;white-space:nowrap}
#attrPaneBody{flex:1 1 auto;overflow:auto}
#attrPane.collapsed #attrPaneBody,#attrPane.collapsed #attrPaneFoot{display:none}
#attrTable{border-collapse:collapse;width:100%}
#attrTable th,#attrTable td{border:1px solid #ddd;padding:2px 6px;
  text-align:left;white-space:nowrap;max-width:320px;overflow:hidden;
  text-overflow:ellipsis}
#attrTable thead th{position:sticky;top:0;background:#f4f4f4;cursor:pointer;
  z-index:1}
#attrTable thead th:hover{background:#e8e8e8}
#attrTable tbody tr:nth-child(even){background:#fafafa}
#attrTable tbody tr:hover{background:#eef6ff;cursor:pointer}
#attrTable tbody tr.selected{background:#ffe9a8}
#attrPaneEmpty{padding:10px;color:#666}
"""

PANE_HTML = """
<div id="attrPane">
  <div id="attrPaneBar">
    <div id="attrPaneTabs"></div>
    <span id="attrPaneNote"></span>
    <span id="attrPaneSpacer"></span>
    <input id="attrFilter" type="search" placeholder="Filter rows..." />
    <button id="attrPaneToggle" title="Show or hide the attribute table">Hide</button>
  </div>
  <div id="attrPaneBody"><div id="attrPaneEmpty">Loading attributes...</div></div>
</div>
"""

# Rendering is capped because a production layer can hold tens of thousands of
# features and a full table would lock the browser.
PANE_JS = r"""
const AttributePane = (function () {
  const MAX_ROWS = 500;
  const MAX_COLUMNS = 24;

  const layers = [];
  let activeKey = null;
  let rows = [];
  let columns = [];
  let sortColumn = null;
  let sortAscending = true;
  let selectedRow = null;

  /* The selected feature, remembered by identity rather than by the row element
     or the Leaflet layer. Both of those are thrown away every time the map moves:
     the bbox map reloads each layer on pan and zoom, clearLayers() destroys the
     popup, and build() re-renders the table. Clicking a row used to zoom to the
     feature and then immediately lose both the popup and the highlight. */
  let selectedLayerKey = null;
  let selectedFeatureId = null;
  let restoring = false;
  let renderTimer = null;

  /* A click that is neither on a row nor on a feature clears the selection.
     In Leaflet a click on a feature also reaches the map's own click handler, so
     without this the map handler would wipe the selection the feature click had
     just made. A selection marks itself and the map handler skips one clear.
     The mark is released on the next turn of the event loop rather than after a
     delay, so it cannot swallow a later click on the background. */
  let selectionJustHappened = false;

  function markSelection() {
    selectionJustHappened = true;
    setTimeout(function () { selectionJustHappened = false; }, 0);
  }

  function clearSelection() {
    selectedLayerKey = null;
    selectedFeatureId = null;
    if (selectedRow) selectedRow.classList.remove('selected');
    selectedRow = null;
    if (typeof map !== 'undefined' && map.closePopup) map.closePopup();
    document.querySelectorAll('.leaflet-popup').forEach(function (node) {
      node.remove();
    });
  }

  /* Open a popup without letting it move the map.

     Leaflet pans the map when a popup will not fit in the view. The bbox map
     reloads every layer on moveend, each reload rebuilds this table, and the
     rebuild restores the selected feature's popup - which panned the map again.
     One row click set off 33 map moves and 306 layer fetches before it settled,
     measured in a browser; with production-sized layers that is the map locking
     up and appearing to re-select rows by itself.

     A restored popup therefore never pans. A popup the user opens by clicking
     still does, because that pan is what they asked for, and the reload it
     triggers finds the popup already open and leaves it alone. */
  function openWithoutPanning(mapLayer) {
    const popup = mapLayer.getPopup && mapLayer.getPopup();
    if (!popup || !popup.options) {
      mapLayer.openPopup();
      return;
    }
    const saved = popup.options.autoPan;
    popup.options.autoPan = false;
    try {
      mapLayer.openPopup();
    } finally {
      popup.options.autoPan = saved;
    }
  }

  /* A stable id for a feature across reloads. OBJECTID is what the services use;
     the fallbacks keep this working for a layer without one. */
  function featureId(feature) {
    const props = (feature && feature.properties) || {};
    for (const key of ['OBJECTID', 'objectid', 'GLOBALID', 'GlobalID', 'globalid']) {
      if (props[key] !== undefined && props[key] !== null && props[key] !== '') {
        return key + ':' + String(props[key]);
      }
    }
    return 'props:' + JSON.stringify(props).slice(0, 200);
  }

  function escapeHtml(value) {
    if (value === null || value === undefined) return '';
    return String(value)
      .replaceAll('&', '&amp;').replaceAll('<', '&lt;').replaceAll('>', '&gt;');
  }

  function register(key, label, layer) {
    layers.push({ key: key, label: label, layer: layer });
    if (activeKey === null) activeKey = key;
  }

  function entryFor(key) {
    return layers.find(function (item) { return item.key === key; });
  }

  /* Collect the features Leaflet currently holds for a layer.
     Recurses, because a layer may be an L.layerGroup wrapping an L.geoJSON
     rather than holding features directly. */
  function collect(entry) {
    const collected = [];
    if (!entry || !entry.layer) return collected;

    function walk(layer) {
      if (!layer) return;
      if (layer.feature) {
        collected.push({ feature: layer.feature, mapLayer: layer });
        return;
      }
      if (layer.eachLayer) layer.eachLayer(walk);
    }

    walk(entry.layer);
    return collected;
  }

  function columnsFor(collected) {
    const seen = [];
    for (const item of collected) {
      const props = item.feature.properties || {};
      for (const key of Object.keys(props)) {
        if (!seen.includes(key)) seen.push(key);
        if (seen.length >= MAX_COLUMNS) return seen;
      }
    }
    return seen;
  }

  function filterText() {
    const input = document.getElementById('attrFilter');
    return input ? input.value.trim().toLowerCase() : '';
  }

  function visibleRows() {
    const needle = filterText();
    if (!needle) return rows;
    return rows.filter(function (item) {
      const props = item.feature.properties || {};
      return Object.keys(props).some(function (key) {
        const value = props[key];
        return value !== null && value !== undefined
          && String(value).toLowerCase().includes(needle);
      });
    });
  }

  function sorted(list) {
    if (sortColumn === null) return list;
    const copy = list.slice();
    copy.sort(function (left, right) {
      const a = (left.feature.properties || {})[sortColumn];
      const b = (right.feature.properties || {})[sortColumn];
      if (a === b) return 0;
      if (a === null || a === undefined) return 1;
      if (b === null || b === undefined) return -1;
      const na = Number(a), nb = Number(b);
      const numeric = !Number.isNaN(na) && !Number.isNaN(nb) && a !== '' && b !== '';
      const result = numeric ? na - nb : String(a).localeCompare(String(b));
      return sortAscending ? result : -result;
    });
    return copy;
  }

  function renderTabs() {
    const container = document.getElementById('attrPaneTabs');
    if (!container) return;
    container.innerHTML = layers.map(function (entry) {
      const count = collect(entry).length;
      const active = entry.key === activeKey ? ' active' : '';
      return '<div class="attr-tab' + active + '" data-key="' + escapeHtml(entry.key) + '">'
        + escapeHtml(entry.label)
        + '<span class="attr-count">' + count.toLocaleString() + '</span></div>';
    }).join('');
    container.querySelectorAll('.attr-tab').forEach(function (tab) {
      tab.addEventListener('click', function () { select(tab.dataset.key); });
    });
  }

  function renderTable() {
    const body = document.getElementById('attrPaneBody');
    const note = document.getElementById('attrPaneNote');
    if (!body) return;

    const shown = sorted(visibleRows());

    if (!shown.length) {
      body.innerHTML = '<div id="attrPaneEmpty">'
        + (rows.length ? 'No rows match the filter.' : 'No features in this layer.')
        + '</div>';
      if (note) note.textContent = '';
      return;
    }

    const capped = shown.slice(0, MAX_ROWS);
    if (note) {
      note.textContent = capped.length < shown.length
        ? 'showing ' + capped.length.toLocaleString() + ' of ' + shown.length.toLocaleString()
        : shown.length.toLocaleString() + ' rows';
    }

    let head = '';
    for (const column of columns) {
      const arrow = column === sortColumn ? (sortAscending ? ' ▲' : ' ▼') : '';
      head += '<th data-column="' + escapeHtml(column) + '">' + escapeHtml(column) + arrow + '</th>';
    }

    let table = '<table id="attrTable"><thead><tr>' + head + '</tr></thead><tbody>';
    capped.forEach(function (item, index) {
      const props = item.feature.properties || {};
      let cells = '';
      for (const column of columns) cells += '<td>' + escapeHtml(props[column]) + '</td>';
      /* Re-apply the highlight as the row is written, so it survives the
         re-render that follows every map move. */
      const isSelected = activeKey === selectedLayerKey
        && featureId(item.feature) === selectedFeatureId;
      table += '<tr data-index="' + index + '"'
        + (isSelected ? ' class="selected"' : '') + '>' + cells + '</tr>';
    });
    table += '</tbody></table>';
    body.innerHTML = table;

    selectedRow = body.querySelector('#attrTable tbody tr.selected');
    if (selectedRow) {
      selectedRow.scrollIntoView({ block: 'nearest' });
      /* Show the feature's attributes on the map again. The `restoring` guard
         only covers re-entering synchronously; the loop this used to cause was
         asynchronous - see openWithoutPanning. */
      if (!restoring) {
        restoring = true;
        try {
          const item = capped[Number(selectedRow.dataset.index)];
          if (item && item.mapLayer && item.mapLayer.openPopup
              && !(item.mapLayer.isPopupOpen && item.mapLayer.isPopupOpen())) {
            /* Sweep any popup DOM Leaflet has lost track of. A layer destroyed
               by clearLayers() while its popup was open leaves the element
               behind, and it is no longer a map layer, so removeLayer cannot
               reach it - only the DOM node can be removed. */
            if (typeof map !== 'undefined' && map.closePopup) map.closePopup();
            document.querySelectorAll('.leaflet-popup').forEach(function (node) {
              node.remove();
            });
            openWithoutPanning(item.mapLayer);
          }
        } finally {
          restoring = false;
        }
      }
    }

    body.querySelectorAll('#attrTable thead th').forEach(function (header) {
      header.addEventListener('click', function () {
        const column = header.dataset.column;
        if (sortColumn === column) { sortAscending = !sortAscending; }
        else { sortColumn = column; sortAscending = true; }
        renderTable();
      });
    });

    body.querySelectorAll('#attrTable tbody tr').forEach(function (tr) {
      tr.addEventListener('click', function () {
        const item = capped[Number(tr.dataset.index)];
        selectedLayerKey = activeKey;
        selectedFeatureId = item ? featureId(item.feature) : null;
        markSelection();
        highlight(tr);
        focusFeature(item);
      });
    });
  }

  function highlight(tr) {
    if (selectedRow) selectedRow.classList.remove('selected');
    selectedRow = tr;
    if (tr) tr.classList.add('selected');
  }

  /* Zoom to a row's feature and open its popup. */
  function focusFeature(item) {
    if (!item || !item.mapLayer) return;
    const mapLayer = item.mapLayer;
    if (mapLayer.getBounds) {
      const bounds = mapLayer.getBounds();
      if (bounds && bounds.isValid()) {
        map.fitBounds(bounds, { maxZoom: 19, padding: [40, 40] });
      }
    } else if (mapLayer.getLatLng) {
      map.setView(mapLayer.getLatLng(), Math.max(map.getZoom(), 18));
    }
    /* Also without panning. The view has just been centred on this feature on
       purpose, and letting the popup pan away from it undid that: in a short map
       - the attribute pane takes 260px, so a laptop can leave the map around
       250px tall - the popup panned far enough that the feature left the
       viewport, the bbox reload dropped it, and the selection had nothing left to
       restore. Measured: clicking the row for OBJECTID 1 ended with rows 2, 5 and
       7 in view and no selection. A tall popup can now be clipped in a short map,
       which is the better failure: the feature stays where it was put. */
    if (mapLayer.openPopup) openWithoutPanning(mapLayer);
  }

  /* Select the table row for a feature clicked on the map. */
  function selectFromMap(mapLayer) {
    for (const entry of layers) {
      const collected = collect(entry);
      if (collected.some(function (item) { return item.mapLayer === mapLayer; })) {
        selectedLayerKey = entry.key;
        selectedFeatureId = mapLayer.feature ? featureId(mapLayer.feature) : null;
        markSelection();
        if (entry.key !== activeKey) select(entry.key);
        break;
      }
    }
    const body = document.getElementById('attrPaneBody');
    if (!body) return;
    const shown = sorted(visibleRows()).slice(0, MAX_ROWS);
    const index = shown.findIndex(function (item) { return item.mapLayer === mapLayer; });
    if (index < 0) return;
    const tr = body.querySelector('#attrTable tbody tr[data-index="' + index + '"]');
    if (!tr) return;
    highlight(tr);
    tr.scrollIntoView({ block: 'nearest' });
  }

  function select(key) {
    activeKey = key;
    const entry = entryFor(key);
    rows = collect(entry);
    columns = columnsFor(rows);
    sortColumn = null;
    sortAscending = true;
    /* selectedLayerKey and selectedFeatureId deliberately survive: renderTable
       re-applies the highlight when the selected feature is in this layer. */
    selectedRow = null;
    renderTabs();
    renderTable();
  }

  function build() {
    const toggle = document.getElementById('attrPaneToggle');
    const pane = document.getElementById('attrPane');
    if (toggle && pane && !toggle.dataset.wired) {
      toggle.dataset.wired = '1';
      toggle.addEventListener('click', function () {
        pane.classList.toggle('collapsed');
        toggle.textContent = pane.classList.contains('collapsed') ? 'Show' : 'Hide';
        if (map && map.invalidateSize) map.invalidateSize();
      });
    }
    const filter = document.getElementById('attrFilter');
    if (filter && !filter.dataset.wired) {
      filter.dataset.wired = '1';
      filter.addEventListener('input', function () { renderTable(); });
    }

    /* Clicking the map background clears the selection. */
    if (typeof map !== 'undefined' && map.on && !build.wiredMapClear) {
      build.wiredMapClear = true;
      map.on('click', function () {
        if (selectionJustHappened) return;
        clearSelection();
      });
    }

    /* So does clicking inside the pane but not on a row - the empty space below
       the last row, for instance. Row clicks are let through to their own
       handler, and so are header clicks: sorting is a table operation, not a
       deselect, and the highlight is meant to survive the re-render. */
    const paneBody = document.getElementById('attrPaneBody');
    if (paneBody && !paneBody.dataset.wiredClear) {
      paneBody.dataset.wiredClear = '1';
      paneBody.addEventListener('click', function (event) {
        const target = event.target;
        if (!target.closest) return;
        if (target.closest('#attrTable tbody tr')) return;
        if (target.closest('#attrTable thead')) return;
        clearSelection();
      });
    }
    scheduleRender();
    if (map && map.invalidateSize) map.invalidateSize();
  }

  /* The map reloads nine layers per move and calls build() as each one finishes,
     so an unguarded build re-read every layer and re-rendered the whole table
     nine times per pan. One render per burst is what the user sees anyway.
     Clicking a tab still renders straight away - that path calls select(). */
  function scheduleRender() {
    if (renderTimer !== null) clearTimeout(renderTimer);
    renderTimer = setTimeout(function () {
      renderTimer = null;
      select(activeKey);
    }, 90);
  }

  return { register: register, build: build, select: select,
           selectFromMap: selectFromMap, clearSelection: clearSelection };
})();
"""


def pane_assets():
    """Return (css, html, js) for embedding in a generated viewer."""
    return PANE_CSS, PANE_HTML, PANE_JS
