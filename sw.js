const V = 'keisen-v2';
const CORE = ['./', 'index.html', 'manifest.json', 'icon-180.png', 'icon-192.png', 'icon-512.png'];
self.addEventListener('install', e => {
  e.waitUntil(caches.open(V).then(c => c.addAll(CORE)).then(() => self.skipWaiting()));
});
self.addEventListener('activate', e => {
  e.waitUntil(caches.keys().then(ks => Promise.all(ks.filter(k => k !== V).map(k => caches.delete(k)))).then(() => self.clients.claim()));
});
self.addEventListener('fetch', e => {
  if (e.request.method !== 'GET') return;
  const u = new URL(e.request.url);
  const same = u.origin === location.origin;
  const font = /(^|\.)fonts\.(googleapis|gstatic)\.com$/.test(u.hostname);
  if (!same && !font) return;
  // 市場データは常にネットワークを優先(オフライン時のみ前回分)
  if (same && /\/market\.json$/.test(u.pathname)) {
    e.respondWith(fetch(e.request).then(res => {
      if (res && res.ok) { const cp = res.clone(); caches.open(V).then(c => c.put(u.pathname, cp)); }
      return res;
    }).catch(() => caches.match(u.pathname)));
    return;
  }
  e.respondWith(caches.match(e.request).then(hit => {
    const net = fetch(e.request).then(res => {
      if (res && (res.ok || res.type === 'opaque')) { const cp = res.clone(); caches.open(V).then(c => c.put(e.request, cp)); }
      return res;
    }).catch(() => hit);
    return hit || net;
  }));
});
