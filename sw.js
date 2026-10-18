// Caches the app shell so it opens instantly and installs as an app.
// Restaurant data is always fetched live and never cached here.
const CACHE = 'lucky-bite-v13';
const SHELL = ['./', './index.html', './manifest.webmanifest', './icon.svg', './icon-192.png', './icon-512.png'];

self.addEventListener('install', e => {
  e.waitUntil(caches.open(CACHE).then(c => c.addAll(SHELL)).then(() => self.skipWaiting()));
});
self.addEventListener('activate', e => {
  e.waitUntil(caches.keys().then(keys => Promise.all(keys.filter(k => k !== CACHE).map(k => caches.delete(k)))).then(() => self.clients.claim()));
});
self.addEventListener('fetch', e => {
  const url = new URL(e.request.url);
  if (url.origin !== location.origin) return; // let API and font requests go straight to the network
  e.respondWith(caches.match(e.request).then(hit => hit || fetch(e.request)));
});
