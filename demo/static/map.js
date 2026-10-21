/* BMA Factory — map view.
 *
 * Two rules this file exists to keep:
 *
 * 1. A pin is never invented. Only projects that already carry
 *    coordinates get a marker. A project with no location is listed
 *    in the side panel with a "Find on map" button, and nothing is
 *    placed until either a suggestion comes back (drawn as a dashed
 *    pin) or the valuer drops the pin by hand.
 *
 * 2. A suggestion is visibly a suggestion. Confirmed pins are solid;
 *    geocoded ones are dashed and say so in the popup. They must never
 *    look the same.
 */
(function () {
  var payload = {};
  var el = document.getElementById('map-pins');
  if (el) {
    try { payload = JSON.parse(el.textContent || '{}'); }
    catch (e) { payload = {}; }
  }
  var GROUPS = payload.groups || {};

  if (typeof L === 'undefined') {
    var side = document.getElementById('map-list');
    if (side) {
      side.innerHTML =
        '<div class="map-empty"><b>The map library did not load.</b><br>' +
        'The project list is still below and still complete.</div>';
      renderList(null);
    }
    return;
  }

  var map = L.map('map', { scrollWheelZoom: true })
           .setView([22.32, 114.17], 11);

  // OpenStreetMap's own tiles. No key, no account, no quota.
  L.tileLayer('https://tile.openstreetmap.org/{z}/{x}/{y}.png', {
    maxZoom: 19,
    attribution: '&copy; <a href="https://www.openstreetmap.org/copyright">OpenStreetMap</a> contributors'
  }).addTo(map);

  var layers = {};
  var markers = {};
  var activeTab = 'pv';

  function esc(s) {
    return String(s == null ? '' : s)
      .replace(/&/g, '&amp;').replace(/</g, '&lt;')
      .replace(/>/g, '&gt;').replace(/"/g, '&quot;');
  }

  function iconFor(pin) {
    var cls = 'map-pin ' +
      (pin.how === 'confirmed' ? 'is-confirmed' : 'is-geocoded') +
      (pin.tab === 'sbr' ? ' is-sbr' : '');
    return L.divIcon({
      className: 'map-marker',
      html: '<div class="' + cls + '"></div>',
      iconSize: [16, 16],
      iconAnchor: [8, 8],
      popupAnchor: [0, -10]
    });
  }

  function popupHtml(pin) {
    var h = '<b>' + esc(pin.name) + '</b>';
    if (pin.address || pin.district) {
      h += '<div class="map-popup-meta">' +
           esc([pin.address, pin.district].filter(Boolean).join(' · ')) +
           '</div>';
    }
    var meta = [];
    if (pin.tab === 'sbr') {
      if (pin.client) meta.push(esc(pin.client));
      if (pin.round) meta.push('Round ' + esc(pin.round));
      if (pin.target) meta.push('target ' + esc(pin.target));
      if (pin.items_needing_update) {
        meta.push('<b style="color:#c5221f">' + esc(pin.items_needing_update) +
                  ' to update</b>');
      }
    } else {
      if (pin.valuation_date) meta.push('valued ' + esc(pin.valuation_date));
      if (pin.adopted_value_hkd) {
        meta.push('HK$' + Number(pin.adopted_value_hkd).toLocaleString());
      }
      if (pin.workbook) meta.push(esc(pin.workbook));
    }
    if (meta.length) h += '<div class="map-popup-meta">' + meta.join(' · ') + '</div>';

    h += '<div class="map-popup-actions">' +
         '<a href="' + esc(pin.href) + '">Open project →</a>';

    if (pin.how === 'geocoded') {
      // The point of the whole exercise: a suggestion is not a fact.
      h += '<button type="button" data-confirm="' + esc(pin.key) + '" ' +
           'data-lat="' + esc(pin.lat) + '" data-lon="' + esc(pin.lon) + '">' +
           'Confirm this location</button>';
      h += '<div class="map-popup-where">Suggested from OpenStreetMap: ' +
           esc(pin.label || 'no label') +
           (pin.lat && pin.lon
             ? ' <br>' + esc(Number(pin.lat).toFixed(5)) + ', ' +
               esc(Number(pin.lon).toFixed(5))
             : '') +
           '</div>';
    } else if (!pin.how) {
      h += '<div class="map-popup-where">No location on file for this one.</div>';
    }
    h += '</div>';
    return h;
  }

  function buildLayer(tabId) {
    var layer = L.layerGroup();
    var pins = GROUPS[tabId] || [];
    pins.forEach(function (pin) {
      if (pin.lat == null || pin.lon == null) return;   // never invent
      var m = L.marker([pin.lat, pin.lon], {
        icon: iconFor(pin),
        title: pin.name,
        alt: pin.name
      }).bindPopup(popupHtml(pin));
      m.addTo(layer);
    });
    return layer;
  }

  function showTab(tabId) {
    activeTab = tabId;
    Object.keys(layers).forEach(function (k) {
      if (k === tabId) { if (!map.hasLayer(layers[k])) map.addLayer(layers[k]); }
      else if (map.hasLayer(layers[k])) map.removeLayer(layers[k]);
    });

    document.querySelectorAll('.map-tab').forEach(function (b) {
      var on = b.getAttribute('data-tab') === tabId;
      b.classList.toggle('is-active', on);
      b.setAttribute('aria-selected', on ? 'true' : 'false');
    });

    var placed = (GROUPS[tabId] || []).filter(function (p) { return p.lat != null; });
    if (placed.length) {
      var bounds = L.latLngBounds(placed.map(function (p) { return [p.lat, p.lon]; }));
      map.fitBounds(bounds, { padding: [40, 40], maxZoom: 16 });
    } else {
      map.setView([22.32, 114.17], 11);
    }

    var nd = document.getElementById('map-nodata');
    if (nd) nd.hidden = placed.length > 0;

    var title = document.getElementById('map-side-title');
    var all = GROUPS[tabId] || [];
    var conf = all.filter(function (p) { return p.how === 'confirmed'; }).length;
    var geo = all.filter(function (p) { return p.how === 'geocoded'; }).length;
    if (title) {
      title.textContent = (tabId === 'sbr'
        ? 'Sales Brochure Review' : 'Property Valuation');
    }
    var cnt = document.getElementById('map-side-count');
    if (cnt) {
      cnt.textContent = all.length + ' project' + (all.length === 1 ? '' : 's') +
        ' · ' + placed.length + ' placed';
    }
    renderList(all);
    void conf; void geo;
  }

  function renderList(pins) {
    var host = document.getElementById('map-list');
    if (!host) return;
    if (!pins) { return; }            // the map library already wrote a message
    if (!pins.length) {
      host.innerHTML = '<div class="map-empty">Nothing here yet.</div>';
      return;
    }
    host.innerHTML = pins.map(function (p) {
      var state = p.how === 'confirmed' ? 'is-confirmed'
                : (p.how === 'geocoded' ? 'is-geocoded' : 'is-unlocated');
      var badge = p.how === 'confirmed'
        ? '<span class="muted small">confirmed</span>'
        : (p.how === 'geocoded'
            ? '<span class="muted small">suggested — confirm it</span>'
            : '<span class="muted small">no location</span>');
      var sub = [p.district, p.category].filter(Boolean).map(esc).join(' · ');
      var addr = p.address
        ? '<div class="map-item-addr">' + esc(p.address) + '</div>'
        : '<div class="map-item-addr muted">No address on file</div>';
      var actions = '<a href="' + esc(p.href) + '">Open →</a>';
      if (!p.how) {
        actions += '<form method="post" action="/map/locate" style="display:inline">' +
          '<input type="hidden" name="key" value="' + esc(p.key) + '">' +
          '<input type="hidden" name="address" value="' + esc(p.address) + '">' +
          '<input type="hidden" name="district" value="' + esc(p.district) + '">' +
          '<input type="hidden" name="name" value="' + esc(p.name) + '">' +
          '<button type="submit">Find on map</button></form>';
      } else if (p.how === 'geocoded') {
        actions += '<button type="button" class="is-located" data-focus="' +
          esc(p.key) + '" data-lat="' + esc(p.lat) + '" data-lon="' + esc(p.lon) +
          '">Show pin</button>';
        actions += '<form method="post" action="/map/confirm" style="display:inline">' +
          '<input type="hidden" name="key" value="' + esc(p.key) + '">' +
          '<input type="hidden" name="lat" value="' + esc(p.lat) + '">' +
          '<input type="hidden" name="lon" value="' + esc(p.lon) + '">' +
          '<button type="submit">Confirm</button></form>';
      }
      return '<div class="map-item ' + state + '">' +
        '<div class="map-item-name">' + esc(p.name) + '</div>' + addr +
        (sub ? '<div class="map-item-meta">' + sub + '</div>' : '') +
        '<div class="map-item-meta">' + badge + '</div>' +
        '<div class="map-item-actions">' + actions + '</div>' +
        '</div>';
    }).join('');
  }

  // "Confirm this location" inside a popup.
  document.addEventListener('click', function (ev) {
    var b = ev.target.closest ? ev.target.closest('[data-confirm]') : null;
    if (b) {
      var f = document.createElement('form');
      f.method = 'post';
      f.action = '/map/confirm';
      f.innerHTML = '<input name="key" value="' + esc(b.getAttribute('data-confirm')) + '">' +
                    '<input name="lat" value="' + esc(b.getAttribute('data-lat')) + '">' +
                    '<input name="lon" value="' + esc(b.getAttribute('data-lon')) + '">';
      document.body.appendChild(f);
      f.submit();
      return;
    }
    var g = ev.target.closest ? ev.target.closest('[data-focus]') : null;
    if (g && map) {
      map.setView([Number(g.getAttribute('data-lat')),
                   Number(g.getAttribute('data-lon'))], 17);
    }
  });

  Object.keys(GROUPS).forEach(function (tabId) {
    layers[tabId] = buildLayer(tabId);
  });
  document.querySelectorAll('.map-tab').forEach(function (b) {
    b.addEventListener('click', function () { showTab(b.getAttribute('data-tab')); });
  });

  // Both tabs may be absent from the payload if the page was rendered
  // without any projects at all.
  showTab(GROUPS[activeTab] ? activeTab : Object.keys(GROUPS)[0] || 'pv');
})();