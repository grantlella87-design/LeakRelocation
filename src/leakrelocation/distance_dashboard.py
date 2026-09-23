"""Render a relocation-distance report as one self-contained HTML page.

No CDN and no external font. The workflow runs inside a corporate network behind
a TLS-intercepting proxy, which is why Leaflet is vendored rather than linked, and
a dashboard that needs the internet to draw a bar chart would be a dashboard that
does not open. Everything - style, charts, interaction - is in the one file, so it
can be mailed to someone who has none of this checked out.

What is embedded is aggregate: cumulative counts per bin, group statistics, and
the furthest relocations by row. The 90,987-row table is not in here. That is a
deliberate limit, not an oversight - the detail belongs in the GeoPackage and in
the --detail CSVs, which stay in the output folder.

The charts are hand-drawn SVG built from the cumulative curve. Any display
binning is a subtraction of two cumulative counts, so re-binning for a chart
cannot disagree with the slider reading off the same array.
"""
import json

PAGE_CSS = """
:root{
  --bg:#f6f7f9; --panel:#ffffff; --ink:#15181d; --muted:#5b6472;
  --line:#dfe3e9; --line-soft:#eceff4;
  --under:#1f6feb; --under-soft:#d3e3fd;
  --over:#c2410c; --over-soft:#fde3d3;
  --grid:#e7eaef; --warn-bg:#fff8e6; --warn-line:#e8c766; --warn-ink:#6b4e05;
}
@media (prefers-color-scheme:dark){
  :root:not([data-theme="light"]){
    --bg:#101317; --panel:#181c22; --ink:#e8ebf0; --muted:#98a2b3;
    --line:#2a313b; --line-soft:#222831;
    --under:#5296ff; --under-soft:#1b3050;
    --over:#f97a45; --over-soft:#43220f;
    --grid:#242b34; --warn-bg:#2a2410; --warn-line:#6b5a1d; --warn-ink:#e8d494;
  }
}
:root[data-theme="dark"]{
  --bg:#101317; --panel:#181c22; --ink:#e8ebf0; --muted:#98a2b3;
  --line:#2a313b; --line-soft:#222831;
  --under:#5296ff; --under-soft:#1b3050;
  --over:#f97a45; --over-soft:#43220f;
  --grid:#242b34; --warn-bg:#2a2410; --warn-line:#6b5a1d; --warn-ink:#e8d494;
}
*{box-sizing:border-box}
body{
  margin:0; padding:24px 16px 64px; background:var(--bg); color:var(--ink);
  font:14px/1.5 -apple-system,BlinkMacSystemFont,"Segoe UI",Roboto,Helvetica,Arial,sans-serif;
  -webkit-font-smoothing:antialiased;
}
.wrap{max-width:1180px; margin:0 auto}
h1{font-size:22px; margin:0 0 4px; letter-spacing:-.01em}
h2{font-size:15px; margin:0 0 14px; letter-spacing:-.005em}
.sub{color:var(--muted); font-size:13px; margin:0 0 22px}
.sub code{font-size:12px; word-break:break-all}
.panel{
  background:var(--panel); border:1px solid var(--line); border-radius:10px;
  padding:18px; margin-bottom:16px;
}
.tiles{display:grid; gap:12px; margin-bottom:16px;
  grid-template-columns:repeat(auto-fit,minmax(min(150px,100%),1fr));}
.tile{background:var(--panel); border:1px solid var(--line); border-radius:10px; padding:14px 16px}
.tile .k{color:var(--muted); font-size:11px; text-transform:uppercase; letter-spacing:.06em}
.tile .v{font-size:24px; font-weight:600; margin-top:6px; font-variant-numeric:tabular-nums; letter-spacing:-.02em}
.tile .n{color:var(--muted); font-size:12px; margin-top:2px}
.warn{background:var(--warn-bg); border:1px solid var(--warn-line); color:var(--warn-ink);
  border-radius:8px; padding:12px 14px; margin-bottom:16px; font-size:13px}
.warn ul{margin:6px 0 0; padding-left:20px}

/* --- the sliding scale ------------------------------------------------ */
.scale-head{display:flex; flex-wrap:wrap; align-items:baseline; gap:10px; margin-bottom:4px}
.scale-head h2{margin:0}
.readout{display:grid; grid-template-columns:1fr 1fr; gap:14px; margin:16px 0 4px}
.readout .side .lab{font-size:11px; text-transform:uppercase; letter-spacing:.06em; color:var(--muted)}
.readout .side .big{font-size:30px; font-weight:650; font-variant-numeric:tabular-nums; letter-spacing:-.02em; line-height:1.15}
.readout .side .cnt{font-size:13px; color:var(--muted); font-variant-numeric:tabular-nums}
.readout .under .big{color:var(--under)}
.readout .over .big{color:var(--over)}
.splitbar{display:flex; height:14px; border-radius:7px; overflow:hidden; background:var(--line-soft); margin:12px 0 18px}
.splitbar i{display:block; height:100%; transition:width .08s linear}
.splitbar .a{background:var(--under)}
.splitbar .b{background:var(--over)}
.sliderow{display:flex; align-items:center; gap:14px; flex-wrap:wrap}
input[type=range]{flex:1 1 320px; min-width:220px; accent-color:var(--under); height:22px}
.numbox{display:flex; align-items:center; gap:6px}
.numbox input{
  width:96px; padding:7px 9px; border:1px solid var(--line); border-radius:7px;
  background:var(--bg); color:var(--ink); font:inherit; font-variant-numeric:tabular-nums;
}
.presets{display:flex; flex-wrap:wrap; gap:6px; margin-top:14px}
.presets button, .toggle button{
  border:1px solid var(--line); background:var(--bg); color:var(--muted);
  border-radius:999px; padding:5px 11px; font:inherit; font-size:12px; cursor:pointer;
}
.presets button:hover, .toggle button:hover{color:var(--ink); border-color:var(--muted)}
.presets button[aria-pressed=true], .toggle button[aria-pressed=true]{
  background:var(--under); border-color:var(--under); color:#fff;
}
.toggle{display:flex; gap:6px; flex-wrap:wrap}
.chart-head{display:flex; justify-content:space-between; align-items:flex-start; gap:12px; flex-wrap:wrap; margin-bottom:12px}
svg{display:block; width:100%; height:auto; overflow:visible}
.note{color:var(--muted); font-size:12px; margin:10px 0 0}
.tw{overflow-x:auto; margin:0 -2px}
table{width:100%; border-collapse:collapse; font-variant-numeric:tabular-nums}
th,td{text-align:right; padding:7px 10px; border-bottom:1px solid var(--line-soft); white-space:nowrap}
th:first-child,td:first-child{text-align:left; white-space:normal; min-width:118px}
thead th{
  color:var(--muted); font-size:11px; text-transform:uppercase; letter-spacing:.05em;
  font-weight:600; border-bottom:1px solid var(--line); position:sticky; top:0; background:var(--panel);
}
tbody tr:hover{background:var(--line-soft)}
.scroll{max-height:420px; overflow:auto; border:1px solid var(--line); border-radius:8px}
.grid2{display:grid; gap:16px;
  grid-template-columns:repeat(auto-fit,minmax(min(430px,100%),1fr));}
.pill{display:inline-block; padding:1px 7px; border-radius:999px; font-size:11px; border:1px solid var(--line); color:var(--muted)}
.pill.hot{background:var(--over-soft); border-color:var(--over); color:var(--over)}
.modebar{display:flex; align-items:center; gap:10px; flex-wrap:wrap;
  background:var(--panel); border:1px solid var(--line); border-radius:999px;
  padding:6px 8px 6px 14px; margin-bottom:16px; font-size:13px}
.modebar .what{color:var(--muted)}
.modebar .now{font-weight:650}
.modebar a, .modebar span.off{
  border:1px solid var(--line); border-radius:999px; padding:4px 11px;
  font-size:12px; text-decoration:none; color:var(--muted)}
.modebar a:hover{color:var(--ink); border-color:var(--muted)}
.modebar span.off{opacity:.55}
.delta{display:grid; gap:12px; margin:4px 0 16px;
  grid-template-columns:repeat(auto-fit,minmax(min(160px,100%),1fr))}
.delta div{border:1px solid var(--line); border-radius:9px; padding:12px 14px;
  display:flex; flex-direction:column}
.delta .v{margin-top:auto}
.delta .k{color:var(--muted); font-size:11px; text-transform:uppercase; letter-spacing:.06em}
.delta .v{font-size:22px; font-weight:650; padding-top:5px; font-variant-numeric:tabular-nums}
.delta .gain .v{color:var(--under)}
.delta .bad .v{color:var(--over)}
.legend{display:flex; gap:14px; flex-wrap:wrap; font-size:12px; color:var(--muted)}
.legend i{width:10px; height:10px; border-radius:2px; display:inline-block; margin-right:5px; vertical-align:-1px}
@media (max-width:640px){
  body{padding:16px 16px 48px}
  .readout{grid-template-columns:1fr}
  .readout .side .big{font-size:26px}
}
"""

PAGE_JS = r"""
(function(){
  var D = window.REPORT;
  var edges = D.curve.edges;
  var series = D.curve.series;
  var ALL = "All relocations";
  var total = (series[ALL] && series[ALL][series[ALL].length-1]) || 0;

  function fmt(n){
    if (n === null || n === undefined) return "—";
    return Number(n).toLocaleString();
  }
  function ft(v, dp){
    if (v === null || v === undefined) return "—";
    return Number(v).toFixed(dp === undefined ? 1 : dp);
  }
  function pct(part, whole){
    if (!whole) return "—";
    return (100*part/whole).toFixed(1) + "%";
  }

  // Largest edge index whose edge is <= value. The slider snaps to edges, so
  // what it reports is a stored count and never an interpolation.
  function indexFor(value){
    var lo = 0, hi = edges.length - 1;
    if (value <= edges[0]) return 0;
    if (value >= edges[hi]) return hi;
    while (lo < hi){
      var mid = (lo + hi + 1) >> 1;
      if (edges[mid] <= value) lo = mid; else hi = mid - 1;
    }
    return lo;
  }
  // Count at or under a distance, for any series.
  function cumAt(name, value){
    var arr = series[name];
    if (!arr) return 0;
    return arr[indexFor(value)];
  }

  var slider   = document.getElementById("thresh");
  var box      = document.getElementById("threshBox");
  var elUnder  = document.getElementById("pctUnder");
  var elOver   = document.getElementById("pctOver");
  var elCUnder = document.getElementById("cntUnder");
  var elCOver  = document.getElementById("cntOver");
  var barA     = document.getElementById("barA");
  var barB     = document.getElementById("barB");
  var byLayer  = document.getElementById("layerSplit");
  var caption  = document.getElementById("threshCaption");

  slider.max = String(edges.length - 1);

  var current = indexFor(D.defaultThresholdFt);

  function layerRows(value){
    var names = Object.keys(series).filter(function(n){ return n !== ALL; });
    if (!names.length) return "";
    var html = '<div class="tw"><table><thead><tr><th>Matched to</th><th>Within</th>' +
               '<th>Over</th><th>% within</th></tr></thead><tbody>';
    names.forEach(function(name){
      var arr = series[name];
      var whole = arr[arr.length-1];
      var under = cumAt(name, value);
      html += '<tr><td>'+esc(name)+'</td><td>'+fmt(under)+'</td><td>'+
              fmt(whole-under)+'</td><td>'+pct(under, whole)+'</td></tr>';
    });
    return html + '</tbody></table></div>';
  }

  function esc(s){
    return String(s).replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;');
  }

  function render(){
    var value = edges[current];
    var under = cumAt(ALL, value);
    var over  = total - under;
    slider.value = String(current);
    if (document.activeElement !== box) box.value = ft(value);
    elUnder.textContent  = pct(under, total);
    elOver.textContent   = pct(over, total);
    elCUnder.textContent = fmt(under) + " of " + fmt(total);
    elCOver.textContent  = fmt(over) + " of " + fmt(total);
    var share = total ? (100*under/total) : 0;
    barA.style.width = share + "%";
    barB.style.width = (100-share) + "%";
    byLayer.innerHTML = layerRows(value);
    caption.textContent = "Relocations of " + ft(value) + " ft or less, against " +
                          "those that moved further.";
    document.querySelectorAll(".presets button").forEach(function(b){
      b.setAttribute("aria-pressed", String(Number(b.dataset.ft) === value));
    });
    drawHistogram();
    drawCurve();
  }

  slider.addEventListener("input", function(){
    current = Number(slider.value); render();
  });
  box.addEventListener("change", function(){
    var v = parseFloat(box.value);
    if (isFinite(v)) { current = indexFor(v); }
    render();
  });
  document.querySelectorAll(".presets button").forEach(function(b){
    b.addEventListener("click", function(){
      current = indexFor(Number(b.dataset.ft)); render();
    });
  });

  /* --- charts --------------------------------------------------------- */

  // How much of the x axis to draw. Full range is mostly empty - the tail runs
  // to the maximum search radius while the mass sits in the first few feet - so
  // the default view is clipped to p99 and says so.
  var views = [
    {id:"p99",  label:"to p99"},
    {id:"pass", label:"to 100 ft"},
    {id:"full", label:"full range"}
  ];
  var view = "p99";
  document.querySelectorAll(".toggle button").forEach(function(b){
    b.addEventListener("click", function(){
      view = b.dataset.view;
      document.querySelectorAll(".toggle button").forEach(function(o){
        o.setAttribute("aria-pressed", String(o.dataset.view === view));
      });
      drawHistogram(); drawCurve();
    });
  });

  function xMax(){
    var last = edges[edges.length-1];
    var base;
    if (view === "pass")      base = Math.min(100, last);
    else if (view === "full") base = last;
    else                      base = Math.max(D.distance.percentiles.p99 || 0, edges[1] || 1);
    // The marker is the point of the chart, so the view grows to keep it in
    // frame rather than letting it slide off the right edge.
    return Math.min(last, Math.max(base, edges[current] * 1.12));
  }

  function ticks(hi, count){
    var raw = hi / count;
    var mag = Math.pow(10, Math.floor(Math.log10(raw)));
    var step = [1,2,2.5,5,10].map(function(m){ return m*mag; })
                 .find(function(s){ return s >= raw; }) || mag*10;
    var out = [];
    for (var t = 0; t <= hi + 1e-9; t += step) out.push(Math.round(t*100)/100);
    return out;
  }

  function svgEl(host, w, h){
    host.innerHTML = "";
    var s = document.createElementNS("http://www.w3.org/2000/svg","svg");
    s.setAttribute("viewBox","0 0 "+w+" "+h);
    s.setAttribute("role","img");
    host.appendChild(s);
    return s;
  }
  function add(parent, name, attrs, text){
    var e = document.createElementNS("http://www.w3.org/2000/svg", name);
    for (var k in attrs) e.setAttribute(k, attrs[k]);
    if (text !== undefined) e.textContent = text;
    parent.appendChild(e);
    return e;
  }

  var W = 940, H = 300, PAD = {t:12, r:16, b:34, l:56};

  function drawHistogram(){
    var host = document.getElementById("histo");
    var hi = xMax();
    var s = svgEl(host, W, H);
    var iw = W - PAD.l - PAD.r, ih = H - PAD.t - PAD.b;
    var bars = 58;
    var step = hi / bars;
    var counts = [], peak = 0;
    for (var i = 0; i < bars; i++){
      var a = i*step, b = (i+1)*step;
      var c = cumAt(ALL, b) - (i === 0 ? 0 : cumAt(ALL, a));
      counts.push(c); if (c > peak) peak = c;
    }
    var overflow = total - cumAt(ALL, hi);
    var yOf = function(c){ return PAD.t + ih - (peak ? (c/peak)*ih : 0); };
    var xOf = function(v){ return PAD.l + (v/hi)*iw; };

    ticks(peak, 4).forEach(function(t){
      add(s,"line",{x1:PAD.l, x2:PAD.l+iw, y1:yOf(t), y2:yOf(t), stroke:"var(--grid)"});
      add(s,"text",{x:PAD.l-8, y:yOf(t)+4, "text-anchor":"end", fill:"var(--muted)",
                    "font-size":"11"}, t.toLocaleString());
    });
    var value = edges[current];
    counts.forEach(function(c, i){
      var a = i*step;
      var w = Math.max(1, iw/bars - 1.5);
      add(s,"rect",{x:xOf(a), y:yOf(c), width:w, height:PAD.t+ih-yOf(c),
                    fill: a + step/2 <= value ? "var(--under)" : "var(--over)",
                    rx:1.5});
    });
    if (value <= hi){
      add(s,"line",{x1:xOf(value), x2:xOf(value), y1:PAD.t-4, y2:PAD.t+ih,
                    stroke:"var(--ink)", "stroke-width":1.5, "stroke-dasharray":"4 3"});
      add(s,"text",{x:xOf(value)+6, y:PAD.t+6, fill:"var(--ink)", "font-size":"11",
                    "font-weight":"600"}, ft(value)+" ft");
    }
    ticks(hi, 6).forEach(function(t){
      add(s,"text",{x:xOf(t), y:H-12, "text-anchor":"middle", fill:"var(--muted)",
                    "font-size":"11"}, t.toLocaleString());
    });
    add(s,"line",{x1:PAD.l, x2:PAD.l+iw, y1:PAD.t+ih, y2:PAD.t+ih, stroke:"var(--line)"});
    document.getElementById("histoNote").textContent =
      "Distance moved, ft. " + (overflow > 0
        ? fmt(overflow) + " relocation" + (overflow===1?"":"s") + " fall beyond " +
          ft(hi) + " ft and are off this view."
        : "Every relocation is in view.");
  }

  function drawCurve(){
    var host = document.getElementById("curve");
    var hi = xMax();
    var s = svgEl(host, W, H);
    var iw = W - PAD.l - PAD.r, ih = H - PAD.t - PAD.b;
    var xOf = function(v){ return PAD.l + (v/hi)*iw; };
    var yOf = function(p){ return PAD.t + ih - (p/100)*ih; };

    [0,25,50,75,100].forEach(function(p){
      add(s,"line",{x1:PAD.l, x2:PAD.l+iw, y1:yOf(p), y2:yOf(p), stroke:"var(--grid)"});
      add(s,"text",{x:PAD.l-8, y:yOf(p)+4, "text-anchor":"end", fill:"var(--muted)",
                    "font-size":"11"}, p+"%");
    });

    var names = [ALL].concat(Object.keys(series).filter(function(n){ return n!==ALL; }));
    var colors = ["var(--under)","var(--over)","#7c5cd6","#0f8a6a","#b8860b"];
    names.forEach(function(name, n){
      var arr = series[name];
      var whole = arr[arr.length-1];
      if (!whole) return;
      var d = "", started = false;
      for (var i = 0; i < edges.length; i++){
        if (edges[i] > hi) break;
        var x = xOf(edges[i]), y = yOf(100*arr[i]/whole);
        d += (started ? "L" : "M") + x.toFixed(1) + " " + y.toFixed(1) + " ";
        started = true;
      }
      add(s,"path",{d:d, fill:"none", stroke:colors[n % colors.length],
                    "stroke-width": n === 0 ? 2.4 : 1.6,
                    "stroke-dasharray": n === 0 ? "" : "5 3"});
    });

    var value = edges[current];
    if (value <= hi){
      var share = total ? 100*cumAt(ALL, value)/total : 0;
      add(s,"line",{x1:xOf(value), x2:xOf(value), y1:PAD.t-4, y2:PAD.t+ih,
                    stroke:"var(--ink)", "stroke-width":1.5, "stroke-dasharray":"4 3"});
      add(s,"line",{x1:PAD.l, x2:xOf(value), y1:yOf(share), y2:yOf(share),
                    stroke:"var(--ink)", "stroke-width":1, "stroke-dasharray":"2 3",
                    opacity:"0.6"});
      add(s,"circle",{cx:xOf(value), cy:yOf(share), r:4, fill:"var(--ink)"});
      add(s,"text",{x:xOf(value)+7, y:yOf(share)-8, fill:"var(--ink)", "font-size":"11",
                    "font-weight":"600"}, share.toFixed(1)+"% within "+ft(value)+" ft");
    }
    ticks(hi, 6).forEach(function(t){
      add(s,"text",{x:xOf(t), y:H-12, "text-anchor":"middle", fill:"var(--muted)",
                    "font-size":"11"}, t.toLocaleString());
    });
    add(s,"line",{x1:PAD.l, x2:PAD.l+iw, y1:PAD.t+ih, y2:PAD.t+ih, stroke:"var(--line)"});

    var legend = names.map(function(name, n){
      return '<span><i style="background:'+colors[n % colors.length]+'"></i>'+
             esc(name)+'</span>';
    }).join("");
    document.getElementById("curveLegend").innerHTML = legend;
  }

  render();
})();
"""


def number(value, places=1):
    """A number for a table cell, or an em dash when there is not one."""
    if value is None:
        return "&mdash;"
    if isinstance(value, float):
        if value != value or value in (float("inf"), float("-inf")):
            return "&mdash;"
        return f"{value:,.{places}f}"
    return f"{value:,}"


def escape(value):
    if value is None:
        return ""
    return (str(value).replace("&", "&amp;").replace("<", "&lt;")
            .replace(">", "&gt;").replace('"', "&quot;"))


def tile(key, value, note=""):
    note_html = f'<div class="n">{escape(note)}</div>' if note else ""
    return (f'<div class="tile"><div class="k">{escape(key)}</div>'
            f'<div class="v">{value}</div>{note_html}</div>')


def stats_table(rows, first_heading):
    """Group statistics: count, share, median, p90, max."""
    if not rows:
        return '<p class="note">Not recorded in this GeoPackage.</p>'
    total = sum(row["count"] for row in rows) or 1
    head = (f"<thead><tr><th>{escape(first_heading)}</th><th>Relocated</th>"
            f"<th>Share</th><th>Median ft</th><th>p90 ft</th><th>Max ft</th>"
            f"</tr></thead>")
    body = []
    for row in rows:
        body.append(
            "<tr>"
            f'<td>{escape(row["name"])}</td>'
            f'<td>{number(row["count"])}</td>'
            f'<td>{number(100.0 * row["count"] / total)}%</td>'
            f'<td>{number(row["median"])}</td>'
            f'<td>{number(row["p90"])}</td>'
            f'<td>{number(row["max"])}</td>'
            "</tr>")
    return f'<div class="tw"><table>{head}<tbody>{"".join(body)}</tbody></table></div>'


def count_table(rows, first_heading, hot=()):
    if not rows:
        return '<p class="note">Not recorded in this GeoPackage.</p>'
    total = sum(row["count"] for row in rows) or 1
    head = (f"<thead><tr><th>{escape(first_heading)}</th><th>Rows</th>"
            f"<th>Share</th></tr></thead>")
    body = []
    for row in rows:
        mark = ' <span class="pill hot">check</span>' if row["name"] in hot else ""
        body.append(
            "<tr>"
            f'<td>{escape(row["name"])}{mark}</td>'
            f'<td>{number(row["count"])}</td>'
            f'<td>{number(100.0 * row["count"] / total)}%</td>'
            "</tr>")
    return f'<div class="tw"><table>{head}<tbody>{"".join(body)}</tbody></table></div>'


def threshold_table(rows):
    head = ("<thead><tr><th>Within</th><th>Relocations</th><th>% within</th>"
            "<th>Beyond</th><th>% beyond</th></tr></thead>")
    body = []
    for row in rows:
        under, over = row["at_or_under"], row["over"]
        whole = under + over
        body.append(
            "<tr>"
            f'<td>{number(row["ft"])} ft</td>'
            f"<td>{number(under)}</td>"
            f'<td>{number(row["pct_at_or_under"])}%</td>'
            f"<td>{number(over)}</td>"
            f'<td>{number(100.0 * over / whole) if whole else "&mdash;"}%</td>'
            "</tr>")
    return f'<div class="tw"><table>{head}<tbody>{"".join(body)}</tbody></table></div>'


def furthest_table(rows):
    if not rows:
        return '<p class="note">No relocation distances to list.</p>'
    columns = [
        ("LeakKey", "Leak"), ("DistanceFt", "Moved ft"),
        ("SearchRadiusFt", "Found at ft"), ("LinkedLayer", "Matched to"),
        ("LeakMaterial", "Leak material"), ("PipeMaterial", "Pipe material"),
        ("LeakDiameter", "Leak dia"), ("PipeDiameter", "Pipe dia"),
        ("LeakAddress", "Location"),
    ]
    present = [(key, title) for key, title in columns if key in rows[0]]
    head = "<thead><tr>" + "".join(
        f"<th>{escape(title)}</th>" for _, title in present) + "</tr></thead>"
    body = []
    for row in rows:
        cells = []
        for key, _ in present:
            value = row.get(key)
            if key in ("DistanceFt", "SearchRadiusFt", "LeakDiameter", "PipeDiameter"):
                cells.append(f"<td>{number(value)}</td>")
            else:
                cells.append(f"<td>{escape(value) or '&mdash;'}</td>")
        body.append("<tr>" + "".join(cells) + "</tr>")
    return (f'<div class="scroll"><table>{head}<tbody>'
            f"{''.join(body)}</tbody></table></div>")


# What each diameter rule is, in one line, for the mode bar.
MODE_LABELS = {
    "exact": ("Exact diameter",
              "the leak's diameter equals the pipe's"),
    "fuzzy": ("Diameter within one nominal size",
              "the pipe may be one nominal size up or down"),
}


def mode_bar(report, switch_links=None):
    """Which rule produced this output, and a way to the other one.

    This is the transition: two outputs exist side by side and the bar says
    which one is on screen. Without it a reader has only the filename to go on,
    and the two pages are otherwise identical.
    """
    mode = report.get("diameter_mode")
    if not mode and not switch_links:
        return ""
    title, gloss = MODE_LABELS.get(
        mode, (mode or "Unrecorded diameter rule", ""))
    parts = ['<span class="what">Diameter rule</span>'
             f'<span class="now">{escape(title)}</span>']
    if gloss:
        parts.append(f'<span class="what">&mdash; {escape(gloss)}</span>')
    for name, href in (switch_links or {}).items():
        other_title = MODE_LABELS.get(name, (name, ""))[0]
        if href:
            parts.append(f'<a href="{escape(href)}">Switch to '
                         f'{escape(other_title.lower())}</a>')
        else:
            parts.append(f'<span class="off" title="That output has not been '
                         f'written yet">No {escape(name)} output yet</span>')
    return f'<div class="modebar">{"".join(parts)}</div>'


def delta_tile(key, value, kind=""):
    return (f'<div class="{kind}"><div class="k">{escape(key)}</div>'
            f'<div class="v">{value}</div></div>')


def comparison_panel(report):
    """The difference between the two diameter rules, per leak.

    The point of having both outputs is not two sets of numbers but the change
    between them, so this leads with what the widened rule added and states
    plainly whether it took anything away.
    """
    comparison = report.get("comparison")
    if not comparison:
        return ""
    if not comparison.get("usable"):
        return ('<div class="panel"><h2>Against the other diameter rule</h2>'
                f'<p class="note">{escape(comparison.get("why", ""))}</p></div>')

    gained = comparison["gained"]
    base, other = comparison["base_label"], comparison["other_label"]
    distance = comparison["gained_distance"]
    tiles = "".join([
        delta_tile(f"Relocated under {base}", number(comparison["base_relocated"])),
        delta_tile(f"Relocated under {other}", number(comparison["other_relocated"])),
        delta_tile("Gained", f"+{number(gained)}", "gain"),
        delta_tile("Lost", number(comparison["lost"]),
                   "bad" if comparison["lost"] else ""),
        delta_tile("Moved to another pipe",
                   number(comparison["moved_to_another_pipe"]),
                   "bad" if comparison["moved_to_another_pipe"] else ""),
    ])

    if gained:
        # Built outside the f-string: a dict literal inside an f-string
        # expression needs no brace doubling, and doubling it makes a set
        # containing a dict.
        gained_row = [{"name": "Gained relocations", **distance}]
        body = f"""
      <p class="note" style="margin:0 0 14px">
        The {number(gained)} leaks below relocated under <b>{escape(other)}</b>
        and not under <b>{escape(base)}</b>. They are the whole reason to widen
        the rule, and they are also the rows where the diameter is an assumption
        rather than a fact, so they are worth reviewing as a set.
      </p>
      <div class="grid2">
        <div>
          <h2>How much slack they took</h2>
          {count_table(comparison["gained_by_tier"], "Diameter match")}
        </div>
        <div>
          <h2>Which pipe layer found them</h2>
          {count_table(comparison["gained_by_layer"], "Matched to")}
        </div>
        <div>
          <h2>Leak size to pipe size</h2>
          {count_table(comparison["gained_by_size_step"], "Step")}
        </div>
        <div>
          <h2>How far they moved</h2>
          {stats_table(gained_row, "Set")}
          <p class="note">
            Against a median of {number(report["distance"]["median"])} ft across
            every relocation in this output.
          </p>
        </div>
      </div>"""
    else:
        body = ('<p class="note">The two rules produced the same set of '
                'relocations. Nothing was gained by widening the diameter.</p>')

    lost_note = ""
    if comparison["lost"] or comparison["moved_to_another_pipe"]:
        lost_note = """
      <p class="note">
        <b>Read the two red figures first.</b> Widening the diameter rule should
        only ever add relocations: an exact diameter outranks an adjacent one at
        any distance, so every leak the strict run placed should keep the same
        pipe. A non-zero Lost or Moved means that did not hold, and the cause is
        worth finding before either output is used.
      </p>"""

    return f"""  <div class="panel">
    <h2>Against the other diameter rule</h2>
    <div class="delta">{tiles}</div>{lost_note}
    {body}
    <p class="note">Paired on <code>{escape(comparison["key"])}</code>. The leak
      number is not unique in the supplemental data, so the two outputs are
      joined on the leak's own id rather than on its number.</p>
  </div>

"""


def dashboard_html(report, title="Leak relocation distance", switch_links=None):
    """The whole page, as one string."""
    totals = report["totals"]
    distance = report["distance"]
    percentiles = distance["percentiles"]
    default_ft = _default_threshold(report)

    # A </script> inside the data would end the block early. Escaping the slash
    # keeps it a string to JSON and inert to the HTML parser.
    payload = json.dumps(
        {**report, "defaultThresholdFt": default_ft},
        allow_nan=False, ensure_ascii=False).replace("</", "<\\/")

    warnings = ""
    if report.get("warnings"):
        items = "".join(f"<li>{escape(text)}</li>" for text in report["warnings"])
        warnings = f'<div class="warn"><strong>Worth knowing</strong><ul>{items}</ul></div>'

    presets = "".join(
        f'<button type="button" data-ft="{row["ft"]}">{number(row["ft"], 0)} ft</button>'
        for row in report["thresholds"])

    agreement = report.get("material_agreement")
    agreement_html = '<p class="note">Materials not recorded in this GeoPackage.</p>'
    if agreement:
        whole = sum(agreement.values()) or 1
        agreement_html = count_table(
            [{"name": "Exact material match", "count": agreement["exact"]},
             {"name": "Material family or other", "count": agreement["family_or_other"]},
             {"name": "One side blank", "count": agreement["unknown"]}],
            "Leak material against matched pipe")
        agreement_html += (
            f'<p class="note">{number(100.0 * agreement["exact"] / whole)}% of '
            f"relocations put the leak on a pipe of exactly its own recorded "
            f"material. The rest rely on the family fallback, which is a "
            f"deliberate rule and a weaker claim than an exact match.</p>")

    source = escape(report.get("source") or "")
    run = escape(report.get("run_utc") or "not recorded")

    return f"""<!doctype html>
<html lang="en" data-theme="auto">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>{escape(title)}</title>
<style>{PAGE_CSS}</style>
</head>
<body>
<div class="wrap">

  <h1>{escape(title)}</h1>
  <p class="sub">
    {number(totals["relocated"])} relocations from {number(totals["audited"])}
    audited leaks &middot; workflow run {run} &middot; report built
    {escape(report["generated_utc"])}<br><code>{source}</code>
  </p>

  {mode_bar(report, switch_links)}
  {warnings}

  <div class="tiles">
    {tile("Relocated", number(totals["relocated"]),
          f'{number(totals["match_rate_pct"])}% of audited leaks matched a pipe')}
    {tile("Median move", f'{number(distance["median"])} ft', "half moved less than this")}
    {tile("90th percentile", f'{number(percentiles["p90"])} ft', "9 in 10 moved less")}
    {tile("99th percentile", f'{number(percentiles["p99"])} ft', "1 in 100 moved more")}
    {tile("Furthest move", f'{number(distance["max"])} ft', "the single longest relocation")}
    {tile("Already on the pipe", number(distance["on_pipe"]),
          f'moved {number(distance["on_pipe_ft"])} ft or less')}
    {tile("No match", number(totals["unmatched"]), "no eligible pipe found")}
  </div>

  <div class="panel">
    <div class="scale-head">
      <h2>How many moved further than&hellip;</h2>
    </div>
    <p class="note" id="threshCaption"></p>

    <div class="readout">
      <div class="side under">
        <div class="lab">Within the distance</div>
        <div class="big" id="pctUnder">&mdash;</div>
        <div class="cnt" id="cntUnder">&mdash;</div>
      </div>
      <div class="side over">
        <div class="lab">Moved further</div>
        <div class="big" id="pctOver">&mdash;</div>
        <div class="cnt" id="cntOver">&mdash;</div>
      </div>
    </div>

    <div class="splitbar"><i class="a" id="barA"></i><i class="b" id="barB"></i></div>

    <div class="sliderow">
      <input type="range" id="thresh" min="0" step="1" value="0"
             aria-label="Distance threshold in feet">
      <div class="numbox">
        <input type="text" id="threshBox" inputmode="decimal"
               aria-label="Distance threshold, feet"> <span class="note">ft</span>
      </div>
    </div>

    <div class="presets">{presets}</div>

    <div style="margin-top:18px" id="layerSplit"></div>
    <p class="note">
      The slider steps in {number(0.1)} ft up to {number(100, 0)} ft and in
      {number(5, 0)} ft beyond, so it is precise where the relocations are and
      quick to drag across the tail. Every figure is a stored count, not an
      interpolation.
    </p>
  </div>

  <div class="panel">
    <div class="chart-head">
      <h2>Where the relocations fall</h2>
      <div class="toggle">
        <button type="button" data-view="p99" aria-pressed="true">to p99</button>
        <button type="button" data-view="pass" aria-pressed="false">to 100 ft</button>
        <button type="button" data-view="full" aria-pressed="false">full range</button>
      </div>
    </div>
    <div id="histo"></div>
    <p class="note" id="histoNote"></p>
  </div>

  <div class="panel">
    <h2>Share within a distance</h2>
    <div id="curve"></div>
    <div class="legend" id="curveLegend" style="margin-top:10px"></div>
    <p class="note">
      Read it as a promise: at any distance on the bottom axis, the curve is the
      percentage of relocations that moved no further than that.
    </p>
  </div>

{comparison_panel(report)}  <div class="grid2">
    <div class="panel">
      <h2>Fixed thresholds</h2>
      {threshold_table(report["thresholds"])}
      <p class="note">
        100 ft is the matcher's first search pass, which makes it the most
        meaningful line here: beyond it, no eligible pipe was within reach on the
        first look.
      </p>
    </div>

    <div class="panel">
      <h2>Which pass found the pipe</h2>
      {stats_table(report["by_radius"], "Search reached")}
      <p class="note">
        The search starts at 100 ft and widens until it finds an eligible pipe. A
        row below the first pass is a leak with nothing suitable nearby, which is
        a different result from a close snap even when the final distance is
        modest.
      </p>
    </div>

    <div class="panel">
      <h2>By pipe layer</h2>
      {stats_table(report["by_layer"], "Matched to")}
      <p class="note">
        Mains are sparser on the ground than services, so a difference between
        these two rows is expected rather than a fault.
      </p>
    </div>

    <div class="panel">
      <h2>By facility type</h2>
      {stats_table(report["by_facility"], "Facility type")}
    </div>

    <div class="panel">
      <h2>By leak material</h2>
      {stats_table(report["by_leak_material"], "Leak material")}
      <p class="note">
        A material whose median move is far above the rest is a sign its records
        are placed less reliably, not that its pipes are further away.
      </p>
    </div>

    <div class="panel">
      <h2>Material agreement</h2>
      {agreement_html}
    </div>

    <div class="panel">
      <h2>Diameter agreement</h2>
      {stats_table(report["by_diameter_match"], "Diameter match")}
      <p class="note">
        <code>exact</code> is the leak's own diameter. <code>one_size_up</code>
        and <code>one_size_down</code> exist only under the widened rule, and on
        those rows the diameter is an assumption rather than a fact.
      </p>
    </div>

    <div class="panel">
      <h2>Why a leak did not match</h2>
      {count_table(report["no_match_reasons"], "Reason")}
      <p class="note">
        These {number(totals["unmatched"])} leaks were not relocated at all, so
        they appear in no distance figure above.
      </p>
    </div>

    <div class="panel">
      <h2>Date check</h2>
      {count_table(report["date_check"], "Outcome", hot=("no_leak_date",))}
      <p class="note">
        <code>ok</code> means the leak date was compared against the pipe's life
        and the pipe was in service. Anything else means the check could not be
        made, so for those rows the date rule decided nothing.
      </p>
    </div>
  </div>

  <div class="panel">
    <h2>The furthest {len(report["furthest"])} relocations</h2>
    <p class="note" style="margin:0 0 12px">
      The review queue. A leak moved this far was not so much relocated as paired
      with the only pipe in range, and each of these is worth opening.
    </p>
    {furthest_table(report["furthest"])}
  </div>

  <p class="note">
    Aggregate counts, group statistics and the furthest
    {len(report["furthest"])} rows. The full table is the
    <code>leak_relocation_audit</code> layer of the GeoPackage above.
  </p>
</div>
<script>window.REPORT = {payload};</script>
<script>{PAGE_JS}</script>
</body>
</html>
"""


def _default_threshold(report):
    """Where the slider starts.

    The first search pass, when the data reaches that far. It is the one
    threshold in the report with a meaning behind it rather than a round number:
    past it the matcher had to widen its search. When every relocation is shorter
    than that, starting there would open the page on "100% within" and say
    nothing, so it falls back to the 90th percentile.
    """
    pass_ft = 100.0
    biggest = report["distance"].get("max") or 0.0
    if biggest >= pass_ft:
        return pass_ft
    return report["distance"]["percentiles"].get("p90") or biggest
