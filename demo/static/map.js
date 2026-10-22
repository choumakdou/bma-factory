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

  function iconFor(pin, n) {
    // A numbered tag, not a dot. The number matches the position of
    // the project in the side list, so "12" on the map is the twelfth
    // thing in the panel and the two can be read against each other.
    var cls = 'map-tag ' +
      (pin.how === 'confirmed' ? 'is-confirmed' : 'is-geocoded') +
      (pin.tab === 'sbr' ? ' is-sbr' : '');
    return L.divIcon({
      className: 'map-marker',
      html: '<div class="' + cls + '" title="' + esc(pin.name) + '">' +
            esc(String(n)) + '</div>',
      iconSize: [24, 24],
      iconAnchor: [12, 12],
      popupAnchor: [0, -14]
    });
  }

  function popupHtml(pin) {
    var h = '<span class="map-popup-no">' + esc(pin._n || '') + '</span>' +
            '<b>' + esc(pin.name) + '</b>';
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
    pins.forEach(function (pin, i) {
      if (pin.lat == null || pin.lon == null) return;   // never invent
      // Numbered in LIST order, not in the order they happened to be
      // located, so the number is stable between visits.
      pin._n = i + 1;
      var m = L.marker([pin.lat, pin.lon], {
        icon: iconFor(pin, pin._n),
        title: pin._n + '. ' + pin.name,
        alt: pin.name
      }).bindPopup(popupHtml(pin));
      m.addTo(layer);
    });
    return layer;
  }

  // Hong Kong proper. Shown when too few projects are placed for a
  // fit-to-pins zoom to mean anything.
  var HK_BOUNDS = L.latLngBounds([22.15, 113.83], [22.60, 114.45]);

  function fitView(mode) {
    var placed = (GROUPS[activeTab] || []).filter(function (p) {
      return p.lat != null;
    });
    if (mode === 'hk') { map.fitBounds(HK_BOUNDS, { padding: [16, 16] }); return; }
    if (!placed.length) { map.fitBounds(HK_BOUNDS, { padding: [16, 16] }); return; }
    var b = L.latLngBounds(placed.map(function (p) { return [p.lat, p.lon]; }));
    // maxZoom keeps two adjacent buildings from zooming to street level
    // just because they are 30m apart; the numbers still separate them.
    map.fitBounds(b, { padding: [48, 48], maxZoom: 17 });
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

    var all = GROUPS[tabId] || [];
    var placed = all.filter(function (p) { return p.lat != null; });

    // Fit to the projects when there are enough of them to fit; show
    // the whole of Hong Kong when there are few or none, because a map
    // zoomed to a single pin says nothing about where the work is.
    var mode = placed.length >= 2 ? 'fit' : 'hk';
    fitView(mode);
    setViewButtons(mode);

    var nd = document.getElementById('map-nodata');
    if (nd) {
      nd.hidden = placed.length > 0;
      var msg = document.getElementById('map-nodata-msg');
      if (msg && !placed.length) {
        msg.textContent = all.length
          ? 'None of these ' + all.length + ' projects has a location yet. ' +
            'The app is looking them up now — reload in a minute, or drop ' +
            'the pins yourself below.'
          : 'Nothing here yet.';
      }
    }

    var title = document.getElementById('map-side-title');
    if (title) {
      title.textContent = (tabId === 'sbr'
        ? 'Sales Brochure Review' : 'Property Valuation');
    }
    var cnt = document.getElementById('map-side-count');
    if (cnt) {
      cnt.textContent = all.length + ' project' + (all.length === 1 ? '' : 's') +
        ' · ' + placed.length + ' placed';
    }
    var pend = document.getElementById('map-pending');
    if (pend) {
      pend.hidden = placed.length >= all.length;
      if (!pend.hidden) {
        pend.textContent = placed.length + ' of ' + all.length +
          ' placed. Locations are being looked up now — reload in a minute ' +
          'to see the rest.';
      }
    }
    renderList(all);
  }

  function setViewButtons(mode) {
    document.querySelectorAll('[data-view]').forEach(function (b) {
      b.classList.toggle('is-active', b.getAttribute('data-view') === mode);
    });
  }

  function renderList(pins) {
    var host = document.getElementById('map-list');
    if (!host) return;
    if (!pins) { return; }            // the map library already wrote a message
    if (!pins.length) {
      host.innerHTML = '<div class="map-empty">Nothing here yet.</div>';
      return;
    }
    host.innerHTML = pins.map(function (p, i) {
      var n = i + 1;
      var state = p.how === 'confirmed' ? 'is-confirmed'
                : (p.how === 'geocoded' ? 'is-geocoded' : 'is-unlocated');
      if (p.tab === 'sbr') state += ' is-sbr';
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
      if (p.lat != null) {
        actions += '<button type="button" class="is-located" data-focus="' +
          esc(p.key) + '" data-lat="' + esc(p.lat) + '" data-lon="' + esc(p.lon) +
          '">Show pin</button>';
      }
      if (p.how === 'geocoded') {
        // A suggestion is only useful if it can be accepted from here
        // as well as from the pin itself.
        actions += '<form method="post" action="/map/confirm" style="display:inline">' +
          '<input type="hidden" name="key" value="' + esc(p.key) + '">' +
          '<input type="hidden" name="lat" value="' + esc(p.lat) + '">' +
          '<input type="hidden" name="lon" value="' + esc(p.lon) + '">' +
          '<button type="submit" class="is-located">Confirm</button></form>';
      }
      // Placing by hand is available for everything. It is the only
      // way to locate an estate the public map has never heard of.
      actions += '<button type="button" data-place="' + esc(p.key) +
                 '" data-place-name="' + esc(p.name) + '">' +
                 (p.how === 'confirmed' ? 'Move pin' : 'Place on map') +
                 '</button>';
      if (p.how !== 'confirmed') {
        actions += '<form method="post" action="/map/locate" style="display:inline">' +
          '<input type="hidden" name="key" value="' + esc(p.key) + '">' +
          '<input type="hidden" name="address" value="' + esc(p.address) + '">' +
          '<input type="hidden" name="district" value="' + esc(p.district) + '">' +
          '<input type="hidden" name="name" value="' + esc(p.name) + '">' +
          '<button type="submit">' +
          (p.how === 'geocoded' ? 'Look up again' : 'Find on map') +
          '</button></form>';
      }
      return '<div class="map-item ' + state + '" id="item-' + n + '">' +
        '<div class="map-item-head">' +
          '<span class="map-item-no">' + n + '</span>' +
          '<div><div class="map-item-name">' + esc(p.name) + '</div>' +
          addr + '</div>' +
        '</div>' +
        (sub ? '<div class="map-item-meta">' + sub + '</div>' : '') +
        '<div class="map-item-meta">' + badge + '</div>' +
        '<div class="map-item-actions">' + actions + '</div>' +
        '</div>';
    }).join('');
  }

  // ---- placing a pin by hand -------------------------------------
  // The geocoder only knows estates that are in OpenStreetMap. Most of
  // Hong Kong's smaller developments are not, and an estate it has
  // never heard of gets no pin at all — which is the safe answer but
  // an unhelpful one. So the valuer can put the pin down himself:
  // one button, then one click on the right spot.
  var placing = null;

  function beginPlace(key, name) {
    placing = key;
    map.getContainer().classList.add('is-placing');
    var banner = document.getElementById('map-placing');
    if (banner) {
      banner.hidden = false;
      var n = document.getElementById('map-placing-name');
      if (n) n.textContent = name || '';
    }
  }

  function endPlace() {
    placing = null;
    map.getContainer().classList.remove('is-placing');
    var banner = document.getElementById('map-placing');
    if (banner) banner.hidden = true;
  }

  map.on('click', function (e) {
    if (!placing) return;
    var key = placing, lat = e.latlng.lat, lon = e.latlng.lng;
    endPlace();
    var f = document.createElement('form');
    f.method = 'post';
    f.action = '/map/confirm';
    f.innerHTML =
      '<input name="key" value="' + esc(key) + '">' +
      '<input name="lat" value="' + esc(lat.toFixed(6)) + '">' +
      '<input name="lon" value="' + esc(lon.toFixed(6)) + '">' +
      '<input name="note" value="placed by hand on the map">';
    document.body.appendChild(f);
    f.submit();
  });

  // "Confirm this location" inside a popup, and the view buttons.
  document.addEventListener('click', function (ev) {
    var v = ev.target.closest ? ev.target.closest('[data-view]') : null;
    if (v && map) { fitView(v.getAttribute('data-view')); return; }

    var pl = ev.target.closest ? ev.target.closest('[data-place]') : null;
    if (pl) {
      beginPlace(pl.getAttribute('data-place'),
                 pl.getAttribute('data-place-name'));
      return;
    }
    var cancel = ev.target.closest ? ev.target.closest('[data-place-cancel]') : null;
    if (cancel) { endPlace(); return; }

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
      // highlight the matching row so the number and the list line up
      var all = GROUPS[activeTab] || [];
      for (var i = 0; i < all.length; i++) {
        if (all[i].key === g.getAttribute('data-focus')) {
          var row = document.getElementById('item-' + (i + 1));
          if (row) {
            row.classList.add('is-flash');
            row.scrollIntoView({block: 'nearest', behavior: 'smooth'});
            setTimeout(function () {
              row.classList.remove('is-flash');
            }, 1600);
          }
          break;
        }
      }
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